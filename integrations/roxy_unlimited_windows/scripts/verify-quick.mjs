// 逐字执行简版 §3 / §4 的代码
const API = 'http://127.0.0.1:50001';

// ---- §3 单开（proxy 换成不存在的会启动失败？不，只是代理不通，窗口照开；这里用 direct 保证可验证）----
const single = await (await fetch(`${API}/browser/create`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ windowName: 'w1', locale: 'pt-BR', proxy: 'direct', open: true }),
})).json();
console.log('[§3] create 返回字段:', Object.keys(single.data).join(', '));
console.log('[§3] locale/timeZone/screen/os:', single.data.locale, '/', single.data.timeZone, '/', single.data.screen, '/', single.data.os);
console.log('[§3] http =', single.data.http, ' driver ok =', String(single.data.driver).endsWith('chromedriver.exe'));

// ---- §4 批量开 10 个（逐字，不加 proxy 参数以还原文档写法，但会继承 proxy；此处加 direct 以免代理污染测试）----
const handles = await Promise.all(Array.from({length:10}, async (_, i) => {
  const r = await fetch(`${API}/browser/create`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ windowName: `bot-${i+1}`, locale: 'pt-BR', proxy: 'direct', open: true })
  });
  return (await r.json()).data;
}));
console.log(`\n[§4] 批量开出 ${handles.length} 个，各自独立端口:`);
console.log(handles.map(h => `  ${h.windowName}  http://${h.http}  pid=${h.pid}`).join('\n'));

// ---- 顺带验证 canvas 隔离（§5④ 的声明）----
const SRC = `(()=>{const c=document.createElement('canvas');c.width=220;c.height=40;const g=c.getContext('2d');
g.textBaseline='top';g.font='16px Arial';g.fillStyle='#f60';g.fillRect(0,0,120,22);g.fillStyle='#069';
g.fillText('RoxyFingerprint!',2,4);const fnv=s=>{let h=2166136261;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619);}return(h>>>0).toString(16).padStart(8,'0');};
return fnv(c.toDataURL());})()`;
const hashes = [];
for (const h of handles.slice(0, 4)) {
  const ver = await (await fetch(`http://127.0.0.1:${h.http.split(':')[1]}/json/version`)).json();
  const ws = new WebSocket(ver.webSocketDebuggerUrl);
  await new Promise((r,j)=>{ws.addEventListener('open',r);ws.addEventListener('error',j);});
  let id=0; const pend=new Map();
  ws.addEventListener('message',ev=>{const m=JSON.parse(ev.data); if(m.id&&pend.has(m.id)){pend.get(m.id)(m);pend.delete(m.id);}});
  const call=(m,p,s)=>new Promise(res=>{const i=++id;pend.set(i,res);ws.send(JSON.stringify({id:i,method:m,params:p,...(s?{sessionId:s}:{})}));setTimeout(()=>{if(pend.has(i)){pend.delete(i);res(null);}},8000);});
  const tg = await call('Target.getTargets',{});
  const sid = (await call('Target.attachToTarget',{targetId:tg.result.targetInfos.find(t=>t.type==='page').targetId,flatten:true})).result.sessionId;
  await call('Page.enable',{},sid);
  await call('Page.navigate',{url:'http://127.0.0.1:50001/_blank'},sid);
  await new Promise(r=>setTimeout(r,1200));
  const r = await call('Runtime.evaluate',{expression:SRC,returnByValue:true},sid);
  hashes.push(r?.result?.result?.value);
  ws.close();
}
console.log(`\n[§5④] 四个档案的 canvas 哈希: ${hashes.join(', ')}`);
console.log(`        互不相同 = ${new Set(hashes).size === hashes.length}`);

await fetch(`${API}/browser/close_all`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
for (const h of [...handles, single.data]) await fetch(`${API}/browser/delete`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({dirId: h.dirId}) });
console.log('\n[cleanup] 全部关闭并删除');
