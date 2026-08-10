"""Validator: evaluation, scoring and weight setting for the Violet subnet."""

from .antigaming import apply_endpoint_collision_penalty, apply_multi_uid_policy
from .discovery import Discovery, MinerRecord, discover
from .evaluator import Evaluator, MinerEvaluation
from .metrics import asr_quality, tts_quality, word_error_rate
from .probes import MinerProbe, ProbeResult
from .qualification import QualificationResult, run_qualification
from .scoring import ComponentScores, MinerScore, compute_components, score_miners
from .store import Observation, ValidatorStore, WindowStats

__all__ = [
    "ComponentScores",
    "Discovery",
    "Evaluator",
    "MinerEvaluation",
    "MinerProbe",
    "MinerRecord",
    "MinerScore",
    "Observation",
    "ProbeResult",
    "QualificationResult",
    "ValidatorStore",
    "WindowStats",
    "apply_endpoint_collision_penalty",
    "apply_multi_uid_policy",
    "asr_quality",
    "compute_components",
    "discover",
    "run_qualification",
    "score_miners",
    "tts_quality",
    "word_error_rate",
]
