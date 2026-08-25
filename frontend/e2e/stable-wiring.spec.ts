/**
 * Stable, real browser wiring coverage.
 *
 * Chromium → Vite proxy → FastAPI → Dispatcher/native tools → Redis/SQLite →
 * SSE → React state. The only injected seam is the deterministic LLM
 * transport installed by backend/scripts/e2e_awaiting_server.py. In
 * particular, there is no page.route/MSW or synthetic HTTP/SSE response.
 */
import { expect, test, type Page, type Response } from '@playwright/test';

async function isolateUser(page: Page, suffix: string) {
  await page.addInitScript((userId: string) => {
    localStorage.clear();
    localStorage.setItem('gismind.user_id', userId);
  }, `e2e_stable_${suffix}_${Date.now()}`);
}

async function submitAndReadChatSse(page: Page, prompt: string): Promise<string> {
  let sse = '';
  const onResponse = async (response: Response) => {
    const request = response.request();
    if (request.method() !== 'POST' || !response.url().endsWith('/api/chat')) return;
    try {
      sse = await response.text();
    } catch {
      // The page may have consumed the body first. UI assertions below still
      // verify the live React state; this only makes SSE framing optional.
    }
  };
  page.on('response', onResponse);
  const textarea = page.locator('textarea').first();
  await textarea.fill(prompt);
  await page.getByRole('button', { name: '发送' }).click();
  await expect(page.getByRole('button', { name: '发送' })).toBeVisible({ timeout: 90_000 });
  page.off('response', onResponse);
  return sse;
}

test.describe('stable production wiring', () => {
  test('text POI reaches real tool, SSE contract, trace and rendered map', async ({ page }, testInfo) => {
    await isolateUser(page, `poi_${testInfo.workerIndex}`);
    await page.goto('/');
    await expect(page.locator('textarea').first()).toBeVisible();

    const sse = await submitAndReadChatSse(page, 'E2E_POI 南京新街口附近 500 米咖啡店并在地图展示');

    // The in-browser trace proves React consumed the production run.plan and
    // native query tool events rather than only a terminal HTTP response.
    await expect(page.getByText('规划来源 · Root LLM')).toBeVisible();
    await expect(page.getByText('query_poi', { exact: false }).first()).toBeVisible();
    await expect(page.locator('.trace-timeline')).toBeVisible();
    await expect(page.getByRole('button', { name: '全屏展开地图' })).toBeVisible({ timeout: 60_000 });

    // When Playwright captured the body, assert the full public SSE contract
    // in order-independent form. The UI assertions above remain mandatory.
    if (sse) {
      for (const eventName of ['status', 'run.plan', 'tool.call.start', 'tool.call.complete', 'map', 'token', 'done']) {
        expect(sse, `SSE must include ${eventName}`).toContain(`event: ${eventName}`);
      }
    }
  });

  test('browser GeoJSON upload keeps its file_id through chat, data_io and map', async ({ page }, testInfo) => {
    await isolateUser(page, `upload_one_${testInfo.workerIndex}`);
    await page.goto('/');
    const fileInput = page.locator('input[type="file"]');
    const uploadResponse = page.waitForResponse(
      (response) => response.request().method() === 'POST' && response.url().endsWith('/api/upload'),
    );
    await fileInput.setInputFiles({
      name: 'e2e_points.geojson',
      mimeType: 'application/geo+json',
      buffer: Buffer.from(JSON.stringify({
        type: 'FeatureCollection',
        features: [
          {
            type: 'Feature',
            properties: { name: 'E2E point' },
            geometry: { type: 'Point', coordinates: [118.792, 32.048] },
          },
        ],
      })),
    });
    const upload = await uploadResponse;
    expect(upload.ok()).toBeTruthy();
    const uploaded = (await upload.json()) as { file_id: string; feature_count: number };
    expect(uploaded.file_id).toMatch(/^file_/);
    expect(uploaded.feature_count).toBe(1);
    await expect(page.getByText('e2e_points.geojson')).toBeVisible();
    await expect(page.getByText('就绪')).toBeVisible();

    const chatRequest = page.waitForRequest(
      (request) => request.method() === 'POST' && request.url().endsWith('/api/chat'),
    );
    const sse = await submitAndReadChatSse(page, 'E2E_UPLOAD_ONE 读取上传的点图层并渲染');
    const request = await chatRequest;
    expect((request.postDataJSON() as { upload_file_ids?: string[] }).upload_file_ids).toEqual([uploaded.file_id]);

    await expect(page.getByText('data_io_read', { exact: false }).first()).toBeVisible();
    await expect(page.getByText('map_layer_build', { exact: false }).first()).toBeVisible();
    await expect(page.getByRole('button', { name: '全屏展开地图' })).toBeVisible({ timeout: 60_000 });
    if (sse) {
      expect(sse).toContain(`"file_id":"${uploaded.file_id}"`);
      expect(sse).toContain('event: map');
      expect(sse).toContain('event: done');
    }
  });

  test('two browser uploads preserve order through DAG dependencies and overlay', async ({ page }, testInfo) => {
    await isolateUser(page, `upload_two_${testInfo.workerIndex}`);
    await page.goto('/');
    const receivedUploads: Array<{ file_id: string; filename: string }> = [];
    const onResponse = async (response: Response) => {
      if (response.request().method() !== 'POST' || !response.url().endsWith('/api/upload')) return;
      try {
        receivedUploads.push(await response.json() as { file_id: string; filename: string });
      } catch {
        // The visible ready chips below are the primary browser assertion.
      }
    };
    page.on('response', onResponse);
    await page.locator('input[type="file"]').setInputFiles([
      {
        name: 'e2e_left.geojson',
        mimeType: 'application/geo+json',
        buffer: Buffer.from(JSON.stringify(polygonFeature('left', [
          [118.790, 32.045], [118.795, 32.045], [118.795, 32.050], [118.790, 32.050], [118.790, 32.045],
        ]))),
      },
      {
        name: 'e2e_right.geojson',
        mimeType: 'application/geo+json',
        buffer: Buffer.from(JSON.stringify(polygonFeature('right', [
          [118.793, 32.047], [118.798, 32.047], [118.798, 32.052], [118.793, 32.052], [118.793, 32.047],
        ]))),
      },
    ]);
    await expect(page.getByText('就绪')).toHaveCount(2, { timeout: 30_000 });
    await expect(page.getByText('e2e_left.geojson')).toBeVisible();
    await expect(page.getByText('e2e_right.geojson')).toBeVisible();
    await expect.poll(() => receivedUploads.length).toBe(2);

    const chatRequest = page.waitForRequest(
      (request) => request.method() === 'POST' && request.url().endsWith('/api/chat'),
    );
    const sse = await submitAndReadChatSse(page, 'E2E_UPLOAD_TWO 计算两个上传面图层的交集并渲染');
    const chatPayload = (await chatRequest).postDataJSON() as { upload_file_ids?: string[] };
    expect(chatPayload.upload_file_ids).toHaveLength(2);
    expect(new Set(chatPayload.upload_file_ids)).toEqual(new Set(receivedUploads.map((upload) => upload.file_id)));

    await expect(page.getByText('计算两个图层的交集')).toBeVisible();
    await expect(page.getByText('overlay', { exact: false }).first()).toBeVisible();
    await expect(page.getByText('渲染交集结果')).toBeVisible();
    await expect(page.getByRole('button', { name: '全屏展开地图' })).toBeVisible({ timeout: 60_000 });
    if (sse) {
      expect(sse).toContain('event: run.plan');
      expect(sse).toContain('event: tool.call.complete');
      expect(sse).toContain('event: map');
      expect(sse).toContain('event: done');
    }
    page.off('response', onResponse);
  });

  test('empty map, expired upload and rejected upload each converge in the UI', async ({ page }, testInfo) => {
    await isolateUser(page, `error_states_${testInfo.workerIndex}`);
    await page.goto('/');

    const emptySse = await submitAndReadChatSse(page, 'E2E_EMPTY_MAP 判断坐标是否在中国范围内');
    await expect(page.getByRole('paragraph').filter({ hasText: '坐标转换完成。' })).toBeVisible();
    await expect(page.getByRole('button', { name: '全屏展开地图' })).toHaveCount(0);
    if (emptySse) {
      expect(emptySse).toContain('event: done');
      expect(emptySse).not.toContain('event: map');
    }

    const expiredSse = await submitAndReadChatSse(page, 'E2E_EXPIRED_UPLOAD 读取已经过期的文件');
    await expect(page.getByText('上传文件已过期或不存在').first()).toBeVisible();
    await expect(page.getByText(/SUBTASK_FAILED|RUN_FAILED/).last()).toBeVisible();
    if (expiredSse) {
      expect(expiredSse).toContain('event: error');
      expect(expiredSse).not.toContain('event: done');
    }

    const uploadResponse = page.waitForResponse(
      (response) => response.request().method() === 'POST' && response.url().endsWith('/api/upload'),
    );
    await page.locator('input[type="file"]').setInputFiles({
      name: 'reject-me.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('not a supported GIS file'),
    });
    const rejected = await uploadResponse;
    expect(rejected.status()).toBe(422);
    await expect(page.getByText('reject-me.txt')).toBeVisible();
    await expect(page.getByTitle('upload failed: HTTP 422')).toBeVisible();
  });

  test('stop aborts the live SSE and prevents later tokens from being appended', async ({ page }, testInfo) => {
    await isolateUser(page, `stop_${testInfo.workerIndex}`);
    await page.goto('/');
    const cancelRequest = page.waitForRequest(
      (request) => request.method() === 'POST' && /\/api\/runs\/[^/]+\/cancel$/.test(request.url()),
      { timeout: 30_000 },
    );
    const textarea = page.locator('textarea').first();
    await textarea.fill('E2E_POI 南京新街口附近 500 米咖啡店并在地图展示');
    await page.getByRole('button', { name: '发送' }).click();
    await expect(page.getByText('query_poi', { exact: false }).first()).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole('button', { name: '停止' })).toBeVisible();

    await page.getByRole('button', { name: '停止' }).click();
    const cancelled = await cancelRequest;
    expect(cancelled.postData()).toBeNull();
    await expect(page.getByText('已取消')).toBeVisible();
    const contentsAtCancel = await page.locator('.prose-chat').allTextContents();
    await page.waitForTimeout(5_000);
    expect(await page.locator('.prose-chat').allTextContents()).toEqual(contentsAtCancel);
    await expect(page.getByRole('button', { name: '发送' })).toBeVisible();
  });

  test('session switch aborts an old stream; refresh restores only the active session history', async ({ page }, testInfo) => {
    await isolateUser(page, `switch_refresh_${testInfo.workerIndex}`);
    await page.goto('/');
    const textarea = page.locator('textarea').first();
    await textarea.fill('E2E_POI 南京新街口附近 500 米咖啡店并在地图展示');
    await page.getByRole('button', { name: '发送' }).click();
    await expect(page.getByText('query_poi', { exact: false }).first()).toBeVisible({ timeout: 30_000 });

    const createSession = page.waitForResponse(
      (response) => response.request().method() === 'POST' && response.url().endsWith('/api/sessions'),
    );
    await page.getByRole('button', { name: '+ NEW DISPATCH' }).click();
    expect((await createSession).ok()).toBeTruthy();
    await expect(page.locator('main').getByText('E2E_POI 南京新街口附近 500 米咖啡店并在地图展示')).toHaveCount(0);
    await page.waitForTimeout(5_000);
    await expect(page.locator('main').getByText('E2E_POI 南京新街口附近 500 米咖啡店并在地图展示')).toHaveCount(0);

    await textarea.fill('E2E_EMPTY_MAP 判断坐标是否在中国范围内');
    await page.getByRole('button', { name: '发送' }).click();
    await expect(page.getByRole('paragraph').filter({ hasText: '坐标转换完成。' })).toBeVisible();
    // Message persistence is intentionally debounced; wait for its public
    // UI effect rather than reading localStorage directly.
    await page.waitForTimeout(800);
    await page.reload();
    await expect(page.getByRole('paragraph').filter({ hasText: '坐标转换完成。' })).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText('规划来源 · Root LLM')).toBeVisible();
  });
});

function polygonFeature(name: string, ring: number[][]) {
  return {
    type: 'FeatureCollection',
    features: [{
      type: 'Feature',
      properties: { name },
      geometry: { type: 'Polygon', coordinates: [ring] },
    }],
  };
}
