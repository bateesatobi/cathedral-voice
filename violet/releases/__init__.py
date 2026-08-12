"""
Published official image manifest for serving-integrity checks (TDD 9.2).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("violet.releases")

BUILTIN_MANIFEST = Path(__file__).parent / "manifest.json"


@dataclass
class ImagePolicy:
    repository: str = ""
    allowed_tags: List[str] = field(default_factory=list)
    allowed_digests: List[str] = field(default_factory=list)


@dataclass
class ReleaseManifest:
    version: str = "0.0.0"
    images: Dict[str, ImagePolicy] = field(default_factory=dict)

    def check_declared(self, service: str, declared: str) -> tuple[bool, str]:
        """Verify a miner-declared image reference against policy."""
        declared = (declared or "").strip()
        if not declared:
            return True, "not declared"

        policy = self.images.get(service)
        if policy is None:
            return True, f"no policy for {service}"

        if policy.allowed_digests:
            normalized = declared if declared.startswith("sha256:") else f"sha256:{declared}"
            if normalized not in policy.allowed_digests and declared not in policy.allowed_digests:
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


def load_release_manifest(path: Optional[str] = None) -> ReleaseManifest:
    manifest_path = Path(path) if path else BUILTIN_MANIFEST
    if not manifest_path.is_file():
        logger.warning("release manifest missing at %s", manifest_path)
        return ReleaseManifest()

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    images: Dict[str, ImagePolicy] = {}
    for key, raw in (data.get("images") or {}).items():
        images[str(key)] = ImagePolicy(
            repository=str(raw.get("repository", "") or ""),
            allowed_tags=[str(t) for t in raw.get("allowed_tags", []) or []],
            allowed_digests=[str(d) for d in raw.get("allowed_digests", []) or []],
        )
    return ReleaseManifest(version=str(data.get("version", "0.0.0")), images=images)
