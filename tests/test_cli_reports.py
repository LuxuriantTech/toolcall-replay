from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from conftest import ROOT, read_json


def run_cli(suite: Path, output: Path) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "toolcall_replay.cli",
            "evaluate",
            str(suite),
            "--output-dir",
            str(output),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_writes_schema_conformant_static_reports_and_exact_diff(
    suite_copy: Path, tmp_path: Path
) -> None:
    output = tmp_path / "out"
    completed = run_cli(suite_copy, output)
    assert completed.returncode == 1, completed.stderr
    assert completed.stdout.startswith("VERDICT suite=synthetic-internal-request digest=")
    report = read_json(output / "report.json")
    schema = json.loads((ROOT / "schemas/report.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(report)
    assert report["verdict"] == "FAIL"
    assert report["cases"][0]["diff"] == {
        "new_failures": ["lookup-scope", "no-directory-export", "update-needs-approval"],
        "resolved_failures": [],
        "unchanged_failures": [],
    }
    html = (output / "report.html").read_text(encoding="utf-8")
    assert "<script" not in html.lower()
    assert report["digest"] in html
    assert "http://" not in html and "https://" not in html


def test_cli_is_deterministic_and_digest_excludes_itself(suite_copy: Path, tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    assert run_cli(suite_copy, first).returncode == 1
    assert run_cli(suite_copy, second).returncode == 1
    first_bytes = (first / "report.json").read_bytes()
    assert first_bytes == (second / "report.json").read_bytes()
    assert (first / "report.html").read_bytes() == (second / "report.html").read_bytes()
    report = json.loads(first_bytes)
    digest = report.pop("digest")
    logical = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    assert digest == hashlib.sha256(logical).hexdigest()
    assert str(suite_copy.resolve()) not in first_bytes.decode("utf-8")
    assert "timestamp" not in first_bytes.decode("utf-8").lower()


def test_cli_all_pass_returns_zero_and_preserves_third_party_file(
    suite_copy: Path, tmp_path: Path
) -> None:
    payload = read_json(suite_copy)
    payload["cases"][0]["candidate_trace"] = payload["cases"][0]["baseline_trace"]
    suite_copy.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    keep = output / "keep.txt"
    keep.write_text("preserve", encoding="utf-8")
    completed = run_cli(suite_copy, output)
    assert completed.returncode == 0, completed.stderr
    assert read_json(output / "report.json")["verdict"] == "PASS"
    assert keep.read_text(encoding="utf-8") == "preserve"


def test_html_escapes_input_text_and_contains_no_script_tag(
    suite_copy: Path, tmp_path: Path
) -> None:
    payload = read_json(suite_copy)
    payload["cases"][0]["rules"][2]["allowed_values"].append("<script>alert(1)</script>")
    suite_copy.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "out"
    assert run_cli(suite_copy, output).returncode == 1
    html = (output / "report.html").read_text(encoding="utf-8").lower()
    assert "<script" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_html_prioritizes_semantic_verdicts_rules_and_expandable_evidence(
    suite_copy: Path, tmp_path: Path
) -> None:
    output = tmp_path / "out"
    assert run_cli(suite_copy, output).returncode == 1
    rendered = (output / "report.html").read_text(encoding="utf-8")

    assert '<main class="report">' in rendered
    assert '<section class="case"' in rendered
    assert '<section class="rule"' in rendered
    assert '<details class="evidence">' in rendered
    assert "<summary>Preuves détaillées</summary>" in rendered
    assert 'aria-label="Verdict global"' in rendered
    assert 'data-verdict="FAIL"' in rendered
    assert "Baseline" in rendered and "7 / 7 règles réussies" in rendered
    assert "Candidate" in rendered and "4 / 7 règles réussies" in rendered
    assert "3 nouveaux échecs" in rendered
    for rule_id in (
        "lookup-present",
        "lookup-before-update",
        "lookup-scope",
        "no-directory-export",
        "update-needs-approval",
        "result-completed",
        "within-step-budget",
    ):
        assert rule_id in rendered

    global_verdict = rendered.index('aria-label="Verdict global"')
    comparison = rendered.index("Comparaison baseline / candidate")
    rules = rendered.index("Résultats par règle")
    evidence = rendered.index("Preuves détaillées")
    assert global_verdict < comparison < rules < evidence
    assert '<pre class="raw-report">' not in rendered


def test_html_has_a_narrow_viewport_reflow_contract(suite_copy: Path, tmp_path: Path) -> None:
    output = tmp_path / "out"
    assert run_cli(suite_copy, output).returncode == 1
    rendered = (output / "report.html").read_text(encoding="utf-8")

    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in rendered
    for declaration in (
        "box-sizing: border-box",
        "max-width: 100%",
        "min-width: 0",
        "overflow-wrap: anywhere",
        "white-space: pre-wrap",
    ):
        assert declaration in rendered


def test_html_has_no_horizontal_overflow_at_390px_when_local_chrome_is_available(
    suite_copy: Path, tmp_path: Path
) -> None:
    chrome = next(
        (
            executable
            for name in ("google-chrome", "chromium", "chromium-browser")
            if (executable := shutil.which(name)) is not None
        ),
        None,
    )
    if chrome is None:
        pytest.skip("local Chromium-compatible browser unavailable")

    output = tmp_path / "out"
    assert run_cli(suite_copy, output).returncode == 1
    report_path = output / "report.html"
    measurement = (
        '<!doctype html><html><body><iframe id="report" '
        f'src="{report_path.as_uri()}" style="width:390px;height:844px;border:0"></iframe>'
        "<script>const frame=document.getElementById('report');frame.addEventListener('load',()=>{"
        "document.body.dataset.viewport=String(frame.contentWindow.innerWidth);"
        "document.body.dataset.scroll=String(frame.contentDocument.documentElement.scrollWidth);"
        "});</script></body></html>"
    )
    instrumented = tmp_path / "measured.html"
    instrumented.write_text(measurement, encoding="utf-8")
    profile = tmp_path / "chrome-profile"
    completed = subprocess.run(
        [
            chrome,
            "--headless",
            "--no-sandbox",
            "--disable-gpu",
            "--allow-file-access-from-files",
            "--window-size=800,1000",
            f"--user-data-dir={profile}",
            "--virtual-time-budget=1000",
            "--dump-dom",
            instrumented.as_uri(),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    match = re.search(r'<body[^>]+data-viewport="(\d+)"[^>]+data-scroll="(\d+)"', completed.stdout)
    assert match is not None, completed.stdout
    viewport, scroll = map(int, match.groups())
    assert viewport == 390
    assert scroll <= viewport


def test_cli_invalid_input_returns_two_and_leaves_no_reserved_artifacts(
    suite_copy: Path, tmp_path: Path
) -> None:
    suite_copy.write_text('{"schema_version":"1.0","schema_version":"1.0"}', encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    third_party = output / "keep.txt"
    third_party.write_text("preserve", encoding="utf-8")
    completed = run_cli(suite_copy, output)
    assert completed.returncode == 2
    assert "ERROR code=SUITE_JSON_INVALID" in completed.stderr
    assert third_party.read_text(encoding="utf-8") == "preserve"
    assert not (output / "report.json").exists()
    assert not (output / "report.html").exists()


def test_cli_reports_trace_truncation_without_artifacts(suite_copy: Path, tmp_path: Path) -> None:
    trace = suite_copy.parent / "traces/bounded-update.candidate.jsonl"
    trace.write_text(trace.read_text(encoding="utf-8").rstrip()[:-1], encoding="utf-8")
    output = tmp_path / "out"
    completed = run_cli(suite_copy, output)
    assert completed.returncode == 2
    assert "ERROR code=TRACE_TRUNCATED" in completed.stderr
    assert not output.exists()


def test_cli_output_path_failure_is_stable_contract_error(suite_copy: Path, tmp_path: Path) -> None:
    output_file = tmp_path / "not-a-directory"
    output_file.write_text("file", encoding="utf-8")
    completed = run_cli(suite_copy, output_file)
    assert completed.returncode == 2
    assert "ERROR code=REPORT_WRITE_FAILED" in completed.stderr
