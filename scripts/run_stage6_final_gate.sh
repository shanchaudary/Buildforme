#!/usr/bin/env bash
set +e

report=stage6_final_blocker_validation.txt
: > "$report"

echo '== correct patch anchor ==' | tee -a "$report"
python scripts/fix_stage6_final_blocker_patcher.py 2>&1 | tee -a "$report"
correction_status=${PIPESTATUS[0]}

echo '== apply ==' | tee -a "$report"
if [ "$correction_status" -eq 0 ]; then
  python scripts/apply_stage6_final_blockers.py 2>&1 | tee -a "$report"
  apply_status=${PIPESTATUS[0]}
else
  apply_status=1
fi

echo '== align atomic migration regression ==' | tee -a "$report"
if [ "$apply_status" -eq 0 ]; then
  python scripts/fix_stage6_atomic_migration_regression.py 2>&1 | tee -a "$report"
  regression_status=${PIPESTATUS[0]}
else
  regression_status=1
fi

syntax_status=99
focused_status=99
full_status=99
policy_status=99
constitution_status=99

if [ "$apply_status" -eq 0 ] && [ "$regression_status" -eq 0 ]; then
  echo '== syntax and diff ==' | tee -a "$report"
  python -m py_compile \
    buildforme/process_termination.py \
    buildforme/outcome_evidence.py \
    buildforme/stage6_smoke_acceptance.py \
    buildforme/process_supervisor.py \
    buildforme/evidence.py \
    buildforme/execution_store.py \
    buildforme/execution_service.py \
    buildforme/provider_discovery.py \
    buildforme/provider_compatibility.py \
    scripts/stage6_real_provider_smoke.py \
    tests/test_stage6_final_blockers.py \
    tests/test_placeholder_runs.py 2>&1 | tee -a "$report"
  syntax_status=${PIPESTATUS[0]}
  if [ "$syntax_status" -eq 0 ]; then
    git diff --check 2>&1 | tee -a "$report"
    syntax_status=${PIPESTATUS[0]}
  fi

  echo '== focused Stage 6 blocker tests ==' | tee -a "$report"
  python -m unittest discover -s tests -p 'test_stage6_final_blockers.py' -v 2>&1 | tee -a "$report"
  blockers_status=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_run_mutation_authority*.py' -v 2>&1 | tee -a "$report"
  mutation_status=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_stage6_execution.py' -v 2>&1 | tee -a "$report"
  execution_status=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_placeholder_runs.py' -v 2>&1 | tee -a "$report"
  migration_status=${PIPESTATUS[0]}
  if [ "$blockers_status" -eq 0 ] && [ "$mutation_status" -eq 0 ] && [ "$execution_status" -eq 0 ] && [ "$migration_status" -eq 0 ]; then
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

echo "statuses correction=$correction_status apply=$apply_status regression=$regression_status syntax=$syntax_status focused=$focused_status full=$full_status policy=$policy_status constitution=$constitution_status" | tee -a "$report"

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

if [ "$correction_status" -eq 0 ] && [ "$apply_status" -eq 0 ] && [ "$regression_status" -eq 0 ] && [ "$syntax_status" -eq 0 ] && [ "$focused_status" -eq 0 ] && [ "$full_status" -eq 0 ] && [ "$policy_status" -eq 0 ] && [ "$constitution_status" -eq 0 ]; then
  rm -f "$report"
  rm -f scripts/apply_stage6_final_blockers.py
  rm -f scripts/fix_stage6_final_blocker_patcher.py
  rm -f scripts/fix_stage6_atomic_migration_regression.py
  rm -f scripts/run_stage6_final_gate.sh
  rm -f .github/workflows/stage6-final-blockers.yml
  restore_original_ci
  git diff --check
  git add -A -- \
    .github/workflows/ci.yml \
    .github/workflows/stage6-final-blockers.yml \
    buildforme/process_termination.py \
    buildforme/outcome_evidence.py \
    buildforme/stage6_smoke_acceptance.py \
    buildforme/process_supervisor.py \
    buildforme/evidence.py \
    buildforme/execution_store.py \
    buildforme/execution_service.py \
    buildforme/provider_discovery.py \
    buildforme/provider_compatibility.py \
    scripts/stage6_real_provider_smoke.py \
    tests/test_stage6_final_blockers.py \
    tests/test_placeholder_runs.py \
    docs/STAGE_6_MULTI_PROVIDER_EXECUTION.md \
    scripts/apply_stage6_final_blockers.py \
    scripts/fix_stage6_final_blocker_patcher.py \
    scripts/fix_stage6_atomic_migration_regression.py \
    scripts/run_stage6_final_gate.sh \
    stage6_final_blocker_validation.txt
  git diff --cached --check
  unexpected=$(git status --porcelain | grep '^??' || true)
  if [ -n "$unexpected" ]; then
    echo "unexpected untracked files:" >&2
    echo "$unexpected" >&2
    exit 1
  fi
  git commit -m "Close Stage 6 execution evidence and termination blockers"
  git push origin HEAD:stage-6-multi-provider-supervised-execution
  exit 0
fi

cp "$report" /tmp/stage6_final_blocker_validation.txt
git restore .
git clean -fd buildforme tests docs scripts
restore_original_ci
cp /tmp/stage6_final_blocker_validation.txt "$report"
git add .github/workflows/ci.yml "$report"
git commit -m "Record Stage 6 final-blocker validation failure"
git push origin HEAD:stage-6-multi-provider-supervised-execution
exit 1
