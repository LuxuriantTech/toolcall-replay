from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

FORBIDDEN_RUNTIME_ROOTS = {
    "asyncio",
    "urllib",
    "http",
    "nntplib",
    "socket",
    "socketserver",
    "ssl",
    "ftplib",
    "smtplib",
    "imaplib",
    "poplib",
    "telnetlib",
    "subprocess",
    "webbrowser",
    "wsgiref",
    "xmlrpc",
}
root = Path(__file__).resolve().parents[1] / "src" / "toolcall_replay"
project = Path(__file__).resolve().parents[1] / "pyproject.toml"
stdlib = set(sys.stdlib_module_names)
violations: list[str] = []
dependencies = (
    tomllib.loads(project.read_text(encoding="utf-8")).get("project", {}).get("dependencies", [])
)
if dependencies:
    violations.extend(f"pyproject.toml:{dependency}" for dependency in dependencies)
for path in root.rglob("*.py"):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [item.name.split(".")[0] for item in node.names]
                if isinstance(node, ast.Import)
                else [str(node.module).split(".")[0]]
            )
            for name in names:
                if name in FORBIDDEN_RUNTIME_ROOTS or name not in stdlib:
                    violations.append(f"{path}:{name}")
if violations:
    print("forbidden runtime imports: " + ", ".join(violations), file=sys.stderr)
    raise SystemExit(1)
print("direct runtime imports: stdlib only; forbidden direct roots absent")
