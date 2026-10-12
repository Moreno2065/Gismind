import type { ChatMessage } from '@/types/message';

function plainText(message: ChatMessage): string | undefined {
  const block = message.blocks[0];
  return message.blocks.length === 1 && block.type === 'text' && !block.artifact_id ? block.content : undefined;
}

function authoritativeMessage(server: ChatMessage, cached: ChatMessage): ChatMessage {
  const artifactIds = new Set(server.blocks.flatMap(block => 'artifact_id' in block && block.artifact_id ? [block.artifact_id] : []));
  const richBlocks = cached.role === server.role ? cached.blocks.filter(block =>
    (block.type !== 'text' || Boolean(block.artifact_id))
    && (!('artifact_id' in block) || !block.artifact_id || !artifactIds.has(block.artifact_id))) : [];
  return { ...cached, ...server, blocks: [...server.blocks, ...richBlocks] };
}

function nextAnchors(length: number, matches: Map<number, number>): Array<number | undefined> {
  const anchors: Array<number | undefined> = new Array(length);
  let next: number | undefined;
  for (let index = length - 1; index >= 0; index--) {
    anchors[index] = next;
    if (matches.has(index)) next = matches.get(index);
  }
  return anchors;
}

/** Reconcile authoritative text with local rich blocks without regrouping turns. */
export function mergeMessageHistory(server: ChatMessage[], cached: ChatMessage[]): ChatMessage[] {
  if (!cached.length) return [...server];
  if (!server.length) return [...cached];
  const matches = new Map<number, number>();
  const used = new Set<number>();

  // Reserve every stable identity first: a repeated legacy string must never
  // consume the cache entry belonging to a different persisted message.
  server.forEach((message, index) => {
    const match = cached.findIndex((candidate, candidateIndex) => !used.has(candidateIndex) && candidate.id === message.id);
    if (match >= 0) { matches.set(index, match); used.add(match); }
  });

  const stableAnchors = nextAnchors(server.length, matches);
  let floor = 0;
  server.forEach((message, index) => {
    const stable = matches.get(index);
    if (stable !== undefined) { floor = Math.max(floor, stable + 1); return; }
    const content = plainText(message);
    if (content === undefined) return;
    const ceiling = Math.max(floor, stableAnchors[index] ?? cached.length);
    for (let candidate = floor; candidate < ceiling; candidate++) {
      if (!used.has(candidate) && cached[candidate].role === message.role && plainText(cached[candidate]) === content) {
        matches.set(index, candidate); used.add(candidate); floor = candidate + 1; break;
      }
    }
  });

  const anchors = nextAnchors(server.length, matches);
  const merged: ChatMessage[] = [];
  let cursor = 0;
  server.forEach((message, index) => {
    const match = matches.get(index);
    if (match !== undefined) {
      while (cursor < match) {
        if (!used.has(cursor)) merged.push(cached[cursor]);
        cursor++;
      }
      cursor = Math.max(cursor, match + 1);
      merged.push(authoritativeMessage(message, cached[match]));
      return;
    }

    // New server messages may belong before, between or after cached turns.
    // The next matched message bounds insertion; server order wins over clock
    // skew, while timestamps place unmatched cached messages in the interval.
    const ceiling = Math.max(cursor, anchors[index] ?? cached.length);
    while (cursor < ceiling) {
      if (!used.has(cursor)) {
        if (Number.isFinite(message.created_at) && Number.isFinite(cached[cursor].created_at)
          && cached[cursor].created_at > message.created_at) break;
        merged.push(cached[cursor]);
      }
      cursor++;
    }
    merged.push(message);
  });
  while (cursor < cached.length) {
    if (!used.has(cursor)) merged.push(cached[cursor]);
    cursor++;
  }
  return merged;
}
