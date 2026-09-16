// Print byte-accurate context around anchors in the minified bundle
import fs from 'node:fs';
const file = process.argv[2];
const src = fs.readFileSync(file, 'utf8');
const anchors = process.argv.slice(3);
for (const a of anchors) {
  console.log(`\n===== anchor: ${JSON.stringify(a)} =====`);
  let i = -1, n = 0;
  while ((i = src.indexOf(a, i + 1)) !== -1 && n < 6) {
    n++;
    const start = Math.max(0, i - 120), end = Math.min(src.length, i + a.length + 160);
    console.log(`-- offset ${i} --`);
    console.log(src.slice(start, end));
  }
  if (!n) console.log('(no match)');
}
