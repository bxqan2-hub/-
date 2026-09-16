// Dump sourcesContent from a sourcemap into a directory tree
import fs from 'node:fs';
import path from 'node:path';

const mapPath = process.argv[2];
const outDir = process.argv[3];
const map = JSON.parse(fs.readFileSync(mapPath, 'utf8'));
console.log(`# sources: ${map.sources?.length}  hasContent: ${map.sourcesContent ? 'yes' : 'no'}`);
const rows = [];
(map.sources || []).forEach((src, i) => {
  const content = map.sourcesContent?.[i] ?? null;
  rows.push({ src, len: content ? content.length : 0 });
  if (!content) return;
  const clean = src
    .replace(/^[a-zA-Z]+:\/\/[^/]*\//, '')
    .replace(/^\.\//, '')
    .replace(/[?<>*|":]/g, '_')
    .replace(/\\/g, '/')
    .replace(/^\/+/, '')
    .replace(/^\.+/, '_');
  if (!clean || clean.endsWith('_') && clean.length < 3) return;
  const target = path.join(outDir, clean);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content);
});
rows.sort((a, b) => b.len - a.len);
for (const r of rows.slice(0, 60)) console.log(`${String(r.len).padStart(8)}  ${r.src}`);
