"""Recording configuration model."""

from __future__ import annotations

from pydantic import BaseModel

DEFAULT_OUTPUT_DIR = "/tmp/cua-recording"


class RecordingConfig(BaseModel):
    """Runtime configuration for session recording."""

    enabled: bool = True
    trace: bool = True
    upload: bool = True
    output_dir: str = DEFAULT_OUTPUT_DIR
