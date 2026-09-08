from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import ReplayError
from .report import build_report, write_reports
from .suite import load_suite

MAX_ERROR_FIELD_CHARS = 512


def _one_line(value: str) -> str:
    escaped = json.dumps(value, ensure_ascii=True)[1:-1]
    if len(escaped) <= MAX_ERROR_FIELD_CHARS:
        return escaped
    return escaped[: MAX_ERROR_FIELD_CHARS - 3] + "..."


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="toolcall-replay")
    command = parser.add_subparsers(dest="command", required=True)
    evaluate = command.add_parser("evaluate")
    evaluate.add_argument("suite")
    evaluate.add_argument("--output-dir", required=True)
    parsed = parser.parse_args(arguments)
    try:
        suite_path = Path(parsed.suite)
        suite = load_suite(suite_path)
        report = build_report(suite)
        write_reports(report, Path(parsed.output_dir))
        print(
            f"VERDICT suite={suite.suite_id} digest={report['digest']} json=report.json html=report.html"
        )
        return 0 if report["verdict"] == "PASS" else 1
    except ReplayError as error:
        print(
            f"ERROR code={_one_line(error.code)} location={_one_line(error.location)} "
            f"message={_one_line(error.message)}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
