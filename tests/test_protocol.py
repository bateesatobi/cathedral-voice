"""Wire protocol: GPU classification, capacity reports, on-chain commitments."""

from __future__ import annotations

import pytest

from violet.chain.commitment import (
    MAX_COMMITMENT_BYTES,
    CommitmentError,
    decode_announcement,
    encode_announcement,
    is_compatible,
)
from violet.constants import GPU_TIERS_BY_KEY, classify_gpu
from violet.miner.gpu import parse_nvidia_smi
from violet.protocol import CapacityReport, GpuInfo, HealthReport, MinerAnnouncement


class TestGpuClassification:
    def test_accepted_models(self):
        assert classify_gpu("NVIDIA H200").key == "h200"
        assert classify_gpu("NVIDIA H100 NVL").key == "h100_nvl"
        assert classify_gpu("NVIDIA H100 80GB HBM3").key == "h100_80"

    def test_a100_split_resolved_by_vram(self):
        # The product name alone does not distinguish 40 GB from 80 GB on every
        # driver version.
        assert classify_gpu("NVIDIA A100-SXM4-40GB", 40).key == "a100_40"
        assert classify_gpu("NVIDIA A100", 80).key == "a100_80"

    def test_nvl_matched_before_plain_h100(self):
        # Order matters: "h100" substring-matches an NVL card too.
        assert classify_gpu("NVIDIA H100 NVL", 94).multiplier == 2.7

    def test_rejected_models(self):
        assert classify_gpu("NVIDIA GeForce RTX 4090", 24) is None
        assert classify_gpu("NVIDIA L40S", 48) is None
        assert classify_gpu("") is None

    def test_multipliers_increase_with_capability(self):
        keys = ["a100_40", "a100_80", "h100_80", "h100_nvl", "h200"]
        multipliers = [GPU_TIERS_BY_KEY[key].multiplier for key in keys]
        assert multipliers == sorted(multipliers)


class TestNvidiaSmiParsing:
    def test_parses_accepted_cards(self):
        output = (
            "0, NVIDIA H100 80GB HBM3, 81559, 1024, 15\n"
            "1, NVIDIA H100 80GB HBM3, 81559, 2048, 30\n"
        )
        accepted, rejected = parse_nvidia_smi(output)
        assert len(accepted) == 2
        assert not rejected
        assert accepted[0].tier_key == "h100_80"
        assert accepted[0].vram_gb == pytest.approx(79.6, abs=0.5)

    def test_reports_rejected_cards_rather_than_dropping_them(self):
        # The operator needs to know why a card earns nothing.
        accepted, rejected = parse_nvidia_smi("0, NVIDIA GeForce RTX 4090, 24564, 512, 5\n")
        assert not accepted
        assert len(rejected) == 1
        assert "4090" in rejected[0]

    def test_mixed_host(self):
        output = (
            "0, NVIDIA H200, 143771, 0, 0\n"
            "1, NVIDIA GeForce RTX 3090, 24576, 0, 0\n"
        )
        accepted, rejected = parse_nvidia_smi(output)
        assert len(accepted) == 1 and len(rejected) == 1

    def test_tolerates_garbage(self):
        accepted, _ = parse_nvidia_smi("not, valid\n\n[N/A]\n")
        assert accepted == []

    def test_empty(self):
        assert parse_nvidia_smi("") == ([], [])


class TestCapacityReport:
    def test_units_sum_multipliers(self):
        report = CapacityReport(
            gpus=[
                GpuInfo(0, "H100", 80, "h100_80", 2.4),
                GpuInfo(1, "H100", 80, "h100_80", 2.4),
            ]
        )
        assert report.capacity_units == pytest.approx(4.8)

    def test_load_factor_bounds(self):
        report = CapacityReport(max_concurrent_asr=4, max_concurrent_tts=4, active_asr=4, active_tts=4)
        assert report.load_factor == 1.0

        idle = CapacityReport(max_concurrent_asr=4, max_concurrent_tts=4)
        assert idle.load_factor == 0.0

    def test_load_factor_without_limits(self):
        assert CapacityReport().load_factor == 0.0

    def test_round_trip(self):
        original = CapacityReport(
            gpus=[GpuInfo(0, "H200", 141, "h200", 3.5, 42.0, 8.0)],
            system_memory_gb=512.0, cpu_count=64,
            max_concurrent_asr=28, max_concurrent_tts=35,
        )
        restored = CapacityReport.from_dict(original.to_dict())
        assert restored.capacity_units == original.capacity_units
        assert restored.gpus[0].product_name == "H200"


class TestHealthReport:
    def test_round_trip(self):
        original = HealthReport(
            status="ok", hotkey="5Abc", services=["asr", "tts"],
            upstreams={"asr": True, "tts": True},
            capacity=CapacityReport(gpus=[GpuInfo(0, "H200", 141, "h200", 3.5)]),
        )
        restored = HealthReport.from_dict(original.to_dict())
        assert restored.status == "ok"
        assert restored.healthy
        assert restored.capacity.capacity_units == pytest.approx(3.5)

    def test_degraded_is_not_healthy_but_is_parseable(self):
        report = HealthReport.from_dict({"status": "degraded", "hotkey": "x"})
        assert not report.healthy

    def test_tolerates_missing_fields(self):
        report = HealthReport.from_dict({})
        assert report.status == "unhealthy"
        assert report.capacity is None


class TestCommitments:
    def test_round_trip(self):
        original = MinerAnnouncement(
            endpoint="https://miner.example.com",
            services=["asr", "tts"],
            gpus={"h100_80": 4, "a100_80": 2},
            asr_image="sha256:abc",
        )
        decoded = decode_announcement(encode_announcement(original))

        assert decoded is not None
        assert decoded.endpoint == original.endpoint
        assert sorted(decoded.services) == ["asr", "tts"]
        assert decoded.gpus == original.gpus
        assert decoded.asr_image == "sha256:abc"
        assert decoded.capacity_units == pytest.approx(4 * 2.4 + 2 * 1.6)

    def test_encoding_is_compact(self):
        encoded = encode_announcement(
            MinerAnnouncement(
                endpoint="https://miner.example.com",
                services=["asr", "tts"],
                gpus={"h200": 8},
            )
        )
        assert len(encoded) < MAX_COMMITMENT_BYTES

    def test_oversized_payload_is_rejected_before_paying_for_it(self):
        with pytest.raises(CommitmentError):
            encode_announcement(
                MinerAnnouncement(
                    endpoint="https://" + "a" * 400 + ".com",
                    services=["asr"],
                    gpus={"h200": 1},
                    asr_image="sha256:" + "b" * 200,
                )
            )

    def test_foreign_commitments_are_ignored_not_fatal(self):
        # The subnet shares commitment space with anything a hotkey publishes.
        assert decode_announcement("") is None
        assert decode_announcement("hello world") is None
        assert decode_announcement("violet1|!!!not-base64!!!") is None
        assert decode_announcement("otherproject|abcdef") is None
        assert decode_announcement(None) is None

    def test_rejects_non_http_endpoint(self):
        import base64
        import json

        payload = base64.urlsafe_b64encode(
            json.dumps({"e": "ftp://x.com", "s": "a", "g": "", "v": 1, "t": 0}).encode()
        ).decode().rstrip("=")
        assert decode_announcement(f"violet1|{payload}") is None

    def test_unknown_gpu_tier_is_skipped_not_fatal(self):
        # A newer spec version may name tiers this build does not know.
        import base64
        import json

        payload = base64.urlsafe_b64encode(
            json.dumps(
                {"e": "https://x.com", "s": "at", "g": "h200:1,b300:4", "v": 1, "t": 0}
            ).encode()
        ).decode().rstrip("=")
        decoded = decode_announcement(f"violet1|{payload}")
        assert decoded is not None
        assert decoded.gpus == {"h200": 1}

    def test_no_services_is_rejected(self):
        with pytest.raises(CommitmentError):
            encode_announcement(
                MinerAnnouncement(endpoint="https://x.com", services=[], gpus={})
            )

    def test_zero_gpu_announcement_is_valid(self):
        # Honest zero: the miner serves but earns no capacity.
        decoded = decode_announcement(
            encode_announcement(
                MinerAnnouncement(endpoint="https://x.com", services=["tts"], gpus={})
            )
        )
        assert decoded is not None and decoded.capacity_units == 0.0

    def test_version_compatibility(self):
        assert is_compatible(MinerAnnouncement("https://x", ["asr"], {}, spec_version=1))
        assert not is_compatible(MinerAnnouncement("https://x", ["asr"], {}, spec_version=0))
        assert not is_compatible(MinerAnnouncement("https://x", ["asr"], {}, spec_version=99))
