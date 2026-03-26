"""Bridge layer — routes agent actions to the browser executor.

The bridge abstracts execution strategy from the agent loop. Uses Patchright
for precise, fast browser interactions via CSS/text/role selectors.

Also handles CAPTCHA detection/solving (transparent to the agent loop).
"""

from bridge.models import DOM_MARKER, ActionResult

__all__ = ["ActionResult", "DOM_MARKER"]
