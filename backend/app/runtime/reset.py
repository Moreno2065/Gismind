"""Destructive reset for Runtime-managed SQLite/CAS state only."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from app.config import get_settings

REQUIRED = ("data_root", "runtime_db", "artifact_dir", "staging_dir", "quarantine_dir", "upload_dir")
LOCKS = ("runtime.worker.lock", "runtime.api.lock")


def _managed_paths():
    configured = dict(get_settings().runtime_paths)
    root = Path(configured["data_root"]).resolve()
    manifest_path = root / "runtime_paths.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"runtime path manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if any(key not in manifest for key in REQUIRED):
        raise RuntimeError("runtime path manifest is incomplete")
    if Path(manifest["data_root"]).resolve() != root:
        raise RuntimeError("runtime path manifest data_root mismatch")
    targets = [Path(manifest[key]).resolve() for key in REQUIRED[1:]]
    for target in targets:
        try: target.relative_to(root)
        except ValueError as exc: raise RuntimeError(f"managed target escapes APP_DATA_ROOT: {target}") from exc
    active = [root / name for name in LOCKS if (root / name).exists()]
    if active:
        raise RuntimeError("active Runtime lock exists: " + ", ".join(map(str, active)))
    return root, targets


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reset managed Gismind Runtime state")
    parser.add_argument("--confirm-destructive-reset", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_destructive_reset:
        print("refusing destructive reset: --confirm-destructive-reset is required", file=sys.stderr)
        return 2
    try:
        _, targets = _managed_paths()
        deleted = []
        db = targets[0]
        for candidate in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
            if candidate.exists(): candidate.unlink(); deleted.append(str(candidate))
        for directory in targets[1:]:
            if directory.exists(): shutil.rmtree(directory); deleted.append(str(directory))
        for path in deleted: print(f"deleted {path}")
        return 0
    except Exception as exc:
        print(f"reset refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
