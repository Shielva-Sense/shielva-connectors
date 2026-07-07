"""Integration Builder — External docs synthesizer.

Fetches provider documentation URLs and/or accepts custom Markdown rules,
synthesizes them into structured connector-generation rules via LLM, then
ingests the result into the connector's RAG knowledge base so Gemini can
search_knowledge and find exact scope names, auth endpoints, rate limits, etc.

Usage:
    results = await synthesize_and_ingest_docs(
        docs_urls=["https://developers.google.com/gmail/api/auth/scopes"],
        custom_rules_md="## My Rules\\n...",
        tenant_id="t1",
        provider="google",
        service="gmail",
        log_cb=lambda msg: ...,
    )
"""

from __future__ import annotations

import re
from collections.abc import Callable

import httpx
import structlog

from integration.services import r2_service

logger = structlog.get_logger(__name__)

# ── HTML → plain text (no external deps) ─────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n{3,}")


def _strip_html(html: str) -> str:
    """Best-effort HTML → readable plain text."""
    # Remove script/style blocks entirely
    html = re.sub(
        r"<(script|style)[^>]*>.*?</(script|style)>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Replace block elements with newlines
    html = re.sub(r"<(br|p|div|h[1-6]|li|tr)[^>]*>", "\n", html, flags=re.IGNORECASE)
    # Strip remaining tags
    text = _TAG_RE.sub("", html)
    # Decode common HTML entities
    for ent, ch in [
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&nbsp;", " "),
    ]:
        text = text.replace(ent, ch)
    # Collapse excessive blank lines
    text = _WS_RE.sub("\n\n", text)
    return text.strip()


# ── LLM synthesis ─────────────────────────────────────────────────────

_SYNTHESIS_PROMPT = """\
You are a Shielva platform engineer extracting precise connector-generation rules from provider documentation.

## Source document
{source_label}

## Raw content (truncated to 12,000 chars)
{content}

## Your task
Extract ONLY facts that a code generator needs to build a correct connector. Produce a Markdown document with these sections (skip sections that have no relevant data):

### Authentication
- auth_type: (oauth2_code | oauth2_pkce | oauth2_client_credentials | api_key | service_account | basic_auth)
- AUTH_URI: exact URL
- TOKEN_URI: exact URL
- REQUIRED_SCOPES: list every scope string EXACTLY as the provider defines it (e.g. `https://mail.google.com/` not `gmail.full`)

### Endpoints
- Base URL
- Any versioning pattern (e.g. /v1/, /api/2/)

### Rate Limits
- Requests per second/minute/day per plan tier (if documented)
- Retry-After header name if applicable

### Pagination
- Pattern (cursor | offset | page_token | link header)
- Parameter names and response field names

### Error Codes
- List HTTP status codes and their meaning for this API

### SDK / Library
- Official Python package name (for pip install)
- Import path for the main client class

### Key Notes
- Any gotchas, required headers, IP allowlisting, token expiry quirks

Be precise. Use code blocks for exact strings. Do not invent or guess — only extract what is explicitly stated in the source."""


async def _synthesize(source_label: str, raw_content: str, provider: str, service: str) -> str:
    """Call the platform LLM to synthesize rules from raw content."""
    from integration.services.llm_client import call_llm

    _synth_base = await r2_service.get_step_prompt("SYNTHESIS_PROMPT", _SYNTHESIS_PROMPT)
    prompt = _synth_base.format(
        source_label=source_label,
        content=raw_content[:12_000],
    )
    try:
        result = await call_llm(
            [{"role": "user", "content": prompt}],
            system="You extract precise technical rules from documentation. Be concise and accurate.",
            max_tokens=2048,
            expect_code=False,
        )
        return result or ""
    except Exception as exc:
        logger.warning("docs_synth.llm_failed", source=source_label, error=str(exc))
        # Fall back to raw content (still useful for RAG)
        return raw_content[:6_000]


# ── URL fetcher ───────────────────────────────────────────────────────


async def _fetch_url(url: str) -> str:
    """Fetch a URL and return plain text. Returns empty string on failure."""
    try:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ShielvaBot/1.0)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "html" in ct:
                return _strip_html(resp.text)
            return resp.text
    except Exception as exc:
        logger.warning("docs_synth.fetch_failed", url=url, error=str(exc))
        return ""


# ── Ingestion helper ─────────────────────────────────────────────────


async def _ingest(content: str, filename: str, tenant_id: str, provider: str, service: str) -> None:
    from integration.services import knowledge_service

    await knowledge_service.ingest_step_output(
        content=content,
        filename=filename,
        tenant_id=tenant_id,
        provider=provider,
        service=service,
        step_type="external_docs",
    )


# ── Public API ────────────────────────────────────────────────────────


async def synthesize_and_ingest_docs(
    *,
    docs_urls: list[str],
    custom_rules_md: str,
    tenant_id: str,
    provider: str,
    service: str,
    log_cb: Callable[[str], None] | None = None,
) -> dict:
    """Fetch docs URLs + custom rules, synthesize, and ingest into connector KB.

    Returns a summary dict: {ingested_urls: [...], custom_rules: bool, errors: [...]}
    """

    def _log(msg: str) -> None:
        logger.info("docs_synth.progress", msg=msg)
        if log_cb:
            log_cb(msg)

    ingested_urls: list[str] = []
    errors: list[str] = []

    # ── 1. Process each URL ───────────────────────────────────────────
    for url in docs_urls:
        url = url.strip()
        if not url:
            continue
        _log(f"📄 Fetching docs: {url}")
        raw = await _fetch_url(url)
        if not raw:
            errors.append(f"Could not fetch: {url}")
            _log(f"  ⚠ Failed to fetch {url}")
            continue

        _log(f"  🧠 Synthesizing rules from {url} ({len(raw)} chars)...")
        synthesized = await _synthesize(
            source_label=f"URL: {url}",
            raw_content=raw,
            provider=provider,
            service=service,
        )
        if not synthesized:
            errors.append(f"Synthesis returned empty for: {url}")
            continue

        # Ingest both the synthesis and the raw text for maximum RAG coverage
        safe_name = re.sub(r"[^a-z0-9]+", "_", url.lower())[:60]
        await _ingest(synthesized, f"synth_{safe_name}.md", tenant_id, provider, service)
        await _ingest(raw[:8_000], f"raw_{safe_name}.txt", tenant_id, provider, service)

        ingested_urls.append(url)
        _log(f"  ✅ Ingested rules from {url}")

    # ── 2. Process custom Markdown rules ─────────────────────────────
    custom_ingested = False
    if custom_rules_md and custom_rules_md.strip():
        _log("📝 Ingesting custom rules markdown...")
        await _ingest(custom_rules_md, "custom_rules.md", tenant_id, provider, service)
        custom_ingested = True
        _log("  ✅ Custom rules ingested")

    return {
        "ingested_urls": ingested_urls,
        "custom_rules": custom_ingested,
        "errors": errors,
    }


# ── Structured field extraction ───────────────────────────────────────

_EXTRACTION_PROMPT = """\
You are extracting structured configuration values from synthesized API documentation.

## Synthesized documentation
{synthesized_md}

## Task
Extract ONLY values that are EXPLICITLY stated in the documentation above.
Return a JSON object with these exact keys (use null if not found):

{{
  "scopes": "<space-separated exact scope strings, e.g. https://mail.google.com/ https://www.googleapis.com/auth/userinfo.email>",
  "base_url": "<base API URL, e.g. https://gmail.googleapis.com/gmail/v1>",
  "auth_url": "<OAuth2 authorization URL>",
  "token_url": "<OAuth2 token exchange URL>",
  "rate_limit_per_min": "<number as string, e.g. 250>",
  "pagination_type": "<one of: cursor | offset | page_token | link_header | none>",
  "api_version": "<version string, e.g. v1>"
}}

Return ONLY the JSON object. No markdown fences, no explanation."""


async def _extract_structured_fields(synthesized_md: str) -> dict:
    """Run a structured LLM extraction on synthesized docs and return field dict."""
    import json as _json

    from integration.services.llm_client import call_llm

    _extr_base = await r2_service.get_step_prompt("EXTRACTION_PROMPT", _EXTRACTION_PROMPT)
    prompt = _extr_base.format(synthesized_md=synthesized_md[:10_000])
    try:
        raw = await call_llm(
            [{"role": "user", "content": prompt}],
            system="Extract JSON only. Return null for missing fields. No prose.",
            max_tokens=512,
            expect_code=False,
        )
        if not raw:
            return {}
        # Strip markdown fences if model added them anyway
        raw = raw.strip().strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        return _json.loads(raw)
    except Exception as exc:
        logger.warning("docs_synth.extract_fields_failed", error=str(exc))
        return {}


async def fetch_and_extract_fields(
    *,
    docs_urls: list[str],
    provider: str,
    service: str,
) -> dict:
    """Fetch docs URLs, synthesize, then extract structured config fields.

    Returns a dict with keys: scopes, base_url, auth_url, token_url,
    rate_limit_per_min, pagination_type, api_version (any may be null/absent).
    """
    combined_synthesis: list[str] = []

    for url in docs_urls:
        url = url.strip()
        if not url:
            continue
        raw = await _fetch_url(url)
        if not raw:
            continue
        synthesized = await _synthesize(
            source_label=f"URL: {url}",
            raw_content=raw,
            provider=provider,
            service=service,
        )
        if synthesized:
            combined_synthesis.append(synthesized)

    if not combined_synthesis:
        return {}

    merged = "\n\n---\n\n".join(combined_synthesis)
    return await _extract_structured_fields(merged)
