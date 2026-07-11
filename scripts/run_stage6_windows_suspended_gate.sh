#!/usr/bin/env bash
set +e
report=stage6_windows_suspended_validation.txt
: > "$report"

echo '== apply Windows suspended-launch hardening ==' | tee -a "$report"
python scripts/apply_stage6_windows_suspended_launch.py 2>&1 | tee -a "$report"
apply_status=${PIPESTATUS[0]}
syntax_status=99
focused_status=99
full_status=99
policy_status=99
constitution_status=99

if [ "$apply_status" -eq 0 ]; then
  echo '== syntax and diff ==' | tee -a "$report"
  python -m py_compile \
    buildforme/windows_job.py \
    buildforme/process_supervisor.py \
    tests/test_stage6_windows_suspended_launch.py 2>&1 | tee -a "$report"
  syntax_status=${PIPESTATUS[0]}
  if [ "$syntax_status" -eq 0 ]; then
    git diff --check 2>&1 | tee -a "$report"
    syntax_status=${PIPESTATUS[0]}
  fi

  echo '== focused Windows containment and Stage 6 tests ==' | tee -a "$report"
  python -m unittest discover -s tests -p 'test_stage6_windows_suspended_launch.py' -v 2>&1 | tee -a "$report"
  windows_status=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_stage6_redteam_round2.py' -v 2>&1 | tee -a "$report"
  redteam_status=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_stage6_final_blockers.py' -v 2>&1 | tee -a "$report"
  blockers_status=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_process_supervisor*.py' -v 2>&1 | tee -a "$report"
  process_status=${PIPESTATUS[0]}
  if [ "$windows_status" -eq 0 ] && [ "$redteam_status" -eq 0 ] && [ "$blockers_status" -eq 0 ] && [ "$process_status" -eq 0 ]; then
    focused_status=0
  else
    focused_status=1
  fi

  echo '== full suite ==' | tee -a "$report"
  python -m unittest discover -s tests -p 'test_*.py' 2>&1 | tee -a "$report"
  full_status=${PIPESTATUS[0]}

  echo '== policy smoke ==' | tee -a "$report"
  python -m buildforme.cli classify data/sample_task.json 2>&1 | tee -a "$report"
  policy_status=${PIPESTATUS[0]}

  echo '== constitution ==' | tee -a "$report"
  python - <<'PY' 2>&1 | tee -a "$report"
from governance.constitution_engine import get_engine
result = get_engine().full_validation_suite()
print(result)
if not result.get("passed"):
    raise SystemExit(1)
PY
  constitution_status=${PIPESTATUS[0]}
fi

echo "statuses apply=$apply_status syntax=$syntax_status focused=$focused_status full=$full_status policy=$policy_status constitution=$constitution_status" | tee -a "$report"

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

if [ "$apply_status" -eq 0 ] && [ "$syntax_status" -eq 0 ] && [ "$focused_status" -eq 0 ] && [ "$full_status" -eq 0 ] && [ "$policy_status" -eq 0 ] && [ "$constitution_status" -eq 0 ]; then
  rm -f "$report" stage6_windows_suspended_validation.txt
  rm -f scripts/apply_stage6_windows_suspended_launch.py
  rm -f scripts/run_stage6_windows_suspended_gate.sh
  restore_original_ci
  git diff --check
  git add -A -- \
    .github/workflows/ci.yml \
    buildforme/windows_job.py \
    buildforme/process_supervisor.py \
    tests/test_stage6_windows_suspended_launch.py \
    docs/STAGE_6_MULTI_PROVIDER_EXECUTION.md \
    scripts/apply_stage6_windows_suspended_launch.py \
    scripts/run_stage6_windows_suspended_gate.sh \
    stage6_windows_suspended_validation.txt
  git diff --cached --check
  unexpected=$(git status --porcelain | grep '^??' || true)
  if [ -n "$unexpected" ]; then
    echo "unexpected untracked files:" >&2
    echo "$unexpected" >&2
    exit 1
  fi
  git commit -m "Close Windows process containment race"
  git push origin HEAD:stage-6-multi-provider-supervised-execution
  exit 0
fi

cp "$report" /tmp/stage6_windows_suspended_validation.txt
git restore .
git clean -fd buildforme tests docs scripts
restore_original_ci
cp /tmp/stage6_windows_suspended_validation.txt "$report"
git add .github/workflows/ci.yml "$report"
git commit -m "Record Stage 6 Windows containment validation failure"
git push origin HEAD:stage-6-multi-provider-supervised-execution
exit 1
