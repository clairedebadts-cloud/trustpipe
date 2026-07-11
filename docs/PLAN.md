# TrustPipe — Plan

## One-line pitch
An MCP server that grounds AI answers about a business in real, verified
data — sourced live when needed, cached for reliability, and honest about
what it doesn't know — instead of stale training data or hallucinated
guesses.

## Problem
AI assistants increasingly act as the front door to businesses (search,
shopping, support), but answer factual questions about pricing, policy, and
promotions from memory — often wrong or outdated. Even secondary web
sources disagree with each other (we found real examples of this
researching Obsidian's pricing page). TrustPipe grounds answers in a
primary source, not guesses or second-hand summaries.

## Architecture

### Tool: `get_company_facts(company_name, country_code, topic)`
```
1. Check facts_cache/{company_name}.json
   → found + fresh → return cached facts immediately (no network needed)
2. Cache miss → attempt live research (web search + fetch)
   → hard timeout (5-8s) — never let a call hang
   → success → save result to cache, return it
   → failure (network error, timeout, empty result):
       → stale cache exists? → return it, flagged:
         "last verified on X, network unavailable for refresh"
       → no cache at all → return an honest error object, never a guess:
         { status: "unavailable", reason: "network error" }
```

This makes the tool genuinely general-purpose (any company, not just one
hardcoded example) while keeping the live demo's core run 100% reliable,
since it runs entirely from cache.

### Guardrails
- Read-only tool. No write/action capability — nothing to confirm-gate.
- Model instructed (system prompt) to answer only from tool results when
  a tool is available for the topic, not blend in outside knowledge.
- If a tool returns "unavailable," the model must say so explicitly rather
  than fall back to guessing.

## Demo (two acts + one bonus)
**Act 1 — Without TrustPipe.** Ask Claude/Gemini factual questions about
Obsidian's pricing/licensing. Capture wrong/uncertain answers — the
commercial-license question is the strongest one (many secondary sources
wrongly claim it was "required until Feb 2026, then removed"; Obsidian's
own FAQ says it was never required).

**Act 2 — With TrustPipe connected.** Same questions, same session.
Correct, sourced answers, served from cache — fast, reliable, no network
dependency during the critical moment.

**Bonus (time permitting) — live research on a second company**, never
rehearsed/cached, to prove the tool generalizes beyond the one example.
If the network hiccups here, the graceful-degradation path is itself the
proof point ("even in a network failure, it tells you it doesn't know
rather than guessing").

## Eval harness
Written before testing, not after — `eval/eval_set.json` holds
question/expected-answer pairs scored against Obsidian's real published
pricing. Score each answer as Correct / Hallucinated / Abstained, run
once without the tool and once with it, compare.

## Explicitly out of scope for today
- No persistent database — cache is flat JSON files on disk.
- No multi-provider benchmarking — architecture is provider-agnostic by
  design, live demo runs on Gemini.
- No write/action tools.

## Cost plan
- Build: Claude Pro (chat + Claude Code) — already paid.
- Live model calls: Gemini API, €10 credit.
- Research calls (cache-miss path): also Gemini, budget-tracked.

## Repo structure
```
trustpipe/
├── README.md
├── requirements.txt
├── .env.example
├── server/
│   ├── server.py            ← MCP server: get_company_facts + others
│   └── facts_cache/
│       └── obsidian.json    ← pre-verified, sourced cache entry
├── eval/
│   ├── eval_sets/
│   │   └── obsidian.json    ← question/expected-answer pairs, paired 1:1
│   │                          with facts_cache/obsidian.json
│   ├── results/              ← collected model answers land here per run
│   │   ├── obsidian_baseline.json   (you create these when running the demo)
│   │   └── obsidian_grounded.json
│   └── run_eval.py          ← generic scorer, takes --company as an arg
└── docs/
    └── PLAN.md               ← this file
```

Eval sets are paired 1:1 with cache entries by company name, so adding a
new company later means adding both `facts_cache/{company}.json` and
`eval_sets/{company}.json` — `run_eval.py` doesn't need to change.

## Build order for Claude Code (each step testable before moving on)
1. `server.py` — cache lookup path only, serving `obsidian.json`. Get this
   working and tested against a real MCP client first.
2. Add the live-research path with timeout + graceful degradation.
3. Write `eval_set.json` (8-10 questions) and `run_eval.py`.
4. Run Act 1 / Act 2 manually, capture real output for the pitch.
5. Only if time remains: bonus live-research demo on a second company.
