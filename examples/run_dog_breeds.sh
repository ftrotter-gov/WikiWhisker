#!/usr/bin/env bash
# =============================================================================
# run_dog_breeds.sh — Generate the dog breeds dataset using WikiWhisker
# =============================================================================
#
# WHAT THIS DOES
# ──────────────
# Crawls Wikipedia starting from "List of dog breeds", visits up to 500
# individual breed pages, and extracts three LLM-answered questions for
# each living (non-extinct) breed:
#
#   health_issues    — Does the breed have significant health problems? (True/False/Unknown)
#   working_dog      — Is it a working dog?                            (True/False/Unknown)
#   family_friendly  — Is it family-friendly?                          (True/False/Unknown)
#
# FILTERING (two-pass)
#   Pass 1 — Deterministic (free):
#     • Rejects pages with "disambiguation", "list of", or "index" in the title.
#     • Accepts only pages whose Wikipedia categories include "dog breeds"
#       OR "working dogs".
#   Pass 2 — LLM (one API call per candidate):
#     • Confirms the page is about a single, currently-existing dog breed.
#     • Rejects extinct breeds, breed groups, kennels, etc.
#
# PREREQUISITES
# ─────────────
#   pip install -r requirements.txt      # includes requests, pyyaml, litellm
#   export OPENAI_API_KEY="sk-..."       # or copy .env.example to .env and fill in
#
# COST ESTIMATE (gpt-4o)
# ──────────────────────
#   • LLM traversal filter:  ~500 API calls  (1 per candidate breed page)
#   • LLM extraction:        ~1,500 API calls (3 fields × ~500 accepted pages)
#   • Total:                 ~2,000 API calls
#   At gpt-4o pricing (~$0.005/call at typical prompt sizes), expect ~$10–$15.
#   Use --model gpt-4o-mini to reduce cost (~10× cheaper, slightly less accurate).
#
# OUTPUT
# ──────
#   results/dog_breeds.json — structured JSON with one record per breed.
#
# USAGE
# ─────
#   # Default — uses gpt-4o:
#   bash examples/run_dog_breeds.sh
#
#   # Use a cheaper/faster model:
#   MODEL=gpt-4o-mini bash examples/run_dog_breeds.sh
#
#   # Use a local Ollama model (no API cost, but slower):
#   MODEL=ollama/llama3 bash examples/run_dog_breeds.sh
#
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STARTING_PAGE="List of dog breeds"

TRAVERSAL_RULES="examples/dog_breeds_traversal.yaml"
EXTRACTION_QUESTIONS="examples/dog_breeds_extraction.yaml"
OUTPUT_FILE="results/dog_breeds.json"

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
# This ensures the correct Python with all dependencies (requests, pyyaml,
# litellm) is used, regardless of the system Python.
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
# On networks that use TLS inspection (e.g. Zscaler), Python's httpx library
# (used by litellm/openai) will fail with SSL certificate errors because the
# proxy presents its own CA certificate, which is not in Python's default
# certifi bundle.
#
# This block automatically appends the live TLS certificate chain from
# api.openai.com to certifi's CA bundle — picking up whatever CA the proxy
# is using — so that httpx can verify the connection.
#
# This is safe: we are adding a CA cert that the OS/network already trusts
# (it's what curl and browsers use), and we only append it if it's not
# already present (checked by bundle size).
#
# If you are NOT behind a proxy, this block is a no-op (the chain will
# already be trusted and the append is harmless).
if command -v openssl &>/dev/null && command -v python &>/dev/null; then
    CERTIFI_BUNDLE=$(python -c "import certifi; print(certifi.where())" 2>/dev/null || true)
    if [[ -n "$CERTIFI_BUNDLE" && -f "$CERTIFI_BUNDLE" ]]; then
        BEFORE_SIZE=$(wc -c < "$CERTIFI_BUNDLE")
        # Extract the root CA (last cert) from the live api.openai.com chain
        # and append it to the certifi bundle so httpx can verify the connection.
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
    # Export only lines that look like VAR=value (skip comments and blanks)
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
echo " WikiWhisker — Dog Breeds Dataset"
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
