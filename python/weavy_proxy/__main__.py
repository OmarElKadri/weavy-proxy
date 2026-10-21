"""`python -m weavy_proxy` — start the local OpenAI-compatible server."""

from __future__ import annotations

import argparse
import json
import os

import uvicorn

from .app import create_app
from .config import CONFIG_PATH, load_config


def main() -> None:
    parser = argparse.ArgumentParser(prog="weavy_proxy", description="OpenAI-compatible image API on top of a Weavy session (browserless)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", default=CONFIG_PATH)
    args = parser.parse_args()

    cfg = load_config(args.config)
    port = args.port or cfg.get("port") or 8787
    app = create_app(args.config)
    print(f"weavy-proxy (browserless) listening on http://{args.host}:{port}/v1")
    print(f"  health:  http://{args.host}:{port}/healthz")
    print(f"  credits: http://{args.host}:{port}/v1/credits")
    uvicorn.run(app, host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    main()
