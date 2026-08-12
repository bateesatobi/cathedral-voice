#!/usr/bin/env python3
"""
Fetch MINER_ACCESS_TOKEN from the Avoices ASRAPI after proving hotkey ownership.

Usage (from repo root, wallet on this machine):

    pip install -e ".[chain]"
    python scripts/fetch_miner_access_token.py \\
        --api-url https://phosai-backend-api-1.onrender.com \\
        --network test \\
        --write-env

Writes MINER_ACCESS_TOKEN to .env and prints the token once.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from violet.miner.access_token import token_challenge_message  # noqa: E402


def _http_json(method: str, url: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} from {url}: {detail}") from exc
    except URLError as exc:
        raise SystemExit(f"could not reach {url}: {exc}") from exc


def _load_wallet(name: str, hotkey: str, path: str | None):
    try:
        import bittensor as bt
    except ImportError as exc:
        raise SystemExit(
            "bittensor is required. Install with: pip install -e '.[chain]'"
        ) from exc
    kwargs = {"name": name, "hotkey": hotkey}
    if path:
        kwargs["path"] = path
    wallet = bt.Wallet(**kwargs)
    return wallet.hotkey.ss58_address, wallet.hotkey


def _upsert_env(key: str, value: str, env_file: Path) -> None:
    text = env_file.read_text(encoding="utf-8") if env_file.is_file() else ""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    line = f"{key}={value}"
    if pattern.search(text):
        text = pattern.sub(line, text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"{line}\n"
    env_file.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch MINER_ACCESS_TOKEN from ASRAPI")
    parser.add_argument(
        "--api-url",
        default=os.getenv("VIOLET_TOKEN_API_URL", "https://phosai-backend-api-1.onrender.com"),
        help="ASRAPI base URL (no trailing path)",
    )
    parser.add_argument(
        "--network",
        default=os.getenv("BT_NETWORK", "test"),
        choices=["test", "testnet", "finney", "main", "mainnet"],
        help="Bittensor network the miner registered on",
    )
    parser.add_argument("--wallet-name", default=os.getenv("BT_WALLET_NAME", "default"))
    parser.add_argument("--wallet-hotkey", default=os.getenv("BT_WALLET_HOTKEY", "default"))
    parser.add_argument("--wallet-path", default=os.getenv("BT_WALLET_PATH") or None)
    parser.add_argument(
        "--coldkey",
        default="",
        help="Optional coldkey SS58 (verified server-side against metagraph)",
    )
    parser.add_argument(
        "--endpoint",
        default=os.getenv("MINER_PUBLIC_ENDPOINT", ""),
        help="Optional public endpoint for audit logging",
    )
    parser.add_argument("--write-env", action="store_true", help="Write MINER_ACCESS_TOKEN to .env")
    parser.add_argument("--env-file", default=".env", help="Env file for --write-env")
    args = parser.parse_args()

    hotkey_ss58, hotkey = _load_wallet(args.wallet_name, args.wallet_hotkey, args.wallet_path)
    base = args.api_url.rstrip("/")
    network = "test" if args.network in {"test", "testnet"} else "finney"

    challenge = _http_json(
        "GET",
        f"{base}/violet/miner/token/challenge?network={network}",
    )
    nonce = str(challenge.get("nonce", "") or "")
    netuid = int(challenge.get("netuid", 0) or 0)
    if len(nonce) < 8 or netuid <= 0:
        raise SystemExit(f"invalid challenge response: {challenge}")

    issued_at = time.time()
    message = token_challenge_message(hotkey_ss58, nonce, network, netuid, issued_at=issued_at)
    signature = hotkey.sign(message).hex()

    payload = {
        "hotkey": hotkey_ss58,
        "coldkey": args.coldkey or None,
        "network": network,
        "netuid": netuid,
        "nonce": nonce,
        "issued_at": issued_at,
        "signature": signature,
        "endpoint": args.endpoint or None,
    }
    result = _http_json("POST", f"{base}/violet/miner/token", payload)

    token = str(result.get("access_token", "") or "")
    if not token:
        raise SystemExit(f"token issuance failed: {result}")

    print(json.dumps({k: result[k] for k in ("hotkey", "uid", "network", "netuid", "expires_at") if k in result}, indent=2))
    print(f"\nMINER_ACCESS_TOKEN={token}\n")

    if args.write_env:
        env_path = Path(args.env_file)
        if not env_path.is_absolute():
            env_path = Path.cwd() / env_path
        _upsert_env("MINER_ACCESS_TOKEN", token, env_path)
        print(f"wrote MINER_ACCESS_TOKEN to {env_path}")

    print(
        "Restart the miner sidecar so it loads the new token:\n"
        "  ./violet/miner/start.sh test --no-follow   # or prod"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
