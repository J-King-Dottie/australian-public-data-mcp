"""Canonical stdio startup used by the module and absolute-path launcher."""

from .runtime import validate_local_runtime
from .server import server


def main() -> None:
    validate_local_runtime()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
