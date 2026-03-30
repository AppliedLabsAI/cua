"""Modal app/bootstrap wiring for the outer API service."""

from __future__ import annotations

from pathlib import Path

import modal
from modal import FilePatternMatcher

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VOLUME_MOUNT = "/recordings"

_exclude_dirs = FilePatternMatcher(
    "output/**", "tests/**", "llm/**", ".git/**", "playbooks/definitions/**"
)
# Exclude files that do not match the source extensions we want in the image.
_exclude_non_source = ~FilePatternMatcher(
    "**/*.py",
    "**/*.js",
    "**/*.json",
    "**/*.yaml",
    "**/*.yml",
    "**/*.toml",
    "**/*.lock",
    "**/*.sh",
)


def _ignore(path: Path) -> bool:
    return _exclude_dirs(path) or _exclude_non_source(path)


modal_app = modal.App(
    name="cua",
    image=modal.Image.debian_slim(python_version="3.13")
    .add_local_dir(
        str(PROJECT_ROOT),
        remote_path="/opt/cua",
        copy=True,
        ignore=_ignore,
    )
    .env({"PYTHONPATH": "/opt/cua"})
    .uv_sync(str(PROJECT_ROOT), extra_options="--no-dev"),
    secrets=[modal.Secret.from_name("llm-secret")],
)

recording_volume = modal.Volume.from_name(
    "cua-recordings", create_if_missing=True, version=2
)
