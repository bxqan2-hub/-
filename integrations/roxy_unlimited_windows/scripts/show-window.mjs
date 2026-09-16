// show-window.mjs — 通过 CDP 让 RoxyChrome 真正显示浏览器窗口
//
// 背景：RoxyChrome 被直接 spawn 时，主窗口 (Chrome_WidgetWin_1) 已创建、
// 标题/尺寸都对，但 style 里没有 WS_VISIBLE —— 外部 ShowWindow() 无效。
// 推断：它是「先建隐藏窗口，等自动化客户端 attach 后才显示」。
// 官方启动器一上来就 puppeteer attach，所以能看见。
//
// 用法: node show-window.mjs [port] [--bounds L,T,W,H]

const port = Number(process.argv.find((a) => /^\d+$/.test(a)) || 62040);
const bi = process.argv.indexOf('--bounds');
const boundsArg = bi >= 0 ? process.argv[bi + 1] : null;
const ui = process.argv.indexOf('--url');
const urlArg = ui >= 0 ? process.argv[ui + 1] : null;
const attachOnly = process.argv.includes('--attach-only');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function cdp(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => {
    ws.addEventListener('open', res, { once: true });
    ws.addEventListener('error', () => rej(new Error('WS_CONNECT_FAILED')), { once: true });
  });
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.id && pending.has(msg.id)) {
      const { res, rej } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
    }
  });
  const send = (method, params = {}, sessionId = undefined) =>
    new Promise((res, rej) => {
      const n = ++id;
      pending.set(n, { res, rej });
      const frame = { id: n, method, params };
      if (sessionId) frame.sessionId = sessionId;
      ws.send(JSON.stringify(frame));
      setTimeout(() => {
        if (pending.has(n)) { pending.delete(n); rej(new Error(`TIMEOUT ${method}`)); }
      }, 10000);
    });
  return { send, close: () => ws.close() };
}

const ver = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
console.log('Browser  :', ver.Browser);
console.log('WS       :', ver.webSocketDebuggerUrl);

const { send, close } = await cdp(ver.webSocketDebuggerUrl);

const targets = (await send('Target.getTargets')).targetInfos;
const pages = targets.filter((t) => t.type === 'page');
console.log('pages    :', pages.length, pages.map((p) => p.title).join(' | '));
if (!pages.length) { console.log('NO_PAGE_TARGET'); close(); process.exit(1); }

for (const p of pages) {
  const { windowId, bounds } = await send('Browser.getWindowForTarget', { targetId: p.targetId });
  console.log(`\n[${p.title}] windowId=${windowId} bounds=${JSON.stringify(bounds)}`);

  if (attachOnly) {
    console.log('  --attach-only: 只查询，不发送任何改变窗口状态的命令');
    continue;
  }

  // 1) 先用 windowState 驱动 Chromium 自己的窗口管理（会走内部 Show()）
  for (const st of ['normal', 'maximized', 'normal']) {
    try {
      await send('Browser.setWindowBounds', { windowId, bounds: { windowState: st } });
      console.log(`  setWindowBounds windowState=${st}  OK`);
      await sleep(250);
    } catch (e) { console.log(`  setWindowBounds windowState=${st}  ERR ${e.message}`); }
  }

  // 2) 再显式给一个可见矩形
  const b = boundsArg
    ? (([l, t, w, h]) => ({ left: l, top: t, width: w, height: h }))(boundsArg.split(',').map(Number))
    : { left: 80, top: 80, width: 1600, height: 900 };
  try {
    await send('Browser.setWindowBounds', { windowId, bounds: { ...b, windowState: 'normal' } });
    console.log('  setWindowBounds rect        OK', JSON.stringify(b));
  } catch (e) { console.log('  setWindowBounds rect        ERR', e.message); }

  await sleep(300);
  const after = await send('Browser.getWindowForTarget', { targetId: p.targetId });
  console.log('  -> now:', JSON.stringify(after.bounds));

  // 3) 可选导航，用来肉眼确认窗口真的能用
  if (urlArg) {
    const { sessionId } = await send('Target.attachToTarget', { targetId: p.targetId, flatten: true });
    await send('Page.enable', {}, sessionId);
    const nav = await send('Page.navigate', { url: urlArg }, sessionId);
    console.log('  navigate ->', nav.frameId ? urlArg : JSON.stringify(nav));
    await sleep(2500);
    const t = await send('Runtime.evaluate', { expression: 'document.title + " @ " + location.href', returnByValue: true }, sessionId);
    console.log('  page now:', t.result?.value);
  }
}

close();
console.log('\nDONE');
