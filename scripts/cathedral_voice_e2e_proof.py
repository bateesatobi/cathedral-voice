#!/usr/bin/env python3
"""Cathedral Voice end-to-end proof harness (G10).

Dry (CI / in-repo)::

    CATHEDRAL_TDX_SIMULATION=1 python scripts/cathedral_voice_e2e_proof.py

Live (production cutover — no simulation)::

    python scripts/cathedral_voice_e2e_proof.py --live \\
      --measurement-file /path/to/measurement.json \\
      --ed25519-private-hex \"$VIOLET_RECEIPT_ED25519_PRIVATE_KEY\" \\
      --ed25519-public-hex \"$CATHEDRAL_RECEIPT_ED25519_PUBLIC_KEY\" \\
      --out ./data/cathedral_voice_e2e_live.json

Add ``--publish`` to POST to the real publisher (requires hybrid token/HMAC env).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_measurement(path: Path) -> str:
    raw = path.read_text(encoding="utf-8").strip()
    # Accept raw JSON object or already-encoded string.
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(data, dict):
        return json.dumps(data, sort_keys=True, separators=(",", ":"))
    return str(data)


async def run_e2e_proof(
    *,
    out_path: Path,
    live: bool = False,
    publish: bool = False,
    measurement_file: Optional[Path] = None,
    ed25519_private_hex: str = "",
    ed25519_public_hex: str = "",
    hotkey: str = "5FCathedralVoiceE2EHotkeyExample",
    challenge: str = "",
    endpoint: str = "http://127.0.0.1:8091",
) -> dict:
    """Run dry (simulated) or live (measurement file + real keys) proof."""
    from violet.cathedral.external_scores import (
        SOURCE_HYBRID,
        CathedralScoreClient,
        CathedralScoreClientConfig,
        build_hybrid_report,
        config_from_env,
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

    if live:
        if os.getenv("CATHEDRAL_TDX_SIMULATION", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise RuntimeError(
                "refusing --live while CATHEDRAL_TDX_SIMULATION is set"
            )
        if measurement_file is None or not measurement_file.is_file():
            raise RuntimeError("--live requires --measurement-file")
        priv = (
            ed25519_private_hex
            or os.getenv("VIOLET_RECEIPT_ED25519_PRIVATE_KEY", "")
        ).strip()
        pub = (
            ed25519_public_hex
            or os.getenv("CATHEDRAL_RECEIPT_ED25519_PUBLIC_KEY", "")
        ).strip()
        if not priv or not pub:
            raise RuntimeError(
                "--live requires Ed25519 private+public keys "
                "(flags or VIOLET_RECEIPT_ED25519_PRIVATE_KEY / "
                "CATHEDRAL_RECEIPT_ED25519_PUBLIC_KEY)"
            )
        measurement_raw = _load_measurement(measurement_file)
        challenge = (
            challenge
            or os.getenv("VIOLET_TDX_CHALLENGE", "").strip()
            or f"live-{int(time.time())}"
        )
        hotkey = (
            os.getenv("VIOLET_TDX_EXPECTED_HOTKEY", "").strip() or hotkey
        )
        endpoint = (
            os.getenv("VIOLET_TDX_EXPECTED_ENDPOINT", "").strip()
            or os.getenv("VIOLET_PUBLIC_ENDPOINT", "").strip()
            or endpoint
        )
        allow_sim = False
        tdx_simulated = False
        metadata_extra: dict[str, Any] = {
            "proof": "cathedral_voice_e2e_live",
            "tdx_simulated": False,
        }
    else:
        os.environ["CATHEDRAL_TDX_SIMULATION"] = "1"
        priv, pub = generate_ed25519_keypair()
        challenge = challenge or f"e2e-{int(time.time())}"
        measurement = simulate_controller_measurement(
            hotkey=hotkey,
            challenge=challenge,
            endpoint=endpoint,
        )
        measurement_raw = measurement.encode()
        allow_sim = True
        tdx_simulated = True
        metadata_extra = {
            "proof": "cathedral_voice_e2e",
            "tdx_simulated": True,
        }

    policy = TdxVerifyPolicy(
        require_measurement=True,
        allow_simulation=allow_sim,
        expected_challenge=challenge or None,
        expected_hotkey=hotkey,
        audience="cathedral_voice_hybrid",
        expected_endpoint=endpoint if live else None,
        quote_verifier_cmd=os.getenv("VIOLET_TDX_QUOTE_VERIFIER", "").strip() or None,
        allowed_mrtd=[
            x.strip()
            for x in (os.getenv("VIOLET_TDX_ALLOWED_MRTD") or "").split(",")
            if x.strip()
        ],
    )
    tdx = verify_controller_measurement(measurement_raw, policy)
    if not tdx.ok:
        raise RuntimeError(tdx.detail)

    audio = b"\x00\x01" * 1024
    prompt = "Cathedral voice end to end proof prompt"
    receipt = build_receipt(
        miner_hotkey=hotkey,
        input_text=prompt,
        voice="eng_female_1",
        audio=audio,
        controller_measurement=(
            measurement_raw
            if isinstance(measurement_raw, str)
            else measurement_raw
        ),
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
        metadata=metadata_extra,
    )

    if publish:
        cfg = config_from_env()
        cfg.enabled = True
        cfg.dry_run = False
        if not (cfg.hybrid_token or cfg.token):
            raise RuntimeError(
                "--publish requires hybrid/shared publisher token env"
            )
        client = CathedralScoreClient(cfg)
    else:
        client = CathedralScoreClient(
            CathedralScoreClientConfig(enabled=True, dry_run=True)
        )
    post = await client.post_report(report)
    await client.close()

    mrtd = ""
    try:
        parsed = json.loads(
            measurement_raw
            if isinstance(measurement_raw, str)
            else measurement_raw.decode("utf-8")
        )
        mrtd = str(parsed.get("mrtd") or "")
    except Exception:
        mrtd = ""

    digests = {
        "request_hash": receipt.request_hash,
        "audio_content_hash": receipt.audio_content_hash,
        "mrtd": mrtd,
        "challenge": challenge,
        "ed25519_public_key": pub,
        "receipt_signature_prefix": (receipt.signature or "")[:48],
        "report_epoch": report["epoch"],
        "report_source": report["source"],
        "scores": len(report["scores"]),
        "post_status": post.get("status"),
        "post_ok": post.get("ok"),
        "post_body": post.get("body"),
        "publisher_report_id": (post.get("body") or {}).get("report_id")
        if isinstance(post.get("body"), dict)
        else None,
        "publisher_vector_hint": (post.get("body") or {}).get("vector_id")
        if isinstance(post.get("body"), dict)
        else None,
    }
    proof = {
        "ok": bool(post.get("ok")) and report["source"] == SOURCE_HYBRID,
        "source": SOURCE_HYBRID,
        "live": live,
        "published": publish,
        "tdx_simulated": tdx_simulated,
        "gpu_attested": False,
        "gpu_memory_confidential": False,
        "digests": digests,
        "out": str(out_path),
        "generated_at": report["generated_at"],
        "note": (
            "Live E2E with hardware measurement."
            if live
            else (
                "In-repo E2E with simulated TDX. Replace simulation with live quotes "
                "via VIOLET_TDX_QUOTE_VERIFIER and enable publisher source "
                "cathedral_voice_hybrid before any reward-bearing loop."
            )
        ),
        "record_ids": [
            "request_hash",
            "audio_content_hash",
            "mrtd",
            "report_epoch",
            "publisher_report_id",
            "publisher_vector_hint",
            "chain_extrinsic",
        ],
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
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use live measurement file + keys; refuse simulation",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="POST to real publisher (requires hybrid token/HMAC env)",
    )
    parser.add_argument("--measurement-file", type=Path, default=None)
    parser.add_argument("--ed25519-private-hex", default="")
    parser.add_argument("--ed25519-public-hex", default="")
    parser.add_argument("--hotkey", default="5FCathedralVoiceE2EHotkeyExample")
    parser.add_argument("--challenge", default="")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8091")
    args = parser.parse_args()
    proof = asyncio.run(
        run_e2e_proof(
            out_path=args.out,
            live=args.live,
            publish=args.publish,
            measurement_file=args.measurement_file,
            ed25519_private_hex=args.ed25519_private_hex,
            ed25519_public_hex=args.ed25519_public_hex,
            hotkey=args.hotkey,
            challenge=args.challenge,
            endpoint=args.endpoint,
        )
    )
    print(
        json.dumps(
            {
                k: proof[k]
                for k in (
                    "ok",
                    "source",
                    "live",
                    "published",
                    "tdx_simulated",
                    "digests",
                    "out",
                )
                if k in proof
            },
            indent=2,
        )
    )
    return 0 if proof.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
