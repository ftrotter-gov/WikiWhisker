# WikiWhisker — Schema Reference

This document describes the exact format of the two YAML configuration files
and the structured JSON output file that WikiWhisker produces.

---

## 1. Command-line interface

```
python wiki_whisker.py STARTING_PAGE [STARTING_PAGE ...]
    --traversal-rules   TRAVERSAL_YAML
    --extraction-questions EXTRACTION_YAML
    [--output OUTPUT_JSON]
```

| Argument | Required | Description |
|---|---|---|
| `STARTING_PAGE` | ✅ (one or more) | Wikipedia page title or full URL. Multiple values are space-separated. |
| `--traversal-rules` | ✅ | Path to the traversal rules YAML file. |
| `--extraction-questions` | ✅ | Path to the extraction questions YAML file. |
| `--output` | ❌ | Output JSON path. Default: `output.json`. |

**Examples**

```bash
# Single starting page, using example files
python wiki_whisker.py "List of dog breeds" \
    --traversal-rules examples/traversal_rules_example.yaml \
    --extraction-questions examples/extraction_questions_example.yaml \
    --output results/dog_breeds.json

# Multiple starting pages
python wiki_whisker.py "Beta blocker" "ACE inhibitor" \
    --traversal-rules examples/clinical_traversal.yaml \
    --extraction-questions examples/clinical_extraction.yaml \
    --output results/cardiac_drugs.json

# Wikipedia URL as starting page
python wiki_whisker.py "https://en.wikipedia.org/wiki/Aspirin" \
    --traversal-rules my_traversal.yaml \
    --extraction-questions my_extraction.yaml
```

---

## 2. Traversal Rules YAML

Controls **which** linked pages to visit and how many.

### Top-level keys

| Key | Type | Default | Description |
|---|---|---|---|
| `max_secondary_pages` | integer | `200` | Hard cap on accepted secondary pages. |
| `request_delay_seconds` | float | `0.5` | Sleep between Wikipedia API calls. |
| `include_starting_pages_in_output` | bool | `false` | Whether starting pages themselves appear as records in the output. |
| `link_filters` | mapping | `{}` | Sub-keys below. All filters must pass for a page to be accepted. |

### `link_filters` sub-keys

All filter keys are optional. Omitting a key means "no restriction on this dimension."

| Key | Type | Description |
|---|---|---|
| `title_contains` | string or list of strings | Page title must contain **at least one** of the given substrings (case-insensitive). |
| `title_not_contains` | string or list of strings | Page title must **not** contain any of the given substrings (case-insensitive). |
| `title_matches_regex` | string | Page title must match this Python regex (case-insensitive, `re.IGNORECASE`). |
| `category_contains` | string or list of strings | At least one of the page's Wikipedia categories must contain at least one of the given substrings. *(Triggers an extra API call per candidate page.)* |
| `category_not_contains` | string or list of strings | None of the page's categories may contain any of the given substrings. *(Triggers an extra API call per candidate page.)* |

### Minimal example

```yaml
max_secondary_pages: 25
link_filters: {}
```

### Full example

```yaml
max_secondary_pages: 50
request_delay_seconds: 0.5
include_starting_pages_in_output: false

link_filters:
  title_not_contains:
    - "disambiguation"
    - "list of"
  category_contains:
    - "dog breeds"
    - "working dogs"
  category_not_contains:
    - "stub"
```

---

## 3. Extraction Questions YAML

Controls **what** data to extract from each visited page and **how** the output
JSON record is structured.

### Top-level structure

```yaml
# Option A — bare list
- field_name: ...
  ...

# Option B — dict with "questions" key (recommended)
questions:
  - field_name: ...
    ...
```

### Field definition keys

| Key | Required | Type | Description |
|---|---|---|---|
| `field_name` | ✅ | string | Key used in every output JSON record. No spaces recommended. |
| `label` | ❌ | string | Human-readable name; appears in the output JSON schema block. |
| `description` | ❌ | string | Prose explanation of the field. |
| `source` | ✅ | string | Where to read the value (see source syntax below). |
| `fallback_sources` | ❌ | list of strings | Additional sources tried in order when the primary source is empty. |
| `type` | ❌ | string | Output type coercion. One of `string` (default), `list`, `int`, `float`, `bool`. |
| `default` | ❌ | any | Value written when all sources return empty. Default: `null`. |

### Source syntax

| Source string | Description |
|---|---|
| `"title"` | The Wikipedia page title. |
| `"category_list"` | Full list of Wikipedia category names (returns a JSON array). Best used with `type: list`. |
| `"infobox:<field>"` | Named parameter from the page's first Infobox template. E.g. `"infobox:birth_date"`. Field names are case-sensitive and match the infobox template parameter names exactly. |
| `"wikidata:<P-id>"` | Wikidata property on the item linked to this page. E.g. `"wikidata:P31"` (instance of), `"wikidata:P495"` (country of origin). Multiple claim values: first is used for scalar types; all are returned for `type: list`. |

### Type coercion

| `type` value | Behaviour |
|---|---|
| `string` | Casts to string. Lists are joined with `", "`. |
| `list` | Always returns a JSON array. Scalars are wrapped in `[...]`. |
| `int` | Strips commas, parses as integer. |
| `float` | Strips commas, parses as float. |
| `bool` | `"yes"` / `"true"` / `"1"` → `true`; everything else → `false`. |

### Example

```yaml
questions:
  - field_name: drug_name
    label: "Drug Name"
    description: "Generic (INN) name of the drug."
    source: "infobox:drug_name"
    fallback_sources:
      - "infobox:name"
      - "title"
    type: string
    default: null

  - field_name: categories
    label: "Wikipedia Categories"
    source: "category_list"
    type: list
    default: []

  - field_name: wikidata_instance_of
    label: "Wikidata: instance of (P31)"
    source: "wikidata:P31"
    type: list
    default: []
```

---

## 4. Output JSON structure

The output file is a single JSON object with two top-level keys: `meta` and
`records`.

```jsonc
{
  "meta": {
    "tool": "WikiWhisker",
    "version": "1.0",
    "starting_pages": ["Beta blocker", "ACE inhibitor"],
    "total_records": 63,
    "output_schema": {
      "schema_version": "1.0",
      "fields": [
        {
          "field_name": "drug_name",
          "label": "Drug Name",
          "type": "string",
          "source": "infobox:drug_name",
          "description": "Generic (INN) name of the drug."
        }
        // … one entry per extraction question …
      ]
    }
  },
  "records": [
    {
      // ── Metadata fields added automatically ──
      "_page_title":       "Metoprolol",
      "_wikipedia_url":    "https://en.wikipedia.org/wiki/Metoprolol",
      "_is_starting_page": false,

      // ── One key per extraction question ──
      "drug_name":         "Metoprolol",
      "categories":        ["Beta blockers", "Antihypertensive agents"],
      "wikidata_instance_of": ["Q12140"]
    }
    // … one object per accepted page …
  ]
}
```

### Automatic metadata fields in every record

| Field | Type | Description |
|---|---|---|
| `_page_title` | string | Canonical Wikipedia page title. |
| `_wikipedia_url` | string | Full URL to the Wikipedia article. |
| `_is_starting_page` | bool | `true` only when `include_starting_pages_in_output: true` and this record is a starting page. |

---

## 5. Data sources reference

### Useful Wikidata properties

| Property | Label |
|---|---|
| P31 | instance of |
| P279 | subclass of |
| P18 | image |
| P495 | country of origin |
| P571 | inception |
| P267 | ATC code |
| P1843 | taxon common name |
| P1916 | FCI breed number |
| P2175 | medical condition treated |
| P3364 | US FDA NDA number |

### Common infobox field names

Infobox field names vary by template. The most reliable approach is to visit
the Wikipedia page, click **Edit**, and look at the infobox template parameters
directly. Common ones:

| Template | Common fields |
|---|---|
| Infobox drug | `drug_name`, `tradename`, `ATC_code`, `bioavailability`, `elimination_half-life`, `routes_of_administration`, `class` |
| Infobox dog breed | `name`, `country`, `weight`, `height`, `life_span` |
| Infobox person | `name`, `birth_date`, `birth_place`, `occupation`, `nationality` |
| Infobox company | `name`, `founded`, `founder`, `headquarters`, `industry` |
| Infobox medical condition | `name`, `synonyms`, `specialty`, `symptoms`, `causes`, `treatment` |
