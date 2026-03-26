"""Recording configuration dataclass."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_OUTPUT_DIR = "/tmp/cua-recording"


@dataclass
class RecordingConfig:
    """Runtime configuration for session recording."""

    enabled: bool = True
    screenshots: bool = True
    trace: bool = True
    upload: bool = True
    output_dir: str = DEFAULT_OUTPUT_DIR

    @classmethod
    def from_dict(cls, data: dict) -> RecordingConfig:
        return cls(
            enabled=data.get("enabled", True),
            screenshots=data.get("screenshots", True),
            trace=data.get("trace", True),
            upload=data.get("upload", True),
            output_dir=data.get("output_dir", DEFAULT_OUTPUT_DIR),
        )
