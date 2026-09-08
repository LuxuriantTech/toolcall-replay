#!/usr/bin/env sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
base=${ARTIFACT_DIR:-$(mktemp -d)}
python="$root/.venv/bin/python"
mkdir -p "$base/one" "$base/two"
set +e
"$python" -m toolcall_replay.cli evaluate "$root/examples/synthetic-suite.json" --output-dir "$base/one"
first=$?
"$python" -m toolcall_replay.cli evaluate "$root/examples/synthetic-suite.json" --output-dir "$base/two"
second=$?
set -e
[ "$first" -eq 1 ] && [ "$second" -eq 1 ]
cmp "$base/one/report.json" "$base/two/report.json"
cmp "$base/one/report.html" "$base/two/report.html"
"$python" - "$base/one/report.json" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
case = report["cases"][0]
expected = ["lookup-scope", "no-directory-export", "update-needs-approval"]
failures = [
    rule["rule_id"] for rule in case["candidate"]["rules"] if rule["verdict"] == "FAIL"
]
assert case["baseline"]["verdict"] == "PASS"
assert case["candidate"]["verdict"] == "FAIL"
assert failures == expected
assert case["diff"] == {
    "new_failures": expected,
    "resolved_failures": [],
    "unchanged_failures": [],
}
print("BASELINE=PASS")
print("CANDIDATE_FAILURES=" + ",".join(failures))
PY
printf 'ARTIFACT_DIR=%s\n' "$base"
