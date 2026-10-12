import { useId, useState } from 'react';

interface Schema {
  type?: string;
  title?: string;
  properties?: Record<string, Schema>;
  required?: string[];
  enum?: unknown[];
  description?: string;
  minLength?: number;
  maxLength?: number;
  minimum?: number;
  maximum?: number;
}

export function hasStructuredFields(schema: Record<string, unknown> | null | undefined): boolean {
  return !!schema && (Object.keys((schema.properties as object) ?? {}).length > 0 || (Array.isArray(schema.required) && schema.required.length > 0));
}

/** Only values entered by the user become patch fields; schema defaults are not consent. */
export function RuntimeInputForm({ schema, disabled, onSubmit }: { schema: Record<string, unknown>; disabled: boolean; onSubmit: (patch: Record<string, unknown>) => void }) {
  const spec = schema as Schema;
  const keys = [...new Set([...(spec.required ?? []), ...Object.keys(spec.properties ?? {})])];
  const [values, setValues] = useState<Record<string, string>>({});
  const [error, setError] = useState('');
  const formId = useId();
  return <form className="runtime-input-form mt-4 space-y-4" noValidate onSubmit={(event) => {
    event.preventDefault();
    try {
      const patch: Record<string, unknown> = {};
      for (const key of keys) {
        const raw = values[key] ?? '';
        if (!raw.trim()) { if (spec.required?.includes(key)) throw new Error(`请填写 ${key}`); continue; }
        const field = spec.properties?.[key] ?? {};
        const type = field.type ?? (field.properties ? 'object' : 'string');
        let value: unknown = raw;
        if (field.enum) {
          value = field.enum.find((allowed) => (typeof allowed === 'string' ? allowed : JSON.stringify(allowed)) === raw);
        } else if (type === 'number' || type === 'integer') {
          value = Number(raw);
          if (!Number.isFinite(value) || (type === 'integer' && !Number.isInteger(value))) throw new Error(`${key} 需要有效的${type === 'integer' ? '整数' : '数字'}`);
        } else if (type === 'object' || type === 'array' || type === 'boolean') {
          try { value = JSON.parse(raw); } catch { throw new Error(`${key} 需要有效的 JSON`); }
          if (type === 'object' && (!value || typeof value !== 'object' || Array.isArray(value))) throw new Error(`${key} 需要 JSON 对象`);
          if (type === 'array' && !Array.isArray(value)) throw new Error(`${key} 需要 JSON 数组`);
          if (type === 'boolean' && typeof value !== 'boolean') throw new Error(`${key} 需要 true 或 false`);
        }
        if (field.enum && !field.enum.some((allowed) => JSON.stringify(allowed) === JSON.stringify(value))) throw new Error(`${key} 不在允许的选项中`);
        if (typeof value === 'number') {
          if (field.minimum !== undefined && value < field.minimum) throw new Error(`${key} 不能小于 ${field.minimum}`);
          if (field.maximum !== undefined && value > field.maximum) throw new Error(`${key} 不能大于 ${field.maximum}`);
        }
        if (typeof value === 'string') {
          const length = Array.from(value).length;
          if (field.minLength !== undefined && length < field.minLength) throw new Error(`${key} 至少需要 ${field.minLength} 个字符`);
          if (field.maxLength !== undefined && length > field.maxLength) throw new Error(`${key} 最多允许 ${field.maxLength} 个字符`);
        }
        patch[key] = value;
      }
      if (!Object.keys(patch).length) throw new Error('请至少填写一项补充信息');
      setError(''); onSubmit(patch);
    } catch (problem) { setError((problem as Error).message); }
  }}>
    {keys.map((key, index) => {
      const field = spec.properties?.[key] ?? {};
      const type = field.type ?? (field.properties ? 'object' : 'string');
      const descriptionId = `${formId}-${index}-description`;
      const control = {
        'aria-label': key,
        'aria-describedby': field.description ? descriptionId : undefined,
        'aria-required': spec.required?.includes(key) ?? false,
        value: values[key] ?? '',
        disabled,
        onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => setValues((current) => ({ ...current, [key]: event.target.value })),
        className: 'mt-2 block w-full rounded-lg border border-ink-600 bg-ink-950 px-3 py-2.5 text-sm text-ink-100 outline-none transition-colors focus:border-amber focus:ring-1 focus:ring-amber/30 disabled:opacity-50',
      };
      return <label key={key} className="block text-sm text-ink-200">
        <span className="flex items-center justify-between gap-3 font-medium">
          {field.title ?? key}
          <span className="text-[10px] font-normal tracking-wider text-ink-400">{spec.required?.includes(key) ? '必填' : '可选'}</span>
        </span>
        {field.description && <span id={descriptionId} className="mt-1 block text-xs leading-relaxed text-ink-400">{field.description}</span>}
        {field.enum ? <select {...control}>
          <option value="">请选择</option>
          {field.enum.map((allowed, optionIndex) => {
            const option = typeof allowed === 'string' ? allowed : JSON.stringify(allowed);
            return <option key={optionIndex} value={option}>{option}</option>;
          })}
        </select> : type === 'boolean' ? <select {...control}>
          <option value="">请选择</option><option value="true">是</option><option value="false">否</option>
        </select> : type === 'object' || type === 'array' ? <textarea {...control} rows={4} spellCheck={false} placeholder={type === 'object' ? 'JSON 对象，例如 {"epsg":4326}' : 'JSON 数组，例如 ["a", "b"]'} />
          : <input {...control} type={type === 'number' || type === 'integer' ? 'number' : 'text'}
            min={field.minimum} max={field.maximum} step={type === 'integer' ? 1 : type === 'number' ? 'any' : undefined}
            minLength={field.minLength} maxLength={field.maxLength} />}
      </label>;
    })}
    {error && <div role="alert" className="text-sm text-signal-error">{error}</div>}
    <button type="submit" disabled={disabled} className="rounded bg-amber px-3 py-1.5 text-sm text-ink-950 disabled:opacity-50">提交补充信息</button>
  </form>;
}
