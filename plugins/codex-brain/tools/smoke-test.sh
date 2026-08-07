#!/usr/bin/env bash
# Smoke test for the codex-brain engine: init dry-run -> apply -> lint,
# against a throwaway vault. Fails closed (set -e) so CI catches a broken
# engine before it reaches a real install.
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="$PLUGIN_ROOT/skills/brain-init/scripts/vault.py"
TEST_VAULT="$(mktemp -d)"
DRYRUN_JSON="$(mktemp)"
LINT_JSON="$(mktemp)"
trap 'rm -rf "$TEST_VAULT" "$DRYRUN_JSON" "$LINT_JSON"' EXIT

echo "== engine: --help =="
python3 "$ENGINE" --help > /dev/null

GEN_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
OP_ID="ci-smoke-test"

echo "== init: dry-run =="
python3 "$ENGINE" init "$TEST_VAULT" \
  --generated-at "$GEN_AT" --operation-id "$OP_ID" > "$DRYRUN_JSON"
# Path passed via sys.argv, not interpolated into the -c script text: Git
# Bash on Windows only rewrites POSIX-style paths (like mktemp's /tmp/...)
# into Windows paths when they appear as actual argv entries to a non-MSYS
# executable. A path embedded inside a quoted -c string is just text to
# bash, so native Windows Python never sees the translated form and fails
# with FileNotFoundError -- confirmed by the first real Windows CI run.
# This is the same pattern tools/check-marketplace-sync.py's manifest
# validation already uses for exactly this reason.
HASH="$(python3 -c "import json, sys; print(json.load(open(sys.argv[1]))['approved_plan_sha256'])" "$DRYRUN_JSON")"

echo "== init: apply =="
python3 "$ENGINE" init "$TEST_VAULT" \
  --generated-at "$GEN_AT" --operation-id "$OP_ID" \
  --approved-plan-sha256 "$HASH" --apply > /dev/null

test -f "$TEST_VAULT/.codex-brain.json"
test -f "$TEST_VAULT/wiki/hot.md"
echo "== apply produced expected files =="

echo "== lint =="
python3 "$ENGINE" lint --vault "$TEST_VAULT" --format json --strict > "$LINT_JSON"
python3 -c "
import json, sys
report = json.load(open(sys.argv[1]))
issues = report['summary']['issues_found']
assert issues == 0, f'expected a clean freshly-initialized vault, found {issues} issue(s)'
print(f\"lint OK: {report['summary']['pages_scanned']} pages scanned, 0 issues\")
" "$LINT_JSON"

echo "== hook: session-start (opt-in, should return context) =="
cd "$TEST_VAULT"
CONTEXT="$(CODEX_BRAIN_SESSION_CONTEXT=1 python3 "$ENGINE" hook session-start)"
[ -n "$CONTEXT" ] || { echo "expected non-empty session-start context, got empty"; exit 1; }

echo "== hook: stop (clean vault, should be silent) =="
STOP_OUTPUT="$(python3 "$ENGINE" hook stop)"
[ -z "$STOP_OUTPUT" ] || { echo "expected empty stop output on a clean vault, got: $STOP_OUTPUT"; exit 1; }

echo ""
echo "ALL SMOKE TESTS PASSED"
