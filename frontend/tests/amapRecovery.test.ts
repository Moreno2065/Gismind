import AMapLoader from '@amap/amap-jsapi-loader';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Exercise the installed loader and its real failed/loading state. JSDOM does
// not fetch external scripts; dispatch only transport events, never LLM replies.
const loader = AMapLoader as typeof AMapLoader & { reset(): void };
const sdkWindow = window as typeof window & { ___onAPILoaded?: (error?: unknown) => void };
let loadAMap: typeof import('@/lib/amap')['loadAMap'];
const sdk = { Map: class {} };

function scripts() {
  return [...document.querySelectorAll<HTMLScriptElement>('script')]
    .filter(script => new URL(script.src).origin === 'https://webapi.amap.com');
}

function succeed() {
  window.AMap = sdk;
  sdkWindow.___onAPILoaded!();
}

beforeEach(async () => {
  vi.useFakeTimers();
  vi.stubEnv('VITE_AMAP_KEY', 'offline-loader-test-key');
  vi.stubEnv('VITE_AMAP_SECURITY_CODE', '');
  loader.reset();
  delete sdkWindow.___onAPILoaded;
  document.body.replaceChildren();
  vi.resetModules();
  ({ loadAMap } = await import('@/lib/amap'));
});

afterEach(() => {
  loader.reset();
  delete sdkWindow.___onAPILoaded;
  document.body.replaceChildren();
  vi.useRealTimers();
  vi.unstubAllEnvs();
});

describe('real AMap loader transport recovery (offline, no provider calls)', () => {
  it('shares one SDK request and caches success for all maps', async () => {
    const first = loadAMap();
    const second = loadAMap();
    expect(second).toBe(first);
    expect(scripts()).toHaveLength(1);
    succeed();
    await expect(first).resolves.toBe(sdk);
    await expect(loadAMap()).resolves.toBe(sdk);
    expect(scripts()).toHaveLength(1);
    expect(window.AMap).toBe(sdk);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('recovers from a terminal script error once, including concurrent waiters', async () => {
    const first = loadAMap();
    const rejected = first.catch(error => error);
    const original = scripts()[0];
    original.dispatchEvent(new Event('error'));
    await vi.advanceTimersByTimeAsync(500);
    expect(scripts()).toHaveLength(1);
    expect(scripts()[0]).not.toBe(original);
    expect(loadAMap()).toBe(first);
    succeed();
    await expect(first).resolves.toBe(sdk);
    await expect(rejected).resolves.toBe(sdk);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('stops after two failed requests, then permits a deliberate manual retry', async () => {
    const first = loadAMap().catch(error => error);
    scripts()[0].dispatchEvent(new Event('error'));
    await vi.advanceTimersByTimeAsync(500);
    expect(scripts()).toHaveLength(1);
    scripts()[0].dispatchEvent(new Event('error'));
    const error = await first;
    expect(error).toBeInstanceOf(Error);
    expect(error.message).toMatch(/网络/);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(scripts()).toHaveLength(0);
    const manual = loadAMap();
    expect(scripts()).toHaveLength(1);
    succeed();
    await expect(manual).resolves.toBe(sdk);
  });

  it('times out without resetting or replacing a still loading SDK', async () => {
    const first = loadAMap().catch(error => error);
    const original = scripts()[0];
    const callback = sdkWindow.___onAPILoaded;
    await vi.advanceTimersByTimeAsync(25_000);
    const error = await first;
    expect(error).toBeInstanceOf(Error);
    expect(error.message).toMatch(/超时/);
    expect(error.message).toMatch(/刷新/);
    const manual = loadAMap();
    expect(scripts()).toEqual([original]);
    expect(sdkWindow.___onAPILoaded).toBe(callback);
    succeed();
    await expect(manual).resolves.toBe(sdk);
    expect(window.AMap).toBe(sdk);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('allows late success after timeout to satisfy the next request', async () => {
    const first = loadAMap().catch(error => error);
    await vi.advanceTimersByTimeAsync(25_000);
    expect(await first).toBeInstanceOf(Error);
    succeed();
    await expect(loadAMap()).resolves.toBe(sdk);
    expect(scripts()).toHaveLength(1);
  });

  it('does not auto-retry a late failure after the consumer deadline', async () => {
    const first = loadAMap().catch(error => error);
    const original = scripts()[0];
    await vi.advanceTimersByTimeAsync(25_000);
    expect(await first).toBeInstanceOf(Error);
    original.dispatchEvent(new Event('error'));
    await vi.advanceTimersByTimeAsync(60_000);
    expect(scripts()).toHaveLength(0);
    const manual = loadAMap();
    expect(scripts()).toHaveLength(1);
    succeed();
    await expect(manual).resolves.toBe(sdk);
  });

  it('does not start the retry if a suspended tab resumes after the deadline', async () => {
    const first = loadAMap().catch(error => error);
    scripts()[0].dispatchEvent(new Event('error'));
    await vi.advanceTimersByTimeAsync(0);
    vi.setSystemTime(Date.now() + 30_000);
    await vi.advanceTimersByTimeAsync(500);
    expect(scripts()).toHaveLength(0);
    expect((await first).message).toMatch(/超时/);
  });

  it('times out an overdue consumer even when success runs before its delayed timer', async () => {
    const first = loadAMap().catch(error => error);
    vi.setSystemTime(Date.now() + 30_000);
    succeed();
    const error = await first;
    expect(error).toBeInstanceOf(Error);
    expect(error.message).toMatch(/超时/);
    // The SDK itself succeeded; a fresh consumer can use it without a reload.
    await expect(loadAMap()).resolves.toBe(sdk);
    expect(scripts()).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('does not retry configuration rejection or expose provider strings and keys', async () => {
    const first = loadAMap().catch(error => error);
    const original = scripts()[0];
    sdkWindow.___onAPILoaded!('INVALID_USER_KEY https://example.test?key=secret-value');
    const error = await first;
    expect(error).toBeInstanceOf(Error);
    expect(error.message).toMatch(/配置|初始化/);
    expect(error.message).not.toContain('secret-value');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(scripts()).toEqual([original]);
    await expect(loadAMap()).rejects.toBeInstanceOf(Error);
    expect(scripts()).toEqual([original]);
  });

  it('rejects missing configuration without injecting a script', async () => {
    vi.stubEnv('VITE_AMAP_KEY', '');
    await expect(loadAMap()).rejects.toThrow('VITE_AMAP_KEY');
    expect(scripts()).toHaveLength(0);
  });

  it('does not accept a success callback without an actual Map constructor', async () => {
    const first = loadAMap().catch(error => error);
    window.AMap = {};
    sdkWindow.___onAPILoaded!();
    expect(await first).toBeInstanceOf(Error);
  });
});
