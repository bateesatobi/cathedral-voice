#!/usr/bin/env python3
"""Fail-closed production preflight for Cathedral Voice miner/validator hosts.

Exits 0 only when production-dangerous config is absent and required knobs for
the selected role are present.

Examples::

    python scripts/cathedral_voice_production_preflight.py --role miner
    python scripts/cathedral_voice_production_preflight.py --role validator
    python scripts/cathedral_voice_production_preflight.py --role both
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _truthy(name: str) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _set(name: str) -> bool:
    return bool((os.getenv(name) or "").strip())


@dataclass
class CheckResult:
    ok: bool
    code: str
    detail: str


def _fail(code: str, detail: str) -> CheckResult:
    return CheckResult(False, code, detail)


def _pass(code: str, detail: str = "ok") -> CheckResult:
    return CheckResult(True, code, detail)


def check_common() -> List[CheckResult]:
    out: List[CheckResult] = []
    if _truthy("CATHEDRAL_TDX_SIMULATION"):
        out.append(
            _fail(
                "tdx_simulation_forbidden",
                "CATHEDRAL_TDX_SIMULATION is set — forbidden on production hosts",
            )
        )
    else:
        out.append(_pass("tdx_simulation_unset"))

    if _truthy("CATHEDRAL_VOICE_HYBRID_ALLOW_SIMULATION"):
        out.append(
            _fail(
                "publisher_sim_allow_forbidden",
                "CATHEDRAL_VOICE_HYBRID_ALLOW_SIMULATION must be unset in production",
            )
        )
    else:
        out.append(_pass("publisher_sim_allow_unset"))

    digests = os.getenv("VIOLET_REQUIRE_IMAGE_DIGESTS")
    if digests is not None and digests.strip() in {"0", "false", "no", "off"}:
        out.append(
            _fail(
                "image_digests_relaxed",
                "VIOLET_REQUIRE_IMAGE_DIGESTS is relaxed — set to 1 or unset (default on)",
            )
        )
    else:
        out.append(_pass("image_digests_strict"))
    return out


def check_miner() -> List[CheckResult]:
    out: List[CheckResult] = []
    if not _truthy("VIOLET_TTS_RECEIPT_ENABLED"):
        out.append(_fail("receipt_disabled", "VIOLET_TTS_RECEIPT_ENABLED must be 1"))
    else:
        out.append(_pass("receipt_enabled"))

    if not _truthy("VIOLET_TTS_RECEIPT_BUFFER"):
        out.append(
            _fail("receipt_buffer_disabled", "VIOLET_TTS_RECEIPT_BUFFER must be 1")
        )
    else:
        out.append(_pass("receipt_buffer_enabled"))

    if not _set("VIOLET_RECEIPT_ED25519_PRIVATE_KEY"):
        out.append(
            _fail(
                "receipt_key_missing",
                "VIOLET_RECEIPT_ED25519_PRIVATE_KEY required (generate in TDX guest)",
            )
        )
    else:
        out.append(_pass("receipt_key_present"))

    if not _set("VIOLET_TDX_MEASUREMENT"):
        out.append(
            _fail(
                "tdx_measurement_missing",
                "VIOLET_TDX_MEASUREMENT required for live hybrid receipts",
            )
        )
    else:
        out.append(_pass("tdx_measurement_present"))

    if not _set("VIOLET_TDX_QUOTE_VERIFIER"):
        out.append(
            _fail(
                "quote_verifier_missing",
                "VIOLET_TDX_QUOTE_VERIFIER required (DCAP wrapper; stdin=quote b64)",
            )
        )
    else:
        out.append(_pass("quote_verifier_present"))

    if not _set("VIOLET_TDX_ALLOWED_MRTD"):
        out.append(
            _fail("mrtd_allowlist_missing", "VIOLET_TDX_ALLOWED_MRTD required")
        )
    else:
        out.append(_pass("mrtd_allowlist_present"))
    return out


def check_validator() -> List[CheckResult]:
    out: List[CheckResult] = []
    if not _set("VALIDATOR_TRUSTED_ASR_URL"):
        out.append(
            _fail(
                "trusted_asr_missing",
                "VALIDATOR_TRUSTED_ASR_URL required for production semantic TTS",
            )
        )
    else:
        out.append(_pass("trusted_asr_present"))

    if not _truthy("VALIDATOR_TTS_SEMANTIC_REQUIRED"):
        out.append(
            _fail(
                "semantic_not_required",
                "VALIDATOR_TTS_SEMANTIC_REQUIRED must be 1 in production",
            )
        )
    else:
        out.append(_pass("semantic_required"))

    if not _set("VALIDATOR_TTS_HOLDOUT_PATH"):
        out.append(
            _fail(
                "holdout_missing",
                "VALIDATOR_TTS_HOLDOUT_PATH required (private holdout evalset)",
            )
        )
    else:
        out.append(_pass("holdout_present"))

    if not _truthy("CATHEDRAL_EXTERNAL_SCORES_ENABLED") and not _truthy(
        "CATHEDRAL_VOICE_SCORES_ENABLED"
    ):
        out.append(
            _fail(
                "external_scores_disabled",
                "CATHEDRAL_EXTERNAL_SCORES_ENABLED must be 1 to publish",
            )
        )
    else:
        out.append(_pass("external_scores_enabled"))

    if _truthy("CATHEDRAL_EXTERNAL_SCORES_DRY_RUN"):
        out.append(
            _fail(
                "dry_run_on",
                "CATHEDRAL_EXTERNAL_SCORES_DRY_RUN must be 0 for live publish",
            )
        )
    else:
        out.append(_pass("dry_run_off"))

    if not _truthy("CATHEDRAL_HYBRID_SCORES_ENABLED"):
        out.append(
            _fail(
                "hybrid_disabled",
                "CATHEDRAL_HYBRID_SCORES_ENABLED must be 1 for hybrid lane",
            )
        )
    else:
        out.append(_pass("hybrid_enabled"))

    if not _set("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_VOICE_HYBRID"):
        out.append(
            _fail(
                "hybrid_token_missing",
                "CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_VOICE_HYBRID required",
            )
        )
    else:
        out.append(_pass("hybrid_token_present"))

    if not _set("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_VOICE_HYBRID"):
        out.append(
            _fail(
                "hybrid_hmac_missing",
                "CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_VOICE_HYBRID required",
            )
        )
    else:
        out.append(_pass("hybrid_hmac_present"))

    if not _set("CATHEDRAL_RECEIPT_ED25519_PUBLIC_KEY"):
        out.append(
            _fail(
                "receipt_pubkey_missing",
                "CATHEDRAL_RECEIPT_ED25519_PUBLIC_KEY required to verify receipts",
            )
        )
    else:
        out.append(_pass("receipt_pubkey_present"))

    if not _truthy("CATHEDRAL_HYBRID_REQUIRE_TDX"):
        out.append(
            _fail(
                "tdx_not_required",
                "CATHEDRAL_HYBRID_REQUIRE_TDX must be 1 in production",
            )
        )
    else:
        out.append(_pass("tdx_required"))

    if not _set("VIOLET_TDX_QUOTE_VERIFIER"):
        out.append(
            _fail(
                "quote_verifier_missing",
                "VIOLET_TDX_QUOTE_VERIFIER required on validator for live quotes",
            )
        )
    else:
        out.append(_pass("quote_verifier_present"))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role",
        choices=("miner", "validator", "both", "common"),
        default="both",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON summary",
    )
    args = parser.parse_args()

    checks: List[CheckResult] = []
    checks.extend(check_common())
    if args.role in ("miner", "both"):
        checks.extend(check_miner())
    if args.role in ("validator", "both"):
        checks.extend(check_validator())

    failed = [c for c in checks if not c.ok]
    payload = {
        "ok": not failed,
        "role": args.role,
        "passed": sum(1 for c in checks if c.ok),
        "failed": len(failed),
        "checks": [{"ok": c.ok, "code": c.code, "detail": c.detail} for c in checks],
    }
    if args.json:
        import json

        print(json.dumps(payload, indent=2))
    else:
        for c in checks:
            mark = "PASS" if c.ok else "FAIL"
            print(f"[{mark}] {c.code}: {c.detail}")
        print(
            f"\nsummary: {'OK' if payload['ok'] else 'NOT READY'} "
            f"({payload['passed']} passed, {payload['failed']} failed)"
        )
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
