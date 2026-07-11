# TrustPipe

An MCP server that grounds AI answers about a business in real, verified
data — sourced live when needed, cached for reliability — instead of stale
training data or hallucinated guesses.

Built for the MCP Hackathon. See [docs/PLAN.md](./docs/PLAN.md) for the
full plan.

## Demo
Two runs of the same questions about Obsidian's real pricing, same model:
1. **Without TrustPipe connected** — baseline (often wrong/uncertain)
2. **With TrustPipe connected** — grounded, correct, cited answers

Accuracy scored against a written eval set (`eval/eval_set.json`), not
vibes — see `eval/run_eval.py`.

## How it works
`get_company_facts(company_name, country_code, topic)`:
1. Checks a local cache first — instant, reliable, no network dependency.
2. On cache miss, runs live research with a hard timeout, caches the
   result for next time.
3. On network failure with no cache available, returns an honest
   "unavailable" status — never a guess.

## Structure
```
trustpipe/
├── server/
│   ├── server.py          ← MCP server exposing get_company_facts
│   └── facts_cache/
│       └── obsidian.json  ← pre-verified data, sourced from obsidian.md/pricing
├── eval/
│   ├── eval_set.json      ← question/expected-answer pairs
│   └── run_eval.py        ← scores before/after accuracy
├── requirements.txt
└── .env.example
```

## Setup
```bash
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env         # then fill in your Gemini API key
```

## Status
🚧 Hackathon build in progress.
