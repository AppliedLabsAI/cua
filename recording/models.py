"""Typed recording metadata and manifest persistence helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ArtifactType = Literal["trace", "screenshot"]
MANIFEST_FILENAME = "manifest.json"


@dataclass
class RecordingArtifact:
    """Metadata for a single recording output file."""

    filename: str
    type: ArtifactType
    size_bytes: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "filename": self.filename,
            "type": self.type,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RecordingArtifact:
        return cls(
            filename=data["filename"],
            type=data["type"],
            size_bytes=data["size_bytes"],
        )


@dataclass
class RecordingManifest:
    """Summary of all recording artifacts for a session."""

    run_id: str
    artifacts: list[RecordingArtifact] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }

    @classmethod
    def from_dict(cls, data: dict) -> RecordingManifest:
        return cls(
            run_id=data.get("run_id", ""),
            artifacts=[
                RecordingArtifact.from_dict(item) for item in data.get("artifacts", [])
            ],
        )


def manifest_path(root: Path) -> Path:
    return root / MANIFEST_FILENAME


def save_recording_manifest(root: Path, manifest: RecordingManifest) -> Path:
    """Persist recording metadata next to the artifacts."""
    root.mkdir(parents=True, exist_ok=True)
    path = manifest_path(root)
    path.write_text(json.dumps(manifest.to_dict(), indent=2))
    return path


def load_recording_manifest(root: Path) -> RecordingManifest | None:
    """Load persisted recording metadata if available."""
    path = manifest_path(root)
    if not path.exists():
        return None
    return RecordingManifest.from_dict(json.loads(path.read_text()))


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

    screenshots = root / "screenshots"
    if screenshots.exists():
        for path in sorted(screenshots.glob("*.jpg")):
            artifacts.append(
                RecordingArtifact(
                    filename=f"screenshots/{path.name}",
                    type="screenshot",
                    size_bytes=path.stat().st_size,
                )
            )
    return artifacts


def list_recording_artifacts(root: Path) -> list[dict[str, str | int]]:
    """Return manifest data if present, else fall back to scanning."""
    manifest = load_recording_manifest(root)
    artifacts = manifest.artifacts if manifest else scan_recording_artifacts(root)
    return [artifact.to_dict() for artifact in artifacts]
