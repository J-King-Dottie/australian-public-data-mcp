"""Bounded retries and shared host coordination for read-only source requests."""

from __future__ import annotations

import atexit
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from random import uniform
from threading import BoundedSemaphore, Lock
from typing import Any

import httpx

MAX_RETRY_WAIT = 5.0
HOST_CONCURRENCY = 2
RETRY_STATUSES = {429, 502, 503, 504}


def retry_after_seconds(value: str, *, now: datetime | None = None) -> float | None:
    """Parse both HTTP Retry-After forms without shortening the publisher's delay."""
    value = value.strip()
    try:
        if value.isascii() and value.isdigit():
            seconds = float(value)
            return seconds if math.isfinite(seconds) else None
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return max(0.0, (date - (now or datetime.now(UTC))).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


class ProviderCooldown(RuntimeError):
    def __init__(self, host: str, seconds: float):
        self.host = host
        self.retry_after_seconds = seconds
        super().__init__(
            f"{host} is cooling down after a source limit or temporary failure; "
            f"retry after {seconds:.1f} seconds."
        )


@dataclass
class _HostState:
    slots: BoundedSemaphore = field(default_factory=lambda: BoundedSemaphore(HOST_CONCURRENCY))
    lock: Lock = field(default_factory=Lock)
    until: float = 0.0


_hosts: dict[str, _HostState] = {}
_hosts_lock = Lock()

_client: httpx.Client | None = None
_client_lock = Lock()


def _shared_client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16)
            )
        return _client


def close() -> None:
    """Release pooled connections after all source requests have finished."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


atexit.register(close)


def _state(host: str) -> _HostState:
    with _hosts_lock:
        return _hosts.setdefault(host, _HostState())


def _defer(state: _HostState, delay: float) -> None:
    with state.lock:
        state.until = max(state.until, time.monotonic() + delay)


def _wait(state: _HostState, host: str, budget: float) -> float:
    """Limit added cooldown waiting; return long provider delays to the caller."""
    while True:
        with state.lock:
            delay = max(0.0, state.until - time.monotonic())
        if not delay:
            return budget
        if delay > budget:
            raise ProviderCooldown(host, delay)
        time.sleep(delay)
        budget -= delay


def get(url: str, *, client: httpx.Client | None = None, **kwargs: Any) -> httpx.Response:
    """GET with at most one retry; never change filters or retry other HTTP failures."""
    target = httpx.URL(url)
    if target.is_relative_url and client is not None:
        target = client.base_url.join(target)
    host = target.host
    state = _state(host)
    budget = MAX_RETRY_WAIT
    send = (client if client is not None else _shared_client()).get
    with state.slots:
        for attempt in range(2):
            budget = _wait(state, host, budget)
            try:
                response = send(url, **kwargs)
            except (httpx.ConnectError, httpx.TimeoutException):
                if attempt:
                    raise
                delay = uniform(1.0, 1.25)
                if delay > budget:
                    raise
                time.sleep(delay)
                budget -= delay
                continue
            if response.status_code not in RETRY_STATUSES:
                return response
            delay = retry_after_seconds(response.headers.get("Retry-After", ""))
            delay = delay if delay and delay > 0 else uniform(1.0, 1.25)
            _defer(state, delay)
            if attempt or delay > budget:
                return response
            response.close()
    raise AssertionError("Request attempts exhausted without a response")
