import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendDir = fileURLToPath(new URL('../', import.meta.url));
const assetsDir = join(frontendDir, 'dist', 'assets');
const chunks = readdirSync(assetsDir)
  .filter((name) => name.endsWith('.js'))
  .map((name) => ({ name, bytes: statSync(join(assetsDir, name)).size }));

const oversized = chunks.filter(({ bytes }) => bytes > 500_000);
if (oversized.length > 0) {
  throw new Error(`JavaScript chunks exceed Vite's 500 kB warning limit: ${oversized.map(({ name, bytes }) => `${name} (${bytes} bytes)`).join(', ')}`);
}

const chartChunk = chunks.find(({ name }) => name.startsWith('ChartView-'));
if (!chartChunk) {
  throw new Error('ChartView must be a separate, on-demand chunk.');
}

const html = readFileSync(join(frontendDir, 'dist', 'index.html'), 'utf8');
if (html.includes(chartChunk.name)) {
  throw new Error('ChartView must not be preloaded by the initial HTML.');
}

console.log(`Bundle check passed: ${chunks.length} JavaScript chunks, largest ${Math.max(...chunks.map(({ bytes }) => bytes))} bytes; ChartView loads on demand.`);
