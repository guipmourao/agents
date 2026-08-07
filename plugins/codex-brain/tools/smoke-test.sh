#!/usr/bin/env bash
# Smoke test for the codex-brain engine: init dry-run -> apply -> lint,
# against a throwaway vault. Fails closed (set -e) so CI catches a broken
# engine before it reaches a real install.
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="$PLUGIN_ROOT/skills/brain-init/scripts/vault.py"
TEST_VAULT="$(mktemp -d)"
trap 'rm -rf "$TEST_VAULT"' EXIT

echo "== engine: --help =="
python3 "$ENGINE" --help > /dev/null

GEN_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
OP_ID="ci-smoke-test"

echo "== init: dry-run =="
python3 "$ENGINE" init "$TEST_VAULT" \
  --generated-at "$GEN_AT" --operation-id "$OP_ID" > /tmp/ci-smoke-dryrun.json
HASH="$(python3 -c "import json; print(json.load(open('/tmp/ci-smoke-dryrun.json'))['approved_plan_sha256'])")"

echo "== init: apply =="
python3 "$ENGINE" init "$TEST_VAULT" \
  --generated-at "$GEN_AT" --operation-id "$OP_ID" \
  --approved-plan-sha256 "$HASH" --apply > /dev/null

test -f "$TEST_VAULT/.codex-brain.json"
test -f "$TEST_VAULT/wiki/hot.md"
echo "== apply produced expected files =="

echo "== lint =="
python3 "$ENGINE" lint --vault "$TEST_VAULT" --format json --strict > /tmp/ci-smoke-lint.json
python3 -c "
import json
report = json.load(open('/tmp/ci-smoke-lint.json'))
issues = report['summary']['issues_found']
assert issues == 0, f'expected a clean freshly-initialized vault, found {issues} issue(s)'
print(f\"lint OK: {report['summary']['pages_scanned']} pages scanned, 0 issues\")
"

echo "== hook: session-start (opt-in, should return context) =="
cd "$TEST_VAULT"
CONTEXT="$(CODEX_BRAIN_SESSION_CONTEXT=1 python3 "$ENGINE" hook session-start)"
[ -n "$CONTEXT" ] || { echo "expected non-empty session-start context, got empty"; exit 1; }

echo "== hook: stop (clean vault, should be silent) =="
STOP_OUTPUT="$(python3 "$ENGINE" hook stop)"
[ -z "$STOP_OUTPUT" ] || { echo "expected empty stop output on a clean vault, got: $STOP_OUTPUT"; exit 1; }

rm -f /tmp/ci-smoke-dryrun.json /tmp/ci-smoke-lint.json
echo ""
echo "ALL SMOKE TESTS PASSED"
