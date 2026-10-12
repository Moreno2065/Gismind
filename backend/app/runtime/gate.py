"""Phase 6/7 deletion gates.

The pre-delete gate is read-only and blocks migration when the new Runtime
inventory is not closed.  It never deletes files or connects to Redis.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath


ROOT = Path(__file__).resolve().parents[3]

# Exact V1 paths discovered after the phase-7 primary cutover.  They are not
# Runtime compatibility shims: each was unreachable and had no live import.
LEGACY_RUNTIME_PATHS = (
    "backend/app/api/memory.py",
    "backend/app/api/sessions.py",
    "backend/app/models/__init__.py",
    "backend/app/models/schemas.py",
    "backend/app/sandbox/tools.py",
    "backend/app/sandbox/__init__.py",
    "backend/app/runtime/migrate_legacy_state.py",
    "backend/app/tools/policy.py",
    "backend/workspace/run_tool_prompts.py",
    "backend/tests/support/__init__.py",
    "backend/tests/support/deterministic_llm.py",
    "frontend/e2e/awaiting-input.spec.ts",
    "frontend/e2e/component_prompts_e2e.py",
    "frontend/e2e/fixtures/nanjing_points.geojson",
    "frontend/e2e/stable-wiring.spec.ts",
    "frontend/playwright.config.ts",
    "frontend/src/components/ThinkingCollapse.tsx",
    "frontend/src/hooks/useSSE.ts",
    "frontend/src/lib/toolLabels.ts",
)

LEGACY_SOURCE_TOKENS = (
    "app.agents",
    "results_data",
    "SessionStore",
    "MemoryStore",
    "migrate_legacy_state",
    "dispatcher_llm",
    "sub_agent_llm",
    "chatStream",
    "resumeChat",
    "useSSE",
)


def pre_delete() -> list[str]:
    failures: list[str] = []
    manifest_path = ROOT / "docs" / "runtime_removal_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("phase") != 6 or manifest.get("status") != "pre-delete":
            failures.append("removal manifest is not phase-6 pre-delete")
        for entry in manifest.get("entries", []):
            path = entry.get("path", "")
            if not path or Path(path).is_absolute() or "*" in path or entry.get("approved") is not False:
                failures.append(f"manifest entry is not an unapproved exact path: {path}")
            if not entry.get("replacement_evidence"):
                failures.append(f"manifest entry lacks replacement evidence: {path}")
    except Exception as exc:
        failures.append(f"invalid removal manifest: {exc}")
    try:
        sys.path.insert(0, str(ROOT / "backend"))
        from app.runtime import ToolContractRegistry
        kernel = {"select_toolkit", "inspect_workspace", "suggest_skill", "load_skill", "proactive_clarification"}
        if {item.name for item in ToolContractRegistry.with_builtins().values()} & kernel:
            failures.append("Kernel semantic operations are present in ToolContract registry")
    except Exception as exc:
        failures.append(f"Runtime contract registry unavailable: {exc}")
    return failures


def post_delete() -> list[str]:
    """Verify the hard cutover and exact-path deletion contract."""
    failures: list[str] = []
    manifest_path = ROOT / "docs" / "runtime_removal_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("phase") != 7:
            failures.append("removal manifest is not phase-7")
        for entry in manifest.get("entries", []):
            rel = entry.get("path", "")
            parts = PureWindowsPath(rel).parts if isinstance(rel, str) else ()
            if (not rel or not isinstance(rel, str) or PureWindowsPath(rel).drive
                    or PureWindowsPath(rel).root or PurePosixPath(rel).is_absolute()
                    or ".." in parts or any(char in rel for char in "*?[]")):
                failures.append(f"manifest path is not exact relative path: {rel}")
                continue
            target = (ROOT / rel).resolve()
            if not target.is_relative_to(ROOT.resolve()):
                failures.append(f"manifest path is not exact relative path: {rel}")
                continue
            if entry.get("approved") is not True:
                failures.append(f"manifest path is not approved: {rel}")
            if rel.startswith("blackbox-results.blocked/"):
                if entry.get("target_kind") != "directory_recursive":
                    failures.append(f"blocked evidence target is not recursive directory: {rel}")
                # This is a read-only *post*-deletion check. absolute_path is
                # historical evidence, not permission to act on another host.
                # Keep it intact while allowing checkout/CI relocation.
                historical = PureWindowsPath(str(entry.get("absolute_path", "")))
                if (not historical.is_absolute() or ".." in historical.parts
                        or tuple(part.casefold() for part in historical.parts[-len(parts):])
                        != tuple(part.casefold() for part in parts)):
                    failures.append(f"blocked evidence absolute path mismatch: {rel}")
            if target.exists():
                failures.append(f"approved deletion target still exists: {rel}")
    except Exception as exc:
        failures.append(f"invalid post-delete manifest: {exc}")

    agents_root = ROOT / "backend" / "app" / "agents"
    if agents_root.exists():
        failures.append("legacy backend/app/agents package residue still exists")

    for rel in LEGACY_RUNTIME_PATHS:
        if (ROOT / rel).exists():
            failures.append(f"post-cutover V1 residue still exists: {rel}")

    for package in (ROOT / "backend" / "app" / "models", ROOT / "backend" / "app" / "sandbox", ROOT / "backend" / "tests" / "support"):
        if package.exists() and any(path.is_file() for path in package.rglob("*")):
            failures.append(f"legacy package residue still exists: {package.relative_to(ROOT)}")

    app_agents_ref = re.compile(r"(?:app\.agents|from\s+app\.agents|import\s+app\.agents)")
    for base in (ROOT / "backend" / "app", ROOT / "backend" / "tests", ROOT / "backend" / "scripts", ROOT / "frontend" / "src"):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path == Path(__file__).resolve():
                continue
            if path.suffix not in {".py", ".ts", ".tsx", ".js", ".jsx"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if app_agents_ref.search(text):
                failures.append(f"orphan app.agents reference: {path.relative_to(ROOT)}")

    for base in (ROOT / "backend" / "app", ROOT / "frontend" / "src"):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path in {Path(__file__).resolve(), ROOT / "backend" / "app" / "api" / "runtime.py"}:
                continue
            if path.suffix not in {".py", ".ts", ".tsx", ".js", ".jsx"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for token in LEGACY_SOURCE_TOKENS:
                if token in text:
                    failures.append(f"post-cutover V1 token: {path.relative_to(ROOT)}:{token}")

    forbidden = re.compile(
        r"(?:^\s*(?:from|import)\s+(?:langgraph|langchain|redis|fakeredis|celery)|"
        r"\b(?:SqliteSaver|RunController|PendingStore|EventCollector)\b|"
        r"\b(?:REDIS_URL|CELERY_BROKER_URL|CELERY_RESULT_BACKEND|APP_CHECKPOINT_DB)\s*=)",
        re.IGNORECASE,
    )
    for base in (ROOT / "backend" / "app", ROOT / "frontend" / "src"):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path == Path(__file__).resolve():
                continue
            if path.suffix not in {".py", ".ts", ".tsx", ".js", ".jsx"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                if forbidden.search(line) and not line.lstrip().startswith(("#", "//")):
                    failures.append(f"forbidden legacy reference: {path.relative_to(ROOT)}:{line_no}")
                    break
    legacy_evidence_dirs = [ROOT / "blackbox", *ROOT.glob("blackbox-results*")]
    for legacy_dir in legacy_evidence_dirs:
        if legacy_dir.exists():
            failures.append(f"legacy evidence directory still exists: {legacy_dir.relative_to(ROOT)}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("pre-delete", "post-delete"), required=True)
    args = parser.parse_args()
    failures = pre_delete() if args.phase == "pre-delete" else post_delete()
    if failures:
        print(json.dumps({"phase": args.phase, "status": "fail", "failures": failures}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"phase": args.phase, "status": "pass", "checks": ["manifest", "legacy-path-scan", "legacy-reference-scan", "evidence-scan"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
