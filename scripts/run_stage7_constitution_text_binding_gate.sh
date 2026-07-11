#!/usr/bin/env bash
set +e
report=stage7_constitution_text_binding_validation.txt
: > "$report"
python scripts/apply_stage7_constitution_text_binding.py 2>&1 | tee -a "$report"
apply_status=${PIPESTATUS[0]}
syntax_status=99
focused_status=99
full_status=99
policy_status=99
constitution_status=99

if [ "$apply_status" -eq 0 ]; then
  rm -f scripts/apply_stage7_constitution_text_binding.py scripts/run_stage7_constitution_text_binding_gate.sh
  python -m py_compile buildforme/review_execution.py tests/test_stage7_packet7b_isolation_contract.py 2>&1 | tee -a "$report"
  syntax_status=${PIPESTATUS[0]}
  if [ "$syntax_status" -eq 0 ]; then git diff --check 2>&1 | tee -a "$report"; syntax_status=${PIPESTATUS[0]}; fi
  python -m unittest discover -s tests -p 'test_stage7_packet7b_isolation_contract.py' -v 2>&1 | tee -a "$report"
  a=${PIPESTATUS[0]}
  python -m unittest discover -s tests -p 'test_stage7_review_execution.py' -v 2>&1 | tee -a "$report"
  b=${PIPESTATUS[0]}
  if [ "$a" -eq 0 ] && [ "$b" -eq 0 ]; then focused_status=0; else focused_status=1; fi
  python -m unittest discover -s tests -p 'test_*.py' 2>&1 | tee -a "$report"
  full_status=${PIPESTATUS[0]}
  python -m buildforme.cli classify data/sample_task.json 2>&1 | tee -a "$report"
  policy_status=${PIPESTATUS[0]}
  python - <<'PY' 2>&1 | tee -a "$report"
from governance.constitution_engine import get_engine
r=get_engine().full_validation_suite(); print(r)
if not r.get('passed'): raise SystemExit(1)
PY
  constitution_status=${PIPESTATUS[0]}
fi

echo "statuses apply=$apply_status syntax=$syntax_status focused=$focused_status full=$full_status policy=$policy_status constitution=$constitution_status" | tee -a "$report"
git config user.name "Buildforme Governance Bot"
git config user.email "actions@users.noreply.github.com"
restore_ci(){ cat > .github/workflows/ci.yml <<'YAML'
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
  git add -A
  git diff --cached --check || exit 91
  git status --short
  git commit -m "Bind reviewer prompts to canonical Constitution text" || exit 92
  published_sha=$(git rev-parse HEAD)
  git push origin HEAD:stage-7-independent-multi-agent-review-loop || exit 93
  remote_sha=$(git ls-remote origin refs/heads/stage-7-independent-multi-agent-review-loop | awk '{print $1}')
  echo "published_sha=$published_sha remote_sha=$remote_sha"
  [ "$published_sha" = "$remote_sha" ] || exit 94
  exit 0
fi
cp "$report" /tmp/stage7_constitution_text_binding_validation.txt
git restore .; git clean -fd buildforme tests docs scripts; restore_ci
cp /tmp/stage7_constitution_text_binding_validation.txt "$report"
git add .github/workflows/ci.yml; git add -f "$report"
git commit -m "Record Constitution text binding validation failure" || exit 95
git push origin HEAD:stage-7-independent-multi-agent-review-loop || exit 96
exit 1
