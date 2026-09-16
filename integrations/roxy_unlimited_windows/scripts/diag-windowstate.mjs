// diag-windowstate.mjs — 证明 CDP 的 windowState 和 OS 实际窗口状态会不一致
//
// 结论（实测）：
//   只发 Browser.setWindowBounds{windowState:'normal'} 时，
//   CDP 会回报 windowState="normal"，但 Win32 IsIconic() 仍然是 True ——
//   窗口并没有真的从最小化里恢复。
//   真正把它拉出来的是 Win32 的 ShowWindow(hwnd, SW_RESTORE)（=9）。
//   注意 SW_SHOW（=5）不还原最小化窗口，这也是"调了 ShowWindow 没反应"的原因。
//
// 所以还原窗口要 CDP + Win32 两步，见 roxy-open.ps1。
//
// 用法: node diag-windowstate.mjs <port>
//   配合查看 OS 侧状态:  pwsh -File roxy-open.ps1 -List

const port = Number(process.argv[2] || 9333);

async function cdp(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => {
    ws.addEventListener('open', res, { once: true });
    ws.addEventListener('error', () => rej(new Error('WS_CONNECT_FAILED')), { once: true });
  });
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { res, rej } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
    }
  });
  const send = (method, params = {}) =>
    new Promise((res, rej) => {
      const n = ++id;
      pending.set(n, { res, rej });
      ws.send(JSON.stringify({ id: n, method, params }));
      setTimeout(() => { if (pending.has(n)) { pending.delete(n); rej(new Error('TIMEOUT ' + method)); } }, 8000);
    });
  return { send, close: () => ws.close() };
}

const ver = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
const { send, close } = await cdp(ver.webSocketDebuggerUrl);
const pages = (await send('Target.getTargets')).targetInfos.filter((t) => t.type === 'page');
const { windowId, bounds } = await send('Browser.getWindowForTarget', { targetId: pages[0].targetId });
console.log('BEFORE:', JSON.stringify(bounds));
await send('Browser.setWindowBounds', { windowId, bounds: { windowState: 'normal' } });
await new Promise((r) => setTimeout(r, 500));
const after = await send('Browser.getWindowForTarget', { targetId: pages[0].targetId });
console.log('AFTER :', JSON.stringify(after.bounds));
close();
