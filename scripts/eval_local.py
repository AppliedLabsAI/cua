"""Run a local CUA evaluation suite and write a JSON report."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import click

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.runner import load_suite, run_suite, write_suite_report
from settings import PRIMARY_MODEL
from telemetry.logging import setup_logging

setup_logging()


async def _run(
    suite_path: str,
    model: str,
    display: str,
    width: int,
    height: int,
    output_path: str,
) -> int:
    suite = await load_suite(suite_path)
    report = await run_suite(
        suite,
        output_root=Path(output_path).parent,
        model=model,
        display=display,
        width=width,
        height=height,
    )
    await write_suite_report(report, output_path)
    print(json.dumps(report.model_dump(), indent=2, ensure_ascii=False))
    return 0 if report.failed == 0 else 1


@click.command()
@click.option("--suite", "suite_path", required=True, help="Path to suite YAML")
@click.option("--model", default=PRIMARY_MODEL, help="Model ID for agent cases")
@click.option("--display", default=":99", help="X display")
@click.option("--width", default=1280, type=int, help="Display width")
@click.option("--height", default=720, type=int, help="Display height")
@click.option(
    "--output",
    "output_path",
    default="output/evals/report.json",
    help="Path to JSON report output",
)
def main(
    suite_path: str,
    model: str,
    display: str,
    width: int,
    height: int,
    output_path: str,
) -> None:
    raise SystemExit(
        asyncio.run(_run(suite_path, model, display, width, height, output_path))
    )


if __name__ == "__main__":
    main()
