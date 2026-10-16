// frontend/src/lib/amap.ts
// 高德 JS API 单例加载器 — 避免重复 load，配置 securityJsCode。

import AMapLoader from '@amap/amap-jsapi-loader';

// 高德 JS API 运行时注入的全局命名空间；无官方 @types，用 any 表示。
type AMapNS = any;

declare global {
  interface Window {
    _AMapSecurityConfig?: { securityJsCode: string };
    AMap?: AMapNS;
  }
}

let loadPromise: Promise<AMapNS> | null = null;
let sdkAttempt: { promise: Promise<AMapNS>; retryable: boolean } | null = null;
const LOAD_DEADLINE_MS = 25_000;
const RETRY_DELAY_MS = 500;
const TIMEOUT_MESSAGE = '地图服务加载超时。可重试等待；若持续无响应，请刷新页面。';

class MapLoadError extends Error {
  constructor(readonly kind: 'network' | 'timeout' | 'configuration', message: string) {
    super(message);
    this.name = 'MapLoadError';
  }
}

function ensureSecurityConfig() {
  if (!window._AMapSecurityConfig) {
    const code = import.meta.env.VITE_AMAP_SECURITY_CODE;
    if (code) {
      window._AMapSecurityConfig = { securityJsCode: code };
    }
  }
}

function requestSdk(key: string): Promise<AMapNS> {
  if (sdkAttempt) {
    // Timeout does not cancel a script. Share the original pending request and
    // never reset a ready SDK: other mounted maps still depend on its globals.
    if (!sdkAttempt.retryable || window.AMap) return sdkAttempt.promise;
    // The installed loader latches terminal script errors until reset(). Only
    // reset after a real onerror, never while its fixed callback is pending.
    (AMapLoader as typeof AMapLoader & { reset(): void }).reset();
  }
  const previousScripts = new Set(document.querySelectorAll('script'));
  const raw = AMapLoader.load({
    key,
    version: '2.0',
    // AMap.GeoJSON removed — unused; all GeoJSON rendering is done manually via mapRenderers.ts
    plugins: ['AMap.Scale', 'AMap.ToolBar'],
  });
  const script = [...document.querySelectorAll('script')].find(node =>
    !previousScripts.has(node) && node.src.startsWith('https://webapi.amap.com/maps?'));
  const attempt = { promise: raw as Promise<AMapNS>, retryable: false };
  attempt.promise = raw.then((mod: AMapNS) => {
    if (typeof mod?.Map !== 'function') {
      throw new MapLoadError('configuration', '地图服务初始化失败，请刷新页面后重试。');
    }
    return mod;
  }, (cause: unknown) => {
    attempt.retryable = cause instanceof Event && cause.type === 'error'
      && cause.target === script && !window.AMap;
    if (attempt.retryable) {
      // This script has definitively failed. Do not leave stale event handlers
      // that could mutate the loader after a fresh attempt starts.
      if (script) { script.onerror = null; script.remove(); }
      throw new MapLoadError('network', '地图服务网络连接失败，请检查网络后重试。');
    }
    // SDK rejections may be strings or Events and can contain credential URLs.
    throw new MapLoadError('configuration', '地图服务配置或初始化失败，请检查配置并刷新页面。');
  });
  sdkAttempt = attempt;
  return attempt.promise;
}

async function waitForSdk(promise: Promise<AMapNS>, remainingMs: number): Promise<AMapNS> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([promise, new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new MapLoadError('timeout', TIMEOUT_MESSAGE)), Math.max(0, remainingMs));
    })]);
  } finally {
    clearTimeout(timer);
  }
}

async function loadWithRecovery(key: string): Promise<AMapNS> {
  const deadline = Date.now() + LOAD_DEADLINE_MS;
  for (let attempt = 0; ; attempt++) {
    // Timer throttling/suspension can consume the budget during backoff.
    if (Date.now() >= deadline) throw new MapLoadError('timeout', TIMEOUT_MESSAGE);
    try {
      const sdk = await waitForSdk(requestSdk(key), deadline - Date.now());
      // A delayed timeout task may lose to a success callback after suspension.
      if (Date.now() >= deadline) throw new MapLoadError('timeout', TIMEOUT_MESSAGE);
      return sdk;
    } catch (error) {
      if (!(error instanceof MapLoadError) || error.kind !== 'network'
        || attempt >= 1 || Date.now() + RETRY_DELAY_MS >= deadline) throw error;
      await new Promise(resolve => setTimeout(resolve, RETRY_DELAY_MS));
    }
  }
}

/** Shared SDK load; one terminal-network retry within a bounded waiting period. */
export function loadAMap(): Promise<AMapNS> {
  if (loadPromise) return loadPromise;
  ensureSecurityConfig();
  const key = import.meta.env.VITE_AMAP_KEY as string | undefined;
  if (!key) return Promise.reject(new Error('VITE_AMAP_KEY 未配置'));
  const current = loadWithRecovery(key).catch((error: unknown) => {
    if (loadPromise === current) loadPromise = null;
    throw error;
  });
  loadPromise = current;
  return loadPromise;
}
