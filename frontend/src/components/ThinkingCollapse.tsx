// frontend/src/components/ThinkingCollapse.tsx
// 折叠思考过程 — ReAct 推理链（tool_call 事件 + Markdown > 引用）。
// 默认收起，只把最终地图和分析报告外露。

import { useState } from 'react';
import { getToolLabel } from '@/lib/toolLabels';
import type { ThinkingStep } from '@/types/message';

interface CodeModeFields {
  code?: string;
  stdout?: string;
  result?: unknown;
  executor_type?: string;
  error?: string;
}

interface ThinkingCollapseProps {
  steps: ThinkingStep[];
  defaultOpen?: boolean;
}

export function ThinkingCollapse({ steps, defaultOpen = false }: ThinkingCollapseProps) {
  const [open, setOpen] = useState(defaultOpen);
  if (steps.length === 0) return null;

  return (
    <div className="mb-3 overflow-hidden rounded-md border border-ink-700 bg-ink-900/60">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition hover:bg-ink-800/60"
        aria-expanded={open}
      >
        <svg
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          className={`text-amber transition-transform ${open ? 'rotate-90' : ''}`}
        >
          <path d="m9 18 6-6-6-6" />
        </svg>
        <span className="font-mono text-[11px] uppercase tracking-wider text-ink-400">
          思考过程
        </span>
        <span className="font-mono text-[10px] text-ink-500">· {steps.length} 步</span>
      </button>
      {open && (
        <ol className="border-t border-ink-700 px-3 py-2">
          {steps.map((step, i) => (
            <li key={i} className="flex gap-2 py-1">
              <span className="font-mono text-[10px] text-ink-500">
                {String(i + 1).padStart(2, '0')}
              </span>
              <div className="flex-1">
                {step.kind === 'tool_call' ? (
                  <ToolCallLine tool={step.tool} args={step.args} />
                ) : step.kind === 'react_trace' ? (
                  <ReactTraceLine step={step} />
                ) : (
                  <div className="font-mono text-[11px] text-ink-300">{step.note}</div>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// 工具分类（用于颜色标记）
const TOOL_CATEGORY: Record<string, string> = {
  // vector 分析
  buffer: 'vector', overlay: 'vector', voronoi: 'vector', isochrone: 'vector',
  clip_layer: 'vector', dissolve_layer: 'vector', merge_layers: 'vector',
  join_by_location: 'vector', join_by_nearest: 'vector', count_points_in_polygon: 'vector',
  extract_by_location: 'vector', convex_hull: 'vector', bounding_boxes: 'vector',
  centroid_layer: 'vector', point_on_surface: 'vector', simplify_geometry: 'vector',
  fix_geometries: 'vector', check_validity: 'vector', multipart_to_singlepart: 'vector',
  delete_duplicate_geometries: 'vector', snap_geometries: 'vector',
  reproject_layer: 'vector', batch_reproject_layers: 'vector',
  // raster
  slope: 'raster', aspect: 'raster', hillshade: 'raster', contour: 'raster',
  reproject_raster: 'raster', clip_raster_by_mask: 'raster', clip_raster_by_extent: 'raster',
  raster_calculator: 'raster', zonal_statistics: 'raster', raster_sampling: 'raster',
  rasterize_vector: 'raster', polygonize_raster: 'raster', reclassify_raster: 'raster',
  terrain_ruggedness_index: 'raster', topographic_position_index: 'raster', roughness: 'raster',
  // attribute
  extract_by_attribute: 'attribute', keep_fields: 'attribute', rename_field: 'attribute',
  field_calculator: 'attribute',
  // io
  load_vector: 'io', load_raster: 'io', load_csv: 'io', csv_to_points: 'io',
  summarize_layer: 'io', export_result: 'io',
};

const CATEGORY_COLORS: Record<string, string> = {
  vector: 'text-cyan-400',
  raster: 'text-purple-400',
  attribute: 'text-green-400',
  io: 'text-yellow-400',
};

function ToolCallLine({ tool, args }: { tool?: string; args?: Record<string, unknown> }) {
  const argStr = args ? formatArgs(args) : '';
  const category = tool ? TOOL_CATEGORY[tool] : undefined;
  const colorClass = category ? CATEGORY_COLORS[category] : 'text-amber';
  return (
    <div className="font-mono text-[11px] leading-relaxed">
      <span className={colorClass}>→ {tool ?? 'tool'}</span>
      {argStr && <span className="text-ink-400"> {argStr}</span>}
    </div>
  );
}

function ReactTraceLine({ step }: { step: ThinkingStep & CodeModeFields }) {
  const tools = step.tool_calls ?? [];
  const results = step.tool_results ?? [];
  const [errorExpanded, setErrorExpanded] = useState(false);
  const isCodeMode = !!step.code;

  return (
    <div className="space-y-1">
      {step.thinking && (
        <div className="font-mono text-[10px] text-ink-400 italic">
          {step.thinking.length > 100 ? step.thinking.slice(0, 100) + '…' : step.thinking}
        </div>
      )}

      {isCodeMode ? (
        <>
          {/* Code-mode 专用渲染：Python 代码 + stdout + error */}
          {step.code && (
            <pre className="font-mono text-[10px] text-ink-200 bg-ink-950 rounded p-2 overflow-x-auto max-h-40 overflow-y-auto">
              {step.code.length > 500 ? step.code.slice(0, 500) + '\n…' : step.code}
            </pre>
          )}
          {step.stdout && (
            <div className="font-mono text-[10px] text-green-400 bg-ink-950 rounded p-1 max-h-20 overflow-y-auto">
              {step.stdout}
            </div>
          )}
          {step.result !== undefined && step.result !== null && String(step.result) !== '{}' && (
            <div className="font-mono text-[10px] text-blue-400 bg-ink-950 rounded p-1 max-h-20 overflow-y-auto">
              {typeof step.result === 'string' ? step.result : JSON.stringify(step.result, null, 2)?.slice(0, 500)}
            </div>
          )}
          {step.error && (
            <div className="font-mono text-[10px] text-red-400 bg-red-950/30 rounded p-1">
              {errorExpanded || step.error.split('\n').length <= 20
                ? step.error
                : (
                  <>
                    {step.error.split('\n').slice(0, 20).join('\n')}
                    <button
                      type="button"
                      className="ml-1 text-ink-500 underline text-[9px]"
                      onClick={() => setErrorExpanded(true)}
                    >
                      展开 ({step.error.split('\n').length - 20} 行更多)
                    </button>
                  </>
                )}
            </div>
          )}
          {step.executor_type && (
            <span className="font-mono text-[9px] px-1 rounded bg-ink-800 text-ink-400">
              {step.executor_type}
            </span>
          )}
        </>
      ) : (
        <>
          {/* JSON-mode 标准渲染 */}
          {tools.map((tc, j) => (
            <ToolCallLine key={j} tool={tc.tool_name} args={tc.params} />
          ))}
          {results.length > 0 && (
            <div className="flex gap-1 flex-wrap">
              {results.map((r, j) => (
                <span
                  key={j}
                  className={`font-mono text-[9px] px-1 rounded ${
                    r.status === 'success' ? 'bg-green-900/40 text-green-400' :
                    r.status === 'error' ? 'bg-red-900/40 text-red-400' :
                    'bg-ink-800 text-ink-400'
                  }`}
                >
                  {getToolLabel(r.tool_name)}: {r.status}
                </span>
              ))}
            </div>
          )}
        </>
      )}

      {step.observer_summary && (
        <div className="font-mono text-[10px] text-ink-500 border-l-2 border-ink-700 pl-2">
          {step.observer_summary}
        </div>
      )}
    </div>
  );
}

function formatArgs(args: Record<string, unknown>): string {
  const entries = Object.entries(args).slice(0, 4); // 只展示前 4 个参数
  const parts = entries.map(([k, v]) => {
    const vs = typeof v === 'string' ? v : JSON.stringify(v);
    const truncated = vs.length > 40 ? `${vs.slice(0, 40)}…` : vs;
    return `${k}=${truncated}`;
  });
  let s = parts.join(' ');
  if (Object.keys(args).length > 4) s += ' …';
  return s;
}
