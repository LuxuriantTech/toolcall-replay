from __future__ import annotations

import hashlib
import html
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

from .errors import ReplayError
from .jsonutil import JsonValue
from .models import Evaluation, Suite
from .rules import evaluate_trace, evaluation_budget

MAX_REPORT_BYTES = 2_000_000
MAX_LOGICAL_REPORT_BYTES = 2_000_000


def _report_too_large(output: Path) -> ReplayError:
    return ReplayError(
        "REPORT_TOO_LARGE",
        str(output),
        f"report exceeds {MAX_REPORT_BYTES}-byte limit",
    )


def _logical_digest(logical: dict[str, JsonValue], location: str) -> str:
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256()
    size = 0
    for chunk in encoder.iterencode(logical):
        encoded = chunk.encode("utf-8")
        if len(encoded) > MAX_LOGICAL_REPORT_BYTES - size:
            raise ReplayError(
                "REPORT_TOO_LARGE",
                location,
                f"logical report exceeds {MAX_LOGICAL_REPORT_BYTES}-byte limit",
            )
        digest.update(encoded)
        size += len(encoded)
    return digest.hexdigest()


def _evaluation(value: Evaluation) -> dict[str, JsonValue]:
    rules = cast(
        JsonValue,
        [
            {
                "rule_id": item.rule_id,
                "type": item.type,
                "verdict": item.verdict,
                "message": item.message,
                "evidence": item.evidence,
            }
            for item in value.rules
        ],
    )
    return {
        "verdict": value.verdict,
        "rules": rules,
    }


def build_report(suite: Suite) -> dict[str, JsonValue]:
    cases: list[JsonValue] = []
    all_pass = True
    budget = evaluation_budget()
    for case in suite.cases:
        baseline = evaluate_trace(case, case.baseline_trace, budget)
        candidate = evaluate_trace(case, case.candidate_trace, budget)
        all_pass = all_pass and baseline.verdict == "PASS" and candidate.verdict == "PASS"
        new: list[JsonValue] = []
        resolved: list[JsonValue] = []
        unchanged: list[JsonValue] = []
        for left, right in zip(baseline.rules, candidate.rules, strict=True):
            if left.verdict == "PASS" and right.verdict == "FAIL":
                new.append(left.rule_id)
            elif left.verdict == "FAIL" and right.verdict == "PASS":
                resolved.append(left.rule_id)
            elif left.verdict == right.verdict == "FAIL":
                unchanged.append(left.rule_id)
        cases.append(
            {
                "case_id": case.case_id,
                "baseline": _evaluation(baseline),
                "candidate": _evaluation(candidate),
                "diff": {
                    "new_failures": new,
                    "resolved_failures": resolved,
                    "unchanged_failures": unchanged,
                },
            }
        )
    logical: dict[str, JsonValue] = {
        "schema_version": "1.0",
        "suite_id": suite.suite_id,
        "verdict": "PASS" if all_pass else "FAIL",
        "cases": cases,
    }
    return {**logical, "digest": _logical_digest(logical, suite.suite_id)}


def _html_fragments(report: dict[str, JsonValue]) -> Iterator[str]:
    def objects(value: JsonValue) -> list[dict[str, JsonValue]]:
        return cast(list[dict[str, JsonValue]], value)

    def object_(value: JsonValue) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], value)

    def strings(value: JsonValue) -> list[str]:
        return cast(list[str], value)

    def escaped_fragments(value: str) -> Iterator[str]:
        for offset in range(0, len(value), 4_096):
            yield html.escape(value[offset : offset + 4_096], quote=True)

    def badge(verdict: JsonValue, label: str) -> str:
        escaped = html.escape(cast(str, verdict), quote=True)
        return (
            f'<span class="badge" data-verdict="{escaped}" aria-label="{label}: {escaped}">'
            f"{escaped}</span>"
        )

    def count_summary(evaluation: dict[str, JsonValue]) -> str:
        rules = objects(evaluation["rules"])
        passed = sum(rule["verdict"] == "PASS" for rule in rules)
        return f"{passed} / {len(rules)} règles réussies"

    def evidence_fragments(value: JsonValue) -> Iterator[str]:
        yield '<details class="evidence"><summary>Preuves détaillées</summary><pre><code>'
        encoder = json.JSONEncoder(
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        for chunk in encoder.iterencode(value):
            yield from escaped_fragments(chunk)
        yield "</code></pre></details>"

    def rule_result_fragments(label: str, rule: dict[str, JsonValue]) -> Iterator[str]:
        yield (
            '<article class="rule-result">'
            f'<div class="result-heading"><h5>{label}</h5>{badge(rule["verdict"], f"Verdict {label}")}</div>'
        )
        yield '<p class="message">'
        yield from escaped_fragments(cast(str, rule["message"]))
        yield "</p>"
        yield from evidence_fragments(rule["evidence"])
        yield "</article>"

    cases = objects(report["cases"])
    verdict = html.escape(cast(str, report["verdict"]), quote=True)
    stylesheet = """
:root { color-scheme: light; --ink: #172033; --muted: #5a6475; --line: #d8dee8; --panel: #ffffff; --canvas: #f4f6fa; --pass: #16724a; --pass-bg: #e7f7ef; --fail: #a52a2a; --fail-bg: #ffeded; }
*, *::before, *::after { box-sizing: border-box; }
html { background: var(--canvas); color: var(--ink); font-family: ui-sans-serif, system-ui, sans-serif; }
body { margin: 0; }
.report { width: min(100% - 2rem, 72rem); max-width: 100%; margin: 0 auto; padding: 2rem 0 4rem; }
.hero, .overview, .case { min-width: 0; background: var(--panel); border: 1px solid var(--line); border-radius: 1rem; box-shadow: 0 0.5rem 1.5rem rgba(23, 32, 51, 0.06); }
.hero { padding: clamp(1.25rem, 4vw, 2.5rem); }
.eyebrow { margin: 0 0 0.5rem; color: var(--muted); font-size: 0.78rem; font-weight: 750; letter-spacing: 0.08em; text-transform: uppercase; }
h1, h2, h3, h4, h5, p { overflow-wrap: anywhere; }
h1 { margin: 0 0 1.25rem; font-size: clamp(2rem, 7vw, 3.5rem); line-height: 1; }
h2, h3, h4, h5 { margin-top: 0; }
.global-verdict { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 0.75rem; padding: 1rem; border: 1px solid var(--line); border-radius: 0.75rem; font-size: 1.1rem; }
.digest { color: var(--muted); font-size: 0.85rem; }
.digest code { display: inline; }
.overview, .case { margin-top: 1rem; padding: clamp(1rem, 3vw, 2rem); }
.comparison-list, .comparison-grid, .rule-grid { display: grid; gap: 1rem; min-width: 0; }
.comparison-list { grid-template-columns: repeat(auto-fit, minmax(min(100%, 22rem), 1fr)); }
.comparison-card, .rule { min-width: 0; border: 1px solid var(--line); border-radius: 0.75rem; padding: 1rem; }
.comparison-grid, .rule-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.trace-summary { display: grid; grid-template-columns: 1fr auto; gap: 0.35rem 0.75rem; align-items: center; min-width: 0; }
.trace-summary strong { grid-column: 1 / -1; font-size: 0.88rem; }
.diff-summary { margin: 1rem 0 0; color: var(--muted); }
.rule { margin-top: 0.75rem; }
.rule-heading, .result-heading { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 0.5rem; align-items: baseline; }
.rule-heading h4, .result-heading h5 { margin-bottom: 0.5rem; }
.rule-type { color: var(--muted); font-size: 0.8rem; }
.rule-result { min-width: 0; padding: 0.9rem; background: var(--canvas); border-radius: 0.6rem; }
.message { min-height: 2.5em; margin: 0.5rem 0; }
.badge { display: inline-flex; width: fit-content; padding: 0.25rem 0.55rem; border-radius: 999px; font-size: 0.75rem; font-weight: 800; letter-spacing: 0.04em; }
.badge[data-verdict="PASS"] { color: var(--pass); background: var(--pass-bg); }
.badge[data-verdict="FAIL"] { color: var(--fail); background: var(--fail-bg); }
code, pre { max-width: 100%; overflow-wrap: anywhere; word-break: break-word; }
details { min-width: 0; }
summary { cursor: pointer; color: var(--muted); font-weight: 650; }
pre { overflow: auto; margin: 0.75rem 0 0; padding: 0.75rem; border: 1px solid var(--line); border-radius: 0.5rem; background: var(--panel); white-space: pre-wrap; font-size: 0.76rem; }
@media (max-width: 40rem) { .report { width: min(100% - 1rem, 72rem); padding-top: 0.5rem; } .comparison-grid, .rule-grid { grid-template-columns: minmax(0, 1fr); } .hero, .overview, .case { border-radius: 0.65rem; } }
""".strip()
    yield (
        "<!doctype html>\n"
        '<html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        f"<title>ToolCall Replay · {verdict}</title><style>{stylesheet}</style></head><body>"
        '<main class="report"><header class="hero">'
        '<p class="eyebrow">Suite <code>'
    )
    yield from escaped_fragments(cast(str, report["suite_id"]))
    yield (
        "</code></p>"
        "<h1>ToolCall Replay</h1>"
        f'<div class="global-verdict" aria-label="Verdict global" data-verdict="{verdict}">'
        f"<span>Verdict global</span><strong>{verdict}</strong></div>"
        '<p class="digest">Digest déterministe : <code>'
    )
    yield from escaped_fragments(cast(str, report["digest"]))
    yield (
        "</code></p>"
        "</header>"
        '<section class="overview" aria-labelledby="overview-title">'
        '<h2 id="overview-title">Comparaison baseline / candidate</h2>'
        '<div class="comparison-list">'
    )
    for case in cases:
        baseline = object_(case["baseline"])
        candidate = object_(case["candidate"])
        diff = object_(case["diff"])
        new_failures = strings(diff["new_failures"])
        resolved_failures = strings(diff["resolved_failures"])
        unchanged_failures = strings(diff["unchanged_failures"])
        yield '<article class="comparison-card"><h3><code>'
        yield from escaped_fragments(cast(str, case["case_id"]))
        yield (
            "</code></h3>"
            '<div class="comparison-grid">'
            '<div class="trace-summary"><span>Baseline</span>'
            f"{badge(baseline['verdict'], 'Verdict baseline')}"
            f"<strong>{count_summary(baseline)}</strong></div>"
            '<div class="trace-summary"><span>Candidate</span>'
            f"{badge(candidate['verdict'], 'Verdict candidate')}"
            f"<strong>{count_summary(candidate)}</strong></div>"
            "</div>"
            f'<p class="diff-summary"><strong>{len(new_failures)} nouveaux échecs</strong>'
            f" · {len(resolved_failures)} résolus · {len(unchanged_failures)} inchangés</p>"
            "</article>"
        )
    yield "</div></section>"
    for case_index, case in enumerate(cases):
        baseline = object_(case["baseline"])
        candidate = object_(case["candidate"])
        yield (
            f'<section class="case" aria-labelledby="case-{case_index}">'
            '<header class="case-heading">'
            f'<p class="eyebrow">Cas {case_index + 1}</p>'
            f'<h2 id="case-{case_index}"><code>'
        )
        yield from escaped_fragments(cast(str, case["case_id"]))
        yield ('</code></h2></header><h3 class="rules-title">Résultats par règle</h3>')
        baseline_rules = objects(baseline["rules"])
        candidate_rules = objects(candidate["rules"])
        for rule_index, (baseline_rule, candidate_rule) in enumerate(
            zip(baseline_rules, candidate_rules, strict=True)
        ):
            heading_id = f"case-{case_index}-rule-{rule_index}"
            yield (
                f'<section class="rule" aria-labelledby="{heading_id}">'
                '<div class="rule-heading">'
                f'<h4 id="{heading_id}"><code>'
            )
            yield from escaped_fragments(cast(str, baseline_rule["rule_id"]))
            yield '</code></h4><span class="rule-type">'
            yield from escaped_fragments(cast(str, baseline_rule["type"]))
            yield '</span></div><div class="rule-grid">'
            yield from rule_result_fragments("Baseline", baseline_rule)
            yield from rule_result_fragments("Candidate", candidate_rule)
            yield "</div></section>"
        yield "</section>"
    yield "</main></body></html>\n"


def _html(report: dict[str, JsonValue]) -> str:
    return "".join(_html_fragments(report))


def _bounded_html(report: dict[str, JsonValue], limit: int, output: Path) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for fragment in _html_fragments(report):
        encoded = fragment.encode("utf-8")
        if len(encoded) > limit - size:
            raise _report_too_large(output)
        chunks.append(encoded)
        size += len(encoded)
    return b"".join(chunks)


def _atomic(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".toolcall-replay-", dir=path.parent)
    try:
        try:
            output = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_reports(report: dict[str, JsonValue], output: Path) -> None:
    try:
        encoder = json.JSONEncoder(
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        chunks: list[bytes] = []
        rendered_size = 0
        for chunk in encoder.iterencode(report):
            encoded = chunk.encode("utf-8")
            if len(encoded) > MAX_REPORT_BYTES - rendered_size:
                raise _report_too_large(output)
            chunks.append(encoded)
            rendered_size += len(encoded)
        if rendered_size == MAX_REPORT_BYTES:
            raise _report_too_large(output)
        chunks.append(b"\n")
        rendered = b"".join(chunks)
        rendered_html = _bounded_html(report, MAX_REPORT_BYTES - len(rendered), output)
        output.mkdir(parents=True, exist_ok=True)
        if not output.is_dir():
            raise OSError("output is not a directory")
        _atomic(output / "report.json", rendered)
        _atomic(output / "report.html", rendered_html)
    except OSError as error:
        raise ReplayError("REPORT_WRITE_FAILED", str(output), "report write failed") from error
