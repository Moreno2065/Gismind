import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RuntimeInputForm } from '@/components/RuntimeInputForm';
import { RunStatusBar } from '@/components/RunStatusBar';
import { projectionFromSnapshot } from '@/hooks/useRuntimeRun';

afterEach(cleanup);

describe('Runtime schema controls', () => {
  it('asks for an explicit enum and boolean choice without applying schema defaults', () => {
    const submit = vi.fn();
    render(<RuntimeInputForm disabled={false} onSubmit={submit} schema={{
      required: ['format'], properties: {
        format: { type: 'string', enum: ['geojson', 'gpkg'], default: 'geojson' },
        approved: { type: 'boolean', default: true },
      },
    }} />);
    const format = screen.getByRole('combobox', { name: 'format' }) as HTMLSelectElement;
    const approved = screen.getByRole('combobox', { name: 'approved' }) as HTMLSelectElement;
    expect(format.value).toBe('');
    expect(approved.value).toBe('');
    fireEvent.change(format, { target: { value: 'gpkg' } });
    fireEvent.click(screen.getByRole('button', { name: '提交补充信息' }));
    expect(submit).toHaveBeenLastCalledWith({ format: 'gpkg' });
    fireEvent.change(approved, { target: { value: 'false' } });
    fireEvent.click(screen.getByRole('button', { name: '提交补充信息' }));
    expect(submit).toHaveBeenLastCalledWith({ format: 'gpkg', approved: false });
  });

  it('exposes schema descriptions and input limits and validates them before submission', () => {
    const submit = vi.fn();
    const { container } = render(<RuntimeInputForm disabled={false} onSubmit={submit} schema={{
      properties: {
        title: { type: 'string', description: '图层显示名称', minLength: 2, maxLength: 12 },
        count: { type: 'integer', minimum: 1, maximum: 10 },
        distance: { type: 'number', minimum: 0.5, maximum: 100 },
      },
    }} />);
    const title = screen.getByRole('textbox', { name: 'title' }) as HTMLInputElement;
    const count = screen.getByRole('spinbutton', { name: 'count' }) as HTMLInputElement;
    const distance = screen.getByRole('spinbutton', { name: 'distance' }) as HTMLInputElement;
    expect(screen.getByText('图层显示名称')).toBeTruthy();
    expect(title.getAttribute('aria-describedby')).toBeTruthy();
    expect([title.minLength, title.maxLength]).toEqual([2, 12]);
    expect([count.min, count.max, count.step]).toEqual(['1', '10', '1']);
    expect([distance.min, distance.max, distance.step]).toEqual(['0.5', '100', 'any']);
    fireEvent.change(title, { target: { value: '短' } });
    fireEvent.submit(container.querySelector('form')!);
    expect(screen.getByRole('alert').textContent).toMatch(/至少.*2/);
    expect(submit).not.toHaveBeenCalled();
    fireEvent.change(title, { target: { value: '医院图层' } });
    fireEvent.change(count, { target: { value: '11' } });
    fireEvent.submit(container.querySelector('form')!);
    expect(screen.getByRole('alert').textContent).toMatch(/10/);
    expect(submit).not.toHaveBeenCalled();
    fireEvent.change(count, { target: { value: '3' } });
    fireEvent.change(distance, { target: { value: '2.5' } });
    fireEvent.submit(container.querySelector('form')!);
    expect(submit).toHaveBeenLastCalledWith({ title: '医院图层', count: 3, distance: 2.5 });
  });

  it('uses multiline JSON fields and preserves object and array types', () => {
    const submit = vi.fn();
    render(<RuntimeInputForm disabled={false} onSubmit={submit} schema={{ properties: {
      crs: { type: 'object' }, ids: { type: 'array' },
    } }} />);
    const crs = screen.getByRole('textbox', { name: 'crs' });
    const ids = screen.getByRole('textbox', { name: 'ids' });
    expect(crs.tagName).toBe('TEXTAREA');
    expect(ids.tagName).toBe('TEXTAREA');
    fireEvent.change(crs, { target: { value: '{"code":4326}' } });
    fireEvent.change(ids, { target: { value: '["a","b"]' } });
    fireEvent.click(screen.getByRole('button', { name: '提交补充信息' }));
    expect(submit).toHaveBeenCalledWith({ crs: { code: 4326 }, ids: ['a', 'b'] });
  });
});

describe('Run status bar', () => {
  const projection = projectionFromSnapshot({
    run_id: 'status-run', session_id: 'session', status: 'running', run_version: 2,
    tasks: [
      { task_id: 'a', task_key: '读取图层', status: 'succeeded', attempts: 1 },
      { task_id: 'b', task_key: '空间筛选', status: 'running', attempts: 1 },
      { task_id: 'c', task_key: '导出结果', status: 'blocked', attempts: 0 },
    ], output_artifacts: [], last_event_id: 4, last_seq: 4,
  });

  it('shows run status and connection recovery separately with factual task counts', () => {
    render(<RunStatusBar projection={projection} connectionState="reconnecting" terminalSettled={false} />);
    expect(screen.getByText('运行中')).toBeTruthy();
    expect(screen.getByText('正在恢复连接')).toBeTruthy();
    expect(screen.getByText('已完成 1 / 3 个步骤')).toBeTruthy();
    expect(screen.getByText('空间筛选')).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.queryByText(/%/)).toBeNull();
  });

  it('keeps a terminal projection visibly syncing until its final event settles', () => {
    const { rerender } = render(<RunStatusBar projection={{ ...projection, status: 'completed' }} connectionState="live" terminalSettled={false} />);
    expect(screen.getByText('正在同步最终结果')).toBeTruthy();
    expect(screen.queryByText('已完成')).toBeNull();
    rerender(<RunStatusBar projection={{ ...projection, status: 'completed' }} connectionState="closed" terminalSettled />);
    expect(screen.getByText('已完成')).toBeTruthy();
    expect(screen.queryByText('正在同步最终结果')).toBeNull();
  });

  it('does not invent task progress while awaiting a snapshot', () => {
    render(<RunStatusBar projection={null} connectionState="connecting" terminalSettled={false} />);
    expect(screen.getByText('正在同步任务')).toBeTruthy();
    expect(screen.queryByText(/个步骤/)).toBeNull();
    expect(screen.queryByRole('progressbar')).toBeNull();
  });
});
