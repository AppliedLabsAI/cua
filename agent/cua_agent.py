"""CUA Agent definition using Pydantic AI.

Central Agent instance that handles the browser automation loop.
Tools, system prompt, and model settings are configured here.
"""

from __future__ import annotations

import copy

from pydantic_ai import Agent, ModelSettings, RunContext, ToolDefinition

from agent.deps import AgentDeps
from agent.prompts import build_system_prompt
from agent.tools import browser_dom
from settings import PRIMARY_MODEL


async def _filter_allowed_actions(
    ctx: RunContext[AgentDeps],
    tool_defs: list[ToolDefinition],
) -> list[ToolDefinition] | None:
    """Dynamically restrict browser_dom action enum via Cognitive Blinders."""
    allowed = ctx.deps.allowed_actions
    if allowed is None:
        return None

    # Deep-copy to avoid mutating the shared ToolDefinition schemas.
    filtered = copy.deepcopy(tool_defs)
    for td in filtered:
        if td.name == "browser_dom":
            action_schema = td.parameters_json_schema.get("properties", {}).get(
                "action", {}
            )
            if "enum" in action_schema:
                action_schema["enum"] = sorted(allowed)
    return filtered


cua_agent: Agent[AgentDeps, str] = Agent(
    PRIMARY_MODEL,
    deps_type=AgentDeps,
    tools=[browser_dom],
    prepare_tools=_filter_allowed_actions,
    instructions=(
        "You are a browser automation agent. You MUST use the browser_dom tool "
        "to interact with web pages. NEVER answer from memory — always navigate "
        "to the page and read the actual content."
    ),
    model_settings=ModelSettings(max_tokens=16384),
)


@cua_agent.system_prompt
def system_prompt(ctx: RunContext[AgentDeps]) -> str:
    """Build the system prompt from runtime deps."""
    return build_system_prompt(
        directive=ctx.deps.directive,
        credentials=ctx.deps.credentials,
        profile_prompt=ctx.deps.profile_prompt,
    )
