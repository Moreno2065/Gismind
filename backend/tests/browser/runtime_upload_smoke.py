"""Real Chromium -> Vite -> HTTP upload checks; never creates Runs or calls LLMs.

Optional local check (Playwright required), not a substitute for live planner E2E.
Start isolated API/Vite servers and pass --url and a fresh --output directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit


def fixtures(root):
    from tests.live.prompt_corpus_2000 import load_cases
    from tests.live.prompt_e2e_2000 import build_uploads

    cases = load_cases()
    records = []
    choices = [
        ("geojson", "buffer", ["空间数据.GEOJSON", "伪装.csv", "无后缀矢量"]),
        ("gpkg", "buffer", ["中文容器.GPKG", "容器无后缀"]),
        ("zip", "buffer", ["配套文件.ZIP", "压缩矢量无后缀"]),
        ("tif", "rastercalc", ["高程.TIF", "栅格无后缀"]),
    ]
    expected = {"geojson": "geojson", "gpkg": "gpkg", "zip": "shapefile_zip", "tif": "geotiff"}
    for fmt, family, names in choices:
        case = next(c for c in cases if c.family == family and c.file_format == fmt)
        _, data, _ = build_uploads(case, root / fmt)[0]
        for name in names:
            records.append((name, data, expected[fmt], True))
    csv = "经度,纬度,名称\n118.8,32.05,南京\n121.47,31.23,上海\n"
    records.extend([
        ("点位.CSV", csv.encode("utf-8-sig"), "csv", True),
        ("中文编码.csv", csv.encode("gb18030"), "csv", True),
        ("制表分隔.TSV", csv.replace(",", "\t").encode("utf-8"), "tsv", True),
        ("表格无后缀", csv.encode("utf-8"), "csv", True),
        ("损坏.geojson", b'{"type":"FeatureCollection","features":[,,,', None, False),
        ("未打包.shp", b"not a bundled shapefile", None, False),
    ])
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    address = urlsplit(args.url)
    if address.scheme != "http" or address.hostname not in {"localhost", "127.0.0.1"}:
        parser.error("Only a local isolated HTTP server is allowed")
    root = Path(__file__).resolve().parents[2] / ".gismind"
    output = args.output.resolve()
    if not output.is_relative_to(root.resolve()) or output == root.resolve():
        parser.error("Output must be a dedicated directory inside backend/.gismind")
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    errors = []
    run_requests = []
    external_requests = []
    browser_version = None
    try:
        from playwright.sync_api import sync_playwright, expect

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser_version = browser.version
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            try:
                def local_only(route):
                    parsed = urlsplit(route.request.url)
                    if parsed.scheme in {"http", "https"} and parsed.netloc != address.netloc:
                        external_requests.append(parsed.hostname)
                        route.abort()
                    elif route.request.method == "POST" and parsed.path == "/api/runs":
                        run_requests.append(parsed.path)
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", local_only)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(args.url)
                page.wait_for_load_state("networkidle")
                expect(page.get_by_role("textbox")).to_be_enabled(timeout=30000)
                page.screenshot(path=str(output / "initial.png"), full_page=True)
                assert page.get_by_role("button", name="上传文件", exact=True).count() == 1
                file_input = page.locator('input[type="file"]')
                expect(file_input).to_be_hidden()
                assert file_input.get_attribute("accept") is None, "Suffix filter would block content-sniffed uploads"
                composer = page.get_by_role("textbox")
                composer.fill("仅检查上传状态，不提交任何运行。")
                send = page.get_by_role("button", name="发送", exact=True)
                for name, data, fmt, accepted in fixtures(output / "fixtures"):
                    with page.expect_response(lambda response: response.request.method == "POST" and urlsplit(response.url).path == "/api/artifacts/uploads", timeout=45000) as pending:
                        file_input.set_input_files({"name": name, "mimeType": "application/octet-stream", "buffer": data})
                    response = pending.value
                    body = response.json()
                    record = {"filename": name, "expected_accept": accepted, "http_status": response.status, "sha256": hashlib.sha256(data).hexdigest()}
                    rows.append(record)
                    if accepted:
                        assert response.status == 201, (name, response.status, body)
                        assert body["type"] == "File" and body["schema"]["inspection"]["format"] == fmt, body
                        assert body["schema"]["file"]["filename"] == name
                        assert body["content_hash"] == record["sha256"]
                        record.update(artifact_id=body["artifact_id"], format=fmt, inspection=body["schema"]["inspection"])
                        expect(page.get_by_text(name + " · 就绪", exact=False)).to_be_visible(timeout=10000)
                        expect(send).to_be_enabled()
                        user_id = page.evaluate("localStorage.getItem('gismind.user_id')")
                        downloaded = context.request.get(args.url.rstrip("/") + "/api/artifacts/" + body["artifact_id"] + "/content", headers={"X-User-Id": user_id})
                        assert downloaded.status == 200 and downloaded.body() == data, "CAS download must equal uploaded bytes"
                    else:
                        assert response.status == 422 and body["error"]["code"] == "ARTIFACT_INVALID", body
                        expect(page.get_by_text(name + " · 失败", exact=False)).to_be_visible(timeout=10000)
                        expect(send).to_be_disabled()
                        composer.press("Enter")
                        # Finish the browser's next two paint turns to observe the key handler.
                        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                        assert not run_requests, "Failed upload must block Enter as well as the Send button"
                        with page.expect_response(lambda response: response.request.method == "POST" and urlsplit(response.url).path == "/api/artifacts/uploads", timeout=45000) as retried:
                            page.get_by_role("button", name="重试上传 " + name, exact=True).click()
                        assert retried.value.status == 422
                        expect(send).to_be_disabled()
                    page.get_by_role("button", name="移除 " + name, exact=True).click()
                    expect(send).to_be_enabled()
                    record["passed"] = True
                page.screenshot(path=str(output / "final.png"), full_page=True)
                assert not errors, errors
                assert not run_requests, run_requests
                assert not external_requests, external_requests
            finally:
                context.close()
                browser.close()
    except Exception as error:
        errors.append(type(error).__name__ + ": " + str(error))
    report = {"suite": "real_browser_http_upload_smoke", "browser": browser_version,
        "completed": len(rows), "passed": sum(row.get("passed", False) for row in rows),
        "cases": rows, "errors": errors, "forbidden_run_requests": run_requests,
        "external_requests": external_requests, "live_llm_calls": 0}
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False))
    return 1 if errors or len(rows) != 15 or any(not row.get("passed") for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
