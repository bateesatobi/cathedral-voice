#!/usr/bin/env python3
"""Cathedral Voice end-to-end proof harness (G10).

Proves the in-repo chain without live TDX hardware or the publisher service::

    simulated TDX measurement
      → Ed25519-signed cathedral_voice_receipt_v1
      → verify_receipt (require_tdx)
      → build_hybrid_report
      → dry-run CathedralScoreClient POST
      → write proof JSON with digests

Run::

    CATHEDRAL_TDX_SIMULATION=1 python scripts/cathedral_voice_e2e_proof.py
    pytest -q tests/test_cathedral_voice_e2e.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def run_e2e_proof(*, out_path: Path) -> dict:
    """Full in-repo proof; returns digests and writes ``out_path``."""
    os.environ["CATHEDRAL_TDX_SIMULATION"] = "1"

    from violet.cathedral.external_scores import (
        SOURCE_HYBRID,
        CathedralScoreClient,
        CathedralScoreClientConfig,
        build_hybrid_report,
    )
    from violet.cathedral.receipt_v1 import (
        build_receipt,
        generate_ed25519_keypair,
        verify_receipt,
    )
    from violet.cathedral.tdx import (
        TdxVerifyPolicy,
        simulate_controller_measurement,
        verify_controller_measurement,
    )

    hotkey = "5FCathedralVoiceE2EHotkeyExample"
    priv, pub = generate_ed25519_keypair()
    challenge = f"e2e-{int(time.time())}"
    measurement = simulate_controller_measurement(
        hotkey=hotkey,
        challenge=challenge,
        endpoint="http://127.0.0.1:8091",
    )
    policy = TdxVerifyPolicy(
        require_measurement=True,
        allow_simulation=True,
        expected_challenge=challenge,
        expected_hotkey=hotkey,
        audience="cathedral_voice_hybrid",
    )
    tdx = verify_controller_measurement(measurement.encode(), policy)
    if not tdx.ok:
        raise RuntimeError(tdx.detail)

    audio = b"\x00\x01" * 1024
    prompt = "Cathedral voice end to end proof prompt"
    receipt = build_receipt(
        miner_hotkey=hotkey,
        input_text=prompt,
        voice="eng_female_1",
        audio=audio,
        controller_measurement=measurement.encode(),
        ed25519_private_key=priv,
    )
    verified = verify_receipt(
        receipt.to_dict(),
        require=True,
        ed25519_public_key_hex=pub,
        require_tdx=True,
        tdx_policy=policy,
    )
    if not verified.ok:
        raise RuntimeError(verified.detail)

    report = build_hybrid_report(
        [{"miner_hotkey": hotkey, "score": 0.91, "receipt": receipt.to_dict()}],
        epoch=int(time.time()),
        ed25519_public_key_hex=pub,
        require_tdx=True,
        tdx_policy=policy,
        metadata={"proof": "cathedral_voice_e2e", "tdx_simulated": True},
    )

    client = CathedralScoreClient(
        CathedralScoreClientConfig(enabled=True, dry_run=True)
    )
    post = await client.post_report(report)
    await client.close()

    digests = {
        "request_hash": receipt.request_hash,
        "audio_content_hash": receipt.audio_content_hash,
        "mrtd": measurement.mrtd,
        "challenge": challenge,
        "ed25519_public_key": pub,
        "receipt_signature_prefix": (receipt.signature or "")[:48],
        "report_epoch": report["epoch"],
        "report_source": report["source"],
        "scores": len(report["scores"]),
        "post_status": post.get("status"),
        "post_ok": post.get("ok"),
    }
    proof = {
        "ok": bool(post.get("ok")) and report["source"] == SOURCE_HYBRID,
        "source": SOURCE_HYBRID,
        "tdx_simulated": True,
        "gpu_attested": False,
        "gpu_memory_confidential": False,
        "digests": digests,
        "out": str(out_path),
        "generated_at": report["generated_at"],
        "note": (
            "In-repo E2E with simulated TDX. Replace simulation with live quotes "
            "via VIOLET_TDX_QUOTE_VERIFIER and enable publisher source "
            "cathedral_voice_hybrid before any reward-bearing loop."
        ),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    return proof


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "data" / "cathedral_voice_e2e_proof.json",
    )
    args = parser.parse_args()
    proof = asyncio.run(run_e2e_proof(out_path=args.out))
    print(
        json.dumps(
            {k: proof[k] for k in ("ok", "source", "tdx_simulated", "digests", "out")},
            indent=2,
        )
    )
    return 0 if proof.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
