"""Tests for recording manifest persistence and compatibility scanning."""

from __future__ import annotations

from pathlib import Path

from recording.manager import scan_recording_artifacts
from recording.models import (
    RecordingArtifact,
    RecordingManifest,
    load_recording_manifest,
    save_recording_manifest,
)


def test_save_and_load_recording_manifest(tmp_path: Path):
    manifest = RecordingManifest(
        run_id="run-123",
        artifacts=[
            RecordingArtifact(
                filename="trace.zip",
                type="trace",
                size_bytes=42,
            )
        ],
    )

    save_recording_manifest(tmp_path, manifest)
    loaded = load_recording_manifest(tmp_path)

    assert loaded is not None
    assert loaded.run_id == "run-123"
    assert loaded.artifacts[0].filename == "trace.zip"


def test_scan_recording_artifacts_uses_manifest_when_present(tmp_path: Path):
    save_recording_manifest(
        tmp_path,
        RecordingManifest(
            run_id="manifest-run",
            artifacts=[
                RecordingArtifact(
                    filename="trace.zip",
                    type="trace",
                    size_bytes=99,
                )
            ],
        ),
    )

    (tmp_path / "trace.zip").write_bytes(b"fake-trace")

    artifacts = scan_recording_artifacts(tmp_path)
    assert artifacts == [
        {
            "filename": "trace.zip",
            "type": "trace",
            "size_bytes": 99,
        }
    ]
