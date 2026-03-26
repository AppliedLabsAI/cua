"""Modal sandbox image definition and sandbox factory.

Builds a custom Ubuntu 24.04 image with a full desktop environment,
Patchright browser, and the CUA agent runtime. Built once, cached by Modal.
"""

from __future__ import annotations

import json

import modal

from api.models import RunConfig

sandbox_image = (
    modal.Image.from_registry("ubuntu:24.04")
    .apt_install(
        # Display + WM
        "xvfb",
        "openbox",
        "tint2",
        "x11vnc",
        # noVNC for browser-based observation
        "novnc",
        "websockify",
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
        "anthropic>=0.52.0",
        "patchright>=1.0.0",
        "fastapi>=0.115.0",
        "uvicorn>=0.34.0",
        "httpx>=0.28.0",
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

# Ports exposed by the sandbox
PORT_NOVNC = 6080
PORT_STATUS = 8090


def create_cua_sandbox(config: RunConfig, app: modal.App) -> modal.Sandbox:
    """Create a Modal sandbox configured for a CUA run.

    Returns the sandbox immediately — startup is asynchronous.
    Use sandbox.tunnels() to get the noVNC and status API URLs.
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
        env["GUARDRAILS_JSON"] = json.dumps(config.guardrails)

    return modal.Sandbox.create(
        "/opt/cua/sandbox/entrypoint.sh",
        app=app,
        image=sandbox_image,
        secrets=secrets,
        encrypted_ports=[PORT_NOVNC, PORT_STATUS],
        timeout=config.timeout_seconds,
        env=env,
    )
