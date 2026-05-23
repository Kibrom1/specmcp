#!/usr/bin/env bash
# =============================================================================
# validate-local.sh — run the three GA validation checks that require
# network access or manual judgment.
#
# Usage:
#   cd <repo-root>
#   bash scripts/validate-local.sh [--skip-fetch] [--skip-llm]
#
# Flags:
#   --skip-fetch   Skip corpus fetch (use if you already ran it)
#   --skip-llm     Skip the LLM-usability prompt (run silently in CI)
#
# No global pip install needed — uses uv (preferred) or the repo's .venv.
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SKIP_FETCH=0
SKIP_LLM=0
for arg in "$@"; do
  case $arg in
    --skip-fetch) SKIP_FETCH=1 ;;
    --skip-llm)   SKIP_LLM=1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Resolve the Python / runner to use — prefer uv, fall back to .venv
# ---------------------------------------------------------------------------

# We set PYTHON and RUN so every command goes through the right environment.
# uv run: no activation needed, uv handles the venv automatically.
# .venv:  activate it so `python` and `pytest` resolve correctly.

if command -v uv &>/dev/null; then
  USE_UV=1
  # Ensure deps are synced (fast no-op if already up to date)
  echo "Using uv — syncing dev dependencies..."
  uv sync --dev --quiet
  PYTHON="uv run python"
  RUN="uv run"
else
  USE_UV=0
  VENV="$REPO_ROOT/.venv"
  if [[ ! -d "$VENV" ]]; then
    echo "No uv found and no .venv present. Creating .venv and installing deps..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -e ".[dev]" --quiet
  fi
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  PYTHON="python"
  RUN=""
fi

# Colour helpers
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'

pass()  { echo -e "  ${GREEN}✓ PASS${RESET}  $1"; }
fail()  { echo -e "  ${RED}✗ FAIL${RESET}  $1"; FAILURES=$((FAILURES + 1)); }
info()  { echo -e "  ${DIM}→${RESET}  $1"; }
warn()  { echo -e "  ${YELLOW}⚠ NOTE${RESET}  $1"; }
header(){ echo -e "\n${BOLD}── $1 ──────────────────────────────────${RESET}"; }

FAILURES=0

# =============================================================================
# 0. Sanity check: specmcp is importable
# =============================================================================
header "Environment"
if $PYTHON -c "import specmcp" 2>/dev/null; then
  SPECMCP_VERSION=$($PYTHON -c "import specmcp; print(specmcp.__version__)")
  pass "specmcp importable (version $SPECMCP_VERSION)"
else
  fail "specmcp not importable — check that the install succeeded above"
  echo -e "\n${RED}Cannot continue without specmcp installed.${RESET}"
  exit 1
fi

# =============================================================================
# 1. Fetch non-vendored corpus specs
# =============================================================================
header "Corpus fetch (Stripe, GitHub, DigitalOcean, Slack)"

CACHE_DIR="test-corpus/.cache"

if [[ $SKIP_FETCH -eq 1 ]]; then
  warn "Skipping fetch (--skip-fetch). Assuming $CACHE_DIR is populated."
else
  echo -e "  ${DIM}Running: $PYTHON scripts/fetch_corpus.py${RESET}"
  if $PYTHON scripts/fetch_corpus.py; then
    pass "All non-vendored specs fetched"
  else
    fail "fetch_corpus.py failed — check network access and try again"
    echo -e "\n  ${DIM}Tip: run manually to see which specs failed:${RESET}"
    echo -e "  ${DIM}  python scripts/fetch_corpus.py${RESET}\n"
  fi
fi

# List what's now in the cache
if [[ -d "$CACHE_DIR" ]]; then
  CACHED=$(ls "$CACHE_DIR" 2>/dev/null | wc -l | tr -d ' ')
  info "$CACHED spec file(s) in $CACHE_DIR"
  ls "$CACHE_DIR" 2>/dev/null | while read f; do
    SIZE=$(du -sh "$CACHE_DIR/$f" 2>/dev/null | cut -f1)
    echo -e "    ${DIM}$f  ($SIZE)${RESET}"
  done
fi

# =============================================================================
# 2. Full corpus integration tests (≥80% conversion rate)
# =============================================================================
header "Corpus integration tests (≥80% conversion rate gate)"

echo -e "  ${DIM}Running: SPECMCP_RUN_CORPUS=1 ${RUN} pytest tests/integration -v -k corpus${RESET}\n"

if SPECMCP_RUN_CORPUS=1 $RUN pytest tests/integration -v -k corpus \
     --tb=short 2>&1 | tee /tmp/specmcp-corpus-results.txt; then
  pass "Corpus integration tests passed"
else
  fail "One or more corpus tests failed — see /tmp/specmcp-corpus-results.txt"
fi

# Extract and print conversion rates from test output
echo ""
grep -E "conversion_rate|success_rate|PASSED|FAILED|SKIPPED|operations" \
  /tmp/specmcp-corpus-results.txt 2>/dev/null | head -30 || true

# =============================================================================
# 3. Stripe startup benchmark (≤5s gate)
# =============================================================================
header "Stripe startup benchmark (≤5s gate)"

STRIPE_SPEC="$CACHE_DIR/stripe.json"

if [[ ! -f "$STRIPE_SPEC" ]]; then
  warn "Stripe spec not found at $STRIPE_SPEC — skipping benchmark"
  warn "Run 'python scripts/fetch_corpus.py' first, then re-run this script"
else
  echo -e "  ${DIM}Timing full pipeline (Load → Normalize → Simplify → Expose) on Stripe spec...${RESET}"

  $PYTHON - <<EOF
import sys, time
sys.path.insert(0, "src")

from specmcp.core.load import load_spec
from specmcp.core.normalize import normalize
from specmcp.core.simplify import simplify
from specmcp.core.expose import ToolRegistry

spec = "$STRIPE_SPEC"
LIMIT_S = 5.0

# Warm-up (import cache)
_, resolved = load_spec(spec)
ops = normalize(resolved)
simp = simplify(ops)
ToolRegistry.build(simp)

# 3 timed runs
times = []
for i in range(3):
    t0 = time.perf_counter()
    _, resolved = load_spec(spec)
    ops = normalize(resolved)
    simp = simplify(ops)
    reg = ToolRegistry.build(simp)
    elapsed = time.perf_counter() - t0
    times.append(elapsed)
    print(f"  Run {i+1}: {elapsed*1000:.0f}ms  ({len(ops)} operations, {len(reg.tools)} tools)")

avg = sum(times) / len(times)
worst = max(times)
print(f"\n  Avg:   {avg*1000:.0f}ms")
print(f"  Worst: {worst*1000:.0f}ms")

if worst <= LIMIT_S:
    print(f"\n  STRIPE_PASS  worst-case {worst:.2f}s ≤ {LIMIT_S}s gate")
    sys.exit(0)
else:
    print(f"\n  STRIPE_FAIL  worst-case {worst:.2f}s > {LIMIT_S}s gate")
    print("  Mitigations: lazy imports, content-hash spec cache, parallel Simplify")
    sys.exit(1)
EOF

  STRIPE_EXIT=$?
  if [[ $STRIPE_EXIT -eq 0 ]]; then
    pass "Stripe startup ≤5s"
  else
    fail "Stripe startup exceeded 5s — see mitigations in design doc §9"
    FAILURES=$((FAILURES + 1))
  fi
fi

# =============================================================================
# 4. LLM-usability eval (manual — prints instructions)
# =============================================================================
header "LLM-usability eval (manual check)"

if [[ $SKIP_LLM -eq 1 ]]; then
  warn "Skipping LLM eval (--skip-llm)"
else
  echo ""
  echo -e "  ${BOLD}This check is manual. Do the following:${RESET}"
  echo ""
  echo -e "  ${BOLD}Step 1${RESET} — Generate inspect output for the top 3 APIs:"
  echo ""
  echo "    $RUN python -m specmcp inspect --spec test-corpus/petstore.json --json \\"
  echo "      > /tmp/inspect-petstore.json"
  echo ""
  if [[ -f "$CACHE_DIR/stripe.json" ]]; then
    echo "    $RUN python -m specmcp inspect --spec $CACHE_DIR/stripe.json --json \\"
    echo "      > /tmp/inspect-stripe.json"
  else
    echo "    # (Stripe not fetched yet — run after fetch_corpus.py)"
  fi
  if [[ -f "$CACHE_DIR/github.json" ]]; then
    echo ""
    echo "    $RUN python -m specmcp inspect --spec $CACHE_DIR/github.json --json \\"
    echo "      > /tmp/inspect-github.json"
  fi
  echo ""
  echo -e "  ${BOLD}Step 2${RESET} — For each spec, paste the inspect JSON into Claude and ask:"
  echo ""
  echo '    "These are MCP tool definitions. Please call the tool that lists'
  echo '     resources. What arguments do you need? Which tool would you call'
  echo '     to create a new resource? Show me the exact arguments you would pass."'
  echo ""
  echo -e "  ${BOLD}Pass criteria:${RESET}"
  echo "    ✓ Claude identifies the correct tool without hallucinating names"
  echo "    ✓ Claude uses argument names from the schema (not invented ones)"
  echo "    ✓ Claude doesn't ask for clarification on obvious required params"
  echo "    ✓ No 'I'm not sure what arguments to pass' for simple operations"
  echo ""
  echo -e "  ${BOLD}Fail criteria (blocks GA):${RESET}"
  echo "    ✗ Claude consistently hallucinates argument names"
  echo "    ✗ Claude calls the wrong tool for a clearly described task"
  echo "    ✗ Schemas are so complex Claude refuses to attempt a call"
  echo ""
  warn "Record your findings in docs/llm-eval-results.md before marking GA."
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo -e "${BOLD}══════════════════════════════════════════════════${RESET}"
if [[ $FAILURES -eq 0 ]]; then
  echo -e "${GREEN}${BOLD}All automated checks passed.${RESET}"
  if [[ $SKIP_LLM -eq 0 ]]; then
    echo -e "${YELLOW}Complete the manual LLM eval above before marking GA.${RESET}"
  fi
else
  echo -e "${RED}${BOLD}$FAILURES automated check(s) failed. See output above.${RESET}"
fi
echo -e "${BOLD}══════════════════════════════════════════════════${RESET}"
echo ""

exit $FAILURES
