"""
Network-wide constants for the Violet subnet.

Everything here is part of the published specification (TDD v1.4). Miners,
validators and the smart router must agree on these values, so they live in one
module rather than being duplicated per-component.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

# --------------------------------------------------------------------------
# Protocol identity
# --------------------------------------------------------------------------

#: Bumped whenever the miner API surface or scoring inputs change in a way that
#: is not backwards compatible. Included in on-chain commitments and in weight
#: submissions (``version_key``) so the network can reason about upgrades.
SPEC_VERSION = 1

#: Prefix stamped on every on-chain miner announcement so foreign commitments on
#: the same netuid are ignored cheaply.
COMMITMENT_MAGIC = "violet1"

#: Registered Violet subnet IDs on Bittensor.
NETUID_MAINNET = 39
NETUID_TESTNET = 292

# --------------------------------------------------------------------------
# Hardware (TDD 4.1)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GpuTier:
    """One accepted GPU class and its capacity weighting."""

    key: str
    display_name: str
    vram_gb: int
    multiplier: float
    status: str
    #: Substrings matched (case-insensitively) against ``nvidia-smi`` output.
    match: tuple

    def matches(self, product_name: str) -> bool:
        name = (product_name or "").lower()
        return any(token in name for token in self.match)


#: Only these GPUs are accepted. Order matters: the first match wins, so more
#: specific entries (H100 NVL) must precede more general ones (H100).
GPU_TIERS: List[GpuTier] = [
    GpuTier("h200", "H200", 141, 3.5, "preferred", ("h200",)),
    GpuTier("h100_nvl", "H100 NVL", 94, 2.7, "excellent", ("h100 nvl", "h100nvl")),
    GpuTier("h100_80", "H100 80 GB", 80, 2.4, "excellent", ("h100",)),
    GpuTier("a100_80", "A100 80 GB", 80, 1.6, "recommended", ("a100-sxm4-80", "a100 80", "a100-80")),
    GpuTier("a100_40", "A100 40 GB", 40, 1.0, "minimum", ("a100",)),
]

GPU_TIERS_BY_KEY: Dict[str, GpuTier] = {tier.key: tier for tier in GPU_TIERS}

#: VRAM tolerance when cross-checking a reported GPU against its tier. Vendors
#: and drivers report slightly different totals (e.g. 81559 MiB for an 80 GB
#: card), so an exact match would produce false accusations of misreporting.
VRAM_TOLERANCE_FRACTION = 0.12

#: Minimum system memory in GB (TDD 4.1).
MIN_SYSTEM_MEMORY_GB = 128


def classify_gpu(product_name: str, vram_gb: float | None = None) -> GpuTier | None:
    """Return the accepted tier for a GPU product name, or ``None`` if rejected.

    ``vram_gb`` disambiguates the A100 40/80 split, which cannot be resolved
    from the product name alone on every driver version.
    """
    for tier in GPU_TIERS:
        if not tier.matches(product_name):
            continue
        if tier.key == "a100_40" and vram_gb and vram_gb >= 60:
            return GPU_TIERS_BY_KEY["a100_80"]
        return tier
    return None


# --------------------------------------------------------------------------
# Incentive mechanism (TDD 7)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseWeights:
    """Relative weighting of the three score components for a network phase."""

    name: str
    capacity: float
    work: float
    quality: float

    def as_tuple(self) -> tuple:
        return (self.capacity, self.work, self.quality)


#: Midpoints of the published ranges. The subnet owner may retune within the
#: documented band (TDD 11) by overriding ``VIOLET_PHASE`` or the individual
#: weight environment variables.
PHASES: Dict[str, PhaseWeights] = {
    "launch": PhaseWeights("launch", 0.75, 0.125, 0.125),
    "growth": PhaseWeights("growth", 0.55, 0.275, 0.175),
    "mature": PhaseWeights("mature", 0.40, 0.45, 0.15),
}

DEFAULT_PHASE = "launch"

#: Rolling scoring window (TDD 7: "six to seven days").
SCORE_WINDOW_DAYS = 7

#: Weight submission cadence (TDD 5 / 7).
WEIGHT_SET_INTERVAL_BLOCKS = 150

#: Blend factor for score smoothing (TDD 9.2): ``new = alpha*current + (1-alpha)*previous``.
SCORE_SMOOTHING_ALPHA = 0.3

# --------------------------------------------------------------------------
# Performance targets (TDD 1 / 8)
# --------------------------------------------------------------------------

#: Sustained first-byte latency target for streaming interactions, milliseconds.
TARGET_FIRST_BYTE_MS = 200

#: Above this, the latency multiplier applied to Work score reaches its floor.
MAX_ACCEPTABLE_FIRST_BYTE_MS = 1500

#: Floor of the latency multiplier so a slow-but-working miner still earns
#: something rather than being pushed to zero and exiting the network.
LATENCY_MULTIPLIER_FLOOR = 0.2

#: Concurrency the network is dimensioned for at 100 simultaneous users (TDD 8).
TARGET_CONCURRENT_ASR_STREAMS = 40
TARGET_CONCURRENT_TTS_STREAMS = 50

# --------------------------------------------------------------------------
# Qualification thresholds (TDD 4.4)
# --------------------------------------------------------------------------

#: Word error rate ceiling on the reference corpus. Deliberately loose: the
#: evaluation set includes low-resource African languages where a strict
#: threshold would reject otherwise healthy miners running the official image.
QUALIFY_MAX_WER = 0.35

#: Health probe must return valid JSON within this many seconds.
QUALIFY_HEALTH_TIMEOUT_S = 5.0

#: Batch ASR of a short sample must complete within this many seconds.
QUALIFY_ASR_TIMEOUT_S = 30.0

#: Batch TTS of a fixed prompt must complete within this many seconds.
QUALIFY_TTS_TIMEOUT_S = 30.0

#: A streaming test must yield its first partial/frame within this many seconds.
QUALIFY_STREAM_FIRST_CHUNK_S = 3.0

#: Sustained availability observation window (TDD 4.4: 30-60 minutes).
QUALIFY_AVAILABILITY_WINDOW_S = 30 * 60

#: Fraction of health checks that may fail during the availability window.
QUALIFY_MAX_HEALTH_FAILURE_RATE = 0.05

#: Minimum bytes of audio for a TTS response to count as real speech.
MIN_TTS_AUDIO_BYTES = 512

#: How long a passed qualification remains valid before re-testing.
QUALIFICATION_TTL_HOURS = 24

# --------------------------------------------------------------------------
# Anti-gaming (TDD 9)
# --------------------------------------------------------------------------

#: Consecutive scoring windows of multi-UID abuse before temporary exclusion.
MULTI_UID_STRIKE_EXCLUSION = 2

#: Strikes before a coldkey is permanently blacklisted.
MULTI_UID_STRIKE_BLACKLIST = 5

#: How long a temporary exclusion lasts.
MULTI_UID_EXCLUSION_HOURS = 72

#: Per-window score decay applied to a miner that is failing health checks.
UNHEALTHY_SCORE_DECAY = 0.5

#: Reported-vs-observed capacity discrepancy tolerated before penalty (TDD 9.2).
RESOURCE_CLAIM_TOLERANCE = 0.25

#: Multiplier applied to a miner caught materially misreporting resources.
RESOURCE_MISREPORT_PENALTY = 0.25

# --------------------------------------------------------------------------
# Services (TDD 3)
# --------------------------------------------------------------------------

SERVICE_ASR = "asr"
SERVICE_TTS = "tts"
SERVICES = (SERVICE_ASR, SERVICE_TTS)

#: Language codes the ASR side of the network is expected to serve. Mirrors
#: ``ASRAPI/utils/utils.py:language_map`` so the router can route by language.
ASR_LANGUAGES = (
    "eng", "swa", "lug", "nyn", "ach", "teo", "lgg", "xog", "ttj", "kin", "myx", "fr",
)

#: TTS language codes, mirroring ``ASRAPI/constants/speakers.py:NEURAL_LANGUAGES``.
TTS_LANGUAGES = (
    "en", "ach", "teo", "fat", "hau", "ibo", "kik", "kin", "lug", "lgg",
    "luo", "pcm", "nyn", "swa", "twi", "wol", "yor",
)
