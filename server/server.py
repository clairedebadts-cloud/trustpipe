import asyncio
import json
import os
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

CACHE_DIR = Path(__file__).parent / "facts_cache"
RESEARCH_TIMEOUT = 8.0

# Curated official domains for companies we care about getting exactly right.
# Any company not listed falls back to a name-in-domain heuristic.
KNOWN_DOMAINS = {
    "obsidian": "obsidian.md",
}

mcp = FastMCP("TrustPipe")


def _filename(company_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", company_name.lower().strip()).strip("_")
    return f"{normalized}.json"


def _source_is_official(source: str, company_name: str, official_domain: str | None = None) -> bool:
    """Verify a live-research result's source URL is the company's own domain.

    Two modes:
    - official_domain provided → strict match against that domain/subdomains.
    - official_domain absent   → best-effort heuristic (curated list, then
      company-name-in-domain), lower confidence.
    """
    if not source:
        return False
    host = urlparse(source if "//" in source else "//" + source).hostname or ""
    host = host.lower().removeprefix("www.")
    if not host:
        return False

    if official_domain:
        expected = urlparse(official_domain if "//" in official_domain else "//" + official_domain).hostname or ""
        expected = expected.lower().removeprefix("www.")
        return bool(expected) and (host == expected or host.endswith("." + expected))

    # Heuristic fallback: curated list, then name-in-domain label.
    key = re.sub(r"[^a-z0-9]+", "", company_name.lower())
    if not key:
        return False
    known = KNOWN_DOMAINS.get(key)
    if known:
        return host == known or host.endswith("." + known)
    return any(key in label for label in host.split("."))


def _load_cache(company_name: str) -> dict | None:
    path = CACHE_DIR / _filename(company_name)
    if not path.exists():
        return None
    try:
        with path.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # Corrupt or unreadable cache file: treat as a miss so the call
        # degrades to live research / "unavailable" instead of crashing.
        return None


def _save_cache(company_name: str, data: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / _filename(company_name)
    with path.open("w") as f:
        json.dump(data, f, indent=2)


async def _live_research(
    company_name: str, country_code: str, topic: str, official_url: str | None = None
) -> dict | None:
    import google.generativeai as genai

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    genai.configure(api_key=api_key)

    domain_restriction = (
        f" Only use information sourced directly from {official_url} — reject any other URL."
        if official_url else ""
    )
    prompt = (
        f"Research factual information about {company_name} (country: {country_code}), "
        f"specifically about: {topic}.{domain_restriction}\n\n"
        f"Return ONLY a valid JSON object with verified facts from the company's own official website. "
        f"Include 'business_name', 'source' (the exact URL you used), "
        f"'retrieved_on' (today is {date.today()}), and the relevant factual fields. "
        f"Do not guess or infer — only include what you can verify from official sources. "
        f"Respond with raw JSON only, no markdown fences."
    )

    # The pinned legacy google-generativeai SDK only supports the
    # `google_search_retrieval` grounding tool, and that tool works with 1.5
    # models. (gemini-2.0's `google_search` tool requires the newer
    # google-genai SDK, which this project does not use.)
    model = genai.GenerativeModel(
        "gemini-1.5-flash",
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
    data = json.loads(raw)

    if not _source_is_official(data.get("source", ""), company_name, official_url):
        return None

    # Flag lower confidence when domain validation was heuristic, not strict.
    if not official_url:
        data["_source_confidence"] = "heuristic"
        data["_note"] = (
            "source domain verified by name heuristic only; "
            "pass official_url for strict domain validation"
        )

    return data


@mcp.tool()
async def get_company_facts(
    company_name: str,
    country_code: str,
    topic: str,
    official_url: str | None = None,
) -> dict:
    """
    Return verified facts about a company's pricing, policies, or key business info.

    Checks local cache first (no network needed). On cache miss, attempts live web
    research with an 8-second hard timeout. On failure, returns a stale cache entry
    (flagged) if one exists, otherwise an honest status object — never a guess.

    Args:
        company_name: The company to look up (e.g. "Obsidian", "Notion").
        country_code: ISO 3166-1 alpha-2 country code (e.g. "US", "FR").
        topic: What to research (e.g. "pricing", "refund policy", "free tier").
        official_url: The company's official website URL (e.g. "https://obsidian.md").
            When provided, live research results are strictly rejected if their source
            domain doesn't match. Without it, a best-effort name heuristic is used
            and results are flagged with lower confidence.
    """
    cached = _load_cache(company_name)

    if cached:
        return cached

    # Cache miss — attempt live research
    try:
        result = await _live_research(company_name, country_code, topic, official_url)
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
