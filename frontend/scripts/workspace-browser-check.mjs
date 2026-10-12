// Real FastAPI upload/UI check. No network stubs and no LLM requests.
// Supply a dedicated disposable backend via GISMIND_VITE_API_TARGET.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const baseURL = process.env.WORKSPACE_TEST_URL || 'http://127.0.0.1:5179';
const output = path.resolve(process.env.WORKSPACE_TEST_OUTPUT || 'test-results/workspace-browser');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, ...(process.env.WORKSPACE_BROWSER_PATH ? { executablePath: process.env.WORKSPACE_BROWSER_PATH } : {}) });
const context = await browser.newContext({ viewport: { width: 1536, height: 960 }, reducedMotion: 'reduce' });
const page = await context.newPage();
const errors = [], posts = [];
page.on('pageerror', error => errors.push(error.message));
page.on('request', request => { if (request.method() === 'POST') posts.push(new URL(request.url()).pathname); });
async function screenshot(name) { await page.screenshot({ path: path.join(output, name + '.png'), fullPage: true }); }
async function checkWidth(label) {
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, `${label}: horizontal overflow`);
}
try {
  await page.goto(baseURL, { waitUntil: 'networkidle' });
  await page.getByRole('textbox', { name: '分析需求' }).waitFor();
  await page.getByRole('button', { name: '发送', exact: true }).waitFor();
  await checkWidth('desktop');
  const sidebar = await page.getByLabel('会话侧栏', { exact: true }).boundingBox();
  const chat = await page.getByLabel('Runtime 对话').boundingBox();
  assert(chat.x >= sidebar.x + sidebar.width, 'sidebar overlaps workspace');
  await screenshot('01-desktop-dark');
  await page.getByRole('radio', { name: 'LIGHT' }).click();
  assert(await page.locator('html').evaluate(element => element.classList.contains('light')));
  await screenshot('02-desktop-light');
  await page.getByRole('radio', { name: 'DARK' }).click();

  // Physical bytes go through the actual multipart inspection/CAS API.
  await page.locator('input[type=file]').setInputFiles({ name: '站点样本.GeoJSON', mimeType: 'application/octet-stream', buffer: Buffer.from(JSON.stringify({ type: 'FeatureCollection', features: [
    { type: 'Feature', geometry: { type: 'Point', coordinates: [116.4, 39.9] }, properties: { 名称: '站点甲', 数量: 2 } },
    { type: 'Feature', geometry: { type: 'Point', coordinates: [116.42, 39.92] }, properties: { 名称: '站点乙', 数量: 3 } },
  ] })) });
  await page.getByText('站点样本.GeoJSON · 就绪', { exact: true }).waitFor();
  await page.getByText('2 要素', { exact: true }).waitFor();
  await page.getByRole('button', { name: '用于下一步' }).waitFor();
  const download = page.waitForEvent('download');
  await page.getByRole('link', { name: '下载原始成果' }).click();
  const downloaded = await download;
  const saved = path.join(output, 'downloaded-upload.geojson');
  await downloaded.saveAs(saved);
  const { readFile } = await import('node:fs/promises');
  const bytes = JSON.parse(await readFile(saved, 'utf8'));
  assert.equal(bytes.features.length, 2);
  assert.equal(bytes.features[0].properties.名称, '站点甲');
  await screenshot('03-upload-inspection');

  await page.setViewportSize({ width: 390, height: 844 });
  await checkWidth('mobile');
  await page.getByRole('button', { name: '展开侧栏', exact: true }).click();
  await page.getByLabel('会话侧栏', { exact: true }).waitFor();
  await page.keyboard.press('Escape');
  assert.equal(await page.getByLabel('会话侧栏', { exact: true }).count(), 0);
  await screenshot('04-mobile-chat');
  await page.getByRole('button', { name: /^成果 / }).click();
  await page.getByRole('heading', { name: '空间成果', exact: true }).waitFor();
  await checkWidth('mobile-artifacts');
  await screenshot('05-mobile-artifacts');
  await page.getByRole('button', { name: '用于下一步' }).click();
  await page.getByLabel('本轮选用的成果').waitFor({ state: 'visible' });
  const selected = page.getByRole('button', { name: /^取消选用 / });
  await selected.click();
  assert.equal(await selected.count(), 0);
  await page.setViewportSize({ width: 320, height: 740 });
  await checkWidth('small-mobile');
  const header = page.locator('header').first();
  const brand = await header.getByText('Gismind', { exact: true }).boundingBox();
  const theme = await page.getByRole('radiogroup', { name: '主题模式' }).boundingBox();
  assert(brand.x + brand.width <= theme.x, 'small-mobile brand overlaps theme');
  await page.locator('.welcome-atelier').evaluate(element => { element.parentElement.scrollTop = 0; });
  const welcomeTop = await page.locator('.welcome-atelier').evaluate(element => ({ content: element.getBoundingClientRect().top, viewport: element.parentElement.getBoundingClientRect().top }));
  assert(welcomeTop.content >= welcomeTop.viewport, 'small-mobile welcome content cannot be scrolled into view');
  await screenshot('06-small-mobile');
  assert(!posts.includes('/api/runs'), 'browser smoke must not create paid LLM runs');
  assert.deepEqual(errors, []);
  await writeFile(path.join(output, 'result.json'), JSON.stringify({ passed: true, browser: await browser.version(), actual_backend: true, no_network_stubs: true, llm_calls: 0, assertions: ['desktop nonoverlap', 'dark/light', 'physical GeoJSON upload', 'independent download parse', '390px and 320px no overflow', 'sidebar escape', 'artifact reuse selection'], page_errors: errors, posts }, null, 2));
  console.log(JSON.stringify({ passed: true, output, page_errors: errors, llm_calls: 0 }));
} catch (error) { await screenshot('failure'); throw error; }
finally { await browser.close(); }
