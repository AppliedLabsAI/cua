"""Modal sandbox image definition and sandbox factory.

Builds a custom Ubuntu 24.04 image with a full desktop environment,
Patchright browser, and the CUA agent runtime. Built once, cached by Modal.
"""

from __future__ import annotations

import json

import modal

from api.models import RunConfig

recording_volume = modal.Volume.from_name(
    "cua-recordings", create_if_missing=True, version=2
)

sandbox_image = (
    modal.Image.from_registry("ubuntu:24.04")
    .apt_install(
        # Display + WM
        "xvfb",
        "openbox",
        "tint2",
        # Browsers
        "chromium-browser",
        "firefox",
        # Desktop apps
        "libreoffice",
        "xterm",
        "thunar",
        # Screenshot + image tools
        "imagemagick",
        # CLI essentials
        "curl",
        "wget",
        "jq",
        "git",
        "nodejs",
        "npm",
        # Fonts
        "fonts-liberation",
        "fonts-noto-cjk",
    )
    .pip_install(
        "anthropic>=0.86",
        "fastapi>=0.135.2",
        "httpx>=0.28.1",
        "opentelemetry-api>=1.40",
        "opentelemetry-sdk>=1.40",
        "opentelemetry-exporter-otlp-proto-grpc>=1.40",
        "opentelemetry-instrumentation-fastapi>=0.61b0",
        "patchright>=1.58.2",
        "uvicorn>=0.42",
    )
    .run_commands(
        "patchright install chromium",
    )
    .add_local_dir("agent", "/opt/cua/agent")
    .add_local_dir("bridge", "/opt/cua/bridge")
    .add_local_dir("api", "/opt/cua/api")
    .add_local_dir("actionlog", "/opt/cua/actionlog")
    .add_local_dir("sandbox", "/opt/cua/sandbox")
    .add_local_dir("profiles", "/opt/cua/profiles")
    .add_local_file("config.py", "/opt/cua/config.py")
    .add_local_file("exceptions.py", "/opt/cua/exceptions.py")
    .add_local_dir("blinders", "/opt/cua/blinders")
    .add_local_dir("guardrails", "/opt/cua/guardrails")
    .add_local_dir("telemetry", "/opt/cua/telemetry")
    .add_local_dir("recording", "/opt/cua/recording")
    .env(
        {
            "DISPLAY": ":99",
            "DISPLAY_NUM": "99",
            "WIDTH": "1280",
            "HEIGHT": "720",
            "PYTHONPATH": "/opt/cua",
        }
    )
)

# Port exposed by the sandbox
PORT_STATUS = 8090


def create_cua_sandbox(
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
    secrets: list[modal.Secret] = [modal.Secret.from_name("anthropic-secret")]

    env: dict[str, str | None] = {
        "DIRECTIVE": config.directive,
        "MODEL": config.model,
        "MAX_STEPS": str(config.max_steps),
        "THINKING_BUDGET": str(config.thinking_budget),
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

    return modal.Sandbox.create(
        "/opt/cua/sandbox/entrypoint.sh",
        app=app,
        image=sandbox_image,
        secrets=secrets,
        encrypted_ports=[PORT_STATUS],
        timeout=config.timeout_seconds,
        env=env,
        volumes={"/recordings": recording_volume},
    )
