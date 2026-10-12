import { describe, expect, it } from 'vitest';
import { mergeMessageHistory } from '@/lib/mergeMessageHistory';
import type { ChatMessage } from '@/types/message';

function text(id: string, content: string, created_at: number, role: ChatMessage['role'] = 'user'): ChatMessage {
  return { id, role, blocks: [{ type: 'text', content }], status: 'done', created_at };
}

function map(id: string, created_at: number): ChatMessage {
  return { id, role: 'assistant', blocks: [{ type: 'map', artifact_id: 'artifact_' + id, layers: [], bbox: [0, 0, 1, 1], expired: true }], status: 'done', created_at };
}

const ids = (messages: ChatMessage[]) => messages.map(message => message.id);

describe('mergeMessageHistory', () => {
  it('keeps each cached map immediately after its corresponding persisted question across two rounds', () => {
    const server = [text('server-one', '问题一', 10), text('server-two', '问题二', 30)];
    const cached = [text('local-one', '问题一', 11), map('map-one', 12), text('local-two', '问题二', 31), map('map-two', 32)];
    expect(ids(mergeMessageHistory(server, cached))).toEqual(['server-one', 'map-one', 'server-two', 'map-two']);
  });

  it('matches identical repeated questions one-to-one in occurrence order', () => {
    const server = [text('first', '再试一次', 10), text('second', '再试一次', 30)];
    const cached = [text('local-first', '再试一次', 11), map('map-first', 12), text('local-second', '再试一次', 31), map('map-second', 32), text('pending-third', '再试一次', 50)];
    expect(ids(mergeMessageHistory(server, cached))).toEqual(['first', 'map-first', 'second', 'map-second', 'pending-third']);
  });

  it('reserves stable ID matches before using repeated legacy text matches', () => {
    const server = [text('first', '相同问题', 10), text('second', '相同问题', 30)];
    const cached = [text('first', '旧版问题文字', 11), map('map-first', 12), text('local-second', '相同问题', 31), map('map-second', 32)];
    const result = mergeMessageHistory(server, cached);
    expect(ids(result)).toEqual(['first', 'map-first', 'second', 'map-second']);
    expect(result[0].blocks).toEqual(server[0].blocks);
  });

  it('inserts older, intervening and newer server records among the cached conversation', () => {
    const server = [text('older', '更早的问题', 1), text('first', '问题一', 10), text('between', '其他窗口的问题', 25), text('second', '问题二', 30), text('newest', '最新问题', 50)];
    const cached = [text('local-first', '问题一', 11), map('map-first', 20), text('local-second', '问题二', 31), map('map-second', 40)];
    expect(ids(mergeMessageHistory(server, cached))).toEqual(['older', 'first', 'map-first', 'between', 'second', 'map-second', 'newest']);
  });

  it('uses server ordering even when new record timestamps are equal or skewed', () => {
    const server = [text('first', '问题一', 10), text('new-one', '新增一', 35), text('new-two', '新增二', 25), text('last', '最后问题', 40)];
    const cached = [text('first', '问题一', 11), map('map-first', 20), text('last', '最后问题', 41), map('map-last', 42)];
    expect(ids(mergeMessageHistory(server, cached))).toEqual(['first', 'map-first', 'new-one', 'new-two', 'last', 'map-last']);
  });

  it('merges histories without common identities by timestamp without front-loading newer server messages', () => {
    const server = [text('older', '服务器旧消息', 1), text('newer', '服务器新消息', 40)];
    const cached = [text('local', '本地未确认消息', 10), map('map-local', 20)];
    expect(ids(mergeMessageHistory(server, cached))).toEqual(['older', 'local', 'map-local', 'newer']);
  });

  it('prefers authoritative text for a stable ID while retaining its cached artifact blocks and run binding', () => {
    const rich = { ...map('answer', 20), run_id: 'run-one', blocks: [text('unused', '旧文字', 0).blocks[0], ...map('unused', 0).blocks] };
    const server = [text('answer', '服务器修正后的文字', 19, 'assistant')];
    const result = mergeMessageHistory(server, [rich]);
    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({ id: 'answer', run_id: 'run-one', created_at: 19 });
    expect(result[0].blocks).toEqual([...server[0].blocks, rich.blocks[1]]);
  });

  it('does not text-match different roles or remove unrelated rich artifact messages', () => {
    const server = [text('server', '同一文字', 10)];
    const cached = [text('assistant', '同一文字', 20, 'assistant'), { ...map('rich', 30), blocks: [{ type: 'text' as const, artifact_id: 'download', content: '同一文字' }] }];
    expect(ids(mergeMessageHistory(server, cached))).toEqual(['server', 'assistant', 'rich']);
  });

  it('returns complete server history when there is no cache', () => {
    const server = [text('one', '问题一', 10), text('two', '问题二', 20)];
    expect(mergeMessageHistory(server, [])).toEqual(server);
  });

  it('retains cached history when no server messages are available', () => {
    const cached = [text('one', '问题一', 10), map('answer', 20)];
    expect(mergeMessageHistory([], cached)).toEqual(cached);
  });

  it('does not mutate either input history or its message blocks', () => {
    const server = [text('one', '问题一', 10), text('new', '新问题', 40)];
    const cached = [text('one', '旧问题一', 11), map('answer', 20)];
    const before = JSON.stringify({ server, cached });
    mergeMessageHistory(server, cached);
    expect(JSON.stringify({ server, cached })).toBe(before);
  });
});
