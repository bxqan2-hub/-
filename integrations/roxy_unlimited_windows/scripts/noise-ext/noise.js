// ============================================================
//  Roxy Profile Noise  —  per-profile canvas / audio noise
//
//  Runs in the MAIN world at document_start so fingerprinting scripts
//  observe the patched prototypes.
//
//  Design rules:
//    * DETERMINISTIC per profile: reseeding from a constant each call
//      means repeated readbacks of the same canvas return the SAME value.
//      Per-read randomness would itself be a detection signal.
//    * Small perturbations (±1) — enough to change the hash, invisible
//      to the eye and to pixel-comparison heuristics.
//    * __SEED__ is substituted per profile by roxy-api.mjs.
// ============================================================
(() => {
  'use strict';
  const BASE_SEED = __SEED__ >>> 0;

  // Non-enumerable marker so tooling can confirm the shim actually ran.
  try {
    Object.defineProperty(window, '__roxyNoiseSeed', { value: BASE_SEED, enumerable: false, configurable: false });
  } catch {}

  // xorshift32 — deterministic, reseeded on every readback
  let s = BASE_SEED || 0x9e3779b9;
  const reseed = () => { s = BASE_SEED || 0x9e3779b9; };
  const next = () => { s ^= s << 13; s >>>= 0; s ^= s >>> 17; s ^= s << 5; s >>>= 0; return s / 4294967296; };

  const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
  const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
  const origToBlob = HTMLCanvasElement.prototype.toBlob;

  let hits = 0;

  // Perturb whole pixels, never clamp: pick the delta direction from the
  // current value so the write always lands. Fully transparent pixels get
  // alpha 0 -> 1, otherwise their RGB is not encoded and the hash would not move.
  const perturb = (data, stridePx) => {
    reseed();
    const pixels = (data.length / 4) | 0;
    for (let p = 0; p < pixels; p += stridePx) {
      if (next() >= 0.3) continue;
      const base = p * 4;
      if (data[base + 3] === 0) {
        data[base + 3] = 1;
        hits++;
        continue;
      }
      const ch = base + [0, 1, 2][(next() * 3) | 0];
      const v = data[ch];
      data[ch] = v >= 128 ? v - 1 : v + 1;
      hits++;
    }
    try { window.__roxyNoiseHits = hits; } catch {}
  };

  // ---- getImageData: perturb the returned buffer in place ----
  CanvasRenderingContext2D.prototype.getImageData = function (...args) {
    const img = origGetImageData.apply(this, args);
    try { perturb(img.data, 13); } catch {}
    return img;
  };

  if (typeof OffscreenCanvasRenderingContext2D !== 'undefined') {
    const og = OffscreenCanvasRenderingContext2D.prototype.getImageData;
    OffscreenCanvasRenderingContext2D.prototype.getImageData = function (...args) {
      const img = og.apply(this, args);
      try { perturb(img.data, 13); } catch {}
      return img;
    };
  }

  // ---- toDataURL / toBlob: rasterise through a noisy copy ----
  const noisyCopy = (canvas) => {
    try {
      const c = document.createElement('canvas');
      c.width = canvas.width; c.height = canvas.height;
      const g = c.getContext('2d');
      g.drawImage(canvas, 0, 0);
      const img = origGetImageData.call(g, 0, 0, c.width, c.height);
      perturb(img.data, 13);
      g.putImageData(img, 0, 0);
      return c;
    } catch { return canvas; }
  };

  HTMLCanvasElement.prototype.toDataURL = function (...args) {
    if (!this.width || !this.height) return origToDataURL.apply(this, args);
    return origToDataURL.apply(noisyCopy(this), args);
  };

  HTMLCanvasElement.prototype.toBlob = function (cb, ...rest) {
    if (typeof cb !== 'function') return origToBlob.apply(this, arguments);
    return origToBlob.call(noisyCopy(this), cb, ...rest);
  };

  // ---- audio: perturb a deterministic sample set ----
  try {
    const ogcd = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function (ch) {
      const buf = ogcd.call(this, ch);
      try {
        reseed();
        const step = Math.max(1, Math.floor(buf.length / 4096));
        for (let i = 0; i < buf.length; i += step) {
          if (next() < 0.2) buf[i] = buf[i] + (next() - 0.5) * 1e-7;
        }
      } catch {}
      return buf;
    };
  } catch {}

  try {
    const ocpa = AnalyserNode.prototype.getFloatFrequencyData;
    AnalyserNode.prototype.getFloatFrequencyData = function (arr) {
      ocpa.call(this, arr);
      try {
        reseed();
        for (let i = 0; i < arr.length; i += 37) {
          if (next() < 0.25) arr[i] = arr[i] + (next() - 0.5) * 1e-4;
        }
      } catch {}
      return arr;
    };
  } catch {}
})();
