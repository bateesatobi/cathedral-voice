"""GPU inventory CLI used by miner install scripts."""

from __future__ import annotations

from violet.miner.gpu_inventory import check_exit_code, collect, format_env, parse_inventory_csv


def test_parse_extended_csv_classifies_blackwell_and_consumer():
    output = (
        "0, NVIDIA GB200, 196608, 0, 0, GPU-aaa, 10.0, 570.00\n"
        "1, NVIDIA GeForce RTX 3090, 24576, 0, 0, GPU-bbb, 8.6, 560.00\n"
        "2, NVIDIA GeForce RTX 3080, 10240, 0, 0, GPU-ccc, 8.6, 560.00\n"
    )
    rows = parse_inventory_csv(output)
    assert rows[0]["tier_key"] == "gb200" and rows[0]["accepted"]
    assert rows[1]["tier_key"] == "rtx_3090" and rows[1]["accepted"]
    assert rows[2]["accepted"] is False
    summary = collect(output)
    assert summary["gpu_count"] == 3
    assert summary["accepted_count"] == 2
    assert summary["rejected_count"] == 1
    env = format_env(summary)
    assert "GPU_COUNT=3" in env
    assert "GPU_ACCEPTED_INDICES=0,1" in env
    assert "GPU_REJECTED_INDICES=2" in env
    assert check_exit_code(summary) == 0


def test_unlisted_only_fails_check():
    output = "0, NVIDIA GeForce RTX 3080, 10240, 0, 0, GPU-ccc, 8.6, 560.00\n"
    summary = collect(output)
    assert check_exit_code(summary) == 2
    assert summary["accepted_count"] == 0


def test_no_gpus_check():
    assert check_exit_code(collect("")) == 3


def test_gh200_not_labelled_h200():
    rows = parse_inventory_csv("0, NVIDIA GH200 144GB, 147456, 0, 0, GPU-x, 9.0, 550.00\n")
    assert rows[0]["tier_key"] == "gh200"
    assert rows[0]["accepted"]
