#!/usr/bin/env bash
set +e
report=stage7_cleanup_diagnostic.txt
python -m unittest tests.test_stage7_review_authority -v > "$report" 2>&1
status=$?
cat "$report"

git config user.name "Buildforme Governance Bot"
git config user.email "actions@users.noreply.github.com"
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
rm -f scripts/run_stage7_cleanup_diagnostic.sh
if [ "$status" -eq 0 ]; then
  rm -f "$report"
  git add -A -- .github/workflows/ci.yml scripts/run_stage7_cleanup_diagnostic.sh
  git commit -m "Complete Stage 7 cleanup diagnostic"
else
  git add -A -- .github/workflows/ci.yml scripts/run_stage7_cleanup_diagnostic.sh
  git add -f "$report"
  git commit -m "Record Stage 7 cleanup test failure"
fi
git push origin HEAD:stage-7-independent-multi-agent-review-loop
exit "$status"
