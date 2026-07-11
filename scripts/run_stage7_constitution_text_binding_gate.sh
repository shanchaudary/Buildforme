#!/usr/bin/env bash
set -euo pipefail

python scripts/apply_stage7_constitution_text_binding.py
rm -f scripts/apply_stage7_constitution_text_binding.py scripts/run_stage7_constitution_text_binding_gate.sh

python -m py_compile buildforme/review_execution.py tests/test_stage7_packet7b_isolation_contract.py
python -m unittest discover -s tests -p 'test_stage7_packet7b_isolation_contract.py' -v
python -m unittest discover -s tests -p 'test_stage7_review_execution.py' -v

git diff --check
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

git config user.name "Buildforme Governance Bot"
git config user.email "actions@users.noreply.github.com"
git add -A
git diff --cached --check
git status --short
git commit -m "Bind reviewer prompts to canonical Constitution text"
published_sha=$(git rev-parse HEAD)
git push origin HEAD:stage-7-independent-multi-agent-review-loop
remote_sha=$(git ls-remote origin refs/heads/stage-7-independent-multi-agent-review-loop | awk '{print $1}')
echo "published_sha=$published_sha"
echo "remote_sha=$remote_sha"
test "$published_sha" = "$remote_sha"
