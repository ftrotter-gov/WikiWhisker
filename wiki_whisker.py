#!/usr/bin/env python3
"""
WikiWhisker — A narrow, purposeful Wikipedia data-extraction tool.

Usage:
    python wiki_whisker.py STARTING_PAGE [STARTING_PAGE ...] \\
        --traversal-rules   config/traversal.yaml \\
        --extraction-questions config/extraction.yaml \\
        [--output results/output.json] \\
        [--model  gpt-4o]
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests
import yaml

# litellm is imported lazily inside llm_call() so the tool still works for
# purely deterministic jobs (infobox / wikidata sources only) even if litellm
# is not installed.

# ---------------------------------------------------------------------------
# Wikipedia API helpers
# ---------------------------------------------------------------------------

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "WikiWhisker/1.0 (https://github.com/ftrotter-gov/WikiWhisker)"

# Maximum number of wikitext characters sent to the LLM for extraction.
# Keeps token costs reasonable; most infoboxes appear in the first 8 000 chars.
LLM_WIKITEXT_MAX_CHARS = 12_000


def _api_get(params: dict) -> dict:
    """Make a GET request to the Wikipedia API and return parsed JSON."""
    params.setdefault("format", "json")
    params.setdefault("formatversion", "2")
    headers = {"User-Agent": USER_AGENT}
    response = requests.get(WIKIPEDIA_API, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def resolve_page_title(title_or_url: str) -> str:
    """Normalize a Wikipedia URL or raw title to a canonical page title."""
    for prefix in (
        "https://en.wikipedia.org/wiki/",
        "http://en.wikipedia.org/wiki/",
    ):
        if title_or_url.startswith(prefix):
            title_or_url = title_or_url[len(prefix):]
            break
    from urllib.parse import unquote
    return unquote(title_or_url).replace("_", " ")


def get_page_wikitext(title: str) -> Optional[str]:
    """Return the raw wikitext for a page, or None if it doesn't exist."""
    data = _api_get({
        "action": "query",
        "titles": title,
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
    })
    pages = data.get("query", {}).get("pages", [])
    if not pages:
        return None
    page = pages[0]
    if page.get("missing"):
        return None
    try:
        return page["revisions"][0]["slots"]["main"]["content"]
    except (KeyError, IndexError):
        return None


def get_page_summary(title: str) -> str:
    """
    Return the introductory plain-text summary of a page (first paragraph).
    Uses the Wikipedia REST summary API — faster and cheaper than full wikitext.
    Returns an empty string if the page is not found or the call fails.
    """
    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(title)}"
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("extract", "")
    except Exception:
        pass
    return ""


# Section headings whose links must NEVER be followed.  Matching is
# case-insensitive; the pattern covers the most common English variants.
_NOISE_SECTION_RE = re.compile(
    r"^==+\s*("
    r"references?|bibliography|bibliographies|footnotes?|notes?|"
    r"further reading|see also|external links?|citations?|sources?"
    r")\s*==+\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Wikilink pattern: [[Target]] or [[Target|Label]]
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#][^\[\]|#]*?)(?:\|[^\[\]]*)?\]\]")


def _strip_noise_sections(wikitext: str) -> str:
    """
    Remove the content of "noise" sections (References, Bibliography,
    Further reading, See also, External links, etc.) from wikitext.
    Returns the wikitext up to the first noise-section heading found.
    If a noise section appears in the middle of the article, everything
    from that heading onward is also removed so that subsequent sections
    (e.g. a "Notes" section followed by "Appendix") are not accidentally
    included.
    """
    lines = wikitext.splitlines(keepends=True)
    result_lines = []
    for line in lines:
        if _NOISE_SECTION_RE.match(line.rstrip()):
            # Stop as soon as we hit any noise-section heading.
            # Wikipedia convention puts these at the end, so we can
            # safely discard everything from here on.
            break
        result_lines.append(line)
    return "".join(result_lines)


def get_page_links(title: str) -> list[str]:
    """
    Return internal Wikipedia links from the *body* of a page (namespace 0
    only), excluding any links that appear in bibliography, references,
    further-reading, see-also, external-links, notes, or footnotes sections.

    Strategy:
      1. Fetch the raw wikitext for the page.
      2. Strip out noise sections (everything from the first noise heading on).
      3. Parse [[wikilinks]] from the remaining body text.
      4. Validate each candidate against the Wikipedia API to ensure it exists
         and lives in namespace 0 (skips redirects to other namespaces,
         non-existent pages, etc.).  A single batched API call is used.
    """
    wikitext = get_page_wikitext(title)
    if not wikitext:
        return []

    body = _strip_noise_sections(wikitext)

    # Extract raw link targets from [[…]] markup in the body.
    raw_targets: list[str] = []
    seen_raw: set[str] = set()
    for m in _WIKILINK_RE.finditer(body):
        target = m.group(1).strip()
        # Skip file/image/category/template namespaces
        if ":" in target:
            continue
        # Normalise: first letter capitalised, spaces
        target = target[:1].upper() + target[1:] if target else target
        target = target.replace("_", " ")
        if target and target not in seen_raw:
            seen_raw.add(target)
            raw_targets.append(target)

    if not raw_targets:
        return []

    # Validate via the API in batches of 50 (API limit).
    valid_links: list[str] = []
    batch_size = 50
    for i in range(0, len(raw_targets), batch_size):
        batch = raw_targets[i : i + batch_size]
        data = _api_get({
            "action": "query",
            "titles": "|".join(batch),
            "redirects": "",          # resolve redirects
        })
        pages = data.get("query", {}).get("pages", [])
        for page in pages:
            # ns == 0 → article namespace; missing → page doesn't exist
            if page.get("ns") == 0 and not page.get("missing"):
                valid_links.append(page["title"])

    return valid_links


def get_page_categories(title: str) -> list[str]:
    """Return category names for a page (without the 'Category:' prefix)."""
    cats = []
    params = {
        "action": "query",
        "titles": title,
        "prop": "categories",
        "cllimit": "max",
    }
    while True:
        data = _api_get(params)
        pages = data.get("query", {}).get("pages", [])
        if pages:
            for cat in pages[0].get("categories", []):
                raw = cat["title"]
                if raw.startswith("Category:"):
                    raw = raw[len("Category:"):]
                cats.append(raw)
        cont = data.get("continue")
        if cont:
            params.update(cont)
        else:
            break
    return cats


def get_wikidata_entity(title: str) -> Optional[dict]:
    """Return the Wikidata entity dict for a Wikipedia page title, or None."""
    data = _api_get({
        "action": "query",
        "titles": title,
        "prop": "pageprops",
        "ppprop": "wikibase_item",
    })
    pages = data.get("query", {}).get("pages", [])
    if not pages:
        return None
    page = pages[0]
    qid = page.get("pageprops", {}).get("wikibase_item")
    if not qid:
        return None
    wd_resp = requests.get(
        f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json",
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    wd_resp.raise_for_status()
    wd_data = wd_resp.json()
    return wd_data.get("entities", {}).get(qid)


# ---------------------------------------------------------------------------
# Infobox extraction
# ---------------------------------------------------------------------------

def extract_infobox_fields(wikitext: str) -> dict[str, str]:
    """
    Parse the first infobox found in wikitext and return a dict of
    field_name -> raw_value pairs.
    """
    fields: dict[str, str] = {}
    match = re.search(r"\{\{\s*[Ii]nfobox", wikitext)
    if not match:
        return fields

    start = match.start()
    depth = 0
    i = start
    end = start
    while i < len(wikitext) - 1:
        if wikitext[i : i + 2] == "{{":
            depth += 1
            i += 2
        elif wikitext[i : i + 2] == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                end = i
                break
        else:
            i += 1

    infobox_text = wikitext[start:end]
    lines = re.split(r"\n\s*\|", infobox_text)
    for line in lines[1:]:
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()

    return fields


def clean_wikimarkup(text: str) -> str:
    """Remove common wikimarkup from a string to get plain text."""
    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"'{2,3}", "", text)
    text = re.sub(r"<ref[^>]*/?>.*?</ref>", "", text, flags=re.DOTALL)
    text = re.sub(r"<ref[^>]*/?>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# LLM integration (via litellm)
# ---------------------------------------------------------------------------

def llm_call(model: str, system_prompt: str, user_message: str) -> str:
    """
    Call an LLM via litellm and return the text content of the first response.
    Raises RuntimeError if litellm is not installed or the call fails.
    """
    try:
        import litellm  # noqa: PLC0415  (lazy import is intentional)
    except ImportError:
        raise RuntimeError(
            "litellm is not installed.  Run:  pip install litellm\n"
            "LLM-based sources/filters require litellm."
        )

    try:
        response = litellm.completion(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0,
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        raise RuntimeError(f"LLM call failed: {exc}") from exc


def llm_traversal_decision(
    title: str,
    prompt: str,
    model: str,
    include_summary: bool = True,
    include_categories: bool = False,
    summary: str = "",
    categories: Optional[list[str]] = None,
) -> bool:
    """
    Ask the LLM whether this page should be included in the crawl.
    Returns True for YES, False for NO (or any non-YES response).
    """
    system = (
        "You are a precise Wikipedia page classifier. "
        "You will be given a Wikipedia page title and possibly additional context. "
        "You must respond with exactly one word: YES or NO. "
        "Do not include any other text, punctuation, or explanation."
    )

    parts = [f"Wikipedia page title: {title}"]
    if include_summary and summary:
        parts.append(f"\nPage summary:\n{summary}")
    if include_categories and categories:
        parts.append(f"\nWikipedia categories: {', '.join(categories)}")
    parts.append(f"\nQuestion: {prompt}")

    user_msg = "\n".join(parts)

    try:
        answer = llm_call(model, system, user_msg)
        return answer.strip().upper().startswith("YES")
    except RuntimeError as exc:
        print(f"  ⚠  LLM traversal call failed for '{title}': {exc}", file=sys.stderr)
        return False


def llm_extract_field(
    title: str,
    page_text: str,
    llm_prompt: str,
    llm_options: Optional[list[str]],
    model: str,
) -> str:
    """
    Ask the LLM to extract one field value from the page text.
    Returns the raw string answer from the LLM.
    """
    if llm_options:
        options_str = ", ".join(f'"{o}"' for o in llm_options)
        constraint = (
            f"You MUST respond with exactly one of these options: {options_str}. "
            "Do not include any other text, punctuation, or explanation."
        )
    else:
        constraint = (
            "Respond with a short, direct answer only. "
            "Do not include any explanation or extra text."
        )

    system = (
        "You are a precise data-extraction assistant working with Wikipedia article text. "
        f"{constraint}"
    )

    # Truncate wikitext to keep costs reasonable
    truncated = page_text[:LLM_WIKITEXT_MAX_CHARS]
    if len(page_text) > LLM_WIKITEXT_MAX_CHARS:
        truncated += "\n[...article truncated...]"

    user_msg = (
        f"Wikipedia article: {title}\n\n"
        f"Article text:\n{truncated}\n\n"
        f"Question: {llm_prompt}"
    )

    try:
        return llm_call(model, system, user_msg)
    except RuntimeError as exc:
        print(f"  ⚠  LLM extraction call failed for '{title}': {exc}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------------------
# Traversal rule evaluation
# ---------------------------------------------------------------------------

def page_passes_deterministic_filters(title: str, rules: dict) -> bool:
    """
    Return True if `title` passes ALL deterministic link_filters.
    Does not call the LLM.
    """
    filters = rules.get("link_filters", {})
    if not filters:
        return True

    title_lower = title.lower()

    tc = filters.get("title_contains")
    if tc:
        needles = [tc] if isinstance(tc, str) else tc
        if not any(n.lower() in title_lower for n in needles):
            return False

    tnc = filters.get("title_not_contains")
    if tnc:
        needles = [tnc] if isinstance(tnc, str) else tnc
        if any(n.lower() in title_lower for n in needles):
            return False

    tmr = filters.get("title_matches_regex")
    if tmr:
        if not re.search(tmr, title, re.IGNORECASE):
            return False

    cc = filters.get("category_contains")
    cnc = filters.get("category_not_contains")
    if cc or cnc:
        cats = [c.lower() for c in get_page_categories(title)]
        if cc:
            needles = [cc] if isinstance(cc, str) else cc
            if not any(any(n.lower() in c for c in cats) for n in needles):
                return False
        if cnc:
            needles = [cnc] if isinstance(cnc, str) else cnc
            if any(any(n.lower() in c for c in cats) for n in needles):
                return False

    return True


def page_passes_llm_filter(title: str, rules: dict, model: str) -> bool:
    """
    If llm_traversal_filter is enabled in `rules`, ask the LLM whether this
    page should be included.  Returns True if the filter is disabled.
    """
    llm_cfg = rules.get("llm_traversal_filter", {})
    if not llm_cfg.get("enabled", False):
        return True

    prompt = llm_cfg.get("prompt", "")
    if not prompt:
        print(
            "  ⚠  llm_traversal_filter.enabled is true but no prompt is set — skipping LLM filter.",
            file=sys.stderr,
        )
        return True

    include_summary = llm_cfg.get("include_page_summary", True)
    include_cats = llm_cfg.get("include_categories", False)

    summary = ""
    categories: list[str] = []

    if include_summary:
        summary = get_page_summary(title)
    if include_cats:
        categories = get_page_categories(title)

    return llm_traversal_decision(
        title=title,
        prompt=prompt,
        model=model,
        include_summary=include_summary,
        include_categories=include_cats,
        summary=summary,
        categories=categories,
    )


# ---------------------------------------------------------------------------
# Extraction question evaluation
# ---------------------------------------------------------------------------

def _resolve_source(
    title: str,
    source: str,
    wikitext: Optional[str],
    infobox: dict,
    entity: Optional[dict],
    question_def: dict,
    model: str,
) -> Any:
    """
    Fetch the raw value of a field from the given source.

    source values:
      "title"                  — page title itself
      "category_list"          — full list of categories
      "infobox:<field_name>"   — field from the page infobox
      "wikidata:<property_id>" — Wikidata property (e.g. "P31")
      "llm"                    — ask the LLM using question_def["llm_prompt"]
    """
    if source == "title":
        return title

    if source == "category_list":
        return get_page_categories(title)

    if source.startswith("infobox:"):
        field = source[len("infobox:"):]
        raw = infobox.get(field, "")
        return clean_wikimarkup(raw) if raw else None

    if source.startswith("wikidata:"):
        prop = source[len("wikidata:"):]
        if entity is None:
            return None
        claims = entity.get("claims", {}).get(prop, [])
        values = []
        for claim in claims:
            try:
                snak = claim["mainsnak"]
                dv = snak.get("datavalue", {})
                dtype = dv.get("type")
                val = dv.get("value")
                if dtype == "string":
                    values.append(val)
                elif dtype == "wikibase-entityid":
                    values.append(val.get("id"))
                elif dtype == "monolingualtext":
                    values.append(val.get("text"))
                elif dtype == "quantity":
                    values.append(val.get("amount"))
                elif dtype == "time":
                    values.append(val.get("time"))
                elif val is not None:
                    values.append(str(val))
            except (KeyError, TypeError):
                continue
        if not values:
            return None
        return values[0] if len(values) == 1 else values

    if source == "llm":
        llm_prompt = question_def.get("llm_prompt", "")
        if not llm_prompt:
            print(
                f"  ⚠  source is 'llm' but no llm_prompt set for field "
                f"'{question_def.get('field_name', '?')}' — returning None.",
                file=sys.stderr,
            )
            return None
        llm_options = question_def.get("llm_options")
        page_text = wikitext or ""
        answer = llm_extract_field(
            title=title,
            page_text=page_text,
            llm_prompt=llm_prompt,
            llm_options=llm_options,
            model=model,
        )
        # Validate against options if provided
        if llm_options and answer not in llm_options:
            # Try case-insensitive match
            for opt in llm_options:
                if opt.lower() == answer.lower():
                    return opt
            # If still no match, return raw answer (don't silently drop it)
        return answer if answer else None

    return None


def answer_extraction_question(
    title: str,
    question_def: dict,
    wikitext: Optional[str],
    infobox: dict,
    entity: Optional[dict],
    model: str,
) -> Any:
    """
    Evaluate one extraction question definition and return the extracted value.
    Tries primary source first, then fallback_sources in order, then default.
    """
    sources = [question_def.get("source", "title")]
    fallbacks = question_def.get("fallback_sources", [])
    sources.extend(fallbacks)

    raw = None
    for src in sources:
        val = _resolve_source(title, src, wikitext, infobox, entity, question_def, model)
        if val is not None and val != "" and val != []:
            raw = val
            break

    if raw is None:
        return question_def.get("default", None)

    target_type = question_def.get("type", "string")
    try:
        if target_type == "list":
            if isinstance(raw, list):
                return raw
            return [raw]
        elif target_type == "int":
            return int(str(raw).replace(",", "").strip())
        elif target_type == "float":
            return float(str(raw).replace(",", "").strip())
        elif target_type == "bool":
            return str(raw).lower() in ("yes", "true", "1")
        else:  # string
            if isinstance(raw, list):
                return ", ".join(str(v) for v in raw)
            return str(raw)
    except (ValueError, TypeError):
        return question_def.get("default", None)


# ---------------------------------------------------------------------------
# Per-page record builder
# ---------------------------------------------------------------------------

def build_page_record(
    title: str,
    questions: list[dict],
    model: str,
    include_metadata: bool = True,
) -> dict:
    """
    Fetch data for a single page and build a JSON-serialisable record
    according to the extraction questions definition.
    """
    print(f"  → Fetching page: {title}", flush=True)

    wikitext = get_page_wikitext(title)
    infobox = extract_infobox_fields(wikitext) if wikitext else {}
    entity = None

    # Lazily fetch Wikidata entity only if a wikidata: source is needed
    for q in questions:
        all_sources = [q.get("source", "")] + q.get("fallback_sources", [])
        if any(s.startswith("wikidata:") for s in all_sources):
            entity = get_wikidata_entity(title)
            break

    record: dict = {}

    if include_metadata:
        record["_page_title"] = title
        record["_wikipedia_url"] = (
            "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
        )

    for q in questions:
        field_name = q.get("field_name") or q.get("label", "unknown")
        record[field_name] = answer_extraction_question(
            title, q, wikitext, infobox, entity, model
        )

    return record


# ---------------------------------------------------------------------------
# Main crawl orchestration
# ---------------------------------------------------------------------------

def run_crawl(
    starting_pages: list[str],
    traversal_rules: dict,
    extraction_questions: list[dict],
    output_path: str,
    model: str,
) -> None:
    """
    Full crawl: resolve starting pages → filter links → extract → write JSON.

    Crawl flow:
      [1] Resolve starting page titles
      [2] Collect all internal links from starting pages
      [3] Apply deterministic filters, then optional LLM filter
      [4] Extract data from accepted pages
      [5] Write structured JSON output
    """
    max_pages = traversal_rules.get("max_secondary_pages", 200)
    delay = traversal_rules.get("request_delay_seconds", 0.5)
    include_starting_pages = traversal_rules.get("include_starting_pages_in_output", False)

    llm_filter_enabled = traversal_rules.get("llm_traversal_filter", {}).get("enabled", False)

    # ── [1] Resolve starting page titles ──────────────────────────────────────
    starting_titles = [resolve_page_title(p) for p in starting_pages]

    # ── [2] Collect candidate secondary pages ─────────────────────────────────
    print("\n[1/3] Collecting links from starting page(s)…", flush=True)
    candidate_links: list[str] = []
    seen: set[str] = set(starting_titles)

    for start_title in starting_titles:
        print(f"  → Starting page: {start_title}", flush=True)
        links = get_page_links(start_title)
        print(f"     Found {len(links)} raw links.", flush=True)
        for link in links:
            if link not in seen:
                seen.add(link)
                candidate_links.append(link)
        time.sleep(delay)

    # ── [3] Apply filters ─────────────────────────────────────────────────────
    filter_label = (
        "deterministic + LLM filters" if llm_filter_enabled else "deterministic filters"
    )
    print(f"\n[2/3] Applying {filter_label}…", flush=True)

    accepted: list[str] = []
    for link in candidate_links:
        if len(accepted) >= max_pages:
            print(
                f"  ⚠  max_secondary_pages ({max_pages}) reached — stopping filter pass.",
                flush=True,
            )
            break

        # Step A: cheap deterministic filters
        if not page_passes_deterministic_filters(link, traversal_rules):
            continue

        # Step B: optional LLM filter (more expensive — called only if A passed)
        if llm_filter_enabled:
            print(f"     LLM filter: asking about '{link}'…", flush=True)
            if not page_passes_llm_filter(link, traversal_rules, model):
                print(f"     ✗ LLM says NO for '{link}'", flush=True)
                time.sleep(delay)
                continue
            print(f"     ✓ LLM says YES for '{link}'", flush=True)

        accepted.append(link)
        time.sleep(delay)

    print(f"  → {len(accepted)} pages accepted.", flush=True)

    # ── [4] Extract data ──────────────────────────────────────────────────────
    print("\n[3/3] Extracting data from accepted pages…", flush=True)
    records: list[dict] = []

    if include_starting_pages:
        for start_title in starting_titles:
            rec = build_page_record(start_title, extraction_questions, model)
            rec["_is_starting_page"] = True
            records.append(rec)
            time.sleep(delay)

    for title in accepted:
        rec = build_page_record(title, extraction_questions, model)
        rec["_is_starting_page"] = False
        records.append(rec)
        time.sleep(delay)

    # ── [5] Build output JSON ─────────────────────────────────────────────────
    output_schema = {
        "schema_version": "1.0",
        "fields": [
            {
                "field_name": q.get("field_name") or q.get("label", "unknown"),
                "label": q.get("label", ""),
                "type": q.get("type", "string"),
                "source": q.get("source", ""),
                "description": q.get("description", ""),
            }
            for q in extraction_questions
        ],
    }

    output = {
        "meta": {
            "tool": "WikiWhisker",
            "version": "1.0",
            "model": model,
            "starting_pages": starting_titles,
            "total_records": len(records),
            "output_schema": output_schema,
        },
        "records": records,
    }

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)

    print(f"\n✅  Done!  {len(records)} records written to {output_path}", flush=True)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wiki_whisker",
        description=(
            "WikiWhisker — crawl a structured subset of Wikipedia pages and\n"
            "extract data into a structured JSON file.\n\n"
            "Provide one or more starting Wikipedia page titles or URLs, a YAML\n"
            "file (under config/) defining which linked pages to follow, and a\n"
            "YAML file (under config/) defining what data to extract from each\n"
            "page.  The results are written as structured JSON.\n\n"
            "LLM calls use the OpenAI ChatGPT API by default (gpt-4o).  Set\n"
            "OPENAI_API_KEY in your environment (copy .env.example to .env).\n"
            "Pass --model to switch to any litellm-supported model."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "starting_pages",
        metavar="STARTING_PAGE",
        nargs="+",
        help=(
            "One or more Wikipedia page titles or full Wikipedia URLs. "
            "Example: 'Aspirin' or 'https://en.wikipedia.org/wiki/Aspirin'"
        ),
    )

    parser.add_argument(
        "--traversal-rules",
        metavar="TRAVERSAL_YAML",
        required=True,
        help=(
            "Path to the YAML file defining link-following rules. "
            "Lives under config/ by convention. "
            "See examples/traversal_rules_example.yaml for the full schema."
        ),
    )

    parser.add_argument(
        "--extraction-questions",
        metavar="EXTRACTION_YAML",
        required=True,
        help=(
            "Path to the YAML file defining what data to extract and how to "
            "structure the output JSON. "
            "Lives under config/ by convention. "
            "See examples/extraction_questions_example.yaml for the full schema."
        ),
    )

    parser.add_argument(
        "--output",
        metavar="OUTPUT_JSON",
        default="output.json",
        help=(
            "Path for the output JSON file. Default: output.json. "
            "Recommended: results/<name>.json"
        ),
    )

    parser.add_argument(
        "--model",
        metavar="MODEL",
        default="gpt-4o",
        help=(
            "litellm model string for LLM calls. Default: gpt-4o. "
            "Examples: gpt-4o-mini, claude-3-5-sonnet-20241022, ollama/llama3. "
            "Only needed when traversal or extraction uses LLM sources/filters."
        ),
    )

    return parser.parse_args(argv)


def load_yaml_file(path: str, label: str) -> Any:
    p = Path(path)
    if not p.exists():
        print(f"ERROR: {label} file not found: {path}", file=sys.stderr)
        sys.exit(1)
    with p.open("r", encoding="utf-8") as fh:
        try:
            return yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            print(
                f"ERROR: Could not parse {label} YAML file '{path}':\n{exc}",
                file=sys.stderr,
            )
            sys.exit(1)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)

    traversal_rules = load_yaml_file(args.traversal_rules, "traversal rules")
    extraction_config = load_yaml_file(args.extraction_questions, "extraction questions")

    if isinstance(extraction_config, list):
        extraction_questions = extraction_config
    elif isinstance(extraction_config, dict):
        extraction_questions = extraction_config.get("questions", [])
    else:
        print(
            "ERROR: extraction questions YAML must be a list or a dict with a 'questions' key.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not extraction_questions:
        print(
            "ERROR: No extraction questions found in the extraction questions YAML.",
            file=sys.stderr,
        )
        sys.exit(1)

    run_crawl(
        starting_pages=args.starting_pages,
        traversal_rules=traversal_rules,
        extraction_questions=extraction_questions,
        output_path=args.output,
        model=args.model,
    )


if __name__ == "__main__":
    main()
