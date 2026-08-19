"""Classify host GPUs the same way Capacity scoring does.

Install scripts call this so they do not re-implement ``nvidia-smi`` parsing
in bash (``nvidia-smi -L`` counts MIG slices; naive comma-split breaks on
quoted CSV; substring matching mis-labels GH200 as H200).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

from ..constants import classify_gpu
from .gpu import parse_nvidia_smi

# Physical GPUs only (not MIG instances). Extra fields are for operator logs.
_INVENTORY_QUERY = (
    "index,name,memory.total,memory.used,utilization.gpu,uuid,compute_cap,driver_version"
)
_NVIDIA_SMI_TIMEOUT_S = 10.0


def run_nvidia_smi_inventory() -> Optional[str]:
    binary = shutil.which("nvidia-smi")
    if not binary:
        return None
    try:
        completed = subprocess.run(
            [
                binary,
                f"--query-gpu={_INVENTORY_QUERY}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=_NVIDIA_SMI_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def parse_inventory_csv(output: str) -> List[Dict[str, Any]]:
    """Parse the extended inventory query into per-GPU dicts."""
    accepted, _rejected = parse_nvidia_smi(output)
    accepted_by_index = {gpu.index: gpu for gpu in accepted}
    rows: List[Dict[str, Any]] = []

    for line in (output or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parts = [part.strip() for part in next(csv.reader(io.StringIO(line)))]
        except csv.Error:
            continue
        if len(parts) < 3:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        name = parts[1]
        try:
            vram_mib = float(parts[2])
        except (TypeError, ValueError):
            vram_mib = 0.0
        vram_gb = round(vram_mib / 1024.0, 1) if vram_mib else 0.0
        gpu = accepted_by_index.get(index)
        tier = classify_gpu(name, vram_gb)
        rows.append(
            {
                "index": index,
                "name": name,
                "vram_gb": vram_gb,
                "uuid": parts[5] if len(parts) > 5 else "",
                "compute_cap": parts[6] if len(parts) > 6 else "",
                "driver_version": parts[7] if len(parts) > 7 else "",
                "accepted": gpu is not None,
                "tier_key": gpu.tier_key if gpu else (tier.key if tier else ""),
                "multiplier": gpu.multiplier if gpu else (tier.multiplier if tier else 0.0),
                "display_name": (
                    gpu.product_name if gpu else (tier.display_name if tier else name)
                ),
            }
        )
    return rows


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    accepted = [row for row in rows if row.get("accepted")]
    rejected = [row for row in rows if not row.get("accepted")]
    counts: Dict[str, int] = {}
    for row in accepted:
        key = str(row.get("tier_key") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    units = round(sum(float(row.get("multiplier") or 0.0) for row in accepted), 4)
    return {
        "gpu_count": len(rows),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "capacity_units": units,
        "tier_counts": counts,
        "gpus": rows,
        "nvidia_smi": bool(rows) or shutil.which("nvidia-smi") is not None,
    }


def collect(output: Optional[str] = None) -> Dict[str, Any]:
    raw = output if output is not None else run_nvidia_smi_inventory()
    if not raw:
        return summarize([])
    return summarize(parse_inventory_csv(raw))


def format_human(summary: Dict[str, Any]) -> str:
    lines = [
        f"GPUs (physical): {summary['gpu_count']}  "
        f"accepted={summary['accepted_count']}  "
        f"rejected={summary['rejected_count']}  "
        f"capacity_units={summary['capacity_units']}"
    ]
    if summary.get("tier_counts"):
        counts = ", ".join(
            f"{count}×{key}"
            for key, count in sorted(summary["tier_counts"].items())
        )
        lines.append(f"Capacity tiers: {counts}")
    for row in summary.get("gpus") or []:
        flag = "ok" if row.get("accepted") else "REJECTED"
        extra = ""
        if row.get("compute_cap"):
            extra += f" sm={row['compute_cap']}"
        if row.get("uuid"):
            extra += f" uuid={row['uuid'][:13]}…" if len(str(row.get("uuid"))) > 14 else f" uuid={row['uuid']}"
        lines.append(
            f"  [{row['index']}] {row['name']}  {row['vram_gb']} GB  "
            f"{row.get('tier_key') or '—'} ×{row.get('multiplier') or 0}  {flag}{extra}"
        )
    if not summary.get("gpus"):
        lines.append("  (no NVIDIA GPUs reported by nvidia-smi --query-gpu=index)")
    return "\n".join(lines)


def format_env(summary: Dict[str, Any]) -> str:
    counts = summary.get("tier_counts") or {}
    tier_summary = ",".join(f"{key}:{count}" for key, count in sorted(counts.items()))
    accepted_idx = ",".join(
        str(row["index"]) for row in summary.get("gpus") or [] if row.get("accepted")
    )
    rejected_idx = ",".join(
        str(row["index"]) for row in summary.get("gpus") or [] if not row.get("accepted")
    )
    return "\n".join(
        [
            f"GPU_COUNT={summary['gpu_count']}",
            f"GPU_ACCEPTED_COUNT={summary['accepted_count']}",
            f"GPU_REJECTED_COUNT={summary['rejected_count']}",
            f"GPU_CAPACITY_UNITS={summary['capacity_units']}",
            f"GPU_TIER_SUMMARY={tier_summary}",
            f"GPU_ACCEPTED_INDICES={accepted_idx}",
            f"GPU_REJECTED_INDICES={rejected_idx}",
        ]
    )


def check_exit_code(summary: Dict[str, Any]) -> int:
    """0 = at least one allowed GPU, 2 = GPUs present but none allowed, 3 = none."""
    if int(summary.get("accepted_count") or 0) >= 1:
        return 0
    if int(summary.get("gpu_count") or 0) >= 1:
        return 2
    return 3


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Inventory GPUs for cathedral-voice Capacity")
    parser.add_argument("--json", action="store_true", help="Print JSON inventory")
    parser.add_argument("--env", action="store_true", help="Print GPU_* assignments for eval")
    parser.add_argument("--human", action="store_true", help="Print operator summary (default)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 0 only if at least one Capacity-allowed GPU is present",
    )
    parser.add_argument(
        "--list-tiers",
        action="store_true",
        help="Print allowed tier keys and exit",
    )
    parser.add_argument(
        "--from-csv",
        metavar="PATH",
        help="Read nvidia-smi CSV from a file instead of invoking nvidia-smi",
    )
    args = parser.parse_args(argv)

    if args.list_tiers:
        from ..constants import GPU_TIERS

        for tier in GPU_TIERS:
            print(
                f"{tier.key}\t{tier.display_name}\t{tier.vram_gb}GB\t×{tier.multiplier}"
            )
        return 0

    raw = None
    if args.from_csv:
        if args.from_csv == "-":
            raw = sys.stdin.read()
        else:
            with open(args.from_csv, encoding="utf-8") as handle:
                raw = handle.read()
    summary = collect(raw)

    if args.check:
        if args.json:
            print(json.dumps(summary, indent=2))
        elif args.env:
            print(format_env(summary))
        return check_exit_code(summary)

    if args.json:
        print(json.dumps(summary, indent=2))
    elif args.env:
        print(format_env(summary))
    else:
        print(format_human(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
