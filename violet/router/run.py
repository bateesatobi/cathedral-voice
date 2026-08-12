"""CLI entry point for the Violet Router HTTP service."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Violet Router HTTP service")
    parser.add_argument("--host", default=os.getenv("VIOLET_ROUTER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("VIOLET_ROUTER_PORT", "8090")))
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "violet.router.server:app",
        host=args.host,
        port=args.port,
        log_level=os.getenv("VIOLET_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
