/**
 * 24 independent additions to the stable real-browser matrix.
 *
 * Every case drives Chromium → Vite proxy → FastAPI → Dispatcher/native
 * tools → Redis/SQLite → SSE → React.  Request/response observers below are
 * passive assertions only; no route interception or synthetic response exists.
 */
import { expect, test, type Page } from '@playwright/test';

const EMPTY_PROMPT = 'E2E_EMPTY_MAP 判断坐标是否在中国范围内';
const EXPIRED_PROMPT = 'E2E_EXPIRED_UPLOAD 读取已经过期的文件';
const ONE_PROMPT = 'E2E_UPLOAD_ONE 读取上传的点图层并渲染';
const TWO_PROMPT = 'E2E_UPLOAD_TWO 计算两个上传面图层的交集并渲染';
const RASTER_PROMPT = 'E2E_UPLOAD_RASTER 计算上传高程栅格的坡度并显示';
const AWAITING_PROMPT = '把我的 DEM 按分级阈值进行栅格重分类';

const TINY_GEOTIFF_B64 = 'SUkqAAgAAAAQAAABAwABAAAAAwAAAAEBAwABAAAAAwAAAAIBAwABAAAAIAAAAAMBAwABAAAAAQAAAAYBAwABAAAAAQAAABEBBAABAAAAbgEAABUBAwABAAAAAQAAABYBAwABAAAAAwAAABcBBAABAAAAJAAAABwBAwABAAAAAQAAAFMBAwABAAAAAwAAAA6DDAADAAAAzgAAAIKEDAAGAAAA5gAAAK+HAwAgAAAAFgEAALCHDAACAAAAVgEAALGHAgAIAAAAZgEAAAAAAAB7FK5H4XqEP3sUrkfheoQ/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADD9Shcj7JdQEjhehSuB0BAAAAAAAAAAAABAAEAAAAHAAAEAAABAAIAAQQAAAEAAQAACAAAAQDmEAEIsYcHAAAABggAAAEAjiMJCLCHAQABAAsIsIcBAAAAiG10lh2kckAAAABAplRYQVdHUyA4NHwAAACAPwAAAEAAAEBAAAAAQAAAgEAAAMBAAABAQAAAwEAAABBB';
const E2E_UPLOAD_TTL_WAIT_MS = 5_500;

async function isolateUser(page: Page, suffix: string): Promise<void> {
  await page.addInitScript((userId: string) => {
    localStorage.clear();
    localStorage.setItem('gismind.user_id', userId);
  }, `e2e_matrix_${suffix}_${Date.now()}`);
}

async function open(page: Page, suffix: string): Promise<void> {
  await isolateUser(page, suffix);
  await page.goto('/');
  await expect(page.locator('textarea').first()).toBeVisible();
}

async function expectVisibleText(page: Page, expected: string | RegExp, timeout = 90_000): Promise<void> {
  const candidates = page.getByText(expected);
  await expect.poll(async () => {
    const count = await candidates.count();
    for (let index = 0; index < count; index += 1) {
      if (await candidates.nth(index).isVisible()) return true;
    }
    return false;
  }, { timeout }).toBe(true);
}

async function send(page: Page, prompt: string, expected: string | RegExp): Promise<void> {
  const textarea = page.locator('textarea').first();
  await textarea.fill(prompt);
  await page.getByRole('button', { name: '发送' }).click();
  await expectVisibleText(page, expected);
  await expect(page.getByRole('button', { name: '发送' })).toBeVisible({ timeout: 30_000 });
}

function pointFeature(name = 'matrix-point') {
  return {
    type: 'FeatureCollection',
    features: [{
      type: 'Feature',
      properties: { name, class: 'station' },
      geometry: { type: 'Point', coordinates: [118.792, 32.048] },
    }],
  };
}

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

async function uploadPoint(page: Page, name = 'matrix-point.geojson') {
  const responsePromise = page.waitForResponse((response) =>
    response.request().method() === 'POST' && response.url().endsWith('/api/upload'),
  );
  await page.locator('input[type="file"]').setInputFiles({
    name,
    mimeType: 'application/geo+json',
    buffer: Buffer.from(JSON.stringify(pointFeature(name))),
  });
  const response = await responsePromise;
  expect(response.ok()).toBeTruthy();
  return await response.json() as { file_id: string; feature_count: number; filename: string };
}

async function uploadPolygons(page: Page) {
  const selectedFilenames = ['matrix-left.geojson', 'matrix-right.geojson'];
  const responses: Array<{ file_id: string; filename: string }> = [];
  const onResponse = async (response: import('@playwright/test').Response) => {
    if (response.request().method() !== 'POST' || !response.url().endsWith('/api/upload')) return;
    responses.push(await response.json() as { file_id: string; filename: string });
  };
  page.on('response', onResponse);
  await page.locator('input[type="file"]').setInputFiles([
    {
      name: 'matrix-left.geojson', mimeType: 'application/geo+json',
      buffer: Buffer.from(JSON.stringify(polygonFeature('left', [
        [118.790, 32.045], [118.795, 32.045], [118.795, 32.050], [118.790, 32.050], [118.790, 32.045],
      ]))),
    },
    {
      name: 'matrix-right.geojson', mimeType: 'application/geo+json',
      buffer: Buffer.from(JSON.stringify(polygonFeature('right', [
        [118.793, 32.047], [118.798, 32.047], [118.798, 32.052], [118.793, 32.052], [118.793, 32.047],
      ]))),
    },
  ]);
  await expect.poll(() => responses.length).toBe(2);
  page.off('response', onResponse);
  const responsesByFilename = new Map(responses.map((response) => [response.filename, response]));
  expect([...responsesByFilename.keys()].sort()).toEqual([...selectedFilenames].sort());
  return selectedFilenames.map((filename) => responsesByFilename.get(filename)!);
}

async function uploadRaster(page: Page) {
  const responsePromise = page.waitForResponse((response) =>
    response.request().method() === 'POST' && response.url().endsWith('/api/upload'),
  );
  await page.locator('input[type="file"]').setInputFiles({
    name: 'matrix-dem.tif', mimeType: 'image/tiff', buffer: Buffer.from(TINY_GEOTIFF_B64, 'base64'),
  });
  const response = await responsePromise;
  expect(response.ok()).toBeTruthy();
  return await response.json() as { file_id: string; geometry_type: string };
}

test.describe('32-case stable correctness matrix', () => {
  test('01 welcome surface renders before any request', async ({ page }) => {
    await open(page, 'welcome');
    await expect(page.getByText('Gismind · 空间智能')).toBeVisible();
  });

  test('02 send is disabled for whitespace-only input', async ({ page }) => {
    await open(page, 'disabled');
    await page.locator('textarea').fill('   ');
    await expect(page.getByRole('button', { name: '发送' })).toBeDisabled();
  });

  test('03 shift-enter keeps text in the composer without sending', async ({ page }) => {
    await open(page, 'shift-enter');
    const textarea = page.locator('textarea');
    await textarea.fill('第一行');
    await textarea.press('Shift+Enter');
    await textarea.type('第二行');
    await expect(textarea).toHaveValue('第一行\n第二行');
    await expect(page.locator('main .prose-chat')).toHaveCount(0);
  });

  test('04 enter submits a real chat request', async ({ page }) => {
    await open(page, 'enter');
    const request = page.waitForRequest((item) => item.method() === 'POST' && item.url().endsWith('/api/chat'));
    const textarea = page.locator('textarea');
    await textarea.fill(EMPTY_PROMPT);
    await textarea.press('Enter');
    expect((await request).postDataJSON()).toMatchObject({ message: EMPTY_PROMPT });
    await expect(page.getByText('坐标转换完成。').last()).toBeVisible({ timeout: 90_000 });
  });

  test('05 empty-map result reaches a terminal assistant answer', async ({ page }) => {
    await open(page, 'empty-answer');
    await send(page, EMPTY_PROMPT, '坐标转换完成。');
  });

  test('06 empty-map result displays Dispatcher provenance and trace', async ({ page }) => {
    await open(page, 'empty-trace');
    await send(page, EMPTY_PROMPT, '坐标转换完成。');
    await expect(page.getByText('规划来源 · Root LLM')).toBeVisible();
    await expect(page.locator('.trace-timeline')).toBeVisible();
  });

  test('07 empty-map result does not create a map affordance', async ({ page }) => {
    await open(page, 'empty-no-map');
    await send(page, EMPTY_PROMPT, '坐标转换完成。');
    await expect(page.getByRole('button', { name: '全屏展开地图' })).toHaveCount(0);
  });

  test('08 browser upload creates one ready chip with server file metadata', async ({ page }) => {
    await open(page, 'one-chip');
    const upload = await uploadPoint(page);
    expect(upload.file_id).toMatch(/^file_/);
    expect(upload.feature_count).toBe(1);
    await expect(page.getByText('matrix-point.geojson')).toBeVisible();
    await expect(page.getByText('就绪')).toBeVisible();
  });

  test('09 browser upload sends its exact file id in chat payload', async ({ page }) => {
    await open(page, 'one-payload');
    const upload = await uploadPoint(page);
    const request = page.waitForRequest((item) => item.method() === 'POST' && item.url().endsWith('/api/chat'));
    await send(page, ONE_PROMPT, '地图图层已生成');
    expect((await request).postDataJSON()).toMatchObject({ upload_file_ids: [upload.file_id] });
  });

  test('10 browser GeoJSON reaches data_io_read in the visible execution trace', async ({ page }) => {
    await open(page, 'one-data-io');
    await uploadPoint(page);
    await send(page, ONE_PROMPT, '地图图层已生成');
    await expect(page.getByText('data_io_read', { exact: false }).first()).toBeVisible();
  });

  test('11 one uploaded point renders one actual vector overlay', async ({ page }) => {
    await open(page, 'one-map');
    await uploadPoint(page);
    await send(page, ONE_PROMPT, '地图图层已生成');
    const map = page.getByTestId('lazy-map').last();
    await expect(map).toHaveAttribute('data-map-ready', 'true');
    await expect(map).toHaveAttribute('data-vector-overlay-count', '1');
  });

  test('12 removing a ready upload removes it from the composer', async ({ page }) => {
    await open(page, 'remove-upload');
    await uploadPoint(page, 'remove-me.geojson');
    await expect(page.getByText('remove-me.geojson')).toBeVisible();
    await page.getByRole('button', { name: '移除' }).click();
    await expect(page.getByText('remove-me.geojson')).toHaveCount(0);
  });

  test('13 rejected upload converges to a visible error chip', async ({ page }) => {
    await open(page, 'rejected-upload');
    const responsePromise = page.waitForResponse((response) =>
      response.request().method() === 'POST' && response.url().endsWith('/api/upload'),
    );
    await page.locator('input[type="file"]').setInputFiles({
      name: 'matrix-reject.txt', mimeType: 'text/plain', buffer: Buffer.from('not GIS'),
    });
    expect((await responsePromise).status()).toBe(422);
    await expect(page.getByText('matrix-reject.txt')).toBeVisible();
    await expect(page.getByText('失败')).toBeVisible();
  });

  test('14 expired upload surfaces a backend error instead of a false done state', async ({ page }) => {
    await open(page, 'expired');
    await uploadPoint(page, 'matrix-expiring.geojson');
    await page.waitForTimeout(E2E_UPLOAD_TTL_WAIT_MS);
    await send(page, EXPIRED_PROMPT, '上传文件已过期或不存在');
    await expect(page.getByText(/SUBTASK_FAILED|RUN_FAILED/).last()).toBeVisible();
  });

  test('15 composer recovers and accepts a new request after an expired upload', async ({ page }) => {
    await open(page, 'expired-recovery');
    await uploadPoint(page, 'matrix-expiring-recovery.geojson');
    await page.waitForTimeout(E2E_UPLOAD_TTL_WAIT_MS);
    await send(page, EXPIRED_PROMPT, '上传文件已过期或不存在');
    await send(page, EMPTY_PROMPT, '坐标转换完成。');
  });

  test('16 two uploads retain both file chips in selection order', async ({ page }) => {
    await open(page, 'two-chips');
    await uploadPolygons(page);
    await expect(page.getByText('matrix-left.geojson')).toBeVisible();
    await expect(page.getByText('matrix-right.geojson')).toBeVisible();
    await expect(page.getByText('就绪')).toHaveCount(2);
  });

  test('17 two-upload chat payload preserves file selection order', async ({ page }) => {
    await open(page, 'two-payload');
    const uploads = await uploadPolygons(page);
    const request = page.waitForRequest((item) => item.method() === 'POST' && item.url().endsWith('/api/chat'));
    await send(page, TWO_PROMPT, '空间分析完成');
    expect((await request).postDataJSON()).toMatchObject({ upload_file_ids: uploads.map((item) => item.file_id) });
  });

  test('18 two uploaded polygons expose overlay as a Dispatcher task', async ({ page }) => {
    await open(page, 'two-dag');
    await uploadPolygons(page);
    await send(page, TWO_PROMPT, '空间分析完成');
    await expect(page.getByText('计算两个图层的交集')).toBeVisible();
    await expect(page.getByText('overlay', { exact: false }).first()).toBeVisible();
  });

  test('19 two-upload overlay renders one vector layer', async ({ page }) => {
    await open(page, 'two-map');
    await uploadPolygons(page);
    await send(page, TWO_PROMPT, '空间分析完成');
    const map = page.getByTestId('lazy-map').last();
    await expect(map).toHaveAttribute('data-map-ready', 'true');
    await expect(map).toHaveAttribute('data-vector-overlay-count', '1');
  });

  test('20 GeoTIFF upload reports Raster metadata through the real upload route', async ({ page }) => {
    await open(page, 'raster-upload');
    const upload = await uploadRaster(page);
    expect(upload.file_id).toMatch(/^file_/);
    expect(upload.geometry_type).toBe('Raster');
    await expect(page.getByText('matrix-dem.tif')).toBeVisible();
  });

  test('21 GeoTIFF file id reaches the real chat payload', async ({ page }) => {
    await open(page, 'raster-payload');
    const upload = await uploadRaster(page);
    const request = page.waitForRequest((item) => item.method() === 'POST' && item.url().endsWith('/api/chat'));
    await send(page, RASTER_PROMPT, '栅格分析完成');
    expect((await request).postDataJSON()).toMatchObject({ upload_file_ids: [upload.file_id] });
  });

  test('22 GeoTIFF workflow exposes data loading and slope tools', async ({ page }) => {
    await open(page, 'raster-trace');
    await uploadRaster(page);
    await send(page, RASTER_PROMPT, '栅格分析完成');
    await expect(page.getByText('data_io_read', { exact: false }).first()).toBeVisible();
    await expect(page.getByText('slope', { exact: false }).first()).toBeVisible();
  });

  test('23 GeoTIFF slope attaches one actual raster ImageLayer', async ({ page }) => {
    await open(page, 'raster-map');
    await uploadRaster(page);
    await send(page, RASTER_PROMPT, '栅格分析完成');
    const map = page.getByTestId('lazy-map').last();
    await expect(map).toHaveAttribute('data-map-ready', 'true');
    await expect(map).toHaveAttribute('data-raster-overlay-count', '1');
  });

  test('24 awaiting-input remains actionable after its SSE stream has ended', async ({ page }) => {
    await open(page, 'awaiting');
    const textarea = page.locator('textarea');
    await textarea.fill(AWAITING_PROMPT);
    await page.getByRole('button', { name: '发送' }).click();
    const banner = page.getByRole('status').filter({ hasText: '等待你的回答' });
    await expect(banner).toBeVisible({ timeout: 90_000 });
    await expect(page.getByRole('button', { name: '回答并继续' })).toBeVisible();
    await expect(textarea).toHaveAttribute('placeholder', '请输入补充信息');
  });
});
