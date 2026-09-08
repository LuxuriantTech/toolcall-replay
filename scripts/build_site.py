from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Never


ASSET_NAMES = ("app.js", "index.html", "styles.css")
MAX_ASSET_BYTES = 1_000_000
MAX_TOTAL_ASSET_BYTES = 2_000_000
SOURCE_DATE_EPOCH = 1_580_601_600


class SiteBuildError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        raise SiteBuildError("ARGUMENT_INVALID", "command arguments are invalid")


def _manifest(assets: dict[str, bytes]) -> bytes:
    value = {
        "assets": [
            {
                "path": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
            for name, payload in sorted(assets.items())
        ],
        "schema_version": 1,
    }
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_file(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(0o644)
    os.utime(path, (SOURCE_DATE_EPOCH, SOURCE_DATE_EPOCH), follow_symlinks=False)


def _read_asset(directory_fd: int, name: str, consumed: int) -> bytes:
    descriptor = -1
    try:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow == 0:
            raise SiteBuildError("SOURCE_INVALID", "site source cannot be admitted safely")
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | nofollow,
            dir_fd=directory_fd,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SiteBuildError("SOURCE_INVALID", "site source content is invalid")
        if before.st_size > MAX_ASSET_BYTES or consumed + before.st_size > MAX_TOTAL_ASSET_BYTES:
            raise SiteBuildError("SOURCE_TOO_LARGE", "site source exceeds admission limits")
        payload = bytearray()
        while block := os.read(descriptor, min(65_536, before.st_size - len(payload) + 1)):
            payload.extend(block)
            if len(payload) > before.st_size or len(payload) > MAX_ASSET_BYTES:
                raise SiteBuildError("SOURCE_INVALID", "site source changed while being read")
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(payload) != before.st_size or before_identity != after_identity:
            raise SiteBuildError("SOURCE_INVALID", "site source changed while being read")
        return bytes(payload)
    except SiteBuildError:
        raise
    except OSError as error:
        raise SiteBuildError("SOURCE_INVALID", "site source content is invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_assets(source: Path) -> dict[str, bytes]:
    directory_fd = -1
    try:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        if nofollow == 0 or directory_flag == 0:
            raise SiteBuildError("SOURCE_INVALID", "site source cannot be admitted safely")
        directory_fd = os.open(
            source,
            os.O_RDONLY | os.O_CLOEXEC | nofollow | directory_flag,
        )
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            raise SiteBuildError("SOURCE_INVALID", "site source directory is invalid")
        with os.scandir(directory_fd) as entries:
            names = sorted(entry.name for entry in entries)
        if names != sorted(ASSET_NAMES):
            raise SiteBuildError("SOURCE_INVALID", "site source inventory is invalid")
        assets: dict[str, bytes] = {}
        consumed = 0
        for name in ASSET_NAMES:
            payload = _read_asset(directory_fd, name, consumed)
            consumed += len(payload)
            assets[name] = payload
        return assets
    except SiteBuildError:
        raise
    except OSError as error:
        raise SiteBuildError("SOURCE_INVALID", "site source directory is invalid") from error
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)


def build_site(output: Path) -> None:
    source = Path(__file__).parent.parent / "src/toolcall_replay_lab/static"
    if os.path.lexists(output):
        raise SiteBuildError("OUTPUT_EXISTS", "output path must not exist")
    try:
        parent = output.parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SiteBuildError("OUTPUT_INVALID", "output parent directory is invalid") from error
    if not parent.is_dir() or not output.name:
        raise SiteBuildError("OUTPUT_INVALID", "output parent directory is invalid")
    planned_output = parent / output.name
    assets = _read_assets(source)
    staging: Path | None = None
    published = False
    try:
        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=parent))
        for name, payload in assets.items():
            _write_file(staging / name, payload)
        _write_file(staging / "asset-manifest.json", _manifest(assets))
        staging.chmod(0o755)
        os.utime(staging, (SOURCE_DATE_EPOCH, SOURCE_DATE_EPOCH), follow_symlinks=False)
        if os.path.lexists(planned_output):
            raise SiteBuildError("OUTPUT_EXISTS", "output path must not exist")
        os.replace(staging, planned_output)
        published = True
    except SiteBuildError:
        raise
    except OSError as error:
        raise SiteBuildError("OUTPUT_INVALID", "output directory could not be published") from error
    finally:
        if not published and staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description="Build the local ToolCall Replay trace lab")
    parser.add_argument("--out-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        build_site(args.out_dir)
    except SiteBuildError as error:
        print(f"ERROR {error.code}: {error.message}", file=sys.stderr)
        return 2
    print("SITE_BUILD_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
