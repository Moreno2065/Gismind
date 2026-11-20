import { expect, it, vi } from 'vitest';

const { useMock } = vi.hoisted(() => ({
  useMock: vi.fn(),
}));

vi.mock('echarts/core', async () => {
  const actual = await vi.importActual<typeof import('echarts/core')>('echarts/core');
  return {
    ...actual,
    use: (...args: Parameters<typeof actual.use>) => {
      useMock(...args);
      return actual.use(...args);
    },
  };
});

it('clears cached chartModulesReady promise on chunk load failure so retry succeeds', async () => {
  useMock.mockImplementationOnce(() => {
    throw new Error('chunk fetch failed');
  });

  const { prepareCharts } = await import('@/components/ChartView');
  await expect(prepareCharts()).rejects.toThrow('chunk fetch failed');
  await expect(prepareCharts()).resolves.toBeUndefined();
  expect(useMock).toHaveBeenCalledTimes(2);
});
