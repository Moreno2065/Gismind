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

function ensureSecurityConfig() {
  if (!window._AMapSecurityConfig) {
    const code = import.meta.env.VITE_AMAP_SECURITY_CODE;
    if (code) {
      window._AMapSecurityConfig = { securityJsCode: code };
    }
  }
}

/** 获取 AMap 命名空间（单例，并发安全） */
export function loadAMap(): Promise<AMapNS> {
  if (loadPromise) return loadPromise;
  ensureSecurityConfig();
  const key = import.meta.env.VITE_AMAP_KEY as string | undefined;
  if (!key) {
    return Promise.reject(new Error('VITE_AMAP_KEY 未配置'));
  }
  loadPromise = AMapLoader.load({
    key,
    version: '2.0',
    // AMap.GeoJSON removed — unused; all GeoJSON rendering is done manually via mapRenderers.ts
    plugins: ['AMap.Scale', 'AMap.ToolBar'],
  })
    .then((mod: unknown) => mod as AMapNS)
    .catch((err: unknown) => {
      loadPromise = null; // 失败后允许重试
      throw err;
    });
  return loadPromise;
}
