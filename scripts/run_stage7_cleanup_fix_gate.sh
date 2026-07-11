#!/usr/bin/env bash
set +e
report=stage7_cleanup_fix_validation.txt
: > "$report"

python scripts/fix_stage7_cleanup_regressions.py 2>&1 | tee -a "$report"
fix_status=${PIPESTATUS[0]}
focused_status=99
full_status=99
policy_status=99
constitution_status=99

if [ "$fix_status" -eq 0 ]; then
  python -m unittest tests.test_stage7_review_authority -v 2>&1 | tee -a "$report"
  focused_status=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_*.py' 2>&1 | tee -a "$report"
  full_status=${PIPESTATUS[0]}
  python -m buildforme.cli classify data/sample_task.json 2>&1 | tee -a "$report"
  policy_status=${PIPESTATUS[0]}
  python - <<'PY' 2>&1 | tee -a "$report"
from governance.constitution_engine import get_engine
result = get_engine().full_validation_suite()
print(result)
if not result.get("passed"):
    raise SystemExit(1)
PY
  constitution_status=${PIPESTATUS[0]}
  git diff --check 2>&1 | tee -a "$report"
  diff_status=${PIPESTATUS[0]}
else
  diff_status=99
fi

echo "statuses fix=$fix_status focused=$focused_status full=$full_status policy=$policy_status constitution=$constitution_status diff=$diff_status" | tee -a "$report"

git config user.name "Buildforme Governance Bot"
git config user.email "actions@users.noreply.github.com"
restore_original_ci() {
  cat > .github/workflows/ci.yml <<'YAML'
name: Buildforme CI

on:
  pull_request:
  push:
    branches:
      - main
      - founder-control-plane-mvp

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Run unit tests
        run: python -m unittest discover -s tests -p 'test_*.py'

      - name: Run policy smoke check
        run: python -m buildforme.cli classify data/sample_task.json
YAML
}

restore_original_ci
rm -f scripts/fix_stage7_cleanup_regressions.py scripts/run_stage7_cleanup_fix_gate.sh stage7_cleanup_diagnostic.txt

if [ "$fix_status" -eq 0 ] && [ "$focused_status" -eq 0 ] && [ "$full_status" -eq 0 ] && [ "$policy_status" -eq 0 ] && [ "$constitution_status" -eq 0 ] && [ "$diff_status" -eq 0 ]; then
  rm -f "$report"
  git add -A -- \
    .github/workflows/ci.yml \
    tests/test_stage7_review_authority.py \
    stage7_cleanup_diagnostic.txt \
    scripts/fix_stage7_cleanup_regressions.py \
    scripts/run_stage7_cleanup_fix_gate.sh
  git diff --cached --check || exit 1
  git commit -m "Restore Stage 7 red-team regression expectations" || exit 1
  git push origin HEAD:stage-7-independent-multi-agent-review-loop || exit 1
  exit 0
fi

cp "$report" /tmp/stage7_cleanup_fix_validation.txt
git restore .
git clean -fd tests scripts
git clean -f stage7_cleanup_fix_validation.txt
restore_original_ci
cp /tmp/stage7_cleanup_fix_validation.txt "$report"
git add .github/workflows/ci.yml
git add -f "$report"
git commit -m "Record Stage 7 cleanup fix failure" || exit 1
git push origin HEAD:stage-7-independent-multi-agent-review-loop || exit 1
exit 1
