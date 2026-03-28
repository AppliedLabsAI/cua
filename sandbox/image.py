"""Modal sandbox image definition and sandbox factory.

Builds a custom Ubuntu 24.04 image with a full desktop environment,
Patchright browser, and the CUA agent runtime. Built once, cached by Modal.
"""

from __future__ import annotations

import json
from pathlib import Path

import modal
from modal import FilePatternMatcher

from api.models import RunConfig

_project_root = Path(__file__).resolve().parent.parent

# Only include source files — invert matcher so everything else is ignored
_exclude_dirs = FilePatternMatcher(
    "output/**", "tests/**", "llm/**", ".git/**", "playbooks/definitions/**"
)
_include_exts = ~FilePatternMatcher(
    "**/*.py",
    "**/*.js",
    "**/*.json",
    "**/*.yaml",
    "**/*.yml",
    "**/*.toml",
    "**/*.lock",
    "**/*.sh",
)

recording_volume = modal.Volume.from_name(
    "cua-recordings", create_if_missing=True, version=2
)

sandbox_image = (
    modal.Image.from_registry("ubuntu:24.04")
    .env({"DEBIAN_FRONTEND": "noninteractive"})
    .run_commands(
        # chromium-browser and firefox on Ubuntu 24.04 require snapd,
        # which fails in Modal containers (can't set capabilities).
        # We skip system browsers and use Patchright's Chromium instead.
        "apt-get update"
        " && apt-get install -y --no-install-recommends "
        "xvfb openbox tint2 "
        "libreoffice xterm thunar "
        "imagemagick "
        "curl wget jq git nodejs npm "
        "fonts-liberation fonts-noto-cjk "
        # Chromium runtime deps (needed by Patchright's bundled Chromium)
        "libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libcups2t64 "
        "libdbus-1-3 libdrm2 libgbm1 libgtk-3-0t64 libnspr4 libnss3 "
        "libpango-1.0-0 libxcomposite1 libxdamage1 libxfixes3 libxkbcommon0 "
        "libxrandr2 "
        "&& rm -rf /var/lib/apt/lists/*"
    )
    .env(
        {
            "DISPLAY": ":99",
            "DISPLAY_NUM": "99",
            "WIDTH": "1280",
            "HEIGHT": "720",
            "PYTHONPATH": "/opt/cua",
        }
    )
    .uv_sync(str(_project_root))
    .run_commands("patchright install chromium")
    # Mount entire project — one call instead of per-directory mounts.
    # Lazy mount (copy=False) so deploys stay fast on code changes.
    .add_local_dir(
        str(_project_root),
        remote_path="/opt/cua",
        ignore=lambda path: _exclude_dirs(path) or _include_exts(path),
    )
)

# Port exposed by the sandbox
PORT_STATUS = 8090


async def create_cua_sandbox(
    config: RunConfig,
    app: modal.App,
    extra_env: dict[str, str] | None = None,
) -> modal.Sandbox:
    """Create a Modal sandbox configured for a CUA run.

    Returns the sandbox immediately — startup is asynchronous.
    Use sandbox.tunnels() to get the status API URL.

    ``extra_env`` is merged into the sandbox environment — used to propagate
    OTel trace context (TRACEPARENT, TRACESTATE) and OTel config vars.
    """
    secrets: list[modal.Secret] = [modal.Secret.from_name("llm-secret")]

    env: dict[str, str | None] = {
        "DIRECTIVE": config.directive,
        "MODEL": config.model,
        "MAX_STEPS": str(config.max_steps),
        "THINKING": config.thinking,
        "WIDTH": str(config.display_width),
        "HEIGHT": str(config.display_height),
        "PROFILE": config.profile,
        "START_URL": config.start_url or "",
        "PROXY_URL": config.proxy or "",
    }

    if config.credentials:
        env["CREDENTIALS_JSON"] = json.dumps(config.credentials)

    if config.guardrails:
        env["GUARDRAILS_JSON"] = json.dumps(
            config.guardrails.model_dump(exclude_none=True)
        )

    if config.recording:
        env["RECORDING_JSON"] = json.dumps(config.recording.model_dump())

    if config.output_schema is not None:
        env["OUTPUT_SCHEMA_JSON"] = json.dumps(config.output_schema)

    # Propagate OTel trace context and config into sandbox
    if extra_env:
        env.update(extra_env)

    from settings import get_settings

    settings = get_settings()
    if not settings.otel_sdk_disabled:
        env.setdefault("OTEL_SDK_DISABLED", "false")
        env.setdefault(
            "OTEL_EXPORTER_OTLP_ENDPOINT", settings.otel_exporter_otlp_endpoint
        )
        env.setdefault(
            "OTEL_EXPORTER_OTLP_INSECURE",
            str(settings.otel_exporter_otlp_insecure).lower(),
        )
        env.setdefault("OTEL_RESOURCE_ENV", settings.otel_resource_env)
        env.setdefault("OTEL_TRACES_SAMPLER_ARG", str(settings.otel_traces_sampler_arg))

    return await modal.Sandbox.create.aio(
        "/opt/cua/sandbox/entrypoint.sh",
        app=app,
        image=sandbox_image,
        secrets=secrets,
        encrypted_ports=[PORT_STATUS],
        timeout=config.timeout_seconds,
        env=env,
        volumes={"/recordings": recording_volume},
    )
