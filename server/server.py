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
RESEARCH_TIMEOUT = 20.0  # wall-clock budget for all fetches + Gemini calls combined

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


def _url_candidates(official_url: str) -> list[str]:
    """Return up to 3 URLs to try in order.

    Start with official_url. If it doesn't already target a pricing/plans
    page, append common variants. If the provided URL has a non-root path
    (suggesting a possible broken deep link), also append the bare domain
    root as a last resort.
    """
    from urllib.parse import urlparse

    parsed = urlparse(official_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.lower().rstrip("/")
    pricing_keywords = ("pricing", "plans", "pricing.html")

    candidates: list[str] = [official_url]

    if not any(kw in path for kw in pricing_keywords):
        candidates.append(f"{base}/pricing")
        candidates.append(f"{base}/plans")

    # bare domain root fallback — only useful if official_url wasn't the root
    if path not in ("", "/") and base not in candidates:
        candidates.append(base)

    return candidates[:3]


async def _gemini_extract(
    page_text: str,
    source_url: str,
    company_name: str,
    topic: str,
    client: object,
    remaining_seconds: float,
) -> dict | None:
    """Run one plain Gemini call to extract structured facts from page_text."""
    from google.genai import types

    prompt = (
        f"The following is the visible text of {company_name}'s official website "
        f"({source_url}), fetched directly today ({date.today()}).\n\n"
        f"Extract ONLY facts about: {topic}.\n\n"
        f"Return a JSON object in the same shape as this example:\n"
        f'{{"business_name": "...", "source": "{source_url}", '
        f'"retrieved_on": "{date.today()}", ...structured fields...}}\n\n'
        f"Use ONLY information present in the page text below. If the page does "
        f"not contain enough information to answer, return "
        f'{{"status": "insufficient_data", "reason": "page did not contain {topic} information"}}.\n'
        f"Do not infer or add facts from your training data. Raw JSON only.\n\n"
        f"PAGE TEXT:\n{page_text}"
    )

    loop = asyncio.get_event_loop()

    def _call():
        return client.models.generate_content(
            model="gemini-flash-latest",
            contents=prompt,
            config=types.GenerateContentConfig(),
        ).text

    try:
        text = await asyncio.wait_for(
            loop.run_in_executor(None, _call),
            timeout=max(remaining_seconds, 3.0),
        )
    except (asyncio.TimeoutError, Exception):
        return None

    if not text:
        return None

    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None

    raw = match.group(1) if match.lastindex else match.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    # Enforce source field — model must not point at a different domain.
    if not _source_is_official(data.get("source", source_url), company_name, source_url):
        data["source"] = source_url

    return data


async def _live_research(
    company_name: str, country_code: str, topic: str, official_url: str | None = None
) -> dict | None:
    """Fetch official_url directly, extract structured facts with Gemini.

    Retry logic (capped at 3 fetches, ~20s total):
    - If official_url 404s/fails: fall back to bare domain root.
    - If fetch succeeds but Gemini returns insufficient_data: try pricing
      path variants (/pricing, /plans) before giving up.
    Source restriction is structural — the model only ever sees text fetched
    from URLs under official_url's domain.
    """
    from google import genai

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or not official_url:
        return None

    client = genai.Client(api_key=api_key)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + RESEARCH_TIMEOUT
    fetches_used = 0
    last_result: dict | None = None

    from urllib.parse import urlparse
    parsed = urlparse(official_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    async def fetch(url: str) -> str | None:
        nonlocal fetches_used
        if fetches_used >= 3:
            return None
        remaining = deadline - loop.time()
        if remaining < 4:  # need at least 4s for a Gemini call after this
            return None
        fetch_timeout = min(5.0, remaining - 3)
        fetches_used += 1
        return await loop.run_in_executor(None, lambda: _fetch_page_text(url, timeout=fetch_timeout))

    # --- Step 1: try the provided URL ---
    page_text = await fetch(official_url)

    if not page_text:
        # 404 or error: try bare domain root if different from official_url
        if base.rstrip("/") != official_url.rstrip("/"):
            page_text = await fetch(base)
        if not page_text:
            return {"status": "unavailable", "reason": "could not fetch official URL"}

    # --- Step 2: extract with Gemini ---
    result = await _gemini_extract(
        page_text, official_url, company_name, topic, client,
        remaining_seconds=deadline - loop.time(),
    )
    if result and result.get("status") != "insufficient_data":
        return result
    last_result = result

    # --- Step 3: retry with pricing-page path variants ---
    path = parsed.path.lower().rstrip("/")
    pricing_keywords = ("pricing", "plans", "pricing.html")
    if any(kw in path for kw in pricing_keywords):
        # already tried a pricing-targeted URL — no useful variants to add
        return last_result

    for variant in (f"{base}/pricing", f"{base}/plans"):
        if fetches_used >= 3 or loop.time() >= deadline - 4:
            break
        page_text = await fetch(variant)
        if not page_text:
            continue
        result = await _gemini_extract(
            page_text, variant, company_name, topic, client,
            remaining_seconds=deadline - loop.time(),
        )
        if result and result.get("status") != "insufficient_data":
            return result
        last_result = result

    return last_result


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
        # Only cache genuine fact payloads — never cache error/status objects.
        if result and result.get("status") not in ("insufficient_data", "unavailable"):
            _save_cache(company_name, result)
            return result
        if result:
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
