"""
The Violet wire protocol: the API surface every miner must expose unmodified.

Design note
-----------
The endpoint paths and payload shapes below are not new. They are the contracts
the Avoices backend already speaks to its ASR and TTS servers:

* ``POST /transcribe``                    - ``ASRAPI/utils/utils.py``
* ``WS   /realtime/transcribe``           - ``ASRAPI/main.py`` realtime proxy
* ``POST /v1/audio/speech/stream``        - ``ASRAPI/utils/tts_synthesis.py``
* ``GET  /v1/voices``                     - ``ASRAPI/main.py`` TTS status probe
* ``POST /v1/audio/speech/clone/upload``  - ``ASRAPI/main.py`` voice cloning
* ``WS   /v1/audio/speech/stream/ws``     - ``ASRAPI/main.py`` TTS streaming proxy

Keeping them identical is deliberate: a Violet miner is a drop-in replacement
for the single-host servers Avoices uses today, so the smart router can be
enabled without touching any product logic, and rolled back just as cheaply.

On top of those, Violet adds three endpoints used only by validators and the
router: ``/health``, ``/capacity`` and ``/violet/info``.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .constants import SPEC_VERSION

# --------------------------------------------------------------------------
# Endpoint paths (the "standardized API" of TDD 4.2)
# --------------------------------------------------------------------------

#: Batch ASR. multipart/form-data: ``file``, ``language``, ``response_format``.
PATH_ASR_TRANSCRIBE = "/transcribe"
#: Streaming ASR. Binary PCM in (16-bit mono), JSON partial transcripts out.
PATH_ASR_STREAM_WS = "/realtime/transcribe"

#: Batch/streamed TTS. JSON ``{text, speaker_id, temperature}`` -> raw PCM body.
PATH_TTS_STREAM = "/v1/audio/speech/stream"
#: Voice catalogue.
PATH_TTS_VOICES = "/v1/voices"
#: Zero-shot cloning. multipart: ``text``, ``temperature``, ``reference_audio``.
PATH_TTS_CLONE = "/v1/audio/speech/clone/upload"
#: Streaming TTS over WebSocket. JSON control in, binary audio frames out.
PATH_TTS_STREAM_WS = "/v1/audio/speech/stream/ws"

#: Violet control plane.
PATH_HEALTH = "/health"
PATH_CAPACITY = "/capacity"
PATH_INFO = "/violet/info"

#: Response headers a miner sets on TTS audio so the caller can frame the PCM
#: without guessing. Mirrors what the current TTS server emits.
HEADER_SAMPLE_RATE = "x-audio-sample-rate"
HEADER_CHANNELS = "x-audio-channels"
HEADER_SAMPLE_WIDTH = "x-audio-sample-width"

#: Set by the miner on every response so the router can attribute work without
#: having to remember which URL it dialled.
HEADER_MINER_HOTKEY = "x-violet-hotkey"
HEADER_MINER_UID = "x-violet-uid"

#: Set by the router on outgoing requests to mark validator traffic. Miners must
#: serve these identically to production traffic (TDD 9.2: evaluation queries
#: are indistinguishable from real ones), so this exists only for logging.
HEADER_REQUEST_ID = "x-violet-request-id"

DEFAULT_SAMPLE_RATE = 24000
DEFAULT_CHANNELS = 1
DEFAULT_SAMPLE_WIDTH = 2


# --------------------------------------------------------------------------
# Structures
# --------------------------------------------------------------------------


@dataclass
class GpuInfo:
    """One physical GPU as reported by the miner."""

    index: int
    product_name: str
    vram_gb: float
    tier_key: str
    multiplier: float
    #: Instantaneous utilisation, 0-100, or ``None`` when unavailable.
    utilization_pct: Optional[float] = None
    memory_used_gb: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GpuInfo":
        return cls(
            index=int(data.get("index", 0)),
            product_name=str(data.get("product_name", "")),
            vram_gb=float(data.get("vram_gb", 0.0)),
            tier_key=str(data.get("tier_key", "")),
            multiplier=float(data.get("multiplier", 0.0)),
            utilization_pct=_opt_float(data.get("utilization_pct")),
            memory_used_gb=_opt_float(data.get("memory_used_gb")),
        )


@dataclass
class CapacityReport:
    """A miner's declared and observed capacity.

    ``capacity_units`` is the quantity the Capacity score is built from: the sum
    of per-GPU tier multipliers. It is a claim, and validators cross-check it
    against observed behaviour before it earns anything (TDD 9.2).
    """

    gpus: List[GpuInfo] = field(default_factory=list)
    system_memory_gb: float = 0.0
    cpu_count: int = 0
    max_concurrent_asr: int = 0
    max_concurrent_tts: int = 0
    active_asr: int = 0
    active_tts: int = 0
    uptime_s: float = 0.0

    @property
    def capacity_units(self) -> float:
        return round(sum(gpu.multiplier for gpu in self.gpus), 4)

    @property
    def load_factor(self) -> float:
        """Current occupancy in ``[0, 1]``; 1 means fully saturated."""
        capacity = self.max_concurrent_asr + self.max_concurrent_tts
        if capacity <= 0:
            return 0.0
        return min(1.0, (self.active_asr + self.active_tts) / capacity)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["capacity_units"] = self.capacity_units
        payload["load_factor"] = round(self.load_factor, 4)
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CapacityReport":
        return cls(
            gpus=[GpuInfo.from_dict(g) for g in data.get("gpus", []) or []],
            system_memory_gb=float(data.get("system_memory_gb", 0.0) or 0.0),
            cpu_count=int(data.get("cpu_count", 0) or 0),
            max_concurrent_asr=int(data.get("max_concurrent_asr", 0) or 0),
            max_concurrent_tts=int(data.get("max_concurrent_tts", 0) or 0),
            active_asr=int(data.get("active_asr", 0) or 0),
            active_tts=int(data.get("active_tts", 0) or 0),
            uptime_s=float(data.get("uptime_s", 0.0) or 0.0),
        )


@dataclass
class HealthReport:
    """Payload of ``GET /health`` (TDD 4.2: "a comprehensive /health endpoint")."""

    status: str  # "ok" | "degraded" | "unhealthy"
    hotkey: str
    spec_version: int = SPEC_VERSION
    services: List[str] = field(default_factory=list)
    #: Per-service upstream reachability, e.g. ``{"asr": True, "tts": False}``.
    upstreams: Dict[str, bool] = field(default_factory=dict)
    capacity: Optional[CapacityReport] = None
    asr_image: str = ""
    tts_image: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def healthy(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "hotkey": self.hotkey,
            "spec_version": self.spec_version,
            "services": list(self.services),
            "upstreams": dict(self.upstreams),
            "capacity": self.capacity.to_dict() if self.capacity else None,
            "asr_image": self.asr_image,
            "tts_image": self.tts_image,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HealthReport":
        capacity = data.get("capacity")
        return cls(
            status=str(data.get("status", "unhealthy")),
            hotkey=str(data.get("hotkey", "")),
            spec_version=int(data.get("spec_version", 0) or 0),
            services=list(data.get("services", []) or []),
            upstreams=dict(data.get("upstreams", {}) or {}),
            capacity=CapacityReport.from_dict(capacity) if capacity else None,
            asr_image=str(data.get("asr_image", "") or ""),
            tts_image=str(data.get("tts_image", "") or ""),
            timestamp=float(data.get("timestamp", 0.0) or 0.0),
        )


@dataclass
class TranscriptSegment:
    """One timestamped ASR segment.

    Both ``start``/``end`` and ``start_time``/``end_time`` appear in the wild;
    ``ASRAPI/utils/utils.py`` normalises either. Miners emit the canonical
    ``start``/``end`` pair.
    """

    text: str
    start: float
    end: float
    speaker: Optional[str] = None
    confidence: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "text": self.text,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
        }
        if self.speaker is not None:
            payload["speaker"] = self.speaker
        if self.confidence is not None:
            payload["confidence"] = round(self.confidence, 4)
        return payload


@dataclass
class MinerAnnouncement:
    """What a miner publishes on chain so validators and the router can find it.

    Kept compact: on-chain commitment space is limited and every byte is paid
    for. Field names are single characters in the serialised form (see
    ``violet.chain.commitment``), expanded here for readability.
    """

    endpoint: str
    services: List[str]
    #: ``tier_key -> count``, e.g. ``{"h100_80": 4}``.
    gpus: Dict[str, int]
    spec_version: int = SPEC_VERSION
    #: Unix seconds, so stale announcements can be aged out.
    announced_at: float = field(default_factory=time.time)
    #: Optional operator-declared image digests, checked against the official set.
    asr_image: str = ""
    tts_image: str = ""

    @property
    def capacity_units(self) -> float:
        from .constants import GPU_TIERS_BY_KEY

        total = 0.0
        for tier_key, count in self.gpus.items():
            tier = GPU_TIERS_BY_KEY.get(tier_key)
            if tier:
                total += tier.multiplier * max(0, int(count))
        return round(total, 4)

    def serves(self, service: str) -> bool:
        return service in self.services


def _opt_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def audio_headers(
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    channels: int = DEFAULT_CHANNELS,
    sample_width: int = DEFAULT_SAMPLE_WIDTH,
) -> Dict[str, str]:
    """Standard PCM framing headers for TTS responses."""
    return {
        HEADER_SAMPLE_RATE: str(sample_rate),
        HEADER_CHANNELS: str(channels),
        HEADER_SAMPLE_WIDTH: str(sample_width),
    }
