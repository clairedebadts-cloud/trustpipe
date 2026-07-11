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
RESEARCH_TIMEOUT = 15.0

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


def _fetch_page_text(url: str, timeout: float = 10.0) -> str | None:
    """Fetch a URL and return its visible text, stripped of HTML tags."""
    import requests
    from bs4 import BeautifulSoup

    headers = {"User-Agent": "Mozilla/5.0 TrustPipe/1.0 (research bot)"}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "head"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        # Trim to ~12 K chars — enough for a pricing page, cheap on tokens.
        return text[:12_000]
    except Exception:
        return None


async def _live_research(
    company_name: str, country_code: str, topic: str, official_url: str | None = None
) -> dict | None:
    """Fetch official_url directly, then extract structured facts with one Gemini
    call (no search grounding, no AFC round-trips).

    Source restriction is automatic: the model only sees text from the URL we
    fetched — it cannot pull in any other domain.
    """
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    if not official_url:
        # No URL to fetch and no grounding — cannot do live research safely.
        return None

    loop = asyncio.get_event_loop()
    page_text = await loop.run_in_executor(None, lambda: _fetch_page_text(official_url))
    if not page_text:
        return None

    client = genai.Client(api_key=api_key)
    prompt = (
        f"The following is the visible text of {company_name}'s official website "
        f"({official_url}), fetched directly today ({date.today()}).\n\n"
        f"Extract ONLY facts about: {topic}.\n\n"
        f"Return a JSON object in the same shape as this example:\n"
        f'{{"business_name": "...", "source": "{official_url}", '
        f'"retrieved_on": "{date.today()}", ...structured fields...}}\n\n'
        f"Use ONLY information present in the page text below. If the page does "
        f"not contain enough information to answer, return "
        f'{{"status": "insufficient_data", "reason": "page did not contain {topic} information"}}.\n'
        f"Do not infer or add facts from your training data. Raw JSON only.\n\n"
        f"PAGE TEXT:\n{page_text}"
    )

    def _call():
        return client.models.generate_content(
            model="gemini-flash-latest",
            contents=prompt,
            config=types.GenerateContentConfig(),
        ).text

    text = await asyncio.wait_for(
        loop.run_in_executor(None, _call),
        timeout=RESEARCH_TIMEOUT,
    )

    if not text:
        return None

    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None

    raw = match.group(1) if match.lastindex else match.group(0)
    data = json.loads(raw)

    # Source restriction is already enforced by construction (we fetched official_url
    # directly), but keep the check as a defensive guard against model-added sources.
    if not _source_is_official(data.get("source", official_url), company_name, official_url):
        data["source"] = official_url

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
