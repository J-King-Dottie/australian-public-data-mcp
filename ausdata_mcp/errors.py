"""Consistent, bounded MCP errors that preserve the failed request's scope."""

from __future__ import annotations

import json
import math
from typing import Any

import httpx
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from .source_http import ProviderCooldown, retry_after_seconds


def tool_error_result(exc: Exception, tool: str, arguments: dict[str, Any]) -> CallToolResult:
    # FastMCP wraps validation and execution exceptions; retain the original category.
    cause = exc
    while isinstance(cause, ToolError) and isinstance(cause.__cause__, Exception):
        cause = cause.__cause__
    code, retryable, delay, status, host = "internal_error", False, None, None, None
    message = "Unexpected tool failure. Check server diagnostics before retrying."
    if isinstance(cause, ProviderCooldown):
        code, retryable = "provider_cooldown", True
        host, delay, message = cause.host, cause.retry_after_seconds, str(cause)
    elif isinstance(cause, httpx.RequestError | httpx.HTTPStatusError):
        host = cause.request.url.host
        if isinstance(cause, httpx.HTTPStatusError):
            status = cause.response.status_code
            delay = retry_after_seconds(cause.response.headers.get("Retry-After", ""))
            if status == 429:
                code, retryable = "rate_limited", True
                delay = max(1.0, delay or 1.0)
                message = f"{host} rate limit (HTTP 429). Retry after {math.ceil(delay)} seconds."
            elif status >= 500:
                code, retryable = "provider_unavailable", True
                message = f"{host} temporarily unavailable (HTTP {status}); retry later."
            elif status in (401, 403):
                code = "access_denied"
                message = f"{host} denied access (HTTP {status}). Check source access requirements."
            elif status == 404:
                code = "not_found"
                message = f"{host} returned HTTP 404. Verify the dataset and source endpoint."
                if tool == "retrieve" and str(arguments.get("datasetId", "")).startswith(
                    ("ABS,", "oecd::", "pdh::")
                ):
                    code = "not_found_or_empty_selection"
                    message += (
                        " The source may have no observations for this combination even if its "
                        "codes are valid. Inspect metadata; broaden sourceFilters or dataKey "
                        "to inspect returned series dimensions if appropriate."
                    )
            else:
                code = "provider_request_error"
                message = f"{host} returned HTTP {status}. Check the dataset, filters and periods."
        else:
            code, retryable = "network_error", True
            if isinstance(cause, httpx.TimeoutException):
                message = f"{host} timed out. Retry later; the requested scope was unchanged."
            else:
                message = f"Could not reach {host}. Check network availability and retry later."
    elif isinstance(cause, ValidationError):
        code, message = "validation_error", str(cause)
    elif isinstance(cause, json.JSONDecodeError):
        code, message = "invalid_source_response", "Source returned malformed JSON; retry later."
    elif isinstance(cause, ValueError):
        code, message = "invalid_request", str(cause)
    elif isinstance(cause, RuntimeError):
        code, message = "source_result_error", str(cause)
    encoded = json.dumps(arguments, ensure_ascii=False)
    truncated = len(encoded) > 6000
    data = {
        "error": {
            "code": code,
            "message": message[:2000],
            "tool": tool,
            "dataset_id": arguments.get("datasetId"),
            "provider_host": host,
            "http_status": status,
            "retryable": retryable,
            "retry_after_seconds": math.ceil(delay) if delay is not None else None,
            "request": {} if truncated else arguments,
            "request_truncated": truncated,
        }
    }
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
        structuredContent=data,
    )
