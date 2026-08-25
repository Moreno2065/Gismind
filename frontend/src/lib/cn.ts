// frontend/src/lib/cn.ts
// 极简 className 合并工具 — 不引入 clsx/tailwind-merge 依赖。

export function cn(...parts: Array<string | number | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}
