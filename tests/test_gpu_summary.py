"""GPU inventory summarisation for router → admin sync."""

from __future__ import annotations

from violet.protocol import GpuInfo
from violet.router.registry import summarize_announced_gpus, summarize_gpus


def test_summarize_gpus_h200_pair():
    gpus = [
        GpuInfo(0, "NVIDIA H200", 141.0, "h200", 3.5),
        GpuInfo(1, "NVIDIA H200", 141.0, "h200", 3.5),
    ]
    model, count, tier, vram, summary = summarize_gpus(gpus)
    assert model == "H200"
    assert count == 2
    assert tier == "h200"
    assert vram == 141.0
    assert summary == "2×H200"


def test_summarize_mixed_gpus():
    gpus = [
        GpuInfo(0, "NVIDIA H100 80GB", 80.0, "h100_80", 2.4),
        GpuInfo(1, "NVIDIA H200", 141.0, "h200", 3.5),
    ]
    model, count, tier, vram, summary = summarize_gpus(gpus)
    assert count == 2
    assert model == "H200"  # highest multiplier wins primary
    assert "H200" in summary and "H100" in summary


def test_summarize_announced():
    model, count, tier, vram, summary = summarize_announced_gpus({"h200": 4})
    assert model == "H200"
    assert count == 4
    assert tier == "h200"
    assert summary == "4×H200"


def test_empty_inventory():
    assert summarize_gpus([]) == ("", 0, "", 0.0, "")
    assert summarize_announced_gpus({}) == ("", 0, "", 0.0, "")
