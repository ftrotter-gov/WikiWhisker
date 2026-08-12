#!/usr/bin/env bash
# =============================================================================
# run_medical_specialties.sh — Generate the medical specialty titles dataset
# =============================================================================
#
# WHAT THIS DOES
# ──────────────
# Crawls Wikipedia starting from "Medical specialty", visits individual
# medical/clinical specialty pages, and extracts two key fields for each:
#
#   specialty_name      — The name of the specialty (e.g. "Neurology")
#   specialist_title    — The practitioner title for that specialty
#                         (e.g. "Neurologist", "Cardiologist", "Pediatrician")
#
# The specialist title is the word or phrase used for a clinician who
# practises that specialty.  Because there is no universal linguistic pattern
# (compare "psychiatry → psychiatrist" vs "pediatrics → pediatrician"), this
# study harvests the titles directly from Wikipedia's infobox "Specialist"
# field and confirms them via an LLM reading the article text.
#
# Only pages that:
#   • belong to the Wikipedia "Medical specialties" category, AND
#   • explicitly name a recognised practitioner title
# are included in the output.
#
# FILTERING (two-pass)
#   Pass 1 — Deterministic (free):
#     • Rejects disambiguation, list, index, history, and outline pages.
#     • Accepts only pages whose Wikipedia categories include
#       "Medical specialties" or "Clinical specialties".
#   Pass 2 — LLM (one API call per candidate):
#     • Confirms the page is about a single, specific medical specialty.
#     • Confirms the page explicitly names a practitioner title.
#     • Rejects pages that only use generic descriptions like
#       "physician who does X" instead of a named specialist title.
#
# EXTRACTION
#   • specialist_title    — from infobox "specialist" field (free, no LLM)
#   • specialist_title_llm — confirmed / extracted by LLM from article text
#                            (one batch API call per accepted page)
#
# PREREQUISITES
# ─────────────
#   pip install -r requirements.txt      # includes requests, pyyaml, litellm
#   export OPENAI_API_KEY="sk-..."       # or copy .env.example to .env and fill in
#
# COST ESTIMATE (gpt-4o, ~50–80 specialty pages)
# ───────────────────────────────────────────────
#   • LLM traversal filter:  ~50–80 API calls  (1 per candidate specialty page)
#   • LLM extraction:        ~50–80 API calls  (1 batch call per accepted page)
#   • Total:                 ~100–160 API calls
#   At gpt-4o pricing this is well under $1.
#   Use --model gpt-4o-mini to reduce cost further.
#
# OUTPUT
# ──────
#   results/medical_specialty_titles.json — one record per specialty, with:
#     • specialty_name       (string)
#     • specialist_title     (string | null — from infobox)
#     • specialist_title_llm (string — confirmed by LLM)
#
# USAGE
# ─────
#   # Default — uses gpt-4o:
#   bash examples/run_medical_specialties.sh
#
#   # Use a cheaper/faster model:
#   MODEL=gpt-4o-mini bash examples/run_medical_specialties.sh
#
#   # Use a local Ollama model (no API cost, but slower):
#   MODEL=ollama/llama3 bash examples/run_medical_specialties.sh
#
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STARTING_PAGE="Medical specialty"

TRAVERSAL_RULES="examples/medical_specialties_traversal.yaml"
EXTRACTION_QUESTIONS="examples/medical_specialties_extraction.yaml"
OUTPUT_FILE="results/medical_specialty_titles.json"

# Allow MODEL to be overridden from the environment (default: gpt-4o)
MODEL="${MODEL:-gpt-4o}"

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

# Ensure we are running from the repo root
if [[ ! -f "wiki_whisker.py" ]]; then
    echo "ERROR: This script must be run from the WikiWhisker repository root." >&2
    echo "       cd to the directory containing wiki_whisker.py and try again." >&2
    exit 1
fi

# Activate the project virtual environment (.venv) if it exists.
if [[ -f ".venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
    echo "Activated virtual environment: .venv"
elif [[ -f "venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
    echo "Activated virtual environment: venv"
else
    echo "WARNING: No .venv or venv directory found." >&2
    echo "  Run:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
fi

# ---------------------------------------------------------------------------
# SSL certificate fix for corporate/Zscaler proxy environments
# ---------------------------------------------------------------------------
if command -v openssl &>/dev/null && command -v python &>/dev/null; then
    CERTIFI_BUNDLE=$(python -c "import certifi; print(certifi.where())" 2>/dev/null || true)
    if [[ -n "$CERTIFI_BUNDLE" && -f "$CERTIFI_BUNDLE" ]]; then
        BEFORE_SIZE=$(wc -c < "$CERTIFI_BUNDLE")
        ZSCALER_CERT=$(echo | openssl s_client -connect api.openai.com:443 -showcerts 2>/dev/null \
            | awk '/BEGIN CERTIFICATE/,/END CERTIFICATE/{print}' \
            | python -c "
import sys
certs = sys.stdin.read().split('-----END CERTIFICATE-----')
for c in reversed(certs):
    c = c.strip()
    if c:
        print(c + '\n-----END CERTIFICATE-----\n')
        break
" 2>/dev/null || true)
        if [[ -n "$ZSCALER_CERT" ]]; then
            echo "$ZSCALER_CERT" >> "$CERTIFI_BUNDLE"
            AFTER_SIZE=$(wc -c < "$CERTIFI_BUNDLE")
            if [[ "$AFTER_SIZE" -gt "$BEFORE_SIZE" ]]; then
                echo "SSL: appended proxy/corporate CA cert to certifi bundle (for TLS inspection proxy)"
            fi
        fi
    fi
fi

# Load .env if it exists (for OPENAI_API_KEY etc.)
if [[ -f ".env" ]]; then
    set -o allexport
    # shellcheck disable=SC1091
    source .env
    set +o allexport
    echo "Loaded environment variables from .env"
fi

# Check that OPENAI_API_KEY is set (required for LLM calls)
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "" >&2
    echo "WARNING: OPENAI_API_KEY is not set." >&2
    echo "  If you are using a non-OpenAI model (e.g. Ollama), this may be fine." >&2
    echo "  Otherwise, set OPENAI_API_KEY in your environment or in a .env file." >&2
    echo "  See .env.example for the required format." >&2
    echo "" >&2
fi

# Ensure the results/ directory exists
mkdir -p results

# ---------------------------------------------------------------------------
# Run WikiWhisker
# ---------------------------------------------------------------------------

echo "============================================================"
echo " WikiWhisker — Medical Specialty Titles Dataset"
echo "============================================================"
echo " Starting page : ${STARTING_PAGE}"
echo " Traversal     : ${TRAVERSAL_RULES}"
echo " Extraction    : ${EXTRACTION_QUESTIONS}"
echo " Output        : ${OUTPUT_FILE}"
echo " Model         : ${MODEL}"
echo "============================================================"
echo ""

python wiki_whisker.py \
    "${STARTING_PAGE}" \
    --traversal-rules   "${TRAVERSAL_RULES}" \
    --extraction-questions "${EXTRACTION_QUESTIONS}" \
    --output            "${OUTPUT_FILE}" \
    --model             "${MODEL}"

echo ""
echo "============================================================"
echo " Done!  Results written to: ${OUTPUT_FILE}"
echo "============================================================"
