#!/usr/bin/env bash
set +e
report=stage7_packet7b_isolation_validation.txt
: > "$report"

echo '== apply Packet 7B isolation hardening ==' | tee -a "$report"
python scripts/apply_stage7_packet7b_isolation.py 2>&1 | tee -a "$report"
apply_status=${PIPESTATUS[0]}
syntax_status=99
focused_status=99
full_status=99
policy_status=99
constitution_status=99

if [ "$apply_status" -eq 0 ]; then
  # The permanent contract requires the final tree to contain no gate machinery.
  # Preserve temporary copies outside the repository, then remove the files before tests.
  cp scripts/apply_stage7_packet7b_isolation.py /tmp/apply_stage7_packet7b_isolation.py
  cp scripts/run_stage7_packet7b_isolation_gate.sh /tmp/run_stage7_packet7b_isolation_gate.sh
  rm -f scripts/apply_stage7_packet7b_isolation.py scripts/run_stage7_packet7b_isolation_gate.sh

  echo '== syntax and diff ==' | tee -a "$report"
  python -m py_compile buildforme/review_execution.py tests/test_stage7_review_execution.py 2>&1 | tee -a "$report"
  syntax_status=${PIPESTATUS[0]}
  if [ "$syntax_status" -eq 0 ]; then
    git diff --check 2>&1 | tee -a "$report"
    syntax_status=${PIPESTATUS[0]}
  fi

  echo '== focused Packet 7B/7C tests ==' | tee -a "$report"
  python -m unittest discover -s tests -p 'test_stage7_review_execution.py' -v 2>&1 | tee -a "$report"
  exec_status=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_stage7_packet7*.py' -v 2>&1 | tee -a "$report"
  packet_status=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_stage7_review_authority.py' -v 2>&1 | tee -a "$report"
  authority_status=${PIPESTATUS[0]}
  if [ "$exec_status" -eq 0 ] && [ "$packet_status" -eq 0 ] && [ "$authority_status" -eq 0 ]; then
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
if not result.get('passed'):
    raise SystemExit(1)
PY
  constitution_status=${PIPESTATUS[0]}
fi

echo "statuses apply=$apply_status syntax=$syntax_status focused=$focused_status full=$full_status policy=$policy_status constitution=$constitution_status" | tee -a "$report"

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

if [ "$apply_status" -eq 0 ] && [ "$syntax_status" -eq 0 ] && [ "$focused_status" -eq 0 ] && [ "$full_status" -eq 0 ] && [ "$policy_status" -eq 0 ] && [ "$constitution_status" -eq 0 ]; then
  rm -f "$report"
  restore_ci
  git diff --check
  git add -A -- .github/workflows/ci.yml buildforme/review_execution.py tests/test_stage7_review_execution.py docs/STAGE_7_INDEPENDENT_MULTI_AGENT_REVIEW.md scripts/apply_stage7_packet7b_isolation.py scripts/run_stage7_packet7b_isolation_gate.sh stage7_packet7b_isolation_validation.txt
  git diff --cached --check
  git commit -m "Isolate Stage 7 reviewer workspaces"
  git push origin HEAD:stage-7-independent-multi-agent-review-loop
  exit 0
fi

cp "$report" /tmp/stage7_packet7b_isolation_validation.txt
printf '\n== validation report tail ==\n'
tail -n 240 "$report" || true
git restore .
git clean -fd buildforme tests docs scripts
restore_ci
cp /tmp/stage7_packet7b_isolation_validation.txt "$report"
git add .github/workflows/ci.yml
git add -f "$report"
git commit -m "Record Packet 7B isolation validation failure"
git push origin HEAD:stage-7-independent-multi-agent-review-loop
exit 1
