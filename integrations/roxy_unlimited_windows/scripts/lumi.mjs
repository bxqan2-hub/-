// ============================================================
//  lumi.conf codec  —  RoxyBrowser fingerprint config
//
//  Reversed from dist/main.mjs (genFingerprintConfig/util.ts):
//    key = "402ead7d23b43b6d1e0528d4f99c59bd"   (32 bytes, AES-256)
//    iv  = "3a105229aa31"                        (12 bytes, GCM nonce)
//    file = base64( ciphertext || authTag(16) )
//
//  This is plain Node crypto, not the wasm module.
//
//  Usage:
//    node lumi.mjs dump   <lumi.conf|profileDir>
//    node lumi.mjs enc    <jsonFile> [outFile]
// ============================================================
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const KEY = Buffer.from('402ead7d23b43b6d1e0528d4f99c59bd', 'utf8');
const IV = Buffer.from('3a105229aa31', 'utf8');

export function decryptLumi(b64) {
  const raw = Buffer.from(b64.trim(), 'base64');
  const tag = raw.subarray(raw.length - 16);
  const ct = raw.subarray(0, raw.length - 16);
  const d = crypto.createDecipheriv('aes-256-gcm', KEY, IV);
  d.setAuthTag(tag);
  return Buffer.concat([d.update(ct), d.final()]).toString('utf8');
}

export function encryptLumi(text) {
  const c = crypto.createCipheriv('aes-256-gcm', KEY, IV);
  const body = Buffer.concat([c.update(text, 'utf8'), c.final()]);
  return Buffer.concat([body, c.getAuthTag()]).toString('base64');
}

const [cmd, arg, arg2] = process.argv.slice(2);

if (cmd === 'dump') {
  let file = arg;
  if (fs.statSync(file).isDirectory()) file = path.join(file, 'lumi.conf');
  const json = decryptLumi(fs.readFileSync(file, 'utf8'));
  fs.writeFileSync(file + '.decrypted.json', json);
  const obj = JSON.parse(json);
  console.log(`# decrypted ${file} (${json.length} bytes) -> ${file}.decrypted.json`);
  console.log(`# top-level keys: ${Object.keys(obj).length}`);
  console.log(JSON.stringify(obj, null, 2).slice(0, 4000));
} else if (cmd === 'enc') {
  const text = fs.readFileSync(arg, 'utf8');
  const out = encryptLumi(text);
  if (arg2) { fs.writeFileSync(arg2, out); console.log(`# wrote ${arg2} (${out.length} bytes)`); }
  else console.log(out);
} else {
  console.error('usage: node lumi.mjs dump <lumi.conf|profileDir> | enc <jsonFile> [outFile]');
  process.exit(2);
}
