"""
Fetch MINER_ACCESS_TOKEN from Avoices ASRAPI after proving hotkey ownership.

Run from the repo root::

    pip install -e ".[chain]"
    python -m violet.miner.fetch_access_token --network test --write-env

On prod GPU hosts the wallet usually lives in Docker volume ``violet-bittensor-wallets``;
this module extracts it automatically when it is not under ``~/.bittensor``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .access_token import token_challenge_message


def _repo_root() -> Path:
    return Path.cwd()


def _load_dotenv() -> None:
    for env_file in (_repo_root() / ".env", Path.cwd() / ".env"):
        if not env_file.is_file():
            continue
        try:
            from dotenv import load_dotenv

            load_dotenv(env_file, override=False)
            return
        except ImportError:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = value.strip().strip('"').strip("'")
            return


def _http_json(method: str, url: str, payload: dict | None = None, timeout: float = 60.0) -> dict:
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


def _hotkey_file(base: Path, name: str, hotkey: str) -> Optional[Path]:
    hotkeys = base / name / "hotkeys"
    for candidate in (hotkeys / hotkey, hotkeys / f"{hotkey}.json"):
        if candidate.is_file():
            return candidate
    return None


def _wallet_roots() -> Iterator[Path]:
    seen: set[str] = set()

    def add(raw: str) -> Iterator[Path]:
        if not raw:
            return
        path = Path(os.path.expanduser(raw)).resolve()
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        yield path

    yield from add(os.getenv("BT_WALLET_PATH", ""))
    wallet_dir = os.getenv("BT_WALLET_DIR", "").strip()
    if wallet_dir:
        expanded = Path(os.path.expanduser(wallet_dir))
        yield from add(str(expanded / "wallets"))
        yield from add(str(expanded))
    yield from add("/home/violet/.bittensor/wallets")
    yield from add(str(Path.home() / ".bittensor" / "wallets"))
    yield from add(str(Path.home() / ".bittensor"))


def extract_wallet_from_docker_volume(volume: str) -> Optional[Path]:
    """Copy ``wallets/`` tree out of the miner wallet volume for signing."""
    if not volume or shutil.which("docker") is None:
        return None
    try:
        subprocess.run(
            ["docker", "volume", "inspect", volume],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        return None

    tmp = Path(tempfile.mkdtemp(prefix="violet-wallet-"))
    script = (
        "set -e; "
        "if [ -d /w/wallets ]; then cp -a /w/wallets /out/; "
        "elif [ -f /w/wallets/. ]; then cp -a /w/wallets /out/; "
        "else mkdir -p /out/wallets && cp -a /w/. /out/wallets/; fi; "
        "find /out -type f | head -20"
    )
    try:
        proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{volume}:/w:ro",
                "-v",
                f"{tmp}:/out",
                "alpine:3.20",
                "sh",
                "-c",
                script,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if proc.stdout.strip():
            print(f"extracted wallet files:\n{proc.stdout.strip()}", file=sys.stderr)
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"docker wallet extract failed: {exc.stderr or exc}", file=sys.stderr)
        return None

    wallets = tmp / "wallets"
    return wallets if wallets.is_dir() else None


def _find_hotkey(name: str, hotkey: str) -> Tuple[Path, Path]:
    for root in _wallet_roots():
        if root.name != "wallets" and (root / "wallets").is_dir():
            root = root / "wallets"
        found = _hotkey_file(root, name, hotkey)
        if found is not None:
            return root, found

    volume = os.getenv("WALLET_VOLUME_NAME", "violet-bittensor-wallets").strip()
    extracted = extract_wallet_from_docker_volume(volume)
    if extracted is not None:
        found = _hotkey_file(extracted, name, hotkey)
        if found is not None:
            return extracted, found

    lines = [
        f"Could not find hotkey file for wallet {name}/{hotkey}.",
        "",
        "Checked:",
        "  • BT_WALLET_PATH / BT_WALLET_DIR from .env",
        f"  • ~/.bittensor/wallets/{name}/hotkeys/{hotkey}",
        f"  • Docker volume {volume!r}",
        "",
        "Diagnostics on this host:",
        "  grep BT_WALLET .env",
        "  btcli wallet list",
        f"  docker run --rm -v {volume}:/w:ro alpine find /w -type f 2>/dev/null | head",
        "",
        "If the wallet is in the volume, re-run after: git pull && pip install -e '.[chain]'",
        "Or pass: --wallet-name NAME --wallet-hotkey HOTKEY --wallet-path /path/to/wallets",
    ]
    raise SystemExit("\n".join(lines))


def _load_wallet(name: str, hotkey: str, wallet_path: str | None):
    try:
        import bittensor as bt
    except ImportError as exc:
        raise SystemExit(
            "bittensor is required. Install with: pip install -e '.[chain]'"
        ) from exc

    if wallet_path:
        root = Path(os.path.expanduser(wallet_path))
        if root.name != "wallets" and (root / "wallets").is_dir():
            root = root / "wallets"
        if _hotkey_file(root, name, hotkey) is None:
            raise SystemExit(f"no hotkey at {root}/{name}/hotkeys/{hotkey}")
    else:
        root, keyfile = _find_hotkey(name, hotkey)
        print(f"using wallet hotkey: {keyfile}", file=sys.stderr)

    wallet = bt.Wallet(name=name, hotkey=hotkey, path=str(root))
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


def main(argv: Optional[list[str]] = None) -> int:
    _load_dotenv()

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
    parser.add_argument(
        "--wallet-path",
        default=os.getenv("BT_WALLET_PATH") or None,
        help="Directory containing wallet name subdirs (usually .../wallets)",
    )
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
    args = parser.parse_args(argv)

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

    summary = {
        k: result[k]
        for k in ("hotkey", "uid", "network", "netuid", "expires_at")
        if k in result
    }
    print(json.dumps(summary, indent=2))
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
