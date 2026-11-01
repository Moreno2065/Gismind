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


@pytest.mark.parametrize("range_header,expected,content_range", [
    ("bytes=0-999", b"0123456789", "bytes 0-9/10"),
    ("bytes=4-999", b"456789", "bytes 4-9/10"),
    ("bytes=4-", b"456789", "bytes 4-9/10"),
    ("bytes=-999", b"0123456789", "bytes 0-9/10"),
])
def test_artifact_ranges_clip_satisfiable_requests_to_the_file(client, range_header, expected, content_range):
    artifacts = client.app.state.artifact_store
    source = artifacts.publish_staged(
        artifacts.stage_bytes(b"0123456789", filename="range.bin"),
        origin="upload", artifact_type="File", media_type="application/octet-stream",
    )
    response = client.get("/api/artifacts/" + source["artifact_id"] + "/content",
                          headers={"Range": range_header})
    assert response.status_code == 206
    assert response.content == expected
    assert response.headers["content-range"] == content_range
    assert int(response.headers["content-length"]) == len(expected)


@pytest.mark.parametrize("range_header", [
    "bytes=10-", "bytes=12-20", "bytes=5-4", "bytes=-0",
    "bytes=0-1,3-4", "0-3", "bytes=+1-3", "bytes=1_0-12", "bytes=0-+3",
])
def test_artifact_invalid_ranges_report_the_actual_file_length(client, range_header):
    artifacts = client.app.state.artifact_store
    source = artifacts.publish_staged(
        artifacts.stage_bytes(b"0123456789", filename="range.bin"),
        origin="upload", artifact_type="File", media_type="application/octet-stream",
    )
    response = client.get("/api/artifacts/" + source["artifact_id"] + "/content",
                          headers={"Range": range_header})
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"
    assert response.json()["error"]["code"] == "ARTIFACT_RANGE_INVALID"


@pytest.mark.parametrize("range_header,expected,content_range", [
    pytest.param("bytes=0-" + "9" * 5000, b"0123456789", "bytes 0-9/10", id="huge-end"),
    pytest.param("bytes=-" + "9" * 5000, b"0123456789", "bytes 0-9/10", id="huge-suffix"),
    pytest.param("bytes=" + "0" * 5000 + "4-9", b"456789", "bytes 4-9/10", id="leading-zeroes"),
    pytest.param("BYTES=1-3", b"123", "bytes 1-3/10", id="case-insensitive-unit"),
])
def test_artifact_range_numerals_are_bounded_without_integer_overflow(client, range_header, expected, content_range):
    artifacts = client.app.state.artifact_store
    source = artifacts.publish_staged(
        artifacts.stage_bytes(b"0123456789", filename="range.bin"),
        origin="upload", artifact_type="File", media_type="application/octet-stream",
    )
    response = client.get("/api/artifacts/" + source["artifact_id"] + "/content",
                          headers={"Range": range_header})
    assert response.status_code == 206
    assert response.content == expected
    assert response.headers["content-range"] == content_range


@pytest.mark.parametrize("kind,expected_status,expected", [
    ("current", 206, b"123"), ("different", 200, b"0123456789"), ("weak", 200, b"0123456789"),
])
def test_artifact_range_respects_the_strong_if_range_validator(client, kind, expected_status, expected):
    artifacts = client.app.state.artifact_store
    source = artifacts.publish_staged(
        artifacts.stage_bytes(b"0123456789", filename="range.bin"),
        origin="upload", artifact_type="File", media_type="application/octet-stream",
    )
    url = "/api/artifacts/" + source["artifact_id"] + "/content"
    etag = client.get(url).headers["etag"]
    validator = etag if kind == "current" else '"another-content"' if kind == "different" else "W/" + etag
    response = client.get(url, headers={"Range": "bytes=1-3", "If-Range": validator})
    assert response.status_code == expected_status
    assert response.content == expected
    assert response.headers["etag"] == etag
    assert response.headers["x-content-type-options"] == "nosniff"
    if expected_status == 200:
        assert "content-range" not in response.headers


def test_artifact_download_ignores_unknown_range_units(client):
    artifacts = client.app.state.artifact_store
    source = artifacts.publish_staged(
        artifacts.stage_bytes(b"0123456789", filename="range.bin"),
        origin="upload", artifact_type="File", media_type="application/octet-stream",
    )
    response = client.get("/api/artifacts/" + source["artifact_id"] + "/content",
                          headers={"Range": "records=0-3"})
    assert response.status_code == 200
    assert response.content == b"0123456789"
    assert "content-range" not in response.headers
