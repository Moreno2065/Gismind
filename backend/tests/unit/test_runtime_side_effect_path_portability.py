from pathlib import Path, PurePosixPath

import pytest

import app.runtime.compiler as compiler_module
from app.runtime.compiler import CompileError, Compiler
from app.runtime.contracts import ArtifactRef, PlannerDraft, PlannerStepDraft, ToolContractRegistry


class _PosixReviewPath(PurePosixPath):
    """Exercise POSIX lexical behavior without changing the process platform."""

    def resolve(self):
        return self if self.is_absolute() else _PosixReviewPath("/workspace") / self


def _compile_publish(path, root):
    return Compiler(ToolContractRegistry.with_builtins(), policy={"allowed_path_root": root}).compile(
        PlannerDraft(run_id="path-review", input_revision=1, user_goal="Publish a file", origin="test", tasks=[
            PlannerStepDraft(task_key="publish", op="publish_file",
                inputs={"input": ArtifactRef(artifact_id="source", type="File")}, params={"path": path}),
        ])
    )


@pytest.mark.parametrize("path_flavor", ["native", "posix"])
@pytest.mark.parametrize("destination", [
    "../escape.gpkg", "safe/../../escape.gpkg", r"..\escape.gpkg",
    r"safe\..\escape.gpkg", r"safe/..\escape.gpkg", r"safe\../escape.gpkg",
    "/outside.gpkg", r"\outside.gpkg", r"C:\outside.gpkg", "C:/outside.gpkg",
    "C:relative.gpkg", "C:", r"\\server\share\outside.gpkg", "//server/share/outside.gpkg",
    r"\\?\C:\outside.gpkg", r"\\.\C:\outside.gpkg",
])
def test_publish_rejects_windows_and_posix_escape_on_every_host(tmp_path, monkeypatch, path_flavor, destination):
    root = str(tmp_path)
    if path_flavor == "posix":
        monkeypatch.setattr(compiler_module, "Path", _PosixReviewPath)
        root = "/workspace/allowed"
    with pytest.raises(CompileError) as error:
        _compile_publish(destination, root)
    assert error.value.code == "INVALID_PARAMETER"
    assert error.value.path.endswith(".params.path")


@pytest.mark.parametrize("destination", ["result.gpkg", "nested/result.gpkg", "./nested/result.gpkg"])
def test_publish_keeps_confined_relative_paths_working(tmp_path, destination):
    plan = _compile_publish(destination, str(tmp_path))
    assert plan.tasks[0].params["path"] == str((Path(tmp_path) / destination).resolve())


def test_windows_process_tests_are_explicitly_platform_scoped():
    import tests.unit.test_runtime_phase1i_windows_fail_closed as windows_tests

    marks = getattr(windows_tests, "pytestmark", [])
    marks = marks if isinstance(marks, list) else [marks]
    assert any(mark.name == "skipif" for mark in marks)
