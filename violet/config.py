"""
Environment-driven configuration for every Violet component.

Each component reads only the section it needs, but they share this module so
that a single ``.env`` file can drive a miner, a validator and the router.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from .constants import (
    DEFAULT_PHASE,
    NETUID_MAINNET,
    NETUID_TESTNET,
    PHASES,
    SCORE_WINDOW_DAYS,
    SERVICES,
    WEIGHT_SET_INTERVAL_BLOCKS,
    PhaseWeights,
)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_list(name: str, default: List[str]) -> List[str]:
    raw = _env(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# --------------------------------------------------------------------------
# Chain
# --------------------------------------------------------------------------


def _default_netuid_for_network(network: str) -> int:
    """Resolve Violet's registered netuid from ``BT_NETWORK``."""
    name = (network or "").strip().lower()
    if name in {"test", "testnet"}:
        return NETUID_TESTNET
    # finney / main / mainnet / empty → mainnet SN39
    return NETUID_MAINNET


def _resolve_netuid() -> int:
    """``VIOLET_NETUID`` wins when set; otherwise mainnet 39 / testnet 292."""
    explicit = _env("VIOLET_NETUID")
    if explicit:
        return int(explicit)
    return _default_netuid_for_network(_env("BT_NETWORK", "finney"))


@dataclass
class ChainConfig:
    """Connection details for the Bittensor network.

    Violet is registered as **netuid 39** on mainnet (``finney``) and
    **netuid 292** on testnet. Set ``BT_NETWORK`` accordingly; override with
    ``VIOLET_NETUID`` only if you intentionally need a different subnet.
    """

    netuid: int = field(default_factory=_resolve_netuid)
    network: str = field(default_factory=lambda: _env("BT_NETWORK", "finney"))
    wallet_name: str = field(default_factory=lambda: _env("BT_WALLET_NAME", "default"))
    wallet_hotkey: str = field(default_factory=lambda: _env("BT_WALLET_HOTKEY", "default"))
    wallet_path: Optional[str] = field(default_factory=lambda: _env("BT_WALLET_PATH") or None)
    #: Set false on a read-only deployment (e.g. the router, or a dashboard).
    signing_enabled: bool = field(default_factory=lambda: _env_bool("BT_SIGNING_ENABLED", True))

    def validate(self) -> None:
        if self.netuid <= 0:
            raise ValueError(
                "VIOLET_NETUID must be set to the registered Violet subnet netuid "
                f"(mainnet={NETUID_MAINNET}, testnet={NETUID_TESTNET})"
            )
        name = (self.network or "").strip().lower()
        if name in {"finney", "main", "mainnet"} and self.netuid != NETUID_MAINNET:
            raise ValueError(
                f"BT_NETWORK={self.network!r} requires VIOLET_NETUID={NETUID_MAINNET} "
                f"(got {self.netuid})"
            )
        if name in {"test", "testnet"} and self.netuid != NETUID_TESTNET:
            raise ValueError(
                f"BT_NETWORK={self.network!r} requires VIOLET_NETUID={NETUID_TESTNET} "
                f"(got {self.netuid})"
            )


# --------------------------------------------------------------------------
# Miner
# --------------------------------------------------------------------------


@dataclass
class MinerConfig:
    """Configuration for the miner sidecar.

    The sidecar does not perform inference itself: it fronts the official ASR
    and TTS containers running on the same host and presents the standardised
    Violet API surface to validators and to the smart router.
    """

    host: str = field(default_factory=lambda: _env("MINER_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("MINER_PORT", 8091))

    #: Publicly reachable base URL announced on chain (TDD 4.3 step 3).
    public_endpoint: str = field(default_factory=lambda: _env("MINER_PUBLIC_ENDPOINT"))

    #: Which services this miner offers. A host may run ASR only, TTS only, or both.
    services: List[str] = field(
        default_factory=lambda: _env_list("MINER_SERVICES", list(SERVICES))
    )

    #: Upstream official containers, reachable on the miner's private network.
    asr_upstream: str = field(
        default_factory=lambda: _env("MINER_ASR_UPSTREAM", "http://violet-asr:9000")
    )
    tts_upstream: str = field(
        default_factory=lambda: _env("MINER_TTS_UPSTREAM", "http://violet-tts:8080")
    )
    #: WebSocket bases. Derived from the HTTP upstreams when left empty.
    asr_ws_upstream: str = field(default_factory=lambda: _env("MINER_ASR_WS_UPSTREAM"))
    tts_ws_upstream: str = field(default_factory=lambda: _env("MINER_TTS_WS_UPSTREAM"))
    #: How realtime ASR is served when upstream has no violet-shaped WS:
    #:   bridge      — proxy WS binary frames to upstream /realtime/transcribe
    #:   batch_proxy — buffer PCM, periodically POST /transcribe, emit partials
    #:   auto        — batch_proxy (etoil/speaches are batch-first)
    asr_stream_mode: str = field(
        default_factory=lambda: _env("MINER_ASR_STREAM_MODE", "batch_proxy")
    )

    #: Image references the operator declares. Validators compare these against
    #: the published official digests (TDD 9.2 "official image requirement").
    asr_image: str = field(default_factory=lambda: _env("MINER_ASR_IMAGE"))
    tts_image: str = field(default_factory=lambda: _env("MINER_TTS_IMAGE"))

    #: Concurrency admission control. Requests beyond these caps are rejected
    #: with 503 so the router can shed load rather than queueing behind a
    #: saturated GPU (TDD 8: "rejects overloaded endpoints").
    max_concurrent_asr: int = field(default_factory=lambda: _env_int("MINER_MAX_CONCURRENT_ASR", 0))
    max_concurrent_tts: int = field(default_factory=lambda: _env_int("MINER_MAX_CONCURRENT_TTS", 0))

    upstream_timeout_s: float = field(
        default_factory=lambda: _env_float("MINER_UPSTREAM_TIMEOUT_S", 600.0)
    )
    health_timeout_s: float = field(
        default_factory=lambda: _env_float("MINER_HEALTH_TIMEOUT_S", 4.0)
    )

    #: How often the sidecar refreshes its GPU inventory, seconds.
    gpu_poll_interval_s: float = field(
        default_factory=lambda: _env_float("MINER_GPU_POLL_INTERVAL_S", 30.0)
    )
    #: How often the announcement is republished on chain, seconds. Commitments
    #: cost a transaction, so this is deliberately infrequent; the endpoint only
    #: needs re-announcing when it changes.
    announce_interval_s: float = field(
        default_factory=lambda: _env_float("MINER_ANNOUNCE_INTERVAL_S", 3600.0)
    )
    #: Announce over the axon extrinsic in addition to the commitment.
    serve_axon: bool = field(default_factory=lambda: _env_bool("MINER_SERVE_AXON", True))

    #: Optional shared secret. When set, the miner requires this bearer token on
    #: inference traffic (HTTP and WebSocket). Health/capacity/info stay open so
    #: validators can evaluate without holding the product token.
    access_token: str = field(default_factory=lambda: _env("MINER_ACCESS_TOKEN"))

    max_upload_bytes: int = field(
        default_factory=lambda: _env_int("MINER_MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
    )
    max_clone_reference_bytes: int = field(
        default_factory=lambda: _env_int("MINER_MAX_CLONE_REFERENCE_BYTES", 10 * 1024 * 1024)
    )
    max_tts_text_chars: int = field(
        default_factory=lambda: _env_int("MINER_MAX_TTS_TEXT_CHARS", 5000)
    )
    ws_max_message_bytes: int = field(
        default_factory=lambda: _env_int("MINER_WS_MAX_MESSAGE_BYTES", 4 * 1024 * 1024)
    )
    ws_idle_timeout_s: float = field(
        default_factory=lambda: _env_float("MINER_WS_IDLE_TIMEOUT_S", 300.0)
    )

    def validate(self) -> None:
        unknown = set(self.services) - set(SERVICES)
        if unknown:
            raise ValueError(f"MINER_SERVICES contains unknown services: {sorted(unknown)}")
        if not self.services:
            raise ValueError("MINER_SERVICES must list at least one of: asr, tts")
        if not self.public_endpoint:
            raise ValueError(
                "MINER_PUBLIC_ENDPOINT must be a stable, publicly reachable base URL"
            )

    def ws_base(self, service: str) -> str:
        explicit = self.asr_ws_upstream if service == "asr" else self.tts_ws_upstream
        if explicit:
            return explicit.rstrip("/")
        http_base = self.asr_upstream if service == "asr" else self.tts_upstream
        base = http_base.rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):]
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):]
        return base


# --------------------------------------------------------------------------
# Validator
# --------------------------------------------------------------------------


@dataclass
class ValidatorConfig:
    """Configuration for the validator neuron."""

    #: Incentive phase. Controls the C/W/Q split (TDD 7).
    phase: str = field(default_factory=lambda: _env("VIOLET_PHASE", DEFAULT_PHASE))

    #: Explicit weight overrides; when any is set, all three should be.
    weight_capacity: Optional[float] = field(
        default_factory=lambda: _env_float("VIOLET_W_CAPACITY", -1.0) or None
    )
    weight_work: Optional[float] = field(
        default_factory=lambda: _env_float("VIOLET_W_WORK", -1.0) or None
    )
    weight_quality: Optional[float] = field(
        default_factory=lambda: _env_float("VIOLET_W_QUALITY", -1.0) or None
    )

    window_days: int = field(
        default_factory=lambda: _env_int("VIOLET_SCORE_WINDOW_DAYS", SCORE_WINDOW_DAYS)
    )
    weight_interval_blocks: int = field(
        default_factory=lambda: _env_int(
            "VIOLET_WEIGHT_INTERVAL_BLOCKS", WEIGHT_SET_INTERVAL_BLOCKS
        )
    )

    #: Seconds between evaluation sweeps of the whole miner set.
    eval_interval_s: float = field(
        default_factory=lambda: _env_float("VALIDATOR_EVAL_INTERVAL_S", 300.0)
    )
    #: Seconds between lightweight health probes.
    health_interval_s: float = field(
        default_factory=lambda: _env_float("VALIDATOR_HEALTH_INTERVAL_S", 60.0)
    )
    #: How many miners are probed concurrently.
    concurrency: int = field(default_factory=lambda: _env_int("VALIDATOR_CONCURRENCY", 8))

    db_path: str = field(
        default_factory=lambda: _env("VALIDATOR_DB_PATH", "./data/validator.sqlite3")
    )
    evalset_path: str = field(default_factory=lambda: _env("VALIDATOR_EVALSET_PATH", ""))

    #: Avoices work-report endpoint, providing signed usage counters per hotkey
    #: (TDD 7 Work score). Empty disables the Work component's organic input.
    work_report_url: str = field(default_factory=lambda: _env("VIOLET_WORK_REPORT_URL"))
    #: Bearer token for fetching work reports from the router (distinct from HMAC).
    work_report_token: str = field(default_factory=lambda: _env("VIOLET_WORK_REPORT_TOKEN"))
    #: HMAC secret for verifying report signatures. Falls back to token only when unset.
    work_report_hmac_secret: str = field(
        default_factory=lambda: _env("VIOLET_WORK_REPORT_HMAC_SECRET")
        or _env("VIOLET_WORK_REPORT_TOKEN")
    )
    #: Ed25519/sr25519 public key (SS58) expected to have signed work reports.
    work_report_signer: str = field(default_factory=lambda: _env("VIOLET_WORK_REPORT_SIGNER"))
    release_manifest_path: str = field(
        default_factory=lambda: _env("VIOLET_RELEASE_MANIFEST_PATH")
    )
    require_endpoint_identity: bool = field(
        default_factory=lambda: _env_bool("VALIDATOR_REQUIRE_IDENTITY", True)
    )

    #: Bind address for the public scoring dashboard (TDD 9.3 mitigation roadmap).
    dashboard_host: str = field(default_factory=lambda: _env("VALIDATOR_DASHBOARD_HOST", "0.0.0.0"))
    dashboard_port: int = field(default_factory=lambda: _env_int("VALIDATOR_DASHBOARD_PORT", 8092))
    dashboard_enabled: bool = field(
        default_factory=lambda: _env_bool("VALIDATOR_DASHBOARD_ENABLED", True)
    )

    #: Dry run: evaluate and score, but never submit weights on chain.
    dry_run: bool = field(default_factory=lambda: _env_bool("VALIDATOR_DRY_RUN", False))

    #: Post scored rounds to Cathedral publisher (SN39 violet_audio blend).
    #: Cathedral validators do not ingest scores directly — only the publisher.
    cathedral_scores_enabled: bool = field(
        default_factory=lambda: _env_bool("CATHEDRAL_EXTERNAL_SCORES_ENABLED", False)
        or _env_bool("CATHEDRAL_VOICE_SCORES_ENABLED", False)
    )
    cathedral_publisher_url: str = field(
        default_factory=lambda: _env(
            "CATHEDRAL_PUBLISHER_URL", "https://api.cathedral.computer"
        )
    )
    cathedral_scores_token: str = field(
        default_factory=lambda: _env("CATHEDRAL_EXTERNAL_SCORES_TOKEN")
    )
    cathedral_scores_hmac: str = field(
        default_factory=lambda: _env("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET")
    )
    cathedral_scores_netuid: int = field(
        default_factory=lambda: _env_int("CATHEDRAL_EXTERNAL_SCORES_NETUID", 39)
    )
    cathedral_scores_dry_run: bool = field(
        default_factory=lambda: _env_bool("CATHEDRAL_EXTERNAL_SCORES_DRY_RUN", False)
    )
    #: When true, still score locally but skip Violet-subnet set_weights (Cathedral-only).
    cathedral_skip_local_weights: bool = field(
        default_factory=lambda: _env_bool("CATHEDRAL_SKIP_LOCAL_WEIGHTS", False)
    )

    #: Colocate Cathedral thin SN39 duties (fetch signed feed → verify → set_weights).
    cathedral_thin_enabled: bool = field(
        default_factory=lambda: _env_bool("CATHEDRAL_THIN_ENABLED", False)
    )
    cathedral_thin_broadcast: bool = field(
        default_factory=lambda: _env_bool("CATHEDRAL_THIN_BROADCAST", False)
    )
    cathedral_thin_interval_s: float = field(
        default_factory=lambda: _env_float("CATHEDRAL_THIN_INTERVAL_S", 1500.0)
    )
    cathedral_thin_dry_run: bool = field(
        default_factory=lambda: _env_bool("CATHEDRAL_THIN_DRY_RUN", True)
    )

    def resolved_weights(self) -> PhaseWeights:
        """Return the active C/W/Q weights, normalised to sum to 1."""
        overrides = (self.weight_capacity, self.weight_work, self.weight_quality)
        if all(w is not None and w >= 0 for w in overrides):
            total = sum(overrides)  # type: ignore[arg-type]
            if total <= 0:
                raise ValueError("VIOLET_W_* overrides must sum to a positive value")
            return PhaseWeights(
                "custom",
                self.weight_capacity / total,  # type: ignore[operator]
                self.weight_work / total,  # type: ignore[operator]
                self.weight_quality / total,  # type: ignore[operator]
            )
        if self.phase not in PHASES:
            raise ValueError(
                f"VIOLET_PHASE={self.phase!r} is not one of {sorted(PHASES)}"
            )
        return PHASES[self.phase]


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------


@dataclass
class RouterConfig:
    """Configuration for the smart router embedded in the Avoices backend."""

    enabled: bool = field(default_factory=lambda: _env_bool("VIOLET_ROUTER_ENABLED", False))

    #: Fallback endpoints used when no healthy miner is available. These are the
    #: legacy single-host ASR/TTS servers, so enabling the router can never make
    #: Avoices less available than it is today.
    fallback_asr_url: str = field(default_factory=lambda: _env("VIOLET_FALLBACK_ASR_URL"))
    fallback_tts_url: str = field(default_factory=lambda: _env("VIOLET_FALLBACK_TTS_URL"))

    #: Seconds between metagraph refreshes for endpoint discovery.
    discovery_interval_s: float = field(
        default_factory=lambda: _env_float("VIOLET_DISCOVERY_INTERVAL_S", 300.0)
    )
    #: Seconds between health probes of known miners.
    health_interval_s: float = field(
        default_factory=lambda: _env_float("VIOLET_ROUTER_HEALTH_INTERVAL_S", 30.0)
    )
    health_timeout_s: float = field(
        default_factory=lambda: _env_float("VIOLET_ROUTER_HEALTH_TIMEOUT_S", 3.0)
    )
    #: Consecutive health failures before a miner is taken out of rotation.
    unhealthy_threshold: int = field(
        default_factory=lambda: _env_int("VIOLET_ROUTER_UNHEALTHY_THRESHOLD", 3)
    )
    #: Attempts across distinct miners before giving up (or using legacy fallback
    #: if ``VIOLET_FALLBACK_*`` is set). Prefer trying every healthy miner rather
    #: than a hardcoded host when product traffic must stay on the subnet.
    max_attempts: int = field(default_factory=lambda: _env_int("VIOLET_ROUTER_MAX_ATTEMPTS", 10))

    #: Comma-separated miner base URLs for local / offline testing without a
    #: chain (e.g. ``http://127.0.0.1:8091``). Seeded into the registry on start.
    static_miners: str = field(default_factory=lambda: _env("VIOLET_STATIC_MINERS"))

    #: Weighting of the selector's scoring terms.
    weight_latency: float = field(default_factory=lambda: _env_float("VIOLET_SELECT_W_LATENCY", 0.4))
    weight_load: float = field(default_factory=lambda: _env_float("VIOLET_SELECT_W_LOAD", 0.4))
    weight_quality: float = field(default_factory=lambda: _env_float("VIOLET_SELECT_W_QUALITY", 0.2))

    #: Minimum on-chain incentive required before a miner receives production
    #: traffic. Zero admits every qualified miner.
    min_incentive: float = field(default_factory=lambda: _env_float("VIOLET_ROUTER_MIN_INCENTIVE", 0.0))

    #: Where completed-work receipts are accumulated for validator pull.
    receipts_db_path: str = field(
        default_factory=lambda: _env("VIOLET_RECEIPTS_DB_PATH", "./data/violet_receipts.sqlite3")
    )
    #: Seed for signing work reports served to validators.
    work_report_signing_key: str = field(
        default_factory=lambda: _env("VIOLET_WORK_REPORT_SIGNING_KEY")
    )
    access_token: str = field(default_factory=lambda: _env("VIOLET_MINER_ACCESS_TOKEN"))


@dataclass
class VioletConfig:
    """Aggregate of every section, for components that need more than one."""

    chain: ChainConfig = field(default_factory=ChainConfig)
    miner: MinerConfig = field(default_factory=MinerConfig)
    validator: ValidatorConfig = field(default_factory=ValidatorConfig)
    router: RouterConfig = field(default_factory=RouterConfig)


def load_config() -> VioletConfig:
    """Load configuration from the environment, applying ``.env`` if present."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # python-dotenv is optional for library consumers
        pass
    return VioletConfig()
