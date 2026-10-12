"""File Artifact downloads must not execute uploader-provided content types."""

import pytest
from fastapi.testclient import TestClient

from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor


@pytest.fixture
def client(tmp_path):
    from app.main import create_app

    runtime = RuntimeStore(RuntimeDB(tmp_path / "runtime.db"))
    runtime.initialize()
    artifacts = ArtifactStore(tmp_path / "data", runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, isolate=False, autostart=False)
    with TestClient(create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor)) as value:
        yield value


@pytest.mark.parametrize("range_header,expected_status", [(None, 200), ("bytes=0-12", 206)])
def test_uploaded_file_download_does_not_render_client_supplied_html_mime(client, range_header, expected_status):
    payload = b'name,value\n"<script>alert(1)</script>",1\n'
    response = client.post(
        "/api/artifacts/uploads",
        files={"file": ("table.CSV", payload, "text/html")},
    )
    assert response.status_code == 201, response.text
    artifact = response.json()

    # UploadFile.content_type is supplied by the requester, not by inspection.
    # A valid CSV can contain HTML-like cell text; content must be a download.
    headers = {"Range": range_header} if range_header else {}
    downloaded = client.get(artifact["content_url"], headers=headers)
    assert downloaded.status_code == expected_status
    assert downloaded.headers["content-type"] == "application/octet-stream"
    assert downloaded.headers["content-disposition"].startswith("attachment")
    assert downloaded.headers["x-content-type-options"] == "nosniff"
