#!/usr/bin/env node
"use strict";

// 关键：必须在 require 任何其他模块、且任何 Date 构造之前设置时区。
// V8 在首次访问时区时会缓存当前 process.env.TZ，之后改不会生效。
// 通过 SENTINEL_TZ 环境变量传入（Python 端按代理出口国家映射，例如 JP → Asia/Tokyo），
// 让 vm 内 sdk 算出的 p 字段时区与代理出口 IP 一致，避免触发"时区/IP 不匹配"风控。
if (process.env.SENTINEL_TZ) {
  process.env.TZ = process.env.SENTINEL_TZ;
}

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");
const { performance } = require("node:perf_hooks");

function readArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    const item = argv[i];
    if (!item.startsWith("--")) continue;
    const key = item.slice(2);
    const next = argv[i + 1];
    if (!next || next.startsWith("--")) {
      args[key] = "1";
      continue;
    }
    args[key] = next;
    i++;
  }
  return args;
}

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

function parseJson(text, source) {
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(`${source} 不是合法 JSON：${error.message}`);
  }
}

function pick(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return "";
}

function truthy(value) {
  return value === true || value === "1" || value === "true" || value === "yes";
}

function readConfig(args) {
  const explicitPath = args.config || process.env.SENTINEL_CONFIG;
  const candidates = explicitPath
    ? [path.resolve(explicitPath)]
    : [
        path.resolve(process.cwd(), "sentinel.config.json"),
        path.resolve(process.cwd(), "tools", "sentinel.config.json"),
        path.resolve(__dirname, "sentinel.config.json"),
        path.resolve(__dirname, "..", "sentinel.config.json"),
      ];

  for (const filePath of candidates) {
    if (!fs.existsSync(filePath)) continue;
    return {
      path: filePath,
      data: parseJson(fs.readFileSync(filePath, "utf8"), filePath),
    };
  }

  return { path: null, data: {} };
}

function configGetter(config) {
  return (...keys) => {
    for (const key of keys) {
      if (config[key] !== undefined && config[key] !== null && config[key] !== "") {
        return config[key];
      }
    }
    return "";
  };
}

function simulateUserInteractionsJs() {
  // 在 vm 内伪造一系列真实用户交互事件，喂给 sdk 注册的 listener。
  // 用 Proxy 包 event 对象，记录 sdk 在 listener 里实际访问了哪些字段。
  return `
    (function(){
      var listeners = window.__listeners || {};
      function wrapEvent(obj) {
        return new Proxy(obj, {
          get: function(t, k) {
            if (typeof k === 'string' && !k.startsWith('__') && k !== 'constructor') {
              tally('event.read.' + (obj.type || '?'), k);
            }
            return t[k];
          }
        });
      }
      function dispatch(type, ev) {
        var arr = listeners[type] || [];
        var wrapped = wrapEvent(ev);
        for (var i = 0; i < arr.length; i++) {
          try { arr[i](wrapped); } catch(e) {
            tally('event.handler.err.' + type, e.message.slice(0, 40));
          }
        }
      }
      // 用一个累积的虚拟时间戳，比每次都 performance.now() 更可控
      var t0 = performance.now();
      var virtualTs = t0;
      function makeEvent(type, dt, extra) {
        virtualTs += dt;  // 时间累加
        var e = Object.assign({
          type: type,
          target: window,
          currentTarget: window,
          isTrusted: true,
          timeStamp: virtualTs,
          defaultPrevented: false,
          bubbles: true,
          cancelable: true,
          composed: true,
          eventPhase: 2,
          preventDefault: function(){},
          stopPropagation: function(){},
          stopImmediatePropagation: function(){},
        }, extra || {});
        return e;
      }

      // 计划：先一段鼠标移动 → 一次 click → keydown 输入 → 偶尔 scroll → wheel
      // 事件之间间隔 5~30 ms，模拟人类操作速度
      function rand(min, max) { return min + Math.random() * (max - min); }
      function randint(min, max) { return Math.floor(rand(min, max + 1)); }

      // 起点
      var x = randint(200, 800);
      var y = randint(150, 600);
      var lastX = x, lastY = y;

      // 1) 模拟 ~120 次鼠标轨迹（带速度变化）
      for (var i = 0; i < 120; i++) {
        // 鼠标的"自然"漂移 + 偶尔大跳
        var dx = (Math.random() - 0.5) * (i % 10 === 0 ? 40 : 12);
        var dy = (Math.random() - 0.5) * (i % 10 === 0 ? 40 : 12);
        x = Math.max(0, Math.min(1920, x + dx));
        y = Math.max(0, Math.min(1080, y + dy));
        var mx = x - lastX, my = y - lastY;
        lastX = x; lastY = y;
        var dt = rand(8, 22);
        dispatch('pointermove', makeEvent('pointermove', dt, {
          clientX: Math.round(x), clientY: Math.round(y),
          screenX: Math.round(x), screenY: Math.round(y),
          pageX: Math.round(x), pageY: Math.round(y),
          movementX: Math.round(mx), movementY: Math.round(my),
          pointerId: 1, pointerType: 'mouse', isPrimary: true,
          buttons: 0, button: -1, pressure: 0, tangentialPressure: 0,
          tiltX: 0, tiltY: 0, twist: 0, width: 1, height: 1,
        }));
      }

      // 2) 一次 click（鼠标停下后 ~100ms）
      virtualTs += 100;
      dispatch('click', makeEvent('click', 0, {
        clientX: Math.round(x), clientY: Math.round(y),
        screenX: Math.round(x), screenY: Math.round(y),
        pageX: Math.round(x), pageY: Math.round(y),
        button: 0, buttons: 1, detail: 1,
      }));

      // 3) keydown：模拟输入 "test@example.com" 的一部分（人类打字速度 80~200ms/字符）
      var typed = 'abcdef';
      for (var i = 0; i < typed.length; i++) {
        var c = typed.charCodeAt(i);
        dispatch('keydown', makeEvent('keydown', rand(80, 220), {
          key: typed[i],
          code: 'Key' + typed[i].toUpperCase(),
          keyCode: c & 0x5f, // 大写
          charCode: 0, which: c & 0x5f,
          ctrlKey: false, shiftKey: false, altKey: false, metaKey: false,
          repeat: false, isComposing: false,
        }));
      }

      // 4) 中间再插一段鼠标（人类边打字边动鼠标）
      for (var i = 0; i < 20; i++) {
        x = Math.max(0, Math.min(1920, x + (Math.random() - 0.5) * 8));
        y = Math.max(0, Math.min(1080, y + (Math.random() - 0.5) * 8));
        dispatch('pointermove', makeEvent('pointermove', rand(15, 40), {
          clientX: Math.round(x), clientY: Math.round(y),
          screenX: Math.round(x), screenY: Math.round(y),
          pageX: Math.round(x), pageY: Math.round(y),
          movementX: 1, movementY: 1,
          pointerId: 1, pointerType: 'mouse', isPrimary: true,
          buttons: 0, button: -1, pressure: 0, tangentialPressure: 0,
          tiltX: 0, tiltY: 0, twist: 0, width: 1, height: 1,
        }));
      }

      // 5) 一次 scroll
      dispatch('scroll', makeEvent('scroll', rand(30, 80), {}));

      // 6) wheel
      dispatch('wheel', makeEvent('wheel', rand(30, 80), {
        deltaX: 0, deltaY: rand(80, 240), deltaZ: 0, deltaMode: 0,
        clientX: Math.round(x), clientY: Math.round(y),
      }));

      // 7) 最后再一段 pointermove + 一次 click（点提交按钮）
      for (var i = 0; i < 30; i++) {
        x = Math.max(0, Math.min(1920, x + (Math.random() - 0.5) * 15));
        y = Math.max(0, Math.min(1080, y + (Math.random() - 0.5) * 15));
        dispatch('pointermove', makeEvent('pointermove', rand(10, 25), {
          clientX: Math.round(x), clientY: Math.round(y),
          screenX: Math.round(x), screenY: Math.round(y),
          pageX: Math.round(x), pageY: Math.round(y),
          movementX: 1, movementY: 1,
          pointerId: 1, pointerType: 'mouse', isPrimary: true,
          buttons: 0, button: -1, pressure: 0, tangentialPressure: 0,
          tiltX: 0, tiltY: 0, twist: 0, width: 1, height: 1,
        }));
      }
      virtualTs += rand(50, 150);
      dispatch('click', makeEvent('click', 0, {
        clientX: Math.round(x), clientY: Math.round(y),
        screenX: Math.round(x), screenY: Math.round(y),
        pageX: Math.round(x), pageY: Math.round(y),
        button: 0, buttons: 1, detail: 1,
      }));
    })();
  `;
}

function normalizeList(value, fallback) {
  const source = Array.isArray(value) ? value.join(",") : pick(value, fallback);
  return String(source)
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function xorDecode(text, key) {
  let output = "";
  const decoded = atobBinary(text);
  for (let i = 0; i < decoded.length; i++) {
    output += String.fromCharCode(decoded.charCodeAt(i) ^ key.charCodeAt(i % key.length));
  }
  return output;
}

function decodeDx(dx, proof) {
  return JSON.parse(xorDecode(dx, proof));
}

function normalizeChallenge(raw) {
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return trimmed;
    raw = parseJson(trimmed, "challenge 字符串");
  }

  const candidates = [
    raw?.cachedChatReq,
    raw?.result?.cachedChatReq,
    raw?.data?.cachedChatReq,
    raw?.data,
    raw,
  ];

  for (const candidate of candidates) {
    if (!candidate || typeof candidate !== "object") continue;
    if (candidate.proofofwork || candidate.token || candidate.turnstile || candidate.so) {
      return candidate;
    }
  }

  throw new Error("challenge 缺少 cachedChatReq/proofofwork/token 字段，无法喂给 SDK");
}

function readChallengeFile(filePath) {
  const absolutePath = path.resolve(filePath);
  const raw = fs.readFileSync(absolutePath, "utf8");
  return normalizeChallenge(parseJson(raw, absolutePath));
}

const OFFICIAL_CHALLENGE_URL = "https://chatgpt.com/backend-api/sentinel/req";

function headerMapFromEnv(options = {}) {
  const headers = {
    accept: "*/*",
    "content-type":
      options.contentType ||
      (options.ignoreEnv ? "" : process.env.SENTINEL_CONTENT_TYPE) ||
      "text/plain;charset=UTF-8",
  };
  const cookie =
    options.cookie ||
    (options.ignoreEnv ? "" : process.env.SENTINEL_COOKIE || process.env.CHATGPT_COOKIE);
  const authorization =
    options.bearer ||
    (options.ignoreEnv ? "" : process.env.SENTINEL_AUTHORIZATION || process.env.CHATGPT_BEARER_TOKEN);
  const userAgent = options.userAgent || (options.ignoreEnv ? "" : process.env.SENTINEL_USER_AGENT);

  if (cookie) headers.cookie = cookie;
  if (authorization) {
    headers.authorization = authorization.toLowerCase().startsWith("bearer ")
      ? authorization
      : `Bearer ${authorization}`;
  }
  if (userAgent) {
    headers["user-agent"] = userAgent;
  }
  if (options.pageUrl) headers.referer = options.pageUrl;
  if (options.origin) headers.origin = options.origin;
  if (options.deviceId) headers["oai-device-id"] = options.deviceId;
  if (process.env.SENTINEL_HEADERS_JSON) {
    Object.assign(headers, parseJson(process.env.SENTINEL_HEADERS_JSON, "SENTINEL_HEADERS_JSON"));
  }
  return headers;
}

function assertAllowedChallengeHost(challengeUrl, officialMode) {
  const host = new URL(challengeUrl).hostname.toLowerCase();
  const allowed = (process.env.SENTINEL_ALLOW_HOST || "")
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);

  if ((host === "chatgpt.com" || host.endsWith(".chatgpt.com")) && !officialMode && !allowed.includes(host)) {
    throw new Error(
      "为避免误打真实生产接口，默认不请求 chatgpt.com。若这是比赛授权接口，请使用 --official 或设置 SENTINEL_ALLOW_HOST=chatgpt.com。"
    );
  }
}

async function fetchChallenge(challengeUrl, flow, proof, deviceId, options = {}) {
  assertAllowedChallengeHost(challengeUrl, options.officialMode);
  const hasCookie = Boolean(
    options.cookie || (options.ignoreEnv ? "" : process.env.SENTINEL_COOKIE || process.env.CHATGPT_COOKIE)
  );
  const hasBearer = Boolean(
    options.bearer ||
      (options.ignoreEnv ? "" : process.env.SENTINEL_AUTHORIZATION || process.env.CHATGPT_BEARER_TOKEN)
  );
  if (options.officialMode && !hasCookie && !hasBearer) {
    throw new Error("官方接口模式至少需要 Cookie 或 Bearer；请传 --cookie 或 --bearer。");
  }
  const body = JSON.stringify({ p: proof, id: deviceId, flow });
  const response = await fetch(challengeUrl, {
    method: "POST",
    headers: headerMapFromEnv({
      pageUrl: options.pageUrl,
      origin: new URL(challengeUrl).origin,
      userAgent: options.userAgent,
      deviceId,
      cookie: options.cookie,
      bearer: options.bearer,
      contentType: options.contentType,
      ignoreEnv: options.ignoreEnv,
    }),
    body,
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`challenge API 返回 HTTP ${response.status}：${text.slice(0, 300)}`);
  }
  return normalizeChallenge(text);
}

function createEventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, listener) {
      const bucket = listeners.get(type) || [];
      bucket.push(listener);
      listeners.set(type, bucket);
    },
    removeEventListener(type, listener) {
      const bucket = listeners.get(type) || [];
      listeners.set(
        type,
        bucket.filter((item) => item !== listener)
      );
    },
    dispatchEvent(event) {
      const bucket = listeners.get(event.type) || [];
      for (const listener of [...bucket]) listener.call(this, event);
    },
  };
}

function btoaBinary(value) {
  return Buffer.from(String(value), "binary").toString("base64");
}

function atobBinary(value) {
  return Buffer.from(String(value), "base64").toString("binary");
}

function createStorage() {
  const data = new Map();
  return {
    get length() { return data.size; },
    getItem(key) {
      const v = data.has(key) ? data.get(key) : null;
      if (process.env.SENTINEL_PROBE_JSON === "1") {
        process.stderr.write(`[probe.storage.get] ${key} = ${v ? String(v).slice(0,80) : 'null'}\n`);
      }
      return v;
    },
    setItem(key, value) {
      data.set(key, String(value));
      if (process.env.SENTINEL_PROBE_JSON === "1") {
        process.stderr.write(`[probe.storage.set] ${key} = ${String(value).slice(0,80)}\n`);
      }
    },
    removeItem(key) { data.delete(key); },
    clear() { data.clear(); },
    key(i) { return [...data.keys()][i] ?? null; },
  };
}

function createDomRect(width = 0, height = 0) {
  return {
    x: 0,
    y: 0,
    width,
    height,
    top: 0,
    left: 0,
    right: width,
    bottom: height,
    toJSON() {
      return {
        x: this.x,
        y: this.y,
        width: this.width,
        height: this.height,
        top: this.top,
        left: this.left,
        right: this.right,
        bottom: this.bottom,
      };
    },
  };
}

function createBrowserContext(options) {
  const windowTarget = createEventTarget();

  const managedTimers = new Set();
  const managedSetTimeout = (callback, delay, ...args) => {
    const id = setTimeout(() => {
      managedTimers.delete(id);
      callback(...args);
    }, delay);
    managedTimers.add(id);
    return id;
  };
  const managedClearTimeout = (id) => {
    managedTimers.delete(id);
    clearTimeout(id);
  };
  const browserPerformance = {
    now: () => performance.now(),
    timeOrigin: performance.timeOrigin || Date.now() - performance.now(),
    memory: {
      jsHeapSizeLimit: options.jsHeapSizeLimit,
    },
  };
  const mathObject = Object.create(Math);
  if (Number.isFinite(options.fixedRandom)) {
    mathObject.random = () => options.fixedRandom;
  }
  const currentScript = { src: options.scriptSrc, length: options.scriptSrc.length };
  const scripts = [
    currentScript,
    { src: "https://js.stripe.com/v3/", length: 24 },
    { src: "https://chatgpt.com/c/prod-4987068829830ddc3ae6683bd4e633f61b79dec9/_ssg.js", length: 82 },
  ];
  const attrs = new Map([["data-build", options.buildId]]);

  let iframeNode = null;
  const bodyChildren = [];
  const cookieValue = options.cookie || "";
  const document = {
    currentScript,
    scripts,
    get cookie() {
      if (process.env.SENTINEL_PROBE_JSON === "1") {
        process.stderr.write("[probe.cookie.get] " + String(cookieValue).slice(0, 100) + "\n");
      }
      return cookieValue;
    },
    set cookie(v) { /* 我们 vm 里不实际持久化 cookie */ },
    documentElement: {
      getAttribute(name) {
        return attrs.get(name) ?? null;
      },
      setAttribute(name, value) {
        attrs.set(name, String(value));
      },
    },
    body: {
      style: {},
      getBoundingClientRect() {
        return createDomRect(options.screen.width, options.screen.height);
      },
      appendChild(node) {
        bodyChildren.push(node);
        node.parentNode = document.body;
        if (node?.tagName === "IFRAME") iframeNode = node;
        managedSetTimeout(() => node?._emitLoad?.(), 0);
        return node;
      },
      removeChild(node) {
        const index = bodyChildren.indexOf(node);
        if (index >= 0) bodyChildren.splice(index, 1);
        if (iframeNode === node) iframeNode = null;
        if (node) node.parentNode = null;
        return node;
      },
    },
    createElement(tagName) {
      if (String(tagName).toLowerCase() !== "iframe") {
        const children = [];
        const element = {
          tagName: String(tagName).toUpperCase(),
          style: {},
          parentNode: null,
          children,
          appendChild(node) {
            children.push(node);
            node.parentNode = element;
            return node;
          },
          removeChild(node) {
            const index = children.indexOf(node);
            if (index >= 0) children.splice(index, 1);
            if (node) node.parentNode = null;
            return node;
          },
          addEventListener() {},
          removeEventListener() {},
          getBoundingClientRect() {
            return createDomRect();
          },
        };
        return element;
      }

      const target = createEventTarget();
      const iframe = {
        tagName: "IFRAME",
        style: {},
        src: "",
        getBoundingClientRect() {
          return createDomRect();
        },
        contentWindow: {
          postMessage(message, origin) {
            Promise.resolve()
              .then(async () => {
                const result = await options.handleIframeMessage(message);
                windowTarget.dispatchEvent({
                  type: "message",
                  source: iframe.contentWindow,
                  origin,
                  data: {
                    type: "response",
                    requestId: message.requestId,
                    result,
                  },
                });
              })
              .catch((error) => {
                windowTarget.dispatchEvent({
                  type: "message",
                  source: iframe.contentWindow,
                  origin,
                  data: {
                    type: "response",
                    requestId: message.requestId,
                    error: error?.message || String(error),
                  },
                });
              });
          },
        },
        addEventListener: target.addEventListener,
        removeEventListener: target.removeEventListener,
        _emitLoad() {
          target.dispatchEvent.call(iframe, { type: "load", target: iframe });
        },
      };
      return iframe;
    },
  };

  const location = new URL(options.pageUrl);
  const navigator = {
    userAgent: options.userAgent,
    language: options.language,
    languages: options.languages,
    hardwareConcurrency: options.hardwareConcurrency,
    bluetooth: { toString: () => "[object Bluetooth]" },
  };
  const localStorage = createStorage();
  const sessionStorage = createStorage();
  const history = {
    length: 1,
    state: null,
    back() {},
    forward() {},
    go() {},
    pushState(state) {
      this.state = state ?? null;
    },
    replaceState(state) {
      this.state = state ?? null;
    },
  };

  const window = Object.assign(windowTarget, {
    window: null,
    self: null,
    top: null,
    parent: null,
    document,
    navigator,
    screen: options.screen,
    location,
    localStorage,
    sessionStorage,
    history,
    performance: browserPerformance,
    crypto: crypto.webcrypto,
    TextEncoder,
    TextDecoder,
    URL,
    URLSearchParams,
    AbortController,
    setTimeout: managedSetTimeout,
    clearTimeout: managedClearTimeout,
    btoa: btoaBinary,
    atob: atobBinary,
    fetch,
    console,
    Math: mathObject,
    Date,
    JSON,
    Array,
    Object,
    Reflect,
    Number,
    String,
    Promise,
    RegExp,
    Error,
    Map,
    Set,
    WeakMap,
    Uint8Array,
    encodeURIComponent,
    decodeURIComponent,
    unescape,
    requestIdleCallback(callback) {
      return managedSetTimeout(() => callback({ timeRemaining: () => 5, didTimeout: false }), 0);
    },
    cancelIdleCallback(id) {
      managedClearTimeout(id);
    },
    __privateStripeFrame8094: {},
    onpageswap: null,
  });

  window.window = window;
  window.self = window;
  window.top = window;
  window.parent = window;

  return {
    iframeNode: () => iframeNode,
    context: vm.createContext({
      window,
      self: window,
      globalThis: window,
      document,
      navigator,
      screen: options.screen,
      location,
      localStorage,
      sessionStorage,
      history,
      performance: browserPerformance,
      crypto: crypto.webcrypto,
      TextEncoder,
      TextDecoder,
      URL,
      URLSearchParams,
      AbortController,
      setTimeout: managedSetTimeout,
      clearTimeout: managedClearTimeout,
      btoa: btoaBinary,
      atob: atobBinary,
      fetch,
      console,
      Math: mathObject,
      Date,
      JSON,
      Array,
      Object,
      Reflect,
      Number,
      String,
      Promise,
      RegExp,
      Error,
      Map,
      Set,
      WeakMap,
      Uint8Array,
      encodeURIComponent,
      decodeURIComponent,
      unescape,
      requestIdleCallback: window.requestIdleCallback,
      cancelIdleCallback: window.cancelIdleCallback,
      __privateStripeFrame8094: window.__privateStripeFrame8094,
      onpageswap: window.onpageswap,
    }),
    clearTimers() {
      for (const id of [...managedTimers]) managedClearTimeout(id);
    },
  };
}

function precompileSdk(sdkPath) {
  const code = fs.readFileSync(sdkPath, "utf8");
  // debugDx 替换不适用于预编译场景；生产环境通常不会开 debugDx。
  // 如果开了 debugDx，main() 会回退到动态编译。
  return new vm.Script(code, { filename: sdkPath });
}

async function main(argv = process.argv.slice(2), writeOutput = true, inMemoryChallenge = null, compiledSdk = null) {
  const args = readArgs(argv);
  if (args.help === "1" || args.h === "1") {
    const helpText = [
      "用法：",
      "  node sentinel-runner.js --cookie \"你的 Cookie\"",
      "  node sentinel-runner.js --bearer \"Bearer 你的 token\"",
      "  node sentinel-runner.js --cookie \"你的 Cookie\" --bearer \"Bearer 你的 token\"",
      "  node sentinel-runner.js --config sentinel.config.json",
      "",
      "默认会读取当前目录、tools 目录或项目根目录的 sentinel.config.json。",
      "",
      "常用参数：",
      "  --flow checkout_session_approval",
      "  --page-url https://chatgpt.com/checkout/openai_llc/cs_xxx",
      "  --device-id 你的_oai-did",
      "  --challenge-url 自定义题目 challenge API",
      "  --sdk 指定 sdk.js 路径",
      "  --no-cookie 生成 token 时不向 challenge API 发送 Cookie",
    ].join("\n");
    if (writeOutput) process.stdout.write(`${helpText}\n`);
    return helpText;
  }

  const { path: configPath, data: config } = readConfig(args);
  const ignoreEnvForCredentials = Boolean(configPath);
  const cfg = configGetter(config);
  const defaultSdkPath = fs.existsSync(path.resolve(__dirname, "sdk.js"))
    ? path.resolve(__dirname, "sdk.js")
    : path.resolve(__dirname, "..", "sdk.js");
  const sdkPath = path.resolve(pick(args["sdk"], cfg("sdk", "sdkPath"), process.env.SENTINEL_SDK_PATH, defaultSdkPath));
  const flow = pick(args.flow, cfg("flow"), process.env.SENTINEL_FLOW, "checkout_session_approval");
  const challengeFile = pick(args["challenge-file"], cfg("challengeFile", "challenge_file"), process.env.SENTINEL_CHALLENGE_FILE);
  const officialMode =
    args.official === "1" ||
    truthy(cfg("official")) ||
    process.env.SENTINEL_OFFICIAL === "1" ||
    (!challengeFile && !args["challenge-url"] && !cfg("challengeUrl", "challenge_url") && !process.env.SENTINEL_CHALLENGE_URL);
  const challengeUrl =
    pick(args["challenge-url"], cfg("challengeUrl", "challenge_url"), process.env.SENTINEL_CHALLENGE_URL) ||
    (officialMode ? OFFICIAL_CHALLENGE_URL : "");
  const noCookie = args["no-cookie"] === "1" || truthy(cfg("noCookie", "no_cookie"));
  const cookieArg = noCookie ? "" : pick(args.cookie, args.cookies, cfg("cookie", "cookies"));
  const bearerArg = pick(args.bearer, args.authorization, cfg("bearer", "bearerToken", "authorization", "accessToken"));
  const contentType = pick(args["content-type"], cfg("contentType", "content_type"));
  const debugDx = args["debug-dx"] === "1" || truthy(cfg("debugDx", "debug_dx"));
  const debugDxLimit = Number(pick(args["debug-dx-limit"], cfg("debugDxLimit", "debug_dx_limit"), 80));
  const deviceId =
    pick(args["device-id"], cfg("deviceId", "device_id", "oaiDid", "oai_did"), process.env.SENTINEL_OAI_DID) ||
    "8a5ad769-e9e7-4461-ae3a-6755d7f46b0b";

  if (!fs.existsSync(sdkPath)) throw new Error(`找不到 SDK 文件：${sdkPath}`);
  if (!inMemoryChallenge && !challengeFile && !challengeUrl) {
    throw new Error("请提供 --challenge-file、--challenge-url 或 --official，用于把题目服务器 challenge 喂回 SDK。");
  }

  let cachedChallenge = null;
  const options = {
    flow,
    pageUrl: pick(args["page-url"], cfg("pageUrl", "page_url"), process.env.SENTINEL_PAGE_URL, "https://chatgpt.com/checkout/openai_llc/cs_ctf"),
    scriptSrc:
      pick(
        args["script-src"],
        cfg("scriptSrc", "script_src"),
        process.env.SENTINEL_SCRIPT_SRC,
      "https://sentinel.openai.com/backend-api/sentinel/sdk.js",
      ),
    buildId: pick(args["build-id"], cfg("buildId", "build_id"), process.env.SENTINEL_BUILD_ID, "prod-4987068829830ddc3ae6683bd4e633f61b79dec9"),
    cookie: noCookie
      ? `oai-did=${deviceId}`
      : cookieArg ||
        (ignoreEnvForCredentials ? "" : process.env.SENTINEL_COOKIE || process.env.CHATGPT_COOKIE) ||
        `oai-did=${deviceId}`,
    userAgent:
      pick(
        args["user-agent"],
        cfg("userAgent", "user_agent"),
        process.env.SENTINEL_USER_AGENT,
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
      ),
    contentType,
    language: pick(args.language, cfg("language"), process.env.SENTINEL_LANGUAGE, "en-US"),
    languages: normalizeList(pick(args.languages, cfg("languages")), process.env.SENTINEL_LANGUAGES || "en-US,en"),
    hardwareConcurrency: Number(pick(args.cores, cfg("cores", "hardwareConcurrency"), process.env.SENTINEL_CORES, 8)),
    jsHeapSizeLimit: Number(pick(args["js-heap-size-limit"], cfg("jsHeapSizeLimit", "js_heap_size_limit"), process.env.SENTINEL_JS_HEAP_SIZE_LIMIT, 4294967296)),
    fixedRandom:
      pick(args.random, cfg("random", "fixedRandom"), process.env.SENTINEL_FIXED_RANDOM)
        ? Number(pick(args.random, cfg("random", "fixedRandom"), process.env.SENTINEL_FIXED_RANDOM))
        : Number.NaN,
    screen: {
      width: Number(pick(args.width, cfg("width", "screenWidth"), process.env.SENTINEL_SCREEN_WIDTH, 1920)),
      height: Number(pick(args.height, cfg("height", "screenHeight"), process.env.SENTINEL_SCREEN_HEIGHT, 1080)),
    },
    async handleIframeMessage(message) {
      if (message.type !== "token" && message.type !== "init") {
        throw new Error(`未知 iframe 消息类型：${message.type}`);
      }
      const proof = message.p;
      if (inMemoryChallenge) {
        // 常驻服务路径：challenge 以对象直接传入，经 normalizeChallenge
        // 保持与 readChallengeFile 完全一致的字段提取逻辑。
        cachedChallenge ||= normalizeChallenge(inMemoryChallenge);
      } else if (challengeFile) {
        cachedChallenge ||= readChallengeFile(challengeFile);
      } else {
        cachedChallenge = await fetchChallenge(challengeUrl, flow, proof, deviceId, {
          officialMode,
          pageUrl: options.pageUrl,
          userAgent: options.userAgent,
          cookie: noCookie ? "" : cookieArg,
          bearer: bearerArg,
          contentType: options.contentType,
          ignoreEnv: ignoreEnvForCredentials,
        });
      }
      if (debugDx && cachedChallenge?.turnstile?.dx) {
        try {
          const decoded = decodeDx(cachedChallenge.turnstile.dx, proof);
          const limit = Number.isFinite(debugDxLimit) && debugDxLimit > 0 ? debugDxLimit : 80;
          process.stderr.write(`dx 前 ${limit} 条指令：${JSON.stringify(decoded.slice(0, limit))}\n`);
        } catch (error) {
          process.stderr.write(`dx 解码失败：${error.message}\n`);
        }
      }
      // 关键：sdk 解码 so 的 collector_dx 用的 key 是 cachedProof。
      // 如果 challenge 文件里带有 _python_proof（Python 端发 sentinel/req 时实际用的 p），
      // 必须把它原样回传给 sdk，否则 sdk 会用 vm 内重新算的不同 p 当 key，导致解码失败。
      const effectiveProof = cachedChallenge?._python_proof || proof;
      return {
        cachedProof: effectiveProof,
        cachedChatReq: cachedChallenge,
      };
    },
  };

  const { context, clearTimers } = createBrowserContext(options);

  // Pre-compiled SDK（sentinel-server.js 在启动时编译一次）复用 vm.Script，
  // 省掉每次请求的 fs.readFileSync + V8 编译开销（~3-5ms/次）。
  // debugDx 模式下不能复用预编译脚本（需要字符串替换），回退动态编译。
  let sdkScript;
  if (compiledSdk && !debugDx) {
    sdkScript = compiledSdk;
  } else {
    let sdkCode = fs.readFileSync(sdkPath, "utf8");
    if (debugDx) {
      sdkCode = sdkCode.replace(
        "Cn.set(n,Cn.get(e)[Cn.get(r)].bind(Cn[t(24)](e)))",
        "(()=>{const __o=Cn.get(e),__p=Cn.get(r);if(!__o||!__o[__p])console.error('[dx bind missing]',typeof __o,__p,Object.prototype.toString.call(__o));return Cn.set(n,__o[__p].bind(__o))})()"
      );
    }
    sdkScript = new vm.Script(sdkCode, { filename: sdkPath });
  }
  // 在 sdk 前注入 JSON.parse 探针，覆盖 vm 真正的全局 JSON
  if (process.env.SENTINEL_PROBE_JSON === "1") {
    // 把探针输出落到固定文件，避免被 Python wrapper 吞 stderr
    const probeLog = process.env.SENTINEL_PROBE_LOG || ".probe/sentinel-probe.log";
    fs.mkdirSync(path.dirname(probeLog), { recursive: true });
    const probeStream = fs.openSync(probeLog, "a");
    const probeWrite = (msg) => {
      try { fs.writeSync(probeStream, msg); } catch (_) {}
      try { process.stderr.write(msg); } catch (_) {}
    };
    probeWrite(`\n=== run @${new Date().toISOString()} flow=${flow} ===\n`);

    // 关键：sandbox 里 globalThis 被故意指向 window（plain object），所以
    // vm 内 `globalThis.x = ...` 写到的是 window，而 vm 自身全局对象（sandbox）取不到。
    // 解决：用 __callTally / __dumpProbeTally 通过 sandbox 的 plain getter/setter 暴露。
    // 简化做法 → 把 callTally 状态直接挂在外层闭包里、sdk 调 tally(name) 进闭包。
    const callTally = Object.create(null);
    context.tally = (name, extra) => {
      const k = name + (extra ? ":" + String(extra).slice(0, 60) : "");
      callTally[k] = (callTally[k] || 0) + 1;
    };
    context.dumpTally = () => {
      const ks = Object.keys(callTally).sort();
      probeWrite(`[probe.api.tally] flow=${flow} entries=${ks.length}\n`);
      for (const k of ks) probeWrite(`  ${k} = ${callTally[k]}\n`);
    };

    const prologue = `
      (function(){
        var __origParse = JSON.parse;
        JSON.parse = function(text, reviver){
          try {
            var v = __origParse.call(JSON, text, reviver);
            try {
              if (typeof text === 'string' && text.length > 50 && text.length < 30000 &&
                  /^\\[\\[\\s*-?\\d/.test(text) && Array.isArray(v) && Array.isArray(v[0])) {
                probeWrite('[probe.JSON.parse.OK.dx] flow=${flow} len=' + text.length +
                  ' arrayCount=' + v.length +
                  ' head3=' + JSON.stringify(v.slice(0, 3)) +
                  ' tail3=' + JSON.stringify(v.slice(-3)) + '\\n');
              }
            } catch(_) {}
            return v;
          } catch (e) {
            try {
              var s = typeof text === 'string' ? text : String(text);
              probeWrite('[probe.JSON.parse.FAIL] err=' + e.message +
                ' len=' + s.length +
                ' head=' + JSON.stringify(s.slice(0, 300)) +
                ' tail=' + JSON.stringify(s.slice(-100)) + '\\n');
              probeWrite('[probe.JSON.parse.FAIL.stack] ' +
                (new Error().stack || '').slice(0, 1200) + '\\n');
            } catch(_) {}
            throw e;
          }
        };

        // 把 stub 直接挂到 window 上（sdk 看到的 globalThis === window）
        try {
          // 1) PerformanceObserver
          if (typeof window.PerformanceObserver === 'undefined') {
            window.PerformanceObserver = function(cb) {
              tally('PerformanceObserver.new');
              this._cb = cb;
              this._types = [];
            };
            window.PerformanceObserver.prototype.observe = function(opts) {
              tally('PerformanceObserver.observe', JSON.stringify(opts));
              this._types.push(opts);
            };
            window.PerformanceObserver.prototype.disconnect = function() {
              tally('PerformanceObserver.disconnect');
            };
            window.PerformanceObserver.prototype.takeRecords = function() {
              tally('PerformanceObserver.takeRecords');
              return [];
            };
          }
        } catch(e){ probeWrite('[probe.patch.PO.ERR] ' + e.message + '\\n'); }

        // 2) performance.* 方法
        try {
          var __perf = window.performance || performance;
          if (__perf) {
            var __origGetEntries = __perf.getEntries;
            __perf.getEntries = function() {
              tally('performance.getEntries');
              return __origGetEntries ? __origGetEntries.call(__perf) : [];
            };
            __perf.getEntriesByType = function(type) {
              tally('performance.getEntriesByType', type);
              return [];
            };
            __perf.getEntriesByName = function(name, type) {
              tally('performance.getEntriesByName', name);
              return [];
            };
            __perf.clearResourceTimings = function() {
              tally('performance.clearResourceTimings');
            };
            __perf.mark = function(n) {
              tally('performance.mark', n);
            };
            __perf.measure = function(n) {
              tally('performance.measure', n);
            };
          }
        } catch(e){ probeWrite('[probe.patch.perf.ERR] ' + e.message + '\\n'); }

        // 3) MutationObserver / IntersectionObserver / ResizeObserver
        ['MutationObserver','IntersectionObserver','ResizeObserver'].forEach(function(name){
          try {
            if (typeof window[name] === 'undefined') {
              window[name] = function(cb) {
                tally(name + '.new');
                this._cb = cb;
              };
              window[name].prototype.observe = function() {
                tally(name + '.observe');
              };
              window[name].prototype.disconnect = function() {
                tally(name + '.disconnect');
              };
              window[name].prototype.takeRecords = function() { return []; };
            }
          } catch(e){ probeWrite('[probe.patch.' + name + '.ERR] ' + e.message + '\\n'); }
        });

        // 4) addEventListener 计数 + 收集回调，方便后面回放伪造事件
        try {
          var __addL = window.addEventListener;
          window.__listeners = window.__listeners || {};
          window.addEventListener = function(type, fn, opts) {
            tally('window.addEventListener', type);
            // 包一层 fn，统计被实际调用次数
            var wrapped = function(ev) {
              tally('listener.invoked', type);
              return fn.call(this, ev);
            };
            (window.__listeners[type] = window.__listeners[type] || []).push(wrapped);
            if (__addL) return __addL.call(window, type, wrapped, opts);
          };
        } catch(_) {}
        try {
          var __dAddL = document.addEventListener;
          document.addEventListener = function(type, fn, opts) {
            tally('document.addEventListener', type);
            if (__dAddL) return __dAddL.call(document, type, fn, opts);
          };
        } catch(_) {}

        // 5) 拦截高风险 window 属性读取
        var sentinelHotKeys = ['cookieStore','visualViewport','onpagehide','onpageshow',
          'onfreeze','onresume','PaymentRequest','onbeforeinstallprompt','navigation',
          'launchQueue','BatteryManager','getBattery','presentation'];
        sentinelHotKeys.forEach(function(k){
          try {
            if (window[k] === undefined) {
              Object.defineProperty(window, k, {
                configurable: true,
                get: function() { tally('window.read', k); return undefined; }
              });
            }
          } catch(_){}
        });
      })();
    `;
    context.probeWrite = probeWrite;
    vm.runInContext(prologue, context, { filename: "probe-json.js" });
    probeWrite("[probe.prologue.done] flow=" + flow + "\n");
    // 立即验证 prologue 的成果是否在
    vm.runInContext(
      "probeWrite('[probe.prologue.verify] win.PerformanceObserver=' + typeof window.PerformanceObserver + ' win.MutationObserver=' + typeof window.MutationObserver + '\\n');",
      context, { filename: "probe-verify-1.js" });
  }

  sdkScript.runInContext(context);

  if (process.env.SENTINEL_PROBE_JSON === "1") {
    try {
      vm.runInContext(
        "if (typeof probeWrite === 'function') {" +
        "  probeWrite('[probe.aftersdk.verify] win.PerformanceObserver=' + typeof window.PerformanceObserver + ' typeof tally=' + typeof tally + '\\n');" +
        "}",
        context, { filename: "probe-verify-2.js" });
    } catch(_) {}
  }

  if (!context.SentinelSDK?.token) {
    throw new Error("SDK 加载后没有暴露 SentinelSDK.token");
  }

  const tokenText = await context.SentinelSDK.token(flow);

  // 在 sessionObserverToken 之前模拟一段真实用户交互事件：
  // sdk 注册了 click/keydown/message/paste/pointermove/scroll/wheel 共 7 类 listener
  // 没有事件喂给它 → so 二进制里收集事件流为空 → 服务端判定非真实用户 → registration_disallowed
  if (cachedChallenge?.so?.required) {
    try {
      vm.runInContext(simulateUserInteractionsJs(), context, {
        filename: "simulate-events.js",
      });
      // 给 sdk 一点点 "时间" 处理事件流
      await new Promise((r) => setTimeout(r, 50));
    } catch (e) {
      process.stderr.write(`[runner] simulate events failed: ${e.message}\n`);
    }
  }

  // 如果 challenge.so.required，再调一次 sessionObserverToken 取 so 字段，
  // 合并到 token 输出里，避免 step12 因缺 openai-sentinel-so-token 头被拒。
  let mergedText = tokenText;
  const soRequired = !!(cachedChallenge?.so?.required);
  if (soRequired && typeof context.SentinelSDK.sessionObserverToken === "function") {
    try {
      const soResult = await context.SentinelSDK.sessionObserverToken(flow);
      // sessionObserverToken 返回值有两种形态：
      //   1. 字符串：sdk 已 base64 包装的 so 直接值
      //   2. 对象：{so, c}（与同一 flow 的 token 共享 c）
      let soField = null;
      if (typeof soResult === "string") {
        soField = soResult;
      } else if (soResult && typeof soResult === "object") {
        // 对象形态需要再 stringify 还原
        soField = soResult.so || null;
      }
      if (soField) {
        const parsed = JSON.parse(tokenText);
        parsed.so = soField;
        mergedText = JSON.stringify(parsed);
        process.stderr.write(
          `[runner] sessionObserverToken merged so field, len=${soField.length}\n`
        );
      } else {
        process.stderr.write(
          `[runner] sessionObserverToken returned no so field (result type=${typeof soResult})\n`
        );
      }
    } catch (e) {
      process.stderr.write(
        `[runner] sessionObserverToken failed: ${e?.stack || e?.message || e}\n`
      );
    }
  } else if (soRequired) {
    process.stderr.write(
      "[runner] challenge.so.required but SentinelSDK.sessionObserverToken not exposed\n"
    );
  }

  clearTimers();

  // 探针：dump api tally
  if (process.env.SENTINEL_PROBE_JSON === "1") {
    try {
      vm.runInContext(
        "if (typeof dumpTally === 'function') dumpTally();" +
        "else if (typeof probeWrite === 'function') probeWrite('[probe.dump.skip] dumpTally not defined\\n');",
        context, { filename: "probe-dump.js" });
    } catch (e) {
      process.stderr.write("[probe.dump] err=" + e.message + "\n");
    }
  }

  if (!writeOutput) return mergedText;
  if (args.pretty || process.env.SENTINEL_PRETTY === "1") {
    process.stdout.write(`${JSON.stringify(JSON.parse(mergedText), null, 2)}\n`);
  } else {
    process.stdout.write(`${mergedText}\n`);
  }
  return mergedText;
}

if (require.main === module) {
  main().catch((error) => fail(error?.stack || error?.message || String(error)));
}

module.exports = {
  main,
  normalizeChallenge,
  precompileSdk,
};
