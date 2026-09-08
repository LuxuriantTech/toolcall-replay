from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path


ALLOWED_PROJECT_ROOTS = {"toolcall_replay", "toolcall_replay_lab"}
ALLOWED_HTTP_IMPORTS = {"http.HTTPStatus", "http.server"}
FORBIDDEN_ROOTS = {
    "asyncio",
    "ftplib",
    "imaplib",
    "importlib",
    "nntplib",
    "poplib",
    "requests",
    "smtplib",
    "socket",
    "ssl",
    "subprocess",
    "telnetlib",
    "urllib",
    "webbrowser",
    "wsgiref",
    "xmlrpc",
}


def _import_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level:
        return []
    module = node.module or ""
    return [f"{module}.{alias.name}" if module else alias.name for alias in node.names]


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    runtime = repository / "src/toolcall_replay_lab"
    project = repository / "pyproject.toml"
    violations: list[str] = []
    try:
        dependencies = (
            tomllib.loads(project.read_text(encoding="utf-8"))
            .get("project", {})
            .get("dependencies", [])
        )
        if dependencies:
            violations.extend(f"pyproject.toml:{dependency}" for dependency in dependencies)
        for path in sorted(runtime.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                for name in _import_names(node):
                    root = name.split(".", 1)[0]
                    forbidden = (
                        root in FORBIDDEN_ROOTS
                        or name.startswith("http.client")
                        or (
                            root == "http"
                            and name not in ALLOWED_HTTP_IMPORTS
                            and not name.startswith("http.server.")
                        )
                        or (
                            root not in sys.stdlib_module_names
                            and root not in ALLOWED_PROJECT_ROOTS
                        )
                    )
                    if forbidden:
                        violations.append(f"{path.relative_to(repository)}:{name}")
    except (OSError, SyntaxError, tomllib.TOMLDecodeError) as error:
        print(f"lab runtime audit could not read source: {type(error).__name__}", file=sys.stderr)
        return 1
    if violations:
        print("forbidden lab runtime imports: " + ", ".join(violations), file=sys.stderr)
        return 1
    print("lab runtime imports: stdlib and replay engine only; outbound clients absent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
