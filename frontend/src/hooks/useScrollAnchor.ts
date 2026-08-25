// frontend/src/hooks/useScrollAnchor.ts
// 滚动锚定：
//   - 自动触底：流式输出新内容时，若滚动条在底部则自动跟随；
//              若用户手动上滚查看历史，则不再强行触底。
//   - 高度突变保护：懒加载地图实例化导致高度突变时，保持当前滚动位置。

import { useCallback, useEffect, useRef } from 'react';

const STICK_THRESHOLD = 80; // px — 距底部多少以内视为"用户在底部"

export function useScrollAnchor<T extends HTMLElement>() {
  const containerRef = useRef<T | null>(null);
  const stickToBottomRef = useRef(true); // 是否跟随底部
  const prevHeightRef = useRef(0); // 上一次内容高度（用于高度突变补偿）

  /** 监听滚动 — 用户上滚则停止触底，回到底部则恢复 */
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onScroll = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      stickToBottomRef.current = distance <= STICK_THRESHOLD;
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, []);

  /** 平滑滚到底部（仅当当前在底部跟随状态） */
  const maybeStick = useCallback(() => {
    const el = containerRef.current;
    if (!el || !stickToBottomRef.current) return;
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  }, []);

  /** 强制立即触底（用户发新消息时） */
  const forceBottom = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    stickToBottomRef.current = true;
    el.scrollTo({ top: el.scrollHeight, behavior: 'auto' });
  }, []);

  /**
   * 高度突变补偿：在某个同步变更前后调用 begin()/end()，
   * 若用户不在底部，则保持其相对视口位置不变。
   */
  const beforeMutation = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    prevHeightRef.current = el.scrollHeight;
  }, []);

  const afterMutation = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    if (stickToBottomRef.current) {
      el.scrollTo({ top: el.scrollHeight, behavior: 'auto' });
      return;
    }
    const delta = el.scrollHeight - prevHeightRef.current;
    if (delta !== 0) {
      el.scrollTop = el.scrollTop + delta;
    }
  }, []);

  return {
    containerRef,
    maybeStick,
    forceBottom,
    beforeMutation,
    afterMutation,
    isSticking: () => stickToBottomRef.current,
  };
}
