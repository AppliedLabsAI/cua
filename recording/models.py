"""Typed recording metadata and manifest persistence helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ArtifactType = Literal["trace"]
MANIFEST_FILENAME = "manifest.json"


class RecordingArtifact(BaseModel):
    """Metadata for a single recording output file."""

    filename: str
    type: ArtifactType
    size_bytes: int


class RecordingManifest(BaseModel):
    """Summary of all recording artifacts for a session."""

    run_id: str
    artifacts: list[RecordingArtifact] = Field(default_factory=list)


def manifest_path(root: Path) -> Path:
    return root / MANIFEST_FILENAME


def save_recording_manifest(root: Path, manifest: RecordingManifest) -> Path:
    """Persist recording metadata next to the artifacts."""
    root.mkdir(parents=True, exist_ok=True)
    path = manifest_path(root)
    path.write_text(json.dumps(manifest.model_dump(), indent=2))
    return path


def load_recording_manifest(root: Path) -> RecordingManifest | None:
    """Load persisted recording metadata if available."""
    path = manifest_path(root)
    if not path.exists():
        return None
    return RecordingManifest.model_validate(json.loads(path.read_text()))


def scan_recording_artifacts(root: Path) -> list[RecordingArtifact]:
    """Best-effort directory scan for backward compatibility."""
    trace = root / "trace.zip"
    artifacts: list[RecordingArtifact] = []
    if trace.exists():
        artifacts.append(
            RecordingArtifact(
                filename="trace.zip",
                type="trace",
                size_bytes=trace.stat().st_size,
            )
        )

    return artifacts


def list_recording_artifacts(root: Path) -> list[dict[str, str | int]]:
    """Return manifest data if present, else fall back to scanning."""
    manifest = load_recording_manifest(root)
    artifacts = manifest.artifacts if manifest else scan_recording_artifacts(root)
    return [artifact.model_dump() for artifact in artifacts]
