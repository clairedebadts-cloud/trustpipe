# TrustPipe — HTTP bridge for the Claude Design "add company" form

Purpose: expose a live baseline-vs-grounded comparison over HTTP so the
Claude Design dashboard (browser-based, no local access) can trigger a
real mini-demo for any company the person types in — separate from the
real MCP connection used in Claude Desktop/Code for the core, pre-scored
Obsidian demo.

This is NOT a real MCP transport. It's a thin wrapper around the existing
logic. The core Act 1/Act 2 demo (pre-scored, 37% → 100%) does not depend
on this — it stays on the genuine MCP connection, fully offline-safe.

## Important design constraint
The pre-written eval set (`eval_sets/obsidian.json`) has expected answers
because we researched Obsidian ourselves ahead of time. For an arbitrary
company typed into the form live, there is no pre-written ground truth —
so this endpoint CANNOT auto-score "correct/hallucinated" the way
run_eval.py does for Obsidian. Instead, it shows the honest thing it can
show: the baseline answer and the grounded answer, side by side, so the
contrast speaks for itself. No fabricated "accuracy %" for companies with
no pre-verified expected answers.

## Endpoint
```
POST /live_compare
Body: { "company": str, "official_url": str, "question": str }

Response: {
  "baseline_answer": str,   # one Gemini call, no tool, general knowledge
  "grounded_answer": str,   # calls get_company_facts(official_url=...),
                             # then formats per docs/SYSTEM_PROMPT.md
                             # contract (bullets, source, date, confidence)
  "source": str | null,     # from the tool result, null if unavailable
  "status": "ok" | "unavailable"
}
```

Reuses `_load_cache`, `_live_research`, `_source_is_official` from
server/server.py directly — do not duplicate the logic. The "question"
field can default to a fixed representative prompt (e.g. "What are
{company}'s current pricing and policies?") if the form doesn't collect
a specific question from the user.

## Minimal implementation plan
1. New file: server/http_bridge.py — FastAPI app, one POST route.
2. It calls get_company_facts() logic first (with official_url for
   strict domain validation), then makes two Gemini calls: one plain
   (baseline), one grounded in the fetched facts + system prompt
   contract (grounded).
3. Run locally: `uvicorn server.http_bridge:app --port 8000`
4. Expose publicly for the demo window only: `ngrok http 8000`
5. Point Claude Design's "Verify and add" button at that ngrok URL,
   POSTing company + official_url, rendering both answers side by side
   in the same before/after card style as the Obsidian results.

## Risk, stated plainly
This adds: a live dependency on your laptop/wifi/tunnel staying up, PLUS
now two live Gemini calls per submission (cost + latency + the same
API-key/tool-config uncertainty flagged earlier — verify those work
before relying on this). Treat this as optional/bonus, demoed only after
the core pre-scored Obsidian result has already been shown safely.

## Fallback if it breaks during rehearsal
Cut it. Present the dashboard with a captured screenshot/example of one
successful live comparison, and describe the capability verbally rather
than performing it live.
