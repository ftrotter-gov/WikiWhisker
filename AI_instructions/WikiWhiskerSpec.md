# WikiWhisker — Full Project Specification

> **Status**: Living specification. Supersedes `WikiWhiskerInitial.md`.
> All implementation decisions should be grounded in this document.

---

## 1. Overview

WikiWhisker is a Python command-line tool that:

1. Accepts one or more Wikipedia page URLs or titles as **starting points**.
2. Follows links from those pages to **secondary pages**, governed by rules defined in a YAML configuration file.
3. Extracts structured data from each secondary page, governed by a second YAML configuration file.
4. Uses a **large language model (LLM)** — accessed via [litellm](https://github.com/BerriAI/litellm) so the model can be swapped without code changes — to interpret traversal rules and answer extraction questions against raw Wikipedia content.
5. Writes the results as a **structured JSON file** whose schema is defined inside the extraction questions YAML.

WikiWhisker is not a general-purpose web crawler. It is a narrow, purposeful data-extraction tool that crawls exactly **two levels deep** (starting pages → secondary pages) and stops.

---

## 2. Data Sources

| Source | What it provides | How it is accessed |
|---|---|---|
| Wikipedia API | Page wikitext, infobox fields, internal links, categories | `https://en.wikipedia.org/w/api.php` — JSON API, no HTML scraping |
| Wikidata API | Structured properties (P-numbers) linked to Wikipedia articles | `https://www.wikidata.org/wiki/Special:EntityData/<QID>.json` |
| LLM (ChatGPT via litellm) | Interprets traversal rules; answers extraction questions in natural language against page content | OpenAI-compatible API, called through `litellm` |

WikiWhisker **never** scrapes Wikipedia HTML directly and **never** renders JavaScript.

---

## 3. Command-Line Interface

```
python wiki_whisker.py  STARTING_PAGE [STARTING_PAGE ...]
                        --traversal-rules   config/traversal.yaml
                        --extraction-questions config/extraction.yaml
                        [--output results/output.json]
                        [--model  gpt-4o]
```

### Arguments

| Argument | Required | Description |
|---|---|---|
| `STARTING_PAGE` | ✅ one or more | Wikipedia page title (e.g. `"Beta blocker"`) or full Wikipedia URL (e.g. `https://en.wikipedia.org/wiki/Aspirin`). Multiple values are space-separated. |
| `--traversal-rules` | ✅ | Path to the **traversal rules YAML** file. Lives under `config/` by convention. |
| `--extraction-questions` | ✅ | Path to the **extraction questions YAML** file. Lives under `config/` by convention. |
| `--output` | ❌ | Path for the output JSON file. Default: `output.json`. Recommended to put under `results/`. |
| `--model` | ❌ | litellm model string to use for LLM calls. Default: `gpt-4o`. Examples: `gpt-4o`, `gpt-4o-mini`, `claude-3-5-sonnet-20241022`, `ollama/llama3`. |

### Example invocations

```bash
# Dog breed taxonomy
python wiki_whisker.py "List of dog breeds" \
    --traversal-rules config/dog_breeds_traversal.yaml \
    --extraction-questions config/dog_breeds_extraction.yaml \
    --output results/dog_breeds.json

# Clinical drug taxonomy, multiple starting pages
python wiki_whisker.py "Beta blocker" "ACE inhibitor" \
    --traversal-rules config/clinical_traversal.yaml \
    --extraction-questions config/clinical_extraction.yaml \
    --output results/cardiac_drugs.json \
    --model gpt-4o-mini

# Wikipedia URL as starting page
python wiki_whisker.py "https://en.wikipedia.org/wiki/Aspirin" \
    --traversal-rules config/my_traversal.yaml \
    --extraction-questions config/my_extraction.yaml
```

---

## 4. Configuration Files

All configuration files live under a **`config/`** subdirectory in the project.
The `examples/` directory contains annotated reference copies that demonstrate
every available option.

```
config/
    dog_breeds_traversal.yaml      ← your traversal rules
    dog_breeds_extraction.yaml     ← your extraction questions
    clinical_traversal.yaml
    clinical_extraction.yaml
    ...

examples/
    traversal_rules_example.yaml   ← fully annotated reference
    extraction_questions_example.yaml
    clinical_traversal.yaml
    clinical_extraction.yaml
```

---

## 5. Traversal Rules YAML

**Purpose**: Tell WikiWhisker *which* pages linked from the starting page(s) to
visit as secondary pages.

**Location**: `config/<name>_traversal.yaml`

**Reference file**: `examples/traversal_rules_example.yaml`

### 5.1 Top-level keys

| Key | Type | Default | Description |
|---|---|---|---|
| `max_secondary_pages` | integer | `200` | Hard cap on accepted secondary pages. Crawl stops when this many pages have been accepted, even if more candidate links remain. |
| `request_delay_seconds` | float | `0.5` | Seconds to sleep between Wikipedia API calls. Respect the API. |
| `include_starting_pages_in_output` | bool | `false` | If true, the starting pages themselves are also fetched, extracted, and included in the output records. |
| `link_filters` | mapping | `{}` | Deterministic pre-filters (see §5.2). Applied before the LLM filter. |
| `llm_traversal_filter` | mapping | `{}` | LLM-based traversal filter (see §5.3). Applied after `link_filters`. |

### 5.2 `link_filters` — deterministic pre-filters

These filters run cheaply and quickly, without calling the LLM, to reduce the
candidate set before the more expensive LLM filter runs.

All active filters must pass for a page to proceed to the LLM filter step.
Omitting a key means "no restriction on this dimension."

| Key | Type | Description |
|---|---|---|
| `title_contains` | string or list[string] | Page title must contain **at least one** of the given substrings (case-insensitive). |
| `title_not_contains` | string or list[string] | Page title must **not** contain any of the given substrings (case-insensitive). |
| `title_matches_regex` | string | Page title must match this Python regex (case-insensitive). |
| `category_contains` | string or list[string] | At least one Wikipedia category of the page must contain at least one of the given substrings. *(Triggers one extra API call per candidate.)* |
| `category_not_contains` | string or list[string] | None of the page's categories may contain any of the given substrings. *(Triggers one extra API call per candidate.)* |

### 5.3 `llm_traversal_filter` — LLM-based filter

After deterministic pre-filters pass, the LLM is optionally asked to decide
whether a given page should be included.

| Key | Type | Required | Description |
|---|---|---|---|
| `enabled` | bool | ❌ | Set to `true` to activate LLM-based filtering. Default: `false`. |
| `prompt` | string | ✅ if enabled | Natural language instruction telling the LLM what kind of pages to include. The page title and a brief summary (from the Wikipedia API extract) are automatically injected. The LLM must answer `YES` or `NO`. |
| `include_page_summary` | bool | ❌ | If `true`, the Wikipedia intro-paragraph text is passed to the LLM along with the title. Default: `true`. |
| `include_categories` | bool | ❌ | If `true`, the page's category list is passed to the LLM. Default: `false`. |

**Example `llm_traversal_filter`**:

```yaml
llm_traversal_filter:
  enabled: true
  prompt: >
    You are deciding whether a Wikipedia page is about a specific dog breed
    (not a list, category, or disambiguation page).
    Answer YES if the page is about a single, named dog breed.
    Answer NO otherwise.
  include_page_summary: true
  include_categories: true
```

The LLM is called with a system prompt that instructs it to respond with a
single word: `YES` or `NO`. Any other response is treated as `NO`.

---

## 6. Extraction Questions YAML

**Purpose**: Define *what* data to extract from each accepted secondary page
and *how* the output JSON record should be structured.

**Location**: `config/<name>_extraction.yaml`

**Reference file**: `examples/extraction_questions_example.yaml`

### 6.1 Top-level structure

```yaml
questions:
  - field_name: ...
    ...
  - field_name: ...
    ...
```

The top-level key is `questions`, whose value is a list of field definitions.
Each field definition describes one piece of data to extract and one key in the
output JSON record.

### 6.2 Field definition keys

| Key | Required | Type | Description |
|---|---|---|---|
| `field_name` | ✅ | string | The key used for this value in every output JSON record. No spaces recommended. |
| `label` | ❌ | string | Human-readable display name. Appears in the output JSON `meta.output_schema` block. |
| `description` | ❌ | string | Prose description of what this field represents. Used as context in the LLM prompt when `source` is `"llm"`. |
| `source` | ✅ | string | Where to read the value. See §6.3. |
| `fallback_sources` | ❌ | list[string] | Additional sources tried in order when the primary source returns empty/null. Same syntax as `source`. |
| `type` | ❌ | string | Output type. One of `string` (default), `list`, `int`, `float`, `bool`. See §6.4. |
| `default` | ❌ | any | Value to write when all sources return empty. Default: `null`. |
| `llm_prompt` | ❌ | string | When `source` is `"llm"`, this is the natural language question asked of the LLM about the page content. Required if `source: "llm"`. |
| `llm_options` | ❌ | list[string] | If provided, the LLM is constrained to answer only with one of these values (multiple-choice mode). |

### 6.3 Source types

| Source string | Description |
|---|---|
| `"title"` | The Wikipedia page title itself. |
| `"category_list"` | The full list of Wikipedia category names for the page (returns a JSON array). Best used with `type: list`. |
| `"infobox:<field_name>"` | A named parameter from the page's first Infobox template. E.g. `"infobox:birth_date"`. Field names are case-sensitive and must match the infobox template parameter name exactly. |
| `"wikidata:<P-id>"` | A Wikidata property on the item linked to this Wikipedia page. E.g. `"wikidata:P31"` (instance of). If the property has multiple values, the first is used for scalar types; all values are returned for `type: list`. |
| `"llm"` | The value is obtained by asking the LLM a question (specified in `llm_prompt`) about the page content. The LLM receives the wikitext or a cleaned text version of the page. |

### 6.4 Type coercion

| `type` | Behaviour |
|---|---|
| `string` | Cast to string. Lists are joined with `", "`. |
| `list` | Always return a JSON array. Scalars are wrapped in `[...]`. |
| `int` | Strip commas, parse as integer. |
| `float` | Strip commas, parse as float. |
| `bool` | `"yes"` / `"true"` / `"1"` → `true`; everything else → `false`. |

### 6.5 LLM extraction example

```yaml
questions:
  - field_name: drug_class
    label: "Drug Class"
    description: "The pharmacological class of the drug."
    source: "llm"
    llm_prompt: >
      Based on the Wikipedia article text provided, what is the pharmacological
      class of this drug? Answer with a short phrase (e.g. 'Beta blocker',
      'ACE inhibitor', 'Calcium channel blocker'). If you cannot determine it,
      answer 'Unknown'.
    type: string
    default: "Unknown"

  - field_name: is_approved_in_usa
    label: "Approved in USA"
    description: "Whether the drug is approved by the US FDA."
    source: "llm"
    llm_prompt: >
      Based on the Wikipedia article, is this drug approved for use in the
      United States by the FDA?
    llm_options:
      - "Yes"
      - "No"
      - "Unknown"
    type: string
    default: "Unknown"
```

---

## 7. LLM Integration

### 7.1 Library

All LLM calls use **[litellm](https://github.com/BerriAI/litellm)**, which
provides a unified interface to OpenAI, Anthropic, Cohere, Ollama, and other
providers. The default model is `gpt-4o` (OpenAI ChatGPT). The model is
overridable via the `--model` CLI flag without any code changes.

### 7.2 API key configuration

API keys are read from environment variables, exactly as litellm expects:

| Model family | Environment variable |
|---|---|
| OpenAI (gpt-4o, gpt-4o-mini, …) | `OPENAI_API_KEY` |
| Anthropic (claude-*) | `ANTHROPIC_API_KEY` |
| Local (ollama/*) | No key needed |

The tool does not manage API keys itself. The operator is responsible for
setting the appropriate environment variable before running the tool.

### 7.3 When the LLM is called

The LLM is called in **two distinct situations**:

#### A. Traversal filtering (optional)

When `llm_traversal_filter.enabled: true` in the traversal rules YAML, the
LLM is called once per candidate secondary page (after deterministic
pre-filters pass) to decide whether the page should be included.

**LLM input**: The page title, optionally the Wikipedia intro-paragraph
summary, and optionally the category list.

**LLM output**: A single word — `YES` (include) or `NO` (exclude).

#### B. Data extraction

When a field definition in the extraction questions YAML has `source: "llm"`,
the LLM is called once per page per such field to answer the `llm_prompt`
question.

**LLM input**: The cleaned wikitext of the page (or a truncated version if
the page is very long), plus the question from `llm_prompt` and any
`llm_options` constraints.

**LLM output**: The answer to the question. If `llm_options` is set, the LLM
is instructed to respond with exactly one of the listed options.

### 7.4 LLM call structure

All LLM calls follow this pattern:

```
System:  You are a precise data-extraction assistant. [task-specific instruction]
User:    [page content + question]
```

The system prompt differs between traversal filtering (instructing YES/NO)
and data extraction (instructing a specific answer format).

### 7.5 Non-LLM fields

Fields with `source` values of `"title"`, `"category_list"`, `"infobox:*"`,
or `"wikidata:*"` are resolved **without any LLM call**, by direct parsing of
Wikipedia API responses. These are always cheaper and faster than LLM calls
and should be preferred where the data is structured enough to be parsed
deterministically.

---

## 8. Crawl Flow

```
START
  │
  ▼
[1] Resolve starting page title(s) from CLI arguments
  │
  ▼
[2] For each starting page:
      → Wikipedia API: fetch all internal links (namespace 0)
  │
  ▼
[3] For each candidate linked page:
    [3a] Apply deterministic link_filters (title, category)
          → FAIL → skip page
          → PASS ↓
    [3b] If llm_traversal_filter.enabled:
              → fetch page summary from Wikipedia API
              → call LLM with traversal prompt
              → LLM returns YES/NO
              → NO → skip page
              → YES ↓
    [3c] Accept page (add to work list)
         Stop when max_secondary_pages reached
  │
  ▼
[4] For each accepted page:
    [4a] Wikipedia API: fetch full wikitext
    [4b] Parse infobox fields from wikitext
    [4c] If any extraction question uses wikidata:*:
              → Wikipedia API: get Wikidata QID
              → Wikidata API: fetch entity JSON
    [4d] For each extraction question:
              source == "title"          → use page title
              source == "category_list"  → Wikipedia API: fetch categories
              source == "infobox:*"      → parse from wikitext
              source == "wikidata:*"     → read from entity JSON
              source == "llm"            → call LLM with llm_prompt + page text
              (try fallback_sources if primary returns empty)
    [4e] Build record dict
  │
  ▼
[5] Build output JSON:
      { "meta": { schema, starting_pages, total_records, … },
        "records": [ … one dict per accepted page … ] }
  │
  ▼
[6] Write JSON to --output path
```

---

## 9. Output JSON Structure

The output is a single JSON file with two top-level keys: `meta` and `records`.

```jsonc
{
  "meta": {
    "tool": "WikiWhisker",
    "version": "1.0",
    "model": "gpt-4o",
    "starting_pages": ["Beta blocker", "ACE inhibitor"],
    "total_records": 63,
    "output_schema": {
      "schema_version": "1.0",
      "fields": [
        {
          "field_name": "drug_class",
          "label": "Drug Class",
          "type": "string",
          "source": "llm",
          "description": "The pharmacological class of the drug."
        }
        // … one entry per extraction question …
      ]
    }
  },
  "records": [
    {
      // ── Metadata fields added automatically to every record ──
      "_page_title":       "Metoprolol",
      "_wikipedia_url":    "https://en.wikipedia.org/wiki/Metoprolol",
      "_is_starting_page": false,

      // ── One key per extraction question ──
      "drug_name":         "Metoprolol",
      "drug_class":        "Beta blocker",
      "categories":        ["Beta blockers", "Antihypertensive agents"],
      "is_approved_in_usa": "Yes"
    }
    // … one record per accepted secondary page …
  ]
}
```

### Automatic metadata fields (always present in every record)

| Field | Type | Description |
|---|---|---|
| `_page_title` | string | Canonical Wikipedia page title. |
| `_wikipedia_url` | string | Full URL to the Wikipedia article. |
| `_is_starting_page` | bool | `true` only when `include_starting_pages_in_output: true` and this record is a starting page. |

---

## 10. Project File Structure

```
WikiWhisker/
│
├── wiki_whisker.py                 ← main entry point
├── requirements.txt                ← Python dependencies
├── .env.example                    ← example environment variable file (not committed)
│
├── config/                         ← user's YAML configuration files (gitignored or project-specific)
│   ├── my_traversal.yaml
│   └── my_extraction.yaml
│
├── examples/                       ← annotated reference YAML files
│   ├── traversal_rules_example.yaml
│   ├── extraction_questions_example.yaml
│   ├── clinical_traversal.yaml
│   └── clinical_extraction.yaml
│
├── results/                        ← output JSON files (gitignored)
│   └── output.json
│
├── docs/
│   └── schema_reference.md         ← full schema reference for both YAML files
│
└── AI_instructions/
    ├── WikiWhiskerInitial.md       ← original rough spec (superseded)
    └── WikiWhiskerSpec.md          ← this document (current authoritative spec)
```

---

## 11. Dependencies

| Package | Purpose |
|---|---|
| `requests` | Wikipedia and Wikidata API calls |
| `PyYAML` | Parsing traversal and extraction YAML config files |
| `litellm` | Unified LLM API (OpenAI / Anthropic / Ollama / etc.) |

Install with:
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 12. What the Tool Does NOT Do

- Does **not** browse the web in any general-purpose sense
- Does **not** follow links beyond secondary pages (exactly two levels deep)
- Does **not** render JavaScript or act as a browser
- Does **not** scrape Wikipedia HTML directly (uses the JSON API only)
- Does **not** perform open-ended research or multi-step reasoning beyond answering the questions defined in the extraction YAML
- Does **not** hallucinate data: LLM answers are grounded in the Wikipedia page text passed to it; if the answer is not present, the field returns the `default` value

---

## 13. Design Principles

1. **Configuration over code**: All crawl behavior is controlled by the two YAML files. Changing targets means editing YAML, not Python.
2. **LLM as interpreter, not driver**: The LLM is used for tasks that require natural language understanding (is this page about X? what is the drug class?). Deterministic structured tasks (fetch wikitext, parse infobox, resolve Wikidata property) use direct API calls.
3. **Model portability**: `litellm` ensures the tool is not locked to any single LLM provider. Operators can swap models via `--model` without touching source code.
4. **Structured, schema-embedded output**: The output JSON carries its own schema in `meta.output_schema`, derived from the extraction questions YAML, so consumers always know the shape of the data without consulting a separate document.
5. **Politeness**: The `request_delay_seconds` setting enforces a sleep between API calls. The Wikipedia API User-Agent header identifies the tool.
