"""Shared runtime lifecycle helpers for evaluation trials."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bridge.browser import BrowserManager
    from recording.manager import RecordingManager


@dataclass(frozen=True)
class TrialRuntime:
    """Browser/recording resources for one evaluation trial."""

    browser: BrowserManager
    recording: RecordingManager


async def launch_browser(
    *,
    display: str,
    width: int,
    height: int,
    start_url: str | None,
    proxy: str | None = None,
) -> BrowserManager:
    from bridge.browser import BrowserManager

    os.environ["DISPLAY"] = display
    browser = BrowserManager()
    await browser.launch(width=width, height=height, start_url=start_url, proxy=proxy)
    return browser


async def init_recording(
    run_id: str,
    output_root: Path,
    browser: BrowserManager,
) -> RecordingManager:
    from recording import RecordingConfig
    from recording.manager import RecordingManager

    config = RecordingConfig(output_dir=str(output_root / run_id), upload=False)
    recording = RecordingManager(config, run_id=run_id)
    await recording.start(browser.context)
    return recording


@asynccontextmanager
async def trial_runtime(
    *,
    run_id: str,
    output_root: Path,
    display: str,
    width: int,
    height: int,
    start_url: str | None,
    proxy: str | None = None,
):
    """Create and clean up the browser + recording runtime for a trial."""
    browser = await launch_browser(
        display=display,
        width=width,
        height=height,
        start_url=start_url,
        proxy=proxy,
    )
    recording = await init_recording(run_id, output_root, browser)
    try:
        yield TrialRuntime(browser=browser, recording=recording)
    finally:
        results = await asyncio.gather(
            recording.stop(),
            browser.close(),
            return_exceptions=True,
        )
        for exc in results:
            if isinstance(exc, Exception):
                raise exc
