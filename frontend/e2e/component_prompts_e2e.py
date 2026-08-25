"""Native Playwright browser E2E for component-derived Gismind prompts."""
from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright


BASE_URL = "http://127.0.0.1:5173"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nanjing_points.geojson"


def _open_clean_page(browser: Any, label: str) -> tuple[Any, Page]:
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    user_id = f"e2e_components_{label}_{uuid.uuid4().hex[:8]}"
    page.add_init_script(
        f"localStorage.setItem('gismind.user_id', {json.dumps(user_id)});"
    )
    page.goto(BASE_URL, wait_until="networkidle")
    page.locator("textarea").first.wait_for(state="visible", timeout=30_000)
    return context, page


def _latest_assistant(page: Page) -> dict[str, Any]:
    page.wait_for_function(
        """
        () => Object.keys(localStorage)
          .filter((key) => key.startsWith('gismind.messages.'))
          .some((key) => {
            try {
              const messages = JSON.parse(localStorage.getItem(key) || '[]');
              const last = messages[messages.length - 1];
              return last?.role === 'assistant' && ['done', 'error'].includes(last.status);
            } catch { return false; }
          })
        """,
        timeout=120_000,
    )
    return page.evaluate(
        """
        () => {
          const assistants = Object.keys(localStorage)
            .filter((key) => key.startsWith('gismind.messages.'))
            .flatMap((key) => {
              try {
                return JSON.parse(localStorage.getItem(key) || '[]')
                  .filter((message) => message.role === 'assistant');
              } catch { return []; }
            });
          assistants.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
          return assistants[0] || null;
        }
        """
    )


def _send(page: Page, prompt: str) -> dict[str, Any]:
    textarea = page.locator("textarea").first
    textarea.fill(prompt)
    with page.expect_response(
        lambda response: response.request.method == "POST"
        and "/api/chat" in response.url
        and "/resume" not in response.url,
        timeout=120_000,
    ) as response_info:
        page.get_by_role("button", name="发送").click()
    response = response_info.value
    page.get_by_role("button", name="发送").wait_for(state="visible", timeout=120_000)
    assert response.ok, f"chat HTTP failed: {response.status}"
    assistant = _latest_assistant(page)
    assert assistant.get("status") == "done", f"assistant ended as {assistant}"
    assert not assistant.get("error"), f"assistant exposed an error: {assistant['error']}"
    page.locator(".trace-timeline").last.wait_for(state="visible", timeout=30_000)
    return assistant


def _assert_tools(assistant: dict[str, Any], expected: set[str]) -> None:
    observed: set[str] = set()
    for event in assistant.get("executionTrace") or []:
        data = event.get("data") if isinstance(event.get("data"), dict) else event
        event_name = data.get("event") or event.get("event")
        if event_name in {"tool.call.start", "tool.call.complete"}:
            name = data.get("tool_name")
            if isinstance(name, str):
                observed.add(name)
    missing = expected - observed
    assert not missing, f"missing tools {sorted(missing)}; observed={sorted(observed)}"


def _assert_map(assistant: dict[str, Any], minimum_features: int = 1) -> None:
    maps = [block for block in assistant.get("blocks") or [] if block.get("type") == "map"]
    assert maps, "SSE did not emit a map event"
    count = int(maps[-1].get("featureCount") or 0)
    assert count >= minimum_features, f"map feature count {count} < {minimum_features}"


def main() -> int:
    results: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            # Prompt 1: external geocoder + text result.
            context, page = _open_clean_page(browser, "geo")
            try:
                prompt = "请查询南京新街口的经纬度，并说明数据源。"
                events = _send(page, prompt)
                _assert_tools(events, {"geo_code"})
                results.append({"component": "geo", "prompt": prompt, "status": "passed"})
            finally:
                context.close()

            # Prompt 2: real local buffer computation + cross-agent artifact + map.
            context, page = _open_clean_page(browser, "buffer")
            try:
                prompt = "以南京夫子庙测试点为中心做 500 米缓冲区，并画在地图上。"
                events = _send(page, prompt)
                _assert_tools(events, {"buffer", "map_layer_build"})
                _assert_map(events)
                results.append({"component": "buffer+viz", "prompt": prompt, "status": "passed"})
            finally:
                context.close()

            # Prompt 3: real upload endpoint + workspace payload + parser + map.
            context, page = _open_clean_page(browser, "upload")
            try:
                page.locator('input[type="file"]').set_input_files(str(FIXTURE))
                page.get_by_text("就绪", exact=True).wait_for(state="visible", timeout=30_000)
                prompt = "读取我刚上传的 GeoJSON，保持属性字段，并把全部要素显示在地图上。"
                events = _send(page, prompt)
                _assert_tools(events, {"data_io_read", "map_layer_build"})
                _assert_map(events, minimum_features=2)
                results.append({"component": "upload+data_io+viz", "prompt": prompt, "status": "passed"})
            finally:
                context.close()

            # Prompt 4: provider-backed POI query + visualization.
            context, page = _open_clean_page(browser, "poi")
            try:
                prompt = "查询南京新街口 500 米内的咖啡店，并把结果标在地图上。"
                events = _send(page, prompt)
                _assert_tools(events, {"query_poi", "map_layer_build"})
                _assert_map(events)
                results.append({"component": "poi+viz", "prompt": prompt, "status": "passed"})
            finally:
                context.close()
        finally:
            browser.close()

    print(json.dumps({"status": "passed", "cases": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
