#!/usr/bin/env python3
"""
WikiWhisker — A narrow, purposeful Wikipedia data-extraction tool.

Usage:
    python wiki_whisker.py STARTING_PAGE [STARTING_PAGE ...] \\
        --traversal-rules   config/traversal.yaml \\
        --extraction-questions config/extraction.yaml \\
        [--output results/output.json] \\
        [--model  gpt-4o] \\
        [--cache-dir json_cache] \\
        [--project-id my_project]

EXTRACTION CONFIG SCHEMA
────────────────────────
The extraction YAML has two sections:

  questions:         (optional) deterministic fields — title, infobox, wikidata,
                     category_list.  Zero LLM cost.

  llm_extraction:    (optional) all LLM-answered fields, batched into a SINGLE
                     API call per page.  The LLM returns one JSON object with
                     all field values at once.

Example:

  questions:
    - field_name: page_title
      source: "title"
      type: string
    - field_name: country_of_origin
      source: "infobox:country"
      fallback_sources: ["wikidata:P495"]
      type: string
      default: null

  llm_extraction:
    prompt_preamble: >
      Read the Wikipedia article and answer each question below.
      Return ONLY a valid JSON object with exactly the keys listed.
    fields:
      - field_name: health_issues
        label: "Significant Health Issues"
        question: >
          Does this breed have significant, breed-specific health problems
          (e.g. hip dysplasia, heart defects, brachycephalic syndrome)?
          Answer True if yes, False if the breed is generally healthy,
          Unknown if the breed is extinct or health info is absent.
        options: ["True", "False", "Unknown"]
        default: "Unknown"
"""

import argparse
import hashlib
import json
import re
import sys
import time
import warnings
from pathlib import Path
from typing import Any, Optional

import requests
import yaml

# Suppress noisy pydantic serialization warnings emitted by older versions of
# litellm when the openai response model doesn't exactly match the expected
# schema.  These are harmless version-skew warnings that do not affect results.
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
warnings.filterwarnings("ignore", message=".*PydanticSerialization.*")
warnings.filterwarnings("ignore", message=".*Expected.*fields.*but got.*")

import os as _os
# Silence litellm's own verbose logging (it emits INFO lines and triggers the
# pydantic serializer path that produces the UserWarning above).
_os.environ.setdefault("LITELLM_LOG", "ERROR")

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
    Remove noise sections (References, See also, External links, etc.).
    Returns the wikitext up to the first noise-section heading.
    """
    lines = wikitext.splitlines(keepends=True)
    result_lines = []
    for line in lines:
        if _NOISE_SECTION_RE.match(line.rstrip()):
            break
        result_lines.append(line)
    return "".join(result_lines)


def get_page_links(title: str) -> list[str]:
    """
    Return internal Wikipedia links from the body of a page (namespace 0 only),
    excluding noise sections.  Validates each link via the API.
    """
    wikitext = get_page_wikitext(title)
    if not wikitext:
        return []

    body = _strip_noise_sections(wikitext)

    raw_targets: list[str] = []
    seen_raw: set[str] = set()
    for m in _WIKILINK_RE.finditer(body):
        target = m.group(1).strip()
        if ":" in target:
            continue
        target = target[:1].upper() + target[1:] if target else target
        target = target.replace("_", " ")
        if target and target not in seen_raw:
            seen_raw.add(target)
            raw_targets.append(target)

    if not raw_targets:
        return []

    valid_links: list[str] = []
    batch_size = 50
    for i in range(0, len(raw_targets), batch_size):
        batch = raw_targets[i : i + batch_size]
        data = _api_get({
            "action": "query",
            "titles": "|".join(batch),
            "redirects": "",
        })
        pages = data.get("query", {}).get("pages", [])
        for page in pages:
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
    """Parse the first infobox and return field_name → raw_value pairs."""
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
        import litellm  # noqa: PLC0415
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


# ---------------------------------------------------------------------------
# Traversal LLM filter  — one YES/NO call per candidate page
# ---------------------------------------------------------------------------

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
    Ask the LLM a single multi-criteria YES/NO question about a page.
    Returns True for YES, False for any other response.
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


# ---------------------------------------------------------------------------
# Extraction LLM batch call  — ONE call per page, returns JSON with all fields
# ---------------------------------------------------------------------------

def llm_batch_extract(
    title: str,
    page_text: str,
    llm_extraction_cfg: dict,
    model: str,
) -> dict[str, Any]:
    """
    Ask the LLM ALL extraction questions for a page in a single API call.

    llm_extraction_cfg has the shape:
      {
        "prompt_preamble": "...",   # optional context / instructions
        "fields": [
          {
            "field_name": "health_issues",
            "question": "Does this breed have significant health problems? ...",
            "options": ["True", "False", "Unknown"],   # optional
            "default": "Unknown"
          },
          ...
        ]
      }

    Returns a dict mapping field_name → extracted value.
    On JSON parse failure, falls back to the default for each field.
    """
    fields = llm_extraction_cfg.get("fields", [])
    if not fields:
        return {}

    preamble = llm_extraction_cfg.get("prompt_preamble", "")

    # Build the list of field specs shown to the LLM
    field_specs = []
    for f in fields:
        fn = f.get("field_name", "unknown")
        q = f.get("question", "")
        opts = f.get("options")
        if opts:
            opts_str = ", ".join(f'"{o}"' for o in opts)
            field_specs.append(f'"{fn}": {q.strip()}  (must be one of: {opts_str})')
        else:
            field_specs.append(f'"{fn}": {q.strip()}')

    field_block = "\n".join(f"  {s}" for s in field_specs)
    field_keys  = ", ".join(f'"{f.get("field_name", "unknown")}"' for f in fields)

    system = (
        "You are a precise data-extraction assistant working with Wikipedia article text. "
        "You will be given article text and a list of questions. "
        f"You MUST respond with ONLY a valid JSON object containing exactly these keys: {field_keys}. "
        "Do not include any explanation, markdown formatting, or text outside the JSON object."
    )

    # Truncate wikitext to keep costs reasonable
    truncated = page_text[:LLM_WIKITEXT_MAX_CHARS]
    if len(page_text) > LLM_WIKITEXT_MAX_CHARS:
        truncated += "\n[...article truncated...]"

    user_msg = (
        f"Wikipedia article: {title}\n\n"
        f"Article text:\n{truncated}\n\n"
        + (f"Instructions: {preamble}\n\n" if preamble else "")
        + f"Answer each question and return a JSON object:\n{field_block}"
    )

    try:
        raw = llm_call(model, system, user_msg)
    except RuntimeError as exc:
        print(f"  ⚠  LLM batch extraction call failed for '{title}': {exc}", file=sys.stderr)
        return {f.get("field_name", "unknown"): f.get("default") for f in fields}

    # Strip markdown code fences if the LLM wrapped the JSON
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.rstrip())

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract the first {...} block
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = {}
        else:
            parsed = {}

    if not parsed:
        print(
            f"  ⚠  LLM batch extraction returned non-JSON for '{title}'. "
            "Using defaults.",
            file=sys.stderr,
        )

    # Build result, validating options and applying defaults
    result: dict[str, Any] = {}
    for f in fields:
        fn = f.get("field_name", "unknown")
        default = f.get("default")
        opts = f.get("options")
        raw_val = parsed.get(fn)

        if raw_val is None:
            result[fn] = default
            continue

        raw_str = str(raw_val).strip()

        if opts:
            # Exact match first
            if raw_str in opts:
                result[fn] = raw_str
            else:
                # Case-insensitive fallback
                matched = next(
                    (o for o in opts if o.lower() == raw_str.lower()), None
                )
                result[fn] = matched if matched is not None else default
        else:
            result[fn] = raw_str

    return result


# ---------------------------------------------------------------------------
# JSON cache — per-project, per-page, resumable
# ---------------------------------------------------------------------------
#
# Cache directory layout:
#
#   json_cache/
#     <project_id>/
#       <safe_title>_<hash8>.json   ← one file per Wikipedia page evaluated
#
# Each cache file:
#
#   {
#     "_page_title": "Labrador Retriever",
#     "_wikipedia_url": "...",
#     "_cache_version": 1,
#     "_llm_traversal": {          ← present if LLM traversal was run
#       "model": "gpt-4o",
#       "decision": true
#     },
#     "_extraction": {             ← present if extraction was completed
#       "model": "gpt-4o",
#       "fields": {                ← key = field_name, value = extracted value
#         "page_title": "Labrador Retriever",
#         "health_issues": "True",
#         ...
#       }
#     }
#   }
#
# RESUMABILITY
# ────────────
# • Traversal phase: cached decision reused if present.
# • Extraction phase: cached if ALL required field_names are present.
# • Model is recorded for auditing but does NOT invalidate the cache.
#   Delete the project cache directory to force a fresh run with a new model.

CACHE_VERSION = 1


def _title_to_cache_filename(title: str) -> str:
    safe = re.sub(r'[^\w\-. ]', '_', title).strip().replace(' ', '_')
    short_hash = hashlib.md5(title.encode("utf-8")).hexdigest()[:8]
    return f"{safe[:80]}_{short_hash}.json"


def _get_cache_path(cache_dir: Path, project_id: str, title: str) -> Path:
    return cache_dir / project_id / _title_to_cache_filename(title)


def _load_cache(cache_path: Path) -> dict:
    if cache_path.exists():
        try:
            with cache_path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache_path: Path, data: dict) -> None:
    """Atomically write the cache JSON for a page."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    tmp.replace(cache_path)


def _derive_project_id(output_path: str) -> str:
    stem = Path(output_path).stem
    return re.sub(r'[^\w]', '_', stem).strip('_') or "wikiwhisker_project"


# ---------------------------------------------------------------------------
# Traversal rule evaluation  (cache-aware)
# ---------------------------------------------------------------------------

def page_passes_deterministic_filters(title: str, rules: dict) -> bool:
    """Return True if title passes ALL deterministic link_filters."""
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


def page_passes_llm_filter(
    title: str,
    rules: dict,
    model: str,
    cache_dir: Optional[Path] = None,
    project_id: str = "",
) -> bool:
    """
    Ask the LLM a single YES/NO gate question about the page.
    Returns True if the filter is disabled or if the LLM says YES.
    Caches the decision for resumability.
    """
    llm_cfg = rules.get("llm_traversal_filter", {})
    if not llm_cfg.get("enabled", False):
        return True

    prompt = llm_cfg.get("prompt", "")
    if not prompt:
        print(
            "  ⚠  llm_traversal_filter.enabled is true but no prompt is set — skipping.",
            file=sys.stderr,
        )
        return True

    # ── Cache check ──────────────────────────────────────────────────────────
    cache_data: dict = {}
    cache_path: Optional[Path] = None
    if cache_dir and project_id:
        cache_path = _get_cache_path(cache_dir, project_id, title)
        cache_data = _load_cache(cache_path)
        if "_llm_traversal" in cache_data:
            cached = cache_data["_llm_traversal"]
            decision = cached.get("decision", False)
            status = "accepted" if decision else "rejected"
            print(
                f"     ↩  Page {status} (cached) '{title}' [model={cached.get('model', '?')}]",
                flush=True,
            )
            return decision

    # ── Fresh LLM call ───────────────────────────────────────────────────────
    include_summary = llm_cfg.get("include_page_summary", True)
    include_cats = llm_cfg.get("include_categories", False)

    summary = ""
    categories: list[str] = []

    if include_summary:
        summary = get_page_summary(title)
    if include_cats:
        categories = get_page_categories(title)

    decision = llm_traversal_decision(
        title=title,
        prompt=prompt,
        model=model,
        include_summary=include_summary,
        include_categories=include_cats,
        summary=summary,
        categories=categories,
    )

    # ── Write to cache ───────────────────────────────────────────────────────
    if cache_path is not None:
        cache_data.setdefault("_page_title", title)
        cache_data.setdefault("_wikipedia_url",
            "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"))
        cache_data["_cache_version"] = CACHE_VERSION
        cache_data["_llm_traversal"] = {
            "model": model,
            "decision": decision,
        }
        _save_cache(cache_path, cache_data)

    return decision


# ---------------------------------------------------------------------------
# Deterministic field extraction  (infobox / wikidata / title / category_list)
# ---------------------------------------------------------------------------

def _resolve_deterministic_source(
    title: str,
    source: str,
    wikitext: Optional[str],
    infobox: dict,
    entity: Optional[dict],
) -> Any:
    """
    Resolve a single deterministic source (no LLM).

    source values:
      "title"                  — page title itself
      "category_list"          — full list of categories
      "infobox:<field_name>"   — field from the page infobox
      "wikidata:<property_id>" — Wikidata property (e.g. "P31")
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

    return None


def resolve_deterministic_field(
    title: str,
    question_def: dict,
    wikitext: Optional[str],
    infobox: dict,
    entity: Optional[dict],
) -> Any:
    """
    Evaluate one deterministic extraction question and return the value.
    Tries primary source, then fallback_sources, then returns default.
    """
    sources = [question_def.get("source", "title")]
    fallbacks = question_def.get("fallback_sources", [])
    sources.extend(fallbacks)

    raw = None
    for src in sources:
        val = _resolve_deterministic_source(title, src, wikitext, infobox, entity)
        if val is not None and val != "" and val != []:
            raw = val
            break

    if raw is None:
        return question_def.get("default", None)

    target_type = question_def.get("type", "string")
    try:
        if target_type == "list":
            return raw if isinstance(raw, list) else [raw]
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
# Per-page record builder  (cache-aware, batch LLM extraction)
# ---------------------------------------------------------------------------

def build_page_record(
    title: str,
    questions: list[dict],
    llm_extraction_cfg: Optional[dict],
    model: str,
    include_metadata: bool = True,
    cache_dir: Optional[Path] = None,
    project_id: str = "",
) -> dict:
    """
    Fetch data for a single page and build a JSON-serialisable record.

    - questions: list of deterministic field definitions (title/infobox/wikidata)
    - llm_extraction_cfg: the llm_extraction block (or None if absent)

    Cache behaviour:
      If all required field_names are present in the cache, the cached values
      are returned and no LLM (or Wikipedia API) calls are made for that page.
    """
    # Collect all required field names
    det_field_names = {
        q.get("field_name") or q.get("label", "unknown") for q in questions
    }
    llm_field_names: set[str] = set()
    if llm_extraction_cfg:
        for f in llm_extraction_cfg.get("fields", []):
            fn = f.get("field_name", "unknown")
            llm_field_names.add(fn)

    all_required_field_names = det_field_names | llm_field_names

    # ── Cache check ──────────────────────────────────────────────────────────
    cache_path: Optional[Path] = None
    cache_data: dict = {}

    if cache_dir and project_id:
        cache_path = _get_cache_path(cache_dir, project_id, title)
        cache_data = _load_cache(cache_path)
        extraction_cache = cache_data.get("_extraction", {})
        cached_fields = extraction_cache.get("fields", {})

        if all_required_field_names and all_required_field_names.issubset(cached_fields.keys()):
            print(
                f"  ↩  Extraction cached for '{title}' "
                f"[model={extraction_cache.get('model', '?')}]",
                flush=True,
            )
            record: dict = {}
            if include_metadata:
                record["_page_title"] = title
                record["_wikipedia_url"] = (
                    "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
                )
            for q in questions:
                fn = q.get("field_name") or q.get("label", "unknown")
                record[fn] = cached_fields.get(fn)
            if llm_extraction_cfg:
                for f in llm_extraction_cfg.get("fields", []):
                    fn = f.get("field_name", "unknown")
                    record[fn] = cached_fields.get(fn)
            return record

    # ── Fresh fetch ──────────────────────────────────────────────────────────
    print(f"  → Processing page: '{title}'", flush=True)

    wikitext = get_page_wikitext(title)
    infobox = extract_infobox_fields(wikitext) if wikitext else {}
    entity = None

    # Lazily fetch Wikidata only if needed
    for q in questions:
        all_sources = [q.get("source", "")] + q.get("fallback_sources", [])
        if any(s.startswith("wikidata:") for s in all_sources):
            entity = get_wikidata_entity(title)
            break

    record = {}
    if include_metadata:
        record["_page_title"] = title
        record["_wikipedia_url"] = (
            "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
        )

    extracted_fields: dict = {}

    # ── Deterministic fields ─────────────────────────────────────────────────
    for q in questions:
        field_name = q.get("field_name") or q.get("label", "unknown")
        value = resolve_deterministic_field(title, q, wikitext, infobox, entity)
        record[field_name] = value
        extracted_fields[field_name] = value

    # ── LLM batch extraction (one call for ALL llm fields) ───────────────────
    if llm_extraction_cfg and llm_extraction_cfg.get("fields"):
        llm_fields = llm_extraction_cfg.get("fields", [])
        llm_results = llm_batch_extract(
            title=title,
            page_text=wikitext or "",
            llm_extraction_cfg=llm_extraction_cfg,
            model=model,
        )
        # Log all field answers on a single line
        answers_str = ", ".join(
            f"{f.get('field_name', '?')}={llm_results.get(f.get('field_name', '?'), '?')!r}"
            for f in llm_fields
        )
        print(f"     ✎  {answers_str}", flush=True)

        for f in llm_fields:
            fn = f.get("field_name", "unknown")
            record[fn] = llm_results.get(fn)
            extracted_fields[fn] = llm_results.get(fn)

    # ── Write to cache ───────────────────────────────────────────────────────
    if cache_path is not None:
        cache_data.setdefault("_page_title", title)
        cache_data.setdefault("_wikipedia_url",
            "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"))
        cache_data["_cache_version"] = CACHE_VERSION
        cache_data["_extraction"] = {
            "model": model,
            "fields": extracted_fields,
        }
        _save_cache(cache_path, cache_data)

    return record


# ---------------------------------------------------------------------------
# Main crawl orchestration
# ---------------------------------------------------------------------------

def run_crawl(
    starting_pages: list[str],
    traversal_rules: dict,
    questions: list[dict],
    llm_extraction_cfg: Optional[dict],
    output_path: str,
    model: str,
    cache_dir: Optional[Path] = None,
    project_id: str = "",
) -> None:
    """
    Full crawl: resolve starting pages → filter links → extract → write JSON.

    Crawl flow:
      [1] Resolve starting page titles
      [2] Collect all internal links from starting pages
      [3] Apply deterministic filters, then optional LLM gate filter
      [4] Extract data from accepted pages (deterministic + batch LLM)
      [5] Write structured JSON output
    """
    max_pages = traversal_rules.get("max_secondary_pages", 200)
    delay = traversal_rules.get("request_delay_seconds", 0.5)
    include_starting_pages = traversal_rules.get("include_starting_pages_in_output", False)
    llm_filter_enabled = traversal_rules.get("llm_traversal_filter", {}).get("enabled", False)

    if cache_dir and project_id:
        project_cache = cache_dir / project_id
        project_cache.mkdir(parents=True, exist_ok=True)
        print(f"  💾  Cache directory: {project_cache}", flush=True)

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

    # ── [3+4] Gate filter + extract — pipelined per page ─────────────────────
    #
    # Rather than running all gate checks first and then all extractions,
    # we process each candidate page end-to-end in one pass:
    #   1. Deterministic filter (free, instant)
    #   2. LLM gate check (one YES/NO call, cached)
    #   3. If accepted: extract + write output immediately
    #
    # This means extraction starts on the very first accepted page and the
    # output file is always up-to-date.  Interrupting the script at any point
    # leaves a valid, partially-complete output file and a cache that allows
    # a full resume without repeating any work.

    filter_label = (
        "gate + extract pipeline" if llm_filter_enabled else "filter + extract pipeline"
    )
    print(f"\n[2/2] Running {filter_label}…", flush=True)

    # Build the output schema once (used in every incremental write)
    all_field_defs = []
    for q in questions:
        all_field_defs.append({
            "field_name": q.get("field_name") or q.get("label", "unknown"),
            "label": q.get("label", ""),
            "type": q.get("type", "string"),
            "source": q.get("source", ""),
            "description": q.get("description", ""),
            "llm": False,
        })
    if llm_extraction_cfg:
        for f in llm_extraction_cfg.get("fields", []):
            all_field_defs.append({
                "field_name": f.get("field_name", "unknown"),
                "label": f.get("label", f.get("field_name", "unknown")),
                "type": "string",
                "source": "llm_batch",
                "description": f.get("question", ""),
                "llm": True,
                "options": f.get("options"),
            })

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _write_output(records: list[dict]) -> None:
        """Atomically write the current records to the output JSON file."""
        payload = {
            "meta": {
                "tool": "WikiWhisker",
                "version": "1.0",
                "model": model,
                "starting_pages": starting_titles,
                "total_records": len(records),
                "cache_dir": str(cache_dir / project_id) if (cache_dir and project_id) else None,
                "output_schema": {
                    "schema_version": "1.0",
                    "fields": all_field_defs,
                },
            },
            "records": records,
        }
        tmp = out_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        tmp.replace(out_path)

    records: list[dict] = []
    accepted_count = 0

    # Optionally include starting pages first
    if include_starting_pages:
        for start_title in starting_titles:
            rec = build_page_record(
                start_title, questions, llm_extraction_cfg, model,
                cache_dir=cache_dir, project_id=project_id,
            )
            rec["_is_starting_page"] = True
            records.append(rec)
            _write_output(records)
            time.sleep(delay)

    # Main pipeline loop: one candidate at a time, gate → extract → save
    for link in candidate_links:
        if accepted_count >= max_pages:
            print(
                f"  ⚠  max_secondary_pages ({max_pages}) reached — stopping.",
                flush=True,
            )
            break

        # Step A: cheap deterministic filters (no LLM, no cost)
        if not page_passes_deterministic_filters(link, traversal_rules):
            continue

        # Step B: optional LLM gate filter (cache-aware, one YES/NO call)
        if llm_filter_enabled:
            is_cached = False
            if cache_dir and project_id:
                cp = _get_cache_path(cache_dir, project_id, link)
                is_cached = bool(_load_cache(cp).get("_llm_traversal"))

            if not is_cached:
                print(f"     Gate check: '{link}'…", flush=True)

            passed = page_passes_llm_filter(
                link, traversal_rules, model,
                cache_dir=cache_dir, project_id=project_id,
            )
            if not passed:
                if not is_cached:
                    print(f"     ✗ Page rejected: '{link}'", flush=True)
                time.sleep(delay)
                continue
            if not is_cached:
                print(f"     ✓ Page accepted: '{link}' — extracting…", flush=True)

        # Step C: extract immediately and write to disk
        accepted_count += 1
        rec = build_page_record(
            link, questions, llm_extraction_cfg, model,
            cache_dir=cache_dir, project_id=project_id,
        )
        rec["_is_starting_page"] = False
        records.append(rec)
        _write_output(records)
        print(f"     [{accepted_count}/{max_pages}] saved '{link}' → {output_path}", flush=True)
        time.sleep(delay)

    # ── Final confirmation ────────────────────────────────────────────────────
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
            "EXTRACTION: Two optional sections in the extraction YAML:\n"
            "  questions:       deterministic fields (title/infobox/wikidata) — free\n"
            "  llm_extraction:  ALL LLM fields batched into ONE API call per page\n\n"
            "TRAVERSAL GATE: llm_traversal_filter in the traversal YAML sends ONE\n"
            "YES/NO call per candidate page (multi-criteria prompt is fine).\n\n"
            "RESUMABILITY: Results cached in json_cache/<project_id>/ so interrupted\n"
            "runs can restart without repeating API calls."
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
        help="Path to the YAML file defining link-following rules.",
    )
    parser.add_argument(
        "--extraction-questions",
        metavar="EXTRACTION_YAML",
        required=True,
        help="Path to the YAML file defining what data to extract.",
    )
    parser.add_argument(
        "--output",
        metavar="OUTPUT_JSON",
        default="output.json",
        help="Path for the output JSON file. Default: output.json.",
    )
    parser.add_argument(
        "--model",
        metavar="MODEL",
        default="gpt-4o",
        help=(
            "litellm model string. Default: gpt-4o. "
            "Examples: gpt-4o-mini, claude-3-5-sonnet-20241022, ollama/llama3."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        metavar="CACHE_DIR",
        default="json_cache",
        help=(
            "Root directory for per-project JSON cache. Default: json_cache. "
            "Pass 'none' to disable caching."
        ),
    )
    parser.add_argument(
        "--project-id",
        metavar="PROJECT_ID",
        default="",
        help=(
            "Project sub-directory inside --cache-dir. "
            "Default: derived from --output filename stem."
        ),
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        default=False,
        help="Disable the JSON cache entirely for this run.",
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

    # Support both bare list and dict-with-questions wrapper
    if isinstance(extraction_config, list):
        questions = extraction_config
        llm_extraction_cfg = None
    elif isinstance(extraction_config, dict):
        questions = extraction_config.get("questions", [])
        llm_extraction_cfg = extraction_config.get("llm_extraction") or None
    else:
        print(
            "ERROR: extraction questions YAML must be a list or a dict.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not questions and not llm_extraction_cfg:
        print(
            "ERROR: No extraction questions found. "
            "Add a 'questions:' section and/or a 'llm_extraction:' section.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Resolve cache settings ────────────────────────────────────────────────
    cache_dir: Optional[Path] = None
    project_id: str = ""

    cache_disabled = (
        args.no_cache
        or not args.cache_dir
        or args.cache_dir.lower() in ("none", "false", "0", "")
    )

    if not cache_disabled:
        cache_dir = Path(args.cache_dir)
        project_id = args.project_id or _derive_project_id(args.output)
        print(f"  💾  Project cache: {cache_dir / project_id}", flush=True)
    else:
        print("  ℹ️   Cache disabled — all LLM calls will be made fresh.", flush=True)

    run_crawl(
        starting_pages=args.starting_pages,
        traversal_rules=traversal_rules,
        questions=questions,
        llm_extraction_cfg=llm_extraction_cfg,
        output_path=args.output,
        model=args.model,
        cache_dir=cache_dir,
        project_id=project_id,
    )


if __name__ == "__main__":
    main()
