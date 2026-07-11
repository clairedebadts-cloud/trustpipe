"""
TrustPipe HTTP bridge — exposes a live baseline-vs-grounded comparison
over HTTP so a browser-based dashboard can trigger a real mini-demo for
any company, without a local MCP connection.

Run:    uvicorn server.http_bridge:app --port 8000
Expose: ngrok http 8000
"""
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel

# Reuse shared logic from server.py — no duplication of cache/research/validation.
from server.server import _load_cache, _live_research, _save_cache

load_dotenv()

GEMINI_CALL_TIMEOUT = 15.0

# The answer contract from docs/SYSTEM_PROMPT.md, adapted for inline context
# (references "provided facts" instead of "tool results" since there's no MCP
# tool call in this HTTP path — the facts are passed directly in the prompt).
_ANSWER_CONTRACT = """\
When answering any factual question about a business's pricing, policy, or
promotions using the verified facts provided, you must:

1. SOURCE RESTRICTION
   Only treat the provided facts as reliable if their "source" field is the
   company's own official domain. If the source is not official, say so
   explicitly and lower your confidence accordingly.

2. STALENESS CHECK
   Check the "retrieved_on" date in the facts against today's date.
   - Less than 1 year old: state it normally.
   - 1 year old or older: prefix each affected fact in *italics* and add:
     "*this was verified over a year ago and may be out of date — recommend
     re-checking*".

3. ANSWER FORMAT — bullet points, not a single confident paragraph:
   - **Answer**: the direct answer, one fact per bullet
   - **Source**: the official source URL and retrieval date
   - **Confidence**: a 1-10 score reflecting source quality and data freshness
   - **Caveats**: anything missing, ambiguous, or worth the user verifying

4. SELF-CRITIQUE before finalising:
   - Did I only use the provided facts — not my own training-data assumptions?
   - Am I inferring anything not directly stated in the facts? If so, flag it.

5. NEUTRAL FRAMING
   Do not soften uncertainty. If facts are unavailable or from an unofficial
   source, say so plainly — never fill the gap with a plausible-sounding guess.\
"""

app = FastAPI(title="TrustPipe HTTP Bridge")


class CompareRequest(BaseModel):
    company: str
    official_url: str
    question: str = ""


class CompareResponse(BaseModel):
    baseline_answer: str
    grounded_answer: str
    source: str | None
    status: str


async def _gemini(prompt: str, system_instruction: str | None = None) -> str:
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "[GEMINI_API_KEY not configured]"

    client = genai.Client(api_key=api_key)
    config_kwargs = {}
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction
    config = types.GenerateContentConfig(**config_kwargs)

    loop = asyncio.get_event_loop()
    response = await asyncio.wait_for(
        loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-flash-latest", contents=prompt, config=config
            ),
        ),
        timeout=GEMINI_CALL_TIMEOUT,
    )
    return response.text


@app.post("/live_compare", response_model=CompareResponse)
async def live_compare(req: CompareRequest) -> CompareResponse:
    question = req.question or f"What are {req.company}'s current pricing and policies?"

    # 1. Facts: cache-first, then live research with strict domain validation.
    facts = _load_cache(req.company)
    if not facts:
        try:
            facts = await _live_research(req.company, "US", question, req.official_url)
            if facts:
                _save_cache(req.company, facts)
        except Exception:
            facts = None

    source = facts.get("source") if facts else None

    # 2. Baseline: plain Gemini call, no facts, no contract — general knowledge only.
    try:
        baseline_answer = await _gemini(
            f"Answer this question about {req.company}: {question}"
        )
    except Exception as exc:
        baseline_answer = f"[baseline call failed: {type(exc).__name__}]"

    # 3. Grounded: facts embedded in prompt + answer contract as system instruction.
    if facts:
        grounded_prompt = (
            f"Here are verified facts about {req.company} sourced from their "
            f"official website:\n\n"
            f"{json.dumps(facts, indent=2)}\n\n"
            f"Question: {question}\n\n"
            f"Answer using ONLY the facts provided above, following the "
            f"answer contract in your system instructions exactly."
        )
        try:
            grounded_answer = await _gemini(grounded_prompt, system_instruction=_ANSWER_CONTRACT)
        except Exception as exc:
            grounded_answer = f"[grounded call failed: {type(exc).__name__}]"
        status = "ok"
    else:
        grounded_answer = (
            "I don't have verified data on this company — I won't guess."
        )
        status = "unavailable"

    return CompareResponse(
        baseline_answer=baseline_answer,
        grounded_answer=grounded_answer,
        source=source,
        status=status,
    )
