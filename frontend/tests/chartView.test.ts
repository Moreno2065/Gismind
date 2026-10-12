import { expect, it, vi } from 'vitest';
import * as echarts from 'echarts/core';
import { prepareCharts } from '@/components/ChartView';

it('renders the bar, line, and pie configs emitted by the chart builder', async () => {
  await prepareCharts();
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => ({
    measureText: (text: string) => ({ width: text.length * 8 }),
  }) as unknown as CanvasRenderingContext2D);

  const chart = echarts.init(document.createElement('div'), undefined, {
    renderer: 'canvas',
    ssr: true,
    width: 320,
    height: 320,
  });

  try {
    for (const type of ['bar', 'line', 'pie']) {
      const axes = type === 'pie' ? {} : {
        xAxis: { type: 'category', data: ['A'] },
        yAxis: { type: 'value' },
      };
      chart.setOption({
        ...axes,
        title: { text: type },
        tooltip: { trigger: type === 'pie' ? 'item' : 'axis' },
        series: [{
          type,
          data: type === 'pie' ? [{ name: 'A', value: 1 }] : [1],
        }],
      }, true);
      expect(chart.getZr().storage.getDisplayList(true).length).toBeGreaterThan(0);
    }
  } finally {
    chart.dispose();
  }
});

it('keeps other ECharts series and components available before a chart mounts', async () => {
  const chartModule = await import('@/components/ChartView');
  const prepareCharts = (chartModule as typeof chartModule & { prepareCharts: () => Promise<void> }).prepareCharts;
  expect(typeof prepareCharts).toBe('function');
  await prepareCharts();

  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => ({
    measureText: (text: string) => ({ width: text.length * 8 }),
  }) as unknown as CanvasRenderingContext2D);

  const chart = echarts.init(document.createElement('div'), undefined, {
    renderer: 'canvas',
    ssr: true,
    width: 320,
    height: 320,
  });
  try {
    chart.setOption({
      xAxis: { type: 'value' },
      yAxis: { type: 'value' },
      dataZoom: [{ type: 'inside' }],
      series: [{ type: 'scatter', data: [[1, 2]] }],
    });
    expect(chart.getModel().getSeriesByIndex(0)).toBeDefined();
    expect(chart.getZr().storage.getDisplayList(true).length).toBeGreaterThan(0);
  } finally {
    chart.dispose();
  }
});
