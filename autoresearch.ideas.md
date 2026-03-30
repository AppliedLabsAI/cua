# Autoresearch Ideas

## Tried & Failed
- Compact link format (no gain)
- Weakening OBSERVE FIRST for goto+login batching (regression - this rule is critical)
- Wider table cell text/href (regression - more verbose = slower LLM)
- Hard nav cap at 20 links (overfitting to Django)
- Selector-based markdown extraction (regression)
- More prompt rules (>11 rules starts confusing the model)

## Promising Ideas To Try
- Auto-attach DOM context after `extract` action (agent sometimes needs to act after extracting)
- Make `extract` with specific CSS selector also return relevant nearby elements
- Reduce page_map size for pages with huge navs by deduplicating near-identical links (e.g., same path prefix)
- Speed optimization: reduce LLM latency by lowering thinking effort for simple navigation steps
- Add `search` or `filter` hints in the page map when search/filter inputs are available
- Pre-inject readability JS via add_init_script (avoid lazy-init overhead on first extract)
- Consider auto-sorting tables by "created at" column if the directive mentions "latest"
