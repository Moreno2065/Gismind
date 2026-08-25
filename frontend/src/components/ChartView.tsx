// frontend/src/components/ChartView.tsx
// ECharts 渲染 — 接收 config，init + setOption。
// 主题化（dark / light 各一套调色板），resize 响应。

import { useEffect, useRef } from 'react';
import * as echarts from 'echarts';
import { useTheme } from '@/hooks/useTheme';

interface ChartViewProps {
  config: Record<string, unknown>;
  height?: number;
}

const DEFAULT_HEIGHT = 320;

export function ChartView({ config, height = DEFAULT_HEIGHT }: ChartViewProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const { theme } = useTheme();

  // 初始化一次，复用实例
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = echarts.init(containerRef.current, undefined, {
      renderer: 'canvas',
    });
    chartRef.current = chart;

    let ro: ResizeObserver | null = null;
    if ('ResizeObserver' in window) {
      ro = new ResizeObserver(() => chart.resize());
      ro.observe(containerRef.current);
    }

    return () => {
      ro?.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  // 配置或主题变化时重新合并默认色板并应用
  useEffect(() => {
    if (!chartRef.current) return;
    const merged = mergeThemeDefaults(config, theme);
    chartRef.current.setOption(merged, false);
  }, [config, theme]);

  return (
    <div
      className="mt-3 overflow-hidden rounded-lg border border-ink-700 bg-ink-900 p-3"
      style={{ height }}
    >
      <div ref={containerRef} className="h-full w-full" />
    </div>
  );
}

/**
 * 注入主题化默认值 — 文字/坐标轴/网格颜色与整体配色一致。
 * 颜色值从 :root / :root.light 的 CSS 变量中取，保证切换主题时随 UI 走。
 */
function mergeThemeDefaults(
  config: Record<string, unknown>,
  theme: 'dark' | 'light',
): Record<string, unknown> {
  const c = {
    ink200: cssRgb('--c-ink-200'),
    ink100: cssRgb('--c-ink-100'),
    ink300: cssRgb('--c-ink-300'),
    ink700: cssRgb('--c-ink-700'),
    amber: cssRgb('--c-amber'),
    signalThinking: cssRgb('--c-signal-thinking'),
    signalFetching: cssRgb('--c-signal-fetching'),
    signalDone: cssRgb('--c-signal-done'),
    signalError: cssRgb('--c-signal-error'),
    osm: cssRgb('--c-osm'),
  };

  // tooltip 背景用画布色，但带透明度
  const canvas = cssRgb('--c-ink-900');
  const tooltipBg = canvas || (theme === 'dark' ? 'rgba(15,20,27,0.95)' : 'rgba(253,250,242,0.95)');
  const tooltipBorder = c.ink700 || (theme === 'dark' ? '#2a3340' : '#d8cdb4');

  const defaults = {
    backgroundColor: 'transparent',
    textStyle: {
      color: c.ink200,
      fontFamily: 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif',
    },
    title: { textStyle: { color: c.ink100 }, subtextStyle: { color: c.ink300 } },
    legend: { textStyle: { color: c.ink300 } },
    tooltip: {
      backgroundColor: tooltipBg,
      borderColor: tooltipBorder,
      textStyle: {
        color: c.ink100,
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        fontSize: 12,
      },
    },
    xAxis: {
      axisLine: { lineStyle: { color: c.ink700 } },
      axisLabel: { color: c.ink300 },
      splitLine: { lineStyle: { color: c.ink700 } },
    },
    yAxis: {
      axisLine: { lineStyle: { color: c.ink700 } },
      axisLabel: { color: c.ink300 },
      splitLine: { lineStyle: { color: c.ink700 } },
    },
    color: [
      c.amber,
      c.signalThinking,
      c.signalDone,
      c.signalFetching,
      c.signalError,
      c.osm,
    ],
  };

  return deepMerge(defaults, config);
}

/** 读取 CSS 变量（'R G B' 三元组）拼成 'rgb(R G B)'；未读到返回 '' */
function cssRgb(name: string): string {
  if (typeof window === 'undefined') return '';
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v ? `rgb(${v})` : '';
}

/** 浅合并 — config 的值优先于 defaults */
function deepMerge(base: Record<string, unknown>, override: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...base };
  for (const k of Object.keys(override)) {
    const bv = base[k];
    const ov = override[k];
    if (bv && typeof bv === 'object' && !Array.isArray(bv) && ov && typeof ov === 'object' && !Array.isArray(ov)) {
      out[k] = deepMerge(bv as Record<string, unknown>, ov as Record<string, unknown>);
    } else {
      out[k] = ov;
    }
  }
  return out;
}
