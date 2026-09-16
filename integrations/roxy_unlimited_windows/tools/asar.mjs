// Minimal asar reader/extractor (no external deps)
import fs from 'node:fs';
import path from 'node:path';

function readHeader(file) {
  const fd = fs.openSync(file, 'r');
  const sz = Buffer.alloc(16);
  fs.readSync(fd, sz, 0, 16, 0);
  const headerSize = sz.readUInt32LE(4);        // pickle payload size
  const jsonSize = sz.readUInt32LE(12);         // json string byte length
  const jsonBuf = Buffer.alloc(jsonSize);
  fs.readSync(fd, jsonBuf, 0, jsonSize, 16);
  fs.closeSync(fd);
  const header = JSON.parse(jsonBuf.toString('utf8'));
  const baseOffset = 8 + headerSize;            // where file data starts
  return { header, baseOffset };
}

function walk(node, prefix, out) {
  for (const [name, val] of Object.entries(node.files || {})) {
    const p = prefix ? prefix + '/' + name : name;
    if (val.files) walk(val, p, out);
    else out.push({ path: p, size: val.size || 0, offset: val.offset ? parseInt(val.offset, 10) : 0, unpacked: !!val.unpacked });
  }
  return out;
}

const cmd = process.argv[2];
const asarPath = process.argv[3];
const { header, baseOffset } = readHeader(asarPath);
const entries = walk(header, '', []);
console.log(`# entries: ${entries.length}  baseOffset: ${baseOffset}  asar: ${fs.statSync(asarPath).size} bytes`);

if (cmd === 'list') {
  const filter = process.argv[4];
  for (const e of entries) {
    if (!filter || e.path.toLowerCase().includes(filter.toLowerCase())) {
      console.log(`${String(e.size).padStart(9)}  ${e.path}`);
    }
  }
} else if (cmd === 'extract') {
  const dest = process.argv[4];
  const fd = fs.openSync(asarPath, 'r');
  let n = 0;
  for (const e of entries) {
    const target = path.join(dest, e.path);
    if (e.unpacked) { fs.mkdirSync(path.dirname(target), { recursive: true }); continue; }
    fs.mkdirSync(path.dirname(target), { recursive: true });
    const buf = Buffer.alloc(e.size);
    fs.readSync(fd, buf, 0, e.size, baseOffset + e.offset);
    fs.writeFileSync(target, buf);
    n++;
  }
  fs.closeSync(fd);
  console.log(`# extracted ${n} files -> ${dest}`);
} else if (cmd === 'cat') {
  const want = process.argv[4];
  const e = entries.find(x => x.path === want) || entries.find(x => x.path.endsWith(want));
  if (!e) { console.error('not found: ' + want); process.exit(2); }
  const fd = fs.openSync(asarPath, 'r');
  const buf = Buffer.alloc(e.size);
  fs.readSync(fd, buf, 0, e.size, baseOffset + e.offset);
  fs.closeSync(fd);
  process.stdout.write(buf);
} else if (cmd === 'tree') {
  const depth = parseInt(process.argv[4] || '2', 10);
  const seen = new Set();
  for (const e of entries) {
    const parts = e.path.split('/');
    for (let i = 1; i <= Math.min(depth, parts.length); i++) {
      const k = parts.slice(0, i).join('/');
      if (!seen.has(k)) { seen.add(k); console.log('  '.repeat(i - 1) + parts[i - 1]); }
    }
  }
}
