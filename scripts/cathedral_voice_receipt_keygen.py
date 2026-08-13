#!/usr/bin/env python3
"""Generate Ed25519 receipt keypair for Cathedral Voice (run inside TDX guest).

Prints hex keys. Optionally appends miner private key to an env file.

Examples::

    python scripts/cathedral_voice_receipt_keygen.py
    python scripts/cathedral_voice_receipt_keygen.py --write-env ./.env.receipt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from violet.cathedral.receipt_v1 import generate_ed25519_keypair

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-env",
        type=Path,
        default=None,
        help="Append VIOLET_RECEIPT_ED25519_PRIVATE_KEY to this file",
    )
    parser.add_argument(
        "--print-public-only",
        action="store_true",
        help="Print only the public key (for validator env)",
    )
    args = parser.parse_args()

    priv, pub = generate_ed25519_keypair()
    if args.print_public_only:
        print(pub)
        return 0

    print("# Cathedral Voice receipt Ed25519 keypair")
    print("# Generate inside the measured TDX guest. Never reuse outside guest.")
    print(f"VIOLET_RECEIPT_ED25519_PRIVATE_KEY={priv}")
    print(f"CATHEDRAL_RECEIPT_ED25519_PUBLIC_KEY={pub}")

    if args.write_env is not None:
        path = args.write_env
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n# cathedral_voice_receipt_keygen\n")
            fh.write(f"VIOLET_RECEIPT_ED25519_PRIVATE_KEY={priv}\n")
            fh.write(f"CATHEDRAL_RECEIPT_ED25519_PUBLIC_KEY={pub}\n")
        print(f"# appended to {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
