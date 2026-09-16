// ============================================================
//  RoxyBrowser - app.asar in-place byte patch (same-length only)
//
//  Why same-length: app.asar stores each file's absolute offset in its
//  JSON header. Growing/shrinking dist/main.mjs would invalidate every
//  later offset and corrupt the archive. Patching the same number of
//  bytes leaves the header, all offsets and the total file size intact,
//  so nothing has to be repacked and no integrity hash of the header moves.
//
//  Usage:
//    node patch_limit.mjs inspect                 # show what would change
//    node patch_limit.mjs apply                   # patch (makes a .bak first)
//    node patch_limit.mjs verify                  # confirm patched state
//    node patch_limit.mjs rollback                # restore from .bak
// ============================================================
import fs from 'node:fs';
import path from 'node:path';

const ASAR = path.resolve(
  process.argv[4] ?? 'resources/app.asar'
);
const BAK = ASAR + '.bak';
const TARGET = 'dist/main.mjs';
const MODE = process.argv[2] ?? 'inspect';

// ---------- patch table (old -> new, byte-length MUST match) ----------
const PATCHES = [
  {
    name: 'concurrency-cap',
    note: 'launchWithLimit() wrapped launch() in pLimit(5); this drops the throttle',
    old: 'async launchWithLimit(e,t){return Xg(()=>this.launch(e,t))}',
    new: 'async launchWithLimit(e,t){return this.launch(e,t)/*nlim*/}',
  },
];

// ---------- minimal asar reader ----------
function readAsar(file) {
  const fd = fs.openSync(file, 'r');
  const head = Buffer.alloc(16);
  fs.readSync(fd, head, 0, 16, 0);
  const headerSize = head.readUInt32LE(4);
  const jsonSize = head.readUInt32LE(12);
  const jsonBuf = Buffer.alloc(jsonSize);
  fs.readSync(fd, jsonBuf, 0, jsonSize, 16);
  fs.closeSync(fd);
  return { header: JSON.parse(jsonBuf.toString('utf8')), baseOffset: 8 + headerSize };
}

function findEntry(node, prefix, want) {
  for (const [name, val] of Object.entries(node.files || {})) {
    const p = prefix ? prefix + '/' + name : name;
    if (val.files) {
      const hit = findEntry(val, p, want);
      if (hit) return hit;
    } else if (p === want) return { path: p, size: val.size, offset: parseInt(val.offset, 10) };
  }
  return null;
}

// ---------- load the file bytes ----------
if (!fs.existsSync(ASAR)) {
  console.error(`[!] asar not found: ${ASAR}`);
  process.exit(2);
}
const { header, baseOffset } = readAsar(ASAR);
const entry = findEntry(header, '', TARGET);
if (!entry) {
  console.error(`[!] ${TARGET} not present in archive`);
  process.exit(2);
}

const fd = fs.openSync(ASAR, 'r');
const data = Buffer.alloc(entry.size);
fs.readSync(fd, data, 0, entry.size, baseOffset + entry.offset);
fs.closeSync(fd);

const text = data.toString('latin1');           // byte-exact round-trip
const asarSizeBefore = fs.statSync(ASAR).size;

// ---------- report ----------
console.log(`asar            : ${ASAR}`);
console.log(`asar size      : ${asarSizeBefore} bytes`);
console.log(`entry          : ${TARGET}  (${entry.size} bytes @ ${baseOffset + entry.offset})`);

let planned = 0;
for (const p of PATCHES) {
  const hits = [];
  let i = -1;
  while ((i = text.indexOf(p.old, i + 1)) !== -1) hits.push(i);
  const lenOk = p.old.length === p.new.length;
  console.log(`\n[patch] ${p.name}`);
  console.log(`  ${p.note}`);
  console.log(`  occurrences  : ${hits.length} ${hits.length === 1 ? '(ok)' : '(EXPECTED EXACTLY 1)'}`);
  console.log(`  length       : old=${p.old.length} new=${p.new.length} ${lenOk ? '(ok)' : '(MISMATCH)'}`);
  if (hits.length === 1 && lenOk) planned++;
}

if (MODE === 'inspect') {
  console.log(`\n=> ${planned}/${PATCHES.length} patch(es) applicable. Run: node patch_limit.mjs apply`);
  process.exit(planned === PATCHES.length ? 0 : 1);
}

if (MODE === 'verify') {
  let done = 0;
  for (const p of PATCHES) if (text.includes(p.new)) done++;
  console.log(`\n=> ${done}/${PATCHES.length} patch(es) present in archive.`);
  process.exit(done === PATCHES.length ? 0 : 1);
}

if (MODE === 'rollback') {
  if (!fs.existsSync(BAK)) { console.error(`[!] no backup at ${BAK}`); process.exit(2); }
  fs.copyFileSync(BAK, ASAR);
  console.log(`\n[ok] restored ${ASAR} from backup (${fs.statSync(ASAR).size} bytes)`);
  process.exit(0);
}

if (MODE === 'apply') {
  if (planned !== PATCHES.length) {
    console.error('\n[!] refusing to apply: a patch did not match exactly once, or length differs.');
    console.error('    The bundle may already be patched or the app version changed.');
    process.exit(2);
  }
  if (!fs.existsSync(BAK)) {
    fs.copyFileSync(ASAR, BAK);
    console.log(`\n[backup] ${BAK}`);
  } else {
    console.log(`\n[backup] existing backup kept: ${BAK}`);
  }

  const out = Buffer.from(text, 'latin1');
  for (const p of PATCHES) {
    const at = out.indexOf(Buffer.from(p.old, 'latin1'));
    Buffer.from(p.new, 'latin1').copy(out, at);
    console.log(`[write] ${p.name} @ file offset ${entry.offset + at} (${p.new.length} bytes)`);
  }
  if (out.length !== entry.size) {
    console.error('[!] length drift detected, aborting without writing');
    process.exit(2);
  }

  const fdw = fs.openSync(ASAR, 'r+');
  fs.writeSync(fdw, out, 0, out.length, baseOffset + entry.offset);
  fs.fsyncSync(fdw);
  fs.closeSync(fdw);

  const asarSizeAfter = fs.statSync(ASAR).size;
  console.log(`[ok] asar size before=${asarSizeBefore} after=${asarSizeAfter} ${asarSizeBefore === asarSizeAfter ? '(unchanged)' : '(CHANGED!)'}`);
  const recheck = readAsar(ASAR);
  console.log(`[ok] header re-parsed, entries=${Object.keys(recheck.header.files).length}, baseOffset=${recheck.baseOffset}`);
  process.exit(0);
}

console.error(`[!] unknown mode: ${MODE} (use inspect|apply|verify|rollback)`);
process.exit(2);
