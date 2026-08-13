"""
Published official image manifest for serving-integrity checks (TDD 9.2).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("violet.releases")

BUILTIN_MANIFEST = Path(__file__).parent / "manifest.json"

#: Mutable / floating tags that must never pass strict digest enforcement.
MUTABLE_TAGS = frozenset(
    {
        "latest",
        "latest-cuda",
        "main",
        "master",
        "dev",
        "nightly",
        "stable",
    }
)

_DIGEST_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$", re.IGNORECASE)
_PINNED_RE = re.compile(
    r"^.+@sha256:[0-9a-f]{64}$",
    re.IGNORECASE,
)


@dataclass
class ImagePolicy:
    repository: str = ""
    allowed_tags: List[str] = field(default_factory=list)
    allowed_digests: List[str] = field(default_factory=list)


@dataclass
class ReleaseManifest:
    version: str = "0.0.0"
    images: Dict[str, ImagePolicy] = field(default_factory=dict)
    #: When true: blank / latest / unpinned / unknown digests fail closed.
    strict_digests: bool = True

    def check_declared(self, service: str, declared: str) -> tuple[bool, str]:
        """Verify a miner-declared image reference against policy."""
        declared = (declared or "").strip()
        if not declared:
            if self.strict_digests:
                return False, f"{service} image required (blank declaration rejected)"
            return True, "not declared"

        policy = self.images.get(service)
        if policy is None:
            if self.strict_digests:
                return False, f"no policy for {service}"
            return True, f"no policy for {service}"

        digest_part = _extract_digest(declared)
        tag_part = _extract_tag(declared)

        if self.strict_digests:
            if tag_part and tag_part.lower() in MUTABLE_TAGS:
                return False, f"mutable tag {tag_part!r} rejected for {service}"
            if not digest_part and not _PINNED_RE.match(declared):
                # Bare repo:tag without digest is never enough in strict mode.
                if tag_part or ":" in declared:
                    return False, (
                        f"unpinned image {declared!r} rejected for {service}; "
                        "declare repository@sha256:…"
                    )
                return False, f"digest required for {service} (got {declared!r})"
            if not policy.allowed_digests:
                return False, (
                    f"no allowed_digests configured for {service}; "
                    "refusing bootstrap tags under strict enforcement"
                )
            normalized = digest_part if digest_part.startswith("sha256:") else f"sha256:{digest_part}"
            allowed = {_normalize_digest(d) for d in policy.allowed_digests}
            if normalized not in allowed and declared not in policy.allowed_digests:
                return False, f"digest {normalized!r} not in official set for {service}"
            return True, "digest allowed"

        # Bootstrap / non-strict path (local CI without pinned digests).
        if policy.allowed_digests:
            if not digest_part:
                return False, f"digest required for {service} when allow-list is set"
            normalized = (
                digest_part if digest_part.startswith("sha256:") else f"sha256:{digest_part}"
            )
            allowed = {_normalize_digest(d) for d in policy.allowed_digests}
            if normalized not in allowed and declared not in policy.allowed_digests:
                return False, f"digest {declared!r} not in official set for {service}"
            return True, "digest allowed"

        if "@" in declared:
            return True, "pinned digest (no allow-list configured)"

        repo_part = declared.split(":")[0] if ":" in declared else declared
        if policy.repository and repo_part != policy.repository and not repo_part.endswith(
            policy.repository.split("/")[-1]
        ):
            if policy.repository not in declared:
                return False, f"repository {repo_part!r} does not match {policy.repository!r}"

        if policy.allowed_tags and ":" in declared:
            tag = declared.rsplit(":", 1)[-1]
            if tag not in policy.allowed_tags:
                return False, f"tag {tag!r} not allowed for {service}"

        return True, "tag allowed"


def _normalize_digest(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("sha256:"):
        return value.lower()
    if _DIGEST_RE.match(value):
        return f"sha256:{value.lower()}"
    return value


def _extract_digest(declared: str) -> str:
    if "@sha256:" in declared:
        return "sha256:" + declared.rsplit("@sha256:", 1)[-1].split()[0].lower()
    if declared.startswith("sha256:"):
        return declared.lower()
    if _DIGEST_RE.match(declared):
        return f"sha256:{declared.lower()}"
    return ""


def _extract_tag(declared: str) -> str:
    if "@" in declared:
        return ""
    if declared.count(":") >= 1 and not declared.startswith("sha256:"):
        return declared.rsplit(":", 1)[-1]
    return ""


def require_image_digests_from_env() -> bool:
    """Strict digest enforcement (default on). Set VIOLET_REQUIRE_IMAGE_DIGESTS=0 to relax."""
    raw = os.getenv("VIOLET_REQUIRE_IMAGE_DIGESTS")
    if raw is None:
        return True
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_release_manifest(path: Optional[str] = None) -> ReleaseManifest:
    manifest_path = Path(path) if path else BUILTIN_MANIFEST
    if not manifest_path.is_file():
        logger.warning("release manifest missing at %s", manifest_path)
        return ReleaseManifest(strict_digests=require_image_digests_from_env())

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    images: Dict[str, ImagePolicy] = {}
    for key, raw in (data.get("images") or {}).items():
        images[str(key)] = ImagePolicy(
            repository=str(raw.get("repository", "") or ""),
            allowed_tags=[str(t) for t in raw.get("allowed_tags", []) or []],
            allowed_digests=[str(d) for d in raw.get("allowed_digests", []) or []],
        )
    strict = data.get("strict_digests")
    if strict is None:
        strict = require_image_digests_from_env()
    return ReleaseManifest(
        version=str(data.get("version", "0.0.0")),
        images=images,
        strict_digests=bool(strict),
    )
