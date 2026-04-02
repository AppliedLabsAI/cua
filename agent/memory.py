"""Session memory for the CUA agent.

Maintains a structured log of pages visited and actions performed across
the entire run. Rendered into the system prompt before each model request
so the LLM retains awareness of prior work even after aggressive context
pruning removes old message content.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bridge import DOM_MARKER
from bridge.url_utils import compact_url, extract_goto_urls, normalize_url

_MAX_FINDING_LEN = 150


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
        if result_text and success:
            finding = _extract_finding(result_text)

        self.actions.append(
            ActionEntry(
                step=step,
                summary=input_summary,
                success=success,
                finding=finding,
            )
        )

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
            parts.append("Actions completed:")
            for entry in completed:
                line = f"  - Step {entry.step}: {entry.summary}"
                if entry.finding:
                    line += f" → {entry.finding}"
                parts.append(line)

        if failed:
            parts.append("")
            parts.append("Failed attempts:")
            for entry in failed:
                line = f"  - Step {entry.step}: {entry.summary}"
                if entry.finding:
                    line += f" → {entry.finding}"
                parts.append(line)

        parts.append("</session_progress>")
        return "\n".join(parts)


def _extract_finding(result_text: str) -> str:
    """Extract the action summary from result text, stripping DOM content."""
    idx = result_text.find(DOM_MARKER)
    text = result_text[:idx].strip() if idx >= 0 else result_text.strip()

    if not text or text in {"OK", "Done", "done"}:
        return ""

    if len(text) > _MAX_FINDING_LEN:
        text = text[:_MAX_FINDING_LEN] + "..."

    return text
