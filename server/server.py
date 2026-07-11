import asyncio
import json
import os
import re
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

CACHE_DIR = Path(__file__).parent / "facts_cache"
RESEARCH_TIMEOUT = 8.0

mcp = FastMCP("TrustPipe")


def _filename(company_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", company_name.lower().strip()).strip("_")
    return f"{normalized}.json"


def _load_cache(company_name: str) -> dict | None:
    path = CACHE_DIR / _filename(company_name)
    if path.exists():
        with path.open() as f:
            return json.load(f)
    return None


def _save_cache(company_name: str, data: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / _filename(company_name)
    with path.open("w") as f:
        json.dump(data, f, indent=2)


async def _live_research(company_name: str, country_code: str, topic: str) -> dict | None:
    import google.generativeai as genai

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    genai.configure(api_key=api_key)

    prompt = (
        f"Research factual information about {company_name} (country: {country_code}), "
        f"specifically about: {topic}.\n\n"
        f"Return ONLY a valid JSON object with verified facts from the company's own official website. "
        f"Include 'business_name', 'source' (the URL you used), "
        f"'retrieved_on' (today is {date.today()}), and the relevant factual fields. "
        f"Do not guess or infer — only include what you can verify from official sources. "
        f"Respond with raw JSON only, no markdown fences."
    )

    model = genai.GenerativeModel(
        "gemini-2.0-flash",
        tools=[{"google_search_retrieval": {}}],
    )

    loop = asyncio.get_event_loop()

    def _call():
        return model.generate_content(prompt).text

    text = await asyncio.wait_for(
        loop.run_in_executor(None, _call),
        timeout=RESEARCH_TIMEOUT,
    )

    if not text:
        return None

    # Strip markdown fences if the model added them anyway
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None

    raw = match.group(1) if match.lastindex else match.group(0)
    return json.loads(raw)


@mcp.tool()
async def get_company_facts(company_name: str, country_code: str, topic: str) -> dict:
    """
    Return verified facts about a company's pricing, policies, or key business info.

    Checks local cache first (no network needed). On cache miss, attempts live web
    research with an 8-second hard timeout. On failure, returns a stale cache entry
    (flagged) if one exists, otherwise an honest status object — never a guess.

    Args:
        company_name: The company to look up (e.g. "Obsidian", "Notion").
        country_code: ISO 3166-1 alpha-2 country code (e.g. "US", "FR").
        topic: What to research (e.g. "pricing", "refund policy", "free tier").
    """
    cached = _load_cache(company_name)

    if cached:
        return cached

    # Cache miss — attempt live research
    try:
        result = await _live_research(company_name, country_code, topic)
        if result:
            _save_cache(company_name, result)
            return result
    except asyncio.TimeoutError:
        if cached:
            return {**cached, "_stale": True, "_note": "network timeout; could not refresh"}
    except Exception:
        if cached:
            return {**cached, "_stale": True, "_note": "network error; could not refresh"}

    return {"status": "unavailable", "reason": "network error or no data found"}


if __name__ == "__main__":
    mcp.run()
