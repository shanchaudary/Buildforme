#!/usr/bin/env bash
set +e

report=stage7_packet7a_validation.txt
: > "$report"

echo '== correct Stage 7 Packet 7A patcher ==' | tee -a "$report"
python scripts/fix_stage7_packet7a_patcher.py 2>&1 | tee -a "$report"
correction_status=${PIPESTATUS[0]}

echo '== apply Stage 7 Packet 7A ==' | tee -a "$report"
if [ "$correction_status" -eq 0 ]; then
  python scripts/apply_stage7_packet7a.py 2>&1 | tee -a "$report"
  apply_status=${PIPESTATUS[0]}
else
  apply_status=1
fi
syntax_status=99
focused_status=99
full_status=99
policy_status=99
constitution_status=99

if [ "$apply_status" -eq 0 ]; then
  echo '== syntax and diff ==' | tee -a "$report"
  python -m py_compile \
    buildforme/review_contracts.py \
    buildforme/review_service.py \
    buildforme/db.py \
    buildforme/execution_store.py \
    buildforme/storage.py \
    buildforme/review_gate.py \
    buildforme/execution_service.py \
    buildforme/server.py \
    tests/test_stage7_review_authority.py \
    tests/test_stage6_execution.py 2>&1 | tee -a "$report"
  syntax_status=${PIPESTATUS[0]}
  if [ "$syntax_status" -eq 0 ]; then
    git diff --check 2>&1 | tee -a "$report"
    syntax_status=${PIPESTATUS[0]}
  fi

  echo '== focused Stage 7 authority tests ==' | tee -a "$report"
  python -m unittest discover -s tests -p 'test_stage7_review_authority.py' -v 2>&1 | tee -a "$report"
  stage7_status=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_run_mutation_authority*.py' -v 2>&1 | tee -a "$report"
  mutation_status=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_stage6_final_blockers.py' -v 2>&1 | tee -a "$report"
  stage6_status=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_stage6_execution.py' -v 2>&1 | tee -a "$report"
  stage6_execution_status=${PIPESTATUS[0]}
  if [ "$stage7_status" -eq 0 ] && [ "$mutation_status" -eq 0 ] && [ "$stage6_status" -eq 0 ] && [ "$stage6_execution_status" -eq 0 ]; then
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

echo "statuses correction=$correction_status apply=$apply_status syntax=$syntax_status focused=$focused_status full=$full_status policy=$policy_status constitution=$constitution_status" | tee -a "$report"

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

if [ "$correction_status" -eq 0 ] && [ "$apply_status" -eq 0 ] && [ "$syntax_status" -eq 0 ] && [ "$focused_status" -eq 0 ] && [ "$full_status" -eq 0 ] && [ "$policy_status" -eq 0 ] && [ "$constitution_status" -eq 0 ]; then
  rm -f "$report"
  rm -f scripts/apply_stage7_packet7a.py scripts/fix_stage7_packet7a_patcher.py scripts/run_stage7_packet7a_gate.sh
  restore_original_ci
  git diff --check
  git add -A -- \
    .github/workflows/ci.yml \
    buildforme/review_contracts.py \
    buildforme/review_service.py \
    buildforme/db.py \
    buildforme/execution_store.py \
    buildforme/storage.py \
    buildforme/review_gate.py \
    buildforme/execution_service.py \
    buildforme/server.py \
    tests/test_stage7_review_authority.py \
    tests/test_stage6_execution.py \
    docs/STAGE_7_INDEPENDENT_MULTI_AGENT_REVIEW.md \
    scripts/apply_stage7_packet7a.py \
    scripts/fix_stage7_packet7a_patcher.py \
    scripts/run_stage7_packet7a_gate.sh
  git diff --cached --check
  unexpected=$(git status --porcelain | grep '^??' || true)
  if [ -n "$unexpected" ]; then
    echo "unexpected untracked files:" >&2
    echo "$unexpected" >&2
    exit 1
  fi
  git commit -m "Add Stage 7 independent review authority"
  git push origin HEAD:stage-7-independent-multi-agent-review-loop
  exit 0
fi

cp "$report" /tmp/stage7_packet7a_validation.txt
printf '\n== validation report tail ==\n'
tail -n 200 "$report" || true
git restore .
git clean -fd buildforme tests docs scripts
restore_original_ci
cp /tmp/stage7_packet7a_validation.txt "$report"
git add .github/workflows/ci.yml
git add -f "$report"
git commit -m "Record Stage 7 Packet 7A validation failure"
git push origin HEAD:stage-7-independent-multi-agent-review-loop
exit 1
