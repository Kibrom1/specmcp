#!/usr/bin/env bash
# specmcp local test runner
#
# Usage:
#   ./scripts/test-local.sh                    # test with bundled petstore spec
#   ./scripts/test-local.sh --spec my.json     # test with your own spec
#   ./scripts/test-local.sh --config mcp.config.yaml  # with config file
#   ./scripts/test-local.sh --verbose          # show full response bodies
#
# What this script does:
#   1. Checks that specmcp is installed (installs from source if not)
#   2. Runs specmcp inspect to preview tools
#   3. Runs specmcp validate to check the spec
#   4. Spawns specmcp serve and tests it over the MCP stdio protocol
#      (same wire format as Claude Desktop)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# ── Colours ────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
    BOLD="\033[1m"; DIM="\033[2m"; GREEN="\033[32m"; RED="\033[31m"
    YELLOW="\033[33m"; RESET="\033[0m"
else
    BOLD=""; DIM=""; GREEN=""; RED=""; YELLOW=""; RESET=""
fi

header()  { echo -e "\n${BOLD}── $* ──${RESET}"; }
ok()      { echo -e "  ${GREEN}✓${RESET}  $*"; }
fail()    { echo -e "  ${RED}✗${RESET}  $*"; }
info()    { echo -e "  ${DIM}$*${RESET}"; }

# ── Defaults ───────────────────────────────────────────────────────────────
SPEC="test-corpus/petstore.json"
CONFIG_ARG=""
VERBOSE_ARG=""
CLIENT_VERBOSE=""

# ── Parse args ─────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --spec)     SPEC="$2"; shift 2 ;;
        --config)   CONFIG_ARG="--config $2"; shift 2 ;;
        --verbose|-v) VERBOSE_ARG="--verbose"; CLIENT_VERBOSE="--verbose"; shift ;;
        -h|--help)
            sed -n '2,20p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

cd "$REPO_ROOT"

echo -e "${BOLD}specmcp local test runner${RESET}"
echo -e "${DIM}$(printf '─%.0s' {1..50})${RESET}"
info "spec   : $SPEC"
info "config : ${CONFIG_ARG:-none}"

# ── Step 1: Check / install specmcp ────────────────────────────────────────
header "Step 1: installation check"

if python -c "import specmcp" 2>/dev/null; then
    SPECMCP_VERSION=$(python -c "from specmcp import __version__; print(__version__)")
    ok "specmcp $SPECMCP_VERSION is installed"
else
    info "specmcp not found — installing from source..."
    pip install -e ".[dev]" --break-system-packages -q
    ok "installed from source"
fi

# ── Step 2: specmcp inspect ────────────────────────────────────────────────
header "Step 2: specmcp inspect"
info "Running: specmcp inspect --spec $SPEC $CONFIG_ARG"
echo ""

if python -m specmcp inspect --spec "$SPEC" $CONFIG_ARG $VERBOSE_ARG; then
    echo ""
    ok "inspect completed successfully"
else
    fail "inspect failed (exit $?)"
    exit 1
fi

# ── Step 3: specmcp validate ───────────────────────────────────────────────
header "Step 3: specmcp validate"
info "Running: specmcp validate --spec $SPEC $CONFIG_ARG"

VALIDATE_OUT=$(python -m specmcp validate --spec "$SPEC" $CONFIG_ARG 2>&1)
VALIDATE_EXIT=$?
echo "$VALIDATE_OUT"

if [ $VALIDATE_EXIT -eq 0 ]; then
    ok "spec and config are valid"
else
    fail "validate failed (exit $VALIDATE_EXIT)"
    exit 1
fi

# ── Step 4: MCP stdio round-trip test ─────────────────────────────────────
header "Step 4: MCP stdio protocol test"
info "Spawning specmcp serve and connecting as an MCP client..."
info "(This exercises the same stdio protocol Claude Desktop uses.)"
echo ""

if python "$SCRIPT_DIR/mcp_client_test.py" \
        --spec "$SPEC" \
        ${CONFIG_ARG:+$(echo $CONFIG_ARG | sed 's/--config /--config /g')} \
        $CLIENT_VERBOSE; then
    echo ""
    ok "MCP client test passed"
else
    echo ""
    fail "MCP client test failed"
    exit 1
fi

# ── Done ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}All checks passed.${RESET} specmcp is working correctly."
echo ""
echo -e "${DIM}To run the full unit + integration test suite:${RESET}"
echo -e "  pytest tests/"
echo ""
echo -e "${DIM}To start the server interactively (for Claude Desktop):${RESET}"
echo -e "  specmcp serve --spec $SPEC${CONFIG_ARG:+ $CONFIG_ARG}"
