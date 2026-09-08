from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from conftest import ROOT


BUILD_SCRIPT = ROOT / "scripts/build_site.py"
SOURCE_DATE_EPOCH = 1_580_601_600
ASSETS = {
    "app.js": b'const label = "trace";\n',
    "index.html": b"<!doctype html>\n<title>Trace lab</title>\n",
    "styles.css": b"body { color: #10243e; }\n",
}
MAX_ASSET_BYTES = 1_000_000


def _project_copy(tmp_path: Path) -> Path:
    assert BUILD_SCRIPT.is_file()
    project = tmp_path / "project"
    script = project / "scripts/build_site.py"
    script.parent.mkdir(parents=True)
    shutil.copy2(BUILD_SCRIPT, script)
    static = project / "src/toolcall_replay_lab/static"
    static.mkdir(parents=True)
    for name, payload in ASSETS.items():
        (static / name).write_bytes(payload)
    return project


def _run_build(project: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(project / "scripts/build_site.py"),
            "--out-dir",
            str(output),
        ],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_raw(project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(project / "scripts/build_site.py"),
            *arguments,
        ],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_bounded_error(
    completed: subprocess.CompletedProcess[str], code: str, output: Path
) -> None:
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.startswith(f"ERROR {code}: ")
    assert completed.stderr.endswith("\n")
    assert len(completed.stderr.splitlines()) == 1
    assert len(completed.stderr.encode("utf-8")) <= 160
    assert "Traceback" not in completed.stderr
    assert not os.path.lexists(output)
    assert not list(output.parent.glob(f".{output.name}.*"))


def test_production_index_suppresses_an_unsolicited_favicon_request() -> None:
    index = (ROOT / "src/toolcall_replay_lab/static/index.html").read_text(encoding="utf-8")

    assert '<link rel="icon" href="data:," />' in index


def test_site_build_is_canonical_and_path_independent(tmp_path: Path) -> None:
    alpha = _project_copy(tmp_path / "alpha")
    beta = _project_copy(tmp_path / "beta")
    for path in alpha.rglob("*"):
        os.utime(path, (1_700_000_000, 1_700_000_000), follow_symlinks=False)
    for path in beta.rglob("*"):
        os.utime(path, (1_800_000_000, 1_800_000_000), follow_symlinks=False)

    alpha_output = tmp_path / "alpha-site"
    beta_output = tmp_path / "beta-site"
    alpha_result = _run_build(alpha, alpha_output)
    beta_result = _run_build(beta, beta_output)

    assert alpha_result.returncode == beta_result.returncode == 0
    assert alpha_result.stdout == beta_result.stdout == "SITE_BUILD_OK\n"
    assert alpha_result.stderr == beta_result.stderr == ""
    assert _tree_bytes(alpha_output) == _tree_bytes(beta_output)
    assert set(_tree_bytes(alpha_output)) == {*ASSETS, "asset-manifest.json"}

    manifest_value = {
        "assets": [
            {
                "path": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
            for name, payload in sorted(ASSETS.items())
        ],
        "schema_version": 1,
    }
    expected_manifest = (
        json.dumps(manifest_value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    assert (alpha_output / "asset-manifest.json").read_bytes() == expected_manifest

    for output in (alpha_output, beta_output):
        assert stat.S_IMODE(output.stat().st_mode) == 0o755
        assert int(output.stat().st_mtime) == SOURCE_DATE_EPOCH
        for path in output.iterdir():
            assert path.is_file() and not path.is_symlink()
            assert stat.S_IMODE(path.stat().st_mode) == 0o644
            assert int(path.stat().st_mtime) == SOURCE_DATE_EPOCH


def test_site_build_refuses_existing_output_without_altering_it(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    output = tmp_path / "site"
    output.mkdir()
    sentinel = output / "owner.txt"
    sentinel.write_text("keep\n", encoding="utf-8")

    completed = _run_build(project, output)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "ERROR OUTPUT_EXISTS: output path must not exist\n"
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_site_build_refuses_symlink_output(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    output = tmp_path / "site"
    output.symlink_to(target, target_is_directory=True)

    completed = _run_build(project, output)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "ERROR OUTPUT_EXISTS: output path must not exist\n"
    assert output.is_symlink()
    assert list(target.iterdir()) == []


def test_site_build_refuses_source_asset_symlink_without_partial_output(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    asset = project / "src/toolcall_replay_lab/static/app.js"
    asset.unlink()
    asset.symlink_to("app.js")
    output = tmp_path / "site"

    completed = _run_build(project, output)

    _assert_bounded_error(completed, "SOURCE_INVALID", output)


def test_site_build_refuses_symlink_source_directory_without_partial_output(
    tmp_path: Path,
) -> None:
    project = _project_copy(tmp_path)
    source = project / "src/toolcall_replay_lab/static"
    moved = project / "static-real"
    source.rename(moved)
    source.symlink_to(moved, target_is_directory=True)
    output = tmp_path / "site"

    completed = _run_build(project, output)

    _assert_bounded_error(completed, "SOURCE_INVALID", output)


def test_site_build_refuses_unexpected_source_entry_without_partial_output(
    tmp_path: Path,
) -> None:
    project = _project_copy(tmp_path)
    (project / "src/toolcall_replay_lab/static/debug.txt").write_text(
        "not shipped\n", encoding="utf-8"
    )
    output = tmp_path / "site"

    completed = _run_build(project, output)

    _assert_bounded_error(completed, "SOURCE_INVALID", output)


def test_site_build_refuses_oversized_source_asset_before_publication(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    (project / "src/toolcall_replay_lab/static/app.js").write_bytes(b"x" * (MAX_ASSET_BYTES + 1))
    output = tmp_path / "site"

    completed = _run_build(project, output)

    _assert_bounded_error(completed, "SOURCE_TOO_LARGE", output)


def test_site_build_accepts_asset_at_individual_limit(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    payload = b"x" * MAX_ASSET_BYTES
    (project / "src/toolcall_replay_lab/static/app.js").write_bytes(payload)
    output = tmp_path / "site"

    completed = _run_build(project, output)

    assert completed.returncode == 0, completed.stderr
    assert (output / "app.js").read_bytes() == payload


def test_site_build_refuses_total_asset_budget_before_publication(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    static = project / "src/toolcall_replay_lab/static"
    for name in ASSETS:
        (static / name).write_bytes(b"x" * 700_000)
    output = tmp_path / "site"

    completed = _run_build(project, output)

    _assert_bounded_error(completed, "SOURCE_TOO_LARGE", output)


def test_site_build_refuses_invalid_output_parent_without_partial_output(
    tmp_path: Path,
) -> None:
    project = _project_copy(tmp_path)
    loop = tmp_path / "loop"
    loop.symlink_to("loop")
    output = loop / "site"

    completed = _run_build(project, output)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "ERROR OUTPUT_INVALID: output parent directory is invalid\n"
    assert len(completed.stderr.splitlines()) == 1
    assert "Traceback" not in completed.stderr
    assert not os.path.lexists(output)


def test_site_build_bounds_invalid_arguments(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)

    completed = _run_raw(project, "--unknown")

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "ERROR ARGUMENT_INVALID: command arguments are invalid\n"
    assert len(completed.stderr.splitlines()) == 1


def test_site_build_bounds_publication_failure_without_partial_output(tmp_path: Path) -> None:
    project = _project_copy(tmp_path)
    parent = tmp_path / "read-only"
    parent.mkdir()
    parent.chmod(0o500)
    output = parent / "site"
    try:
        completed = _run_build(project, output)
    finally:
        parent.chmod(0o700)

    _assert_bounded_error(completed, "OUTPUT_INVALID", output)
