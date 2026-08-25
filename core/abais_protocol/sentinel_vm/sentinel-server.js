#!/usr/bin/env node
"use strict";

// ============================================================================
// Sentinel 常驻 HTTP 服务
//
// 背景：
//   旧实现 core/sentinel_runner.py 每次生成 token 都 spawn 一个新的
//   `node sentinel-runner.js` 子进程。实测每个进程 RSS ~47MB、冷启动 ~180ms，
//   而真正有用功（读 sdk + 编译 + 建 vm context）只占 ~5ms。500 并发 × 3 次
//   sentinel 调用 → 同时几百个 node 进程 → 4核8线程/17G 机器内存爆 + CPU 风暴卡死。
//
//   本服务把 node 进程「常驻化」：一个进程只启动一次 V8，循环接收 HTTP 请求，
//   每次请求复用 sentinel-runner.js 的 main()，只付 ~5ms 的「建 context + 跑 sdk」开销。
//
// 时区（关键）：
//   sdk.js 内部 `new Date()` 的时区由 process.env.TZ 决定，且 V8 在首次访问后
//   会永久缓存，进程中途改 TZ 无效。因此「一个进程 = 一个固定时区」。
//   Python 端按代理出口国家（JP→Asia/Tokyo 等）为每个时区启动独立 worker 进程，
//   通过 SENTINEL_TZ 环境变量注入。本文件必须在任何 require / Date 之前设置 TZ。
//
// 协议：
//   - 启动后向 stdout 输出恰好一行 JSON：{"ready":true,"port":<实际端口>,"pid":...,"tz":...}
//     Python 端读这一行拿到端口（SENTINEL_SERVER_PORT=0 时由 OS 分配空闲端口）。
//   - GET  /health → {"ok":true,...}  健康检查
//   - POST /token  → 请求体 JSON：
//       {
//         "challenge":   <sentinel/req 返回的完整对象，含 _python_proof>,
//         "flow":        "username_password_create" | ...,
//         "device_id":   "<oai-did>",
//         "user_agent":  "<UA，必须与 Python 端一致>",
//         "page_url":    "<当前页面 URL>",
//         "sdk":         "<sdk.js 绝对路径，可选>",
//         "width": 1920, "height": 1080, "cores": 32,
//         "language": "ja-JP", "languages": "ja-JP,ja,en-US,en",
//         "no_cookie":   true
//       }
//     响应：{"ok":true,"token":"<sentinel-token JSON 字符串>"}
//         或 {"ok":false,"error":"<诊断信息>"}
// ============================================================================

// —— 时区必须最先设置（在 require sentinel-runner.js 之前）——
if (process.env.SENTINEL_TZ) {
  process.env.TZ = process.env.SENTINEL_TZ;
}

const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const runner = require("./sentinel-runner.js");

// ============================================================================
// 启动时预编译 SDK（方案二：vm.Script 复用）
//
// sdk.js 每次加载内容相同，在进程启动时编译一次为 vm.Script 对象，
// 后续每个请求直接 script.runInContext()，省掉 fs.readFileSync + V8 编译。
// 运行时不传 --sdk 参数时，用 body.sdk 字段动态定位（兼容旧请求）。
// ============================================================================
const defaultSdkPath = (() => {
  const candidates = [
    path.resolve(__dirname, "sdk.js"),
    path.resolve(__dirname, "..", "sdk.js"),
  ];
  for (const cand of candidates) {
    if (fs.existsSync(cand)) return cand;
  }
  return null;
})();

let compiledSdk = null;
if (defaultSdkPath) {
  try {
    compiledSdk = runner.precompileSdk(defaultSdkPath);
    process.stderr.write(`[sentinel-server] pre-compiled SDK: ${defaultSdkPath}\n`);
  } catch (e) {
    process.stderr.write(`[sentinel-server] SDK pre-compile failed: ${e.message}\n`);
  }
}

// 把 HTTP 请求体转成 sentinel-runner.js main() 所需的 argv 数组。
// 与旧 sentinel_runner.py 构造的命令行参数保持一一对应。
function buildArgv(body) {
  const argv = [];
  const push = (key, value) => {
    if (value !== undefined && value !== null && value !== "") {
      argv.push("--" + key, String(value));
    }
  };
  push("flow", body.flow);
  push("device-id", body.device_id);
  push("page-url", body.page_url);
  push("user-agent", body.user_agent);
  push("sdk", body.sdk);
  push("script-src", body.script_src);
  push("width", body.width);
  push("height", body.height);
  push("cores", body.cores);
  push("language", body.language);
  push("languages", body.languages);
  if (body.no_cookie) argv.push("--no-cookie");
  return argv;
}

function sendJson(res, status, payload) {
  const text = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(text),
  });
  res.end(text);
}

const server = http.createServer((req, res) => {
  if (req.method === "GET" && req.url === "/health") {
    sendJson(res, 200, { ok: true, pid: process.pid, tz: process.env.TZ || "" });
    return;
  }

  if (req.method !== "POST" || req.url !== "/token") {
    sendJson(res, 404, { ok: false, error: "not found" });
    return;
  }

  const chunks = [];
  let tooLarge = false;
  req.on("data", (chunk) => {
    if (tooLarge) return;
    chunks.push(chunk);
    // challenge 体一般几 KB，给个宽松上限防异常
    if (chunks.reduce((n, c) => n + c.length, 0) > 8 * 1024 * 1024) {
      tooLarge = true;
    }
  });
  req.on("end", async () => {
    if (tooLarge) {
      sendJson(res, 413, { ok: false, error: "request body too large" });
      return;
    }
    let body;
    try {
      body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    } catch (e) {
      sendJson(res, 400, { ok: false, error: "invalid JSON body" });
      return;
    }
    try {
      const argv = buildArgv(body);
      const challenge = body.challenge;
      if (!challenge || typeof challenge !== "object") {
        sendJson(res, 400, { ok: false, error: "missing challenge object" });
        return;
      }
      // 传入预编译的 SDK vm.Script；若 body.sdk 与启动时路径不同，
      // main() 内部会回退到 fs.readFileSync + 动态编译（向后兼容）。
      const sdkScript = (body.sdk === defaultSdkPath) ? compiledSdk : null;
      const token = await runner.main(argv, false, challenge, sdkScript);
      sendJson(res, 200, { ok: true, token });
    } catch (e) {
      sendJson(res, 500, {
        ok: false,
        error: (e && (e.stack || e.message)) || String(e),
      });
    }
  });
  req.on("error", () => {
    try {
      sendJson(res, 400, { ok: false, error: "request stream error" });
    } catch (_) {
      /* ignore */
    }
  });
});

// 单进程不必处理大量并发连接（Python 端用进程池横向扩展）；
// 但仍放开 keep-alive 以复用 TCP 连接，降低握手开销。
server.keepAliveTimeout = 60_000;
server.headersTimeout = 65_000;

const host = "127.0.0.1";
const port = Number(process.env.SENTINEL_SERVER_PORT || 0); // 0 → OS 分配空闲端口

server.listen(port, host, () => {
  const addr = server.address();
  // 唯一一行 stdout：握手信号。Python 端按行读取后即认为服务就绪。
  process.stdout.write(
    JSON.stringify({
      ready: true,
      port: addr.port,
      pid: process.pid,
      tz: process.env.TZ || "",
    }) + "\n"
  );
});

server.on("error", (err) => {
  process.stderr.write(`[sentinel-server] listen error: ${err.message}\n`);
  process.exit(1);
});

// 收到终止信号时干净退出（Python 端 atexit 会发送）
function shutdown() {
  try {
    server.close(() => process.exit(0));
  } catch (_) {
    process.exit(0);
  }
  // 兜底：1.5s 内没关干净就强退
  setTimeout(() => process.exit(0), 1500).unref();
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
