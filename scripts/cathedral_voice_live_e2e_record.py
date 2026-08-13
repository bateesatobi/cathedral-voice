#!/usr/bin/env python3
"""Record a live Cathedral Voice hybrid E2E evidence pack (G10).

Runs production preflight, then live e2e proof with ``--publish``, and writes a
single evidence JSON that operators attach to the cutover ticket.

Requires hardware measurement + publisher deploy. Does not set simulation.

Example::

    python scripts/cathedral_voice_live_e2e_record.py \\
      --measurement-file /var/lib/violet/tdx_measurement.json \\
      --out ./data/cathedral_voice_live_evidence.json
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_e2e_module():
    path = ROOT / "scripts" / "cathedral_voice_e2e_proof.py"
    spec = importlib.util.spec_from_file_location("cathedral_voice_e2e_proof", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_preflight(role: str) -> dict:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "cathedral_voice_production_preflight.py"),
        "--role",
        role,
        "--json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        payload = {
            "ok": False,
            "error": "preflight_parse_failed",
            "stdout": (proc.stdout or "")[:500],
            "stderr": (proc.stderr or "")[:500],
        }
    payload["exit_code"] = proc.returncode
    return payload


async def _amain(args: argparse.Namespace) -> int:
    evidence: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": False,
        "preflight_validator": None,
        "proof": None,
        "ids": {},
    }

    if not args.skip_preflight:
        evidence["preflight_validator"] = _run_preflight("validator")
        if not evidence["preflight_validator"].get("ok"):
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(evidence, indent=2))
            return 1

    proof_mod = _load_e2e_module()
    proof = await proof_mod.run_e2e_proof(
        out_path=args.out.with_suffix(".proof.json"),
        live=True,
        publish=not args.no_publish,
        measurement_file=args.measurement_file,
    )
    digests = proof.get("digests") or {}
    evidence["proof"] = proof
    evidence["ids"] = {
        "request_hash": digests.get("request_hash"),
        "audio_content_hash": digests.get("audio_content_hash"),
        "mrtd": digests.get("mrtd"),
        "report_epoch": digests.get("report_epoch"),
        "publisher_report_id": digests.get("publisher_report_id"),
        "publisher_vector_hint": digests.get("publisher_vector_hint")
        or (args.vector_id or None),
        "chain_extrinsic": args.chain_extrinsic or None,
    }
    evidence["ok"] = bool(proof.get("ok")) and all(
        evidence["ids"].get(k)
        for k in ("request_hash", "audio_content_hash", "mrtd", "report_epoch")
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))
    return 0 if evidence["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--measurement-file", type=Path, required=True)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "data" / "cathedral_voice_live_evidence.json",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip preflight (not recommended)",
    )
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="Build/verify only; do not POST to publisher",
    )
    parser.add_argument(
        "--chain-extrinsic",
        default="",
        help="Optional extrinsic hash to attach after thin-relay set_weights",
    )
    parser.add_argument(
        "--vector-id",
        default="",
        help="Optional signed vector id observed from thin relay",
    )
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
