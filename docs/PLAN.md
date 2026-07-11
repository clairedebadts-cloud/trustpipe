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

### Tool: `get_company_facts(company_name, country_code, topic, official_url=None)`
```
1. Check facts_cache/{company_name}.json
   → found → return cached facts immediately (no network needed)
2. Cache miss → attempt live research (web search + fetch)
   → SOURCE RESTRICTION (two-tier):
       → official_url provided → strict domain check: reject any result
         whose source URL's domain doesn't match official_url exactly
         (subdomains allowed). Aggregators, forums, third-party summaries
         are rejected even if they rank highly in search results.
       → official_url absent → best-effort heuristic (curated domain
         list, then company-name-in-domain). Results that pass are
         flagged with "_source_confidence": "heuristic" so downstream
         models can lower their stated confidence accordingly.
   → hard timeout (8s) — never let a call hang
   → success → save result to cache with "source" (official URL) and
     "retrieved_on" (today's date) fields, return it
   → failure (network error, timeout, empty result, or only
     non-official sources found):
       → stale cache exists? → return it, flagged:
         "last verified on X, network unavailable for refresh"
       → no cache at all → return an honest error object, never a guess:
         { status: "unavailable", reason: "network error" }
```

**Why official_url is optional:** The cache-served demo path (Act 2) never
touches live research, so official_url is irrelevant for that path. For the
bonus live-research act, passing official_url upgrades source checking from
heuristic to strict — callers who know the company's URL should always pass it.

Every cache entry carries `source` and `retrieved_on`. Staleness (1 year+)
is not judged by the server — it's flagged by the AI at answer time per
`docs/SYSTEM_PROMPT.md`, since "how stale is too stale" is a judgment call
best made visibly, not silently baked into the tool.

### Guardrails
- Read-only tool. No write/action capability — nothing to confirm-gate.
- Model instructed (system prompt) to answer only from tool results when
  a tool is available for the topic, not blend in outside knowledge.
- If a tool returns "unavailable," the model must say so explicitly rather
  than fall back to guessing.
- Official-source-only research (see tool logic above) — no aggregators
  or third-party summaries treated as ground truth.

### Anti-overconfidence answer contract
Grounding alone doesn't fix overconfidence — a model can call the right
tool and still state the answer with false certainty, or quietly blend in
outside knowledge. Per Andrew Ng's agentic-reflection approach (self-
critique over single-shot answers), `docs/SYSTEM_PROMPT.md` requires every
answer to:
- Use bullet points, not one confident paragraph
- Cite source + retrieval date per fact
- Flag anything 1+ year old in *italics* with a re-check recommendation
- Include a 1-10 confidence score reflecting source quality and freshness
- Self-critique before finalizing: did I only use official tool data, or
  did I infer/assume anything?
- State plainly when data is unavailable rather than filling the gap

This is what should make "with TrustPipe" answers visibly different in
*structure*, not just correctness, from the baseline run.

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
Written before testing, not after — `eval/eval_sets/obsidian.json` holds
question/expected-answer pairs scored against Obsidian's real published
pricing. Two scoring layers, per Andrew Ng's rubric-based evaluation
approach rather than a single pass/fail judgment:

**Layer 1 — factual accuracy** (as before): Correct / Hallucinated /
Abstained, run once without the tool and once with it.

**Layer 2 — answer quality rubric** (with-tool run only, since this tests
whether the answer contract was followed, not just whether the tool was
used):
- Cited an official source? (Y/N)
- Flagged staleness where applicable? (Y/N)
- Gave a confidence score? (Y/N)
- Self-critiqued before finalizing? (Y/N)

Layer 2 catches the case Layer 1 misses: a technically correct answer
delivered with false confidence or without showing its sourcing — exactly
the overconfidence failure mode grounding alone doesn't fix.

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
