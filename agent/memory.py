"""Session memory for the CUA agent.

Maintains a structured log of pages visited and actions performed across
the entire run. Rendered into the system prompt before each model request
so the LLM retains awareness of prior work even after aggressive context
pruning removes old message content.

Design principles:
- Extract results are the primary source of task-relevant data. They get
  extra storage budget and are rendered prominently.
- Failed actions record error details so the LLM avoids repeating mistakes.
- Navigation actions are compressed into a single summary line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bridge import DOM_MARKER
from bridge.url_utils import compact_url, extract_goto_urls, normalize_url

_MAX_FINDING_LEN = 150
# Extract actions contain the actual task-relevant data; allow more room.
_MAX_EXTRACT_FINDING_LEN = 600
# Max chars for error detail stored in memory (enough to show the selector).
_MAX_ERROR_LEN = 120


@dataclass(frozen=True)
class PageVisit:
    """Record of a page the agent navigated to."""

    url: str
    step: int
    display: str


@dataclass(frozen=True)
class ActionEntry:
    """Record of an action with optional observation."""

    step: int
    summary: str
    success: bool
    finding: str = ""
    error_detail: str = ""


@dataclass
class SessionMemory:
    """Accumulates structured session history for LLM context injection.

    Call ``record()`` after each action. Call ``render()`` inside the
    system prompt builder to produce a compact ``<session_progress>`` block.
    """

    pages: list[PageVisit] = field(default_factory=list)
    actions: list[ActionEntry] = field(default_factory=list)

    def record(
        self,
        *,
        step: int,
        action: str,
        tool_input: dict,
        input_summary: str,
        result_text: str | None,
        success: bool,
        visited_urls: list[str] | None = None,
    ) -> None:
        """Record a completed action into session memory."""
        if success:
            for url in visited_urls or extract_goto_urls(action, tool_input):
                self.pages.append(
                    PageVisit(
                        url=url,
                        step=step,
                        display=compact_url(url),
                    )
                )

        finding = ""
        error_detail = ""
        if success and result_text:
            # Extract actions carry task-relevant data — store more of it.
            max_len = (
                _MAX_EXTRACT_FINDING_LEN if action == "extract" else _MAX_FINDING_LEN
            )
            finding = _extract_finding(result_text, max_len=max_len)

            # Deduplicate: if this extract produced the same data as a previous
            # one, drop the earlier entry to keep memory lean.
            if finding and action == "extract":
                self._dedup_extract(finding)
        elif not success and result_text:
            # Store concise error info so the LLM avoids repeating the same mistake.
            error_detail = _extract_error_detail(result_text)

        self.actions.append(
            ActionEntry(
                step=step,
                summary=input_summary,
                success=success,
                finding=finding,
                error_detail=error_detail,
            )
        )

    def _dedup_extract(self, new_finding: str) -> None:
        """Remove earlier extract entries whose findings are a subset of the new one.

        When the agent extracts a broader container that includes data from a
        previous narrower extraction, the old entry is redundant. This keeps
        memory lean and avoids confusing the LLM with duplicate data.
        """
        # Use a simple substring check: if the old finding is contained in
        # the new one (or vice versa), keep only the longer one.
        trimmed: list[ActionEntry] = []
        for entry in self.actions:
            if not entry.finding or not entry.success:
                trimmed.append(entry)
                continue
            # If the old finding is a prefix/subset of the new finding, skip it.
            old_norm = entry.finding[:200].strip()
            new_norm = new_finding[:200].strip()
            if old_norm and old_norm in new_norm:
                continue
            trimmed.append(entry)
        self.actions = trimmed

    def render(self) -> str:
        """Render session memory as a compact text block for the system prompt.

        Returns an empty string if no actions have been recorded yet.
        """
        if not self.actions:
            return ""

        completed = [entry for entry in self.actions if entry.success]
        failed = [entry for entry in self.actions if not entry.success]

        parts: list[str] = ["<session_progress>"]
        parts.append(f"Steps completed: {len(completed)}")

        if self.pages:
            parts.append("")
            parts.append("Pages visited:")
            seen: dict[str, tuple[str, list[int]]] = {}
            for visit in self.pages:
                key = normalize_url(visit.url) or visit.display
                if key not in seen:
                    seen[key] = (visit.display, [visit.step])
                else:
                    seen[key][1].append(visit.step)
            for url_display, steps in seen.values():
                step_str = ", ".join(str(s) for s in steps)
                parts.append(f"  - {url_display} (step {step_str})")

        if completed:
            parts.append("")
            # Separate data-bearing actions from navigational ones
            data_actions = [e for e in completed if e.finding]
            nav_actions = [e for e in completed if not e.finding]
            if data_actions:
                parts.append("Data gathered:")
                for entry in data_actions:
                    parts.append(
                        f"  - Step {entry.step}: {entry.summary} → {entry.finding}"
                    )
            if nav_actions:
                parts.append("")
                parts.append(
                    "Navigation steps: "
                    + ", ".join(f"{e.step}:{e.summary}" for e in nav_actions)
                )

        if failed:
            parts.append("")
            parts.append("Failed attempts (do not repeat these):")
            for entry in failed:
                line = f"  - Step {entry.step}: {entry.summary}"
                if entry.error_detail:
                    line += f" — {entry.error_detail}"
                elif entry.finding:
                    line += f" → {entry.finding}"
                parts.append(line)

        parts.append("</session_progress>")
        return "\n".join(parts)


def _extract_finding(result_text: str, max_len: int = _MAX_FINDING_LEN) -> str:
    """Extract the action summary from result text, stripping DOM content."""
    idx = result_text.find(DOM_MARKER)
    text = result_text[:idx].strip() if idx >= 0 else result_text.strip()

    if not text or text in {"OK", "Done", "done"}:
        return ""

    if len(text) > max_len:
        text = text[:max_len] + "..."

    return text


# Patterns for extracting useful error context from failure messages.
_TIMEOUT_RE = re.compile(r'waiting for locator\("([^"]+)"\)')
_GENERIC_ERROR_RE = re.compile(r"^(?:Error:\s*)?(.+?)(?:\n|$)")


def _extract_error_detail(error_text: str) -> str:
    """Extract a concise, actionable error summary from failure text.

    For timeouts, reports the selector that failed. For other errors,
    extracts the first meaningful line.
    """
    # Timeout on a specific selector — most common failure type.
    m = _TIMEOUT_RE.search(error_text)
    if m:
        selector = m.group(1)
        return f"timeout on selector: {selector[:80]}"

    # Generic: take the first line.
    m = _GENERIC_ERROR_RE.match(error_text.strip())
    if m:
        detail = m.group(1).strip()
        if len(detail) > _MAX_ERROR_LEN:
            detail = detail[:_MAX_ERROR_LEN] + "..."
        return detail

    return ""
