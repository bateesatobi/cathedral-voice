"""Release image digest enforcement (G07)."""

from __future__ import annotations

import os

from violet.releases import ImagePolicy, ReleaseManifest


def _strict_manifest(**kwargs) -> ReleaseManifest:
    images = {
        "asr": ImagePolicy(
            repository="simonallanachuka/etoil-api",
            allowed_tags=["latest"],
            allowed_digests=[
                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ],
        ),
        "tts": ImagePolicy(
            repository="ghcr.io/cathedral-voice/spark-tts",
            allowed_tags=["latest"],
            allowed_digests=[
                "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            ],
        ),
    }
    return ReleaseManifest(version="test", images=images, strict_digests=True, **kwargs)


def test_reject_blank_declaration():
    m = _strict_manifest()
    ok, detail = m.check_declared("asr", "")
    assert not ok
    assert "blank" in detail or "required" in detail


def test_reject_mutable_latest_tag():
    m = _strict_manifest()
    ok, detail = m.check_declared("asr", "simonallanachuka/etoil-api:latest")
    assert not ok
    assert "mutable" in detail


def test_reject_unknown_digest():
    m = _strict_manifest()
    ok, detail = m.check_declared(
        "asr",
        "simonallanachuka/etoil-api@sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    )
    assert not ok
    assert "not in official set" in detail


def test_accept_allowed_digest():
    m = _strict_manifest()
    ok, detail = m.check_declared(
        "asr",
        "simonallanachuka/etoil-api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    assert ok
    assert detail == "digest allowed"


def test_empty_allow_list_fails_closed_in_strict():
    m = ReleaseManifest(
        images={
            "asr": ImagePolicy(
                repository="x/y", allowed_tags=["latest"], allowed_digests=[]
            )
        },
        strict_digests=True,
    )
    ok, detail = m.check_declared(
        "asr", "x/y@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    assert not ok
    assert "no allowed_digests" in detail


def test_bootstrap_non_strict_allows_listed_tag(monkeypatch):
    monkeypatch.setenv("VIOLET_REQUIRE_IMAGE_DIGESTS", "0")
    m = ReleaseManifest(
        images={
            "asr": ImagePolicy(
                repository="simonallanachuka/etoil-api",
                allowed_tags=["latest"],
                allowed_digests=[],
            )
        },
        strict_digests=False,
    )
    ok, _ = m.check_declared("asr", "simonallanachuka/etoil-api:latest")
    assert ok
