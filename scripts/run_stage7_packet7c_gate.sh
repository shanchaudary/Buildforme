#!/usr/bin/env bash
set +e
report=stage7_packet7c_validation.txt
: > "$report"

echo '== correct Packet 7C patcher ==' | tee -a "$report"
python scripts/fix_stage7_packet7c_patcher.py 2>&1 | tee -a "$report"
fix_status=${PIPESTATUS[0]}
apply_status=99
syntax_status=99
focused_status=99
full_status=99
policy_status=99
constitution_status=99

if [ "$fix_status" -eq 0 ]; then
  echo '== apply Packet 7C ==' | tee -a "$report"
  python scripts/apply_stage7_packet7c.py 2>&1 | tee -a "$report"
  apply_status=${PIPESTATUS[0]}
fi

if [ "$apply_status" -eq 0 ]; then
  python -m py_compile \
    buildforme/provider_discovery.py \
    buildforme/provider_compatibility.py \
    buildforme/review_execution.py \
    buildforme/execution_store.py \
    tests/test_stage6_redteam_round2.py \
    tests/test_stage7_review_execution.py \
    tests/test_stage7_packet7c_claude_reviewer.py \
    tests/test_stage7_packet7c_contract.py 2>&1 | tee -a "$report"
  syntax_status=${PIPESTATUS[0]}
  if [ "$syntax_status" -eq 0 ]; then
    git diff --check 2>&1 | tee -a "$report"
    syntax_status=${PIPESTATUS[0]}
  fi

  python -m unittest discover -s tests -p 'test_stage7_packet7c*.py' -v 2>&1 | tee -a "$report"
  p7c_status=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_stage7_review_execution.py' -v 2>&1 | tee -a "$report"
  p7b_status=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_stage7_review_authority.py' -v 2>&1 | tee -a "$report"
  p7a_status=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_provider_compatibility.py' -v 2>&1 | tee -a "$report"
  compat_status=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_stage6_redteam_round2.py' -v 2>&1 | tee -a "$report"
  stage6_redteam_status=${PIPESTATUS[0]}
  if [ "$p7c_status" -eq 0 ] && [ "$p7b_status" -eq 0 ] && [ "$p7a_status" -eq 0 ] && [ "$compat_status" -eq 0 ] && [ "$stage6_redteam_status" -eq 0 ]; then
    focused_status=0
  else
    focused_status=1
  fi

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
fi

echo "statuses fix=$fix_status apply=$apply_status syntax=$syntax_status focused=$focused_status full=$full_status policy=$policy_status constitution=$constitution_status" | tee -a "$report"

git config user.name "Buildforme Governance Bot"
git config user.email "actions@users.noreply.github.com"
restore_ci() {
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

restore_ci
if [ "$fix_status" -eq 0 ] && [ "$apply_status" -eq 0 ] && [ "$syntax_status" -eq 0 ] && [ "$focused_status" -eq 0 ] && [ "$full_status" -eq 0 ] && [ "$policy_status" -eq 0 ] && [ "$constitution_status" -eq 0 ]; then
  rm -f "$report" stage7_packet7c_validation.txt
  rm -f \
    scripts/apply_stage7_packet7c.py \
    scripts/fix_stage7_packet7c_patcher.py \
    scripts/run_stage7_packet7c_gate.sh
  git add -A -- \
    .github/workflows/ci.yml \
    buildforme/provider_discovery.py \
    buildforme/provider_compatibility.py \
    buildforme/review_execution.py \
    buildforme/execution_store.py \
    tests/test_stage6_redteam_round2.py \
    tests/test_stage7_review_execution.py \
    tests/test_stage7_packet7c_claude_reviewer.py \
    tests/test_stage7_packet7c_contract.py \
    docs/STAGE_7_INDEPENDENT_MULTI_AGENT_REVIEW.md \
    stage7_packet7c_validation.txt \
    scripts/apply_stage7_packet7c.py \
    scripts/fix_stage7_packet7c_patcher.py \
    scripts/run_stage7_packet7c_gate.sh
  git diff --cached --check || exit 1
  git commit -m "Add verified Claude independent reviewer contract" || exit 1
  git push origin HEAD:stage-7-independent-multi-agent-review-loop || exit 1
  exit 0
fi

cp "$report" /tmp/stage7_packet7c_validation.txt
git restore .
git clean -fd buildforme tests docs scripts
git clean -f stage7_packet7c_validation.txt
restore_ci
cp /tmp/stage7_packet7c_validation.txt "$report"
git add .github/workflows/ci.yml
git add -f "$report"
git commit -m "Record Packet 7C validation failure" || exit 1
git push origin HEAD:stage-7-independent-multi-agent-review-loop || exit 1
exit 1
