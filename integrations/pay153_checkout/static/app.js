const $ = (id) => document.getElementById(id);
const API_BASE = '/pay153/api';
const form = $('checkoutForm');
const privateMode = location.pathname.replace(/\/+$/, '').endsWith('/private-checkout');
let jobId = '';
let pollTimer = 0;
let countdownTimer = 0;
let displayedProgress = 0;
let targetProgress = 0;
let progressStatus = 'idle';
let progressFrame = 0;
let progressLastTick = 0;
let proxySaveTimer = 0;
let paypalProxyCheckTimer = 0;
let paypalProxyCheckSequence = 0;
let paypalProxyCheckController = null;
let logAutoFollow = true;
let renderedLogKey = '';
let formStateSaveTimer = 0;

const FORM_STATE_KEY = 'pay153.checkout.form.v1';
const JOB_STATE_KEY = 'pay153.checkout.job.v1';

const PROXY_STORAGE_KEYS = {
  entry: 'pay153.proxy_pool_1',
  exit: 'pay153.proxy_pool_2'
};

const providerDefaults = {
  hosted: {country: 'US', currency: 'USD'}, paypal: {country: 'US', currency: 'USD'},
  paypal_oaics: {country: 'BR', currency: 'USD'},
  ideal: {country: 'NL', currency: 'EUR'}, upi: {country: 'IN', currency: 'INR'},
  pix: {country: 'BR', currency: 'BRL'}, momo: {country: 'VN', currency: 'VND'}, gcash: {country: 'PH', currency: 'PHP'}, kakao: {country: 'KR', currency: 'KRW'},
  ph_short: {country: 'PH', currency: 'PHP'}
};

const paypalOaicsCountries = [
  ['BR','巴西','USD'], ['DE','德国','EUR'], ['US','美国','USD'], ['GB','英国','GBP'],
  ['FR','法国','EUR'], ['JP','日本','JPY'], ['CA','加拿大','CAD'], ['AU','澳大利亚','AUD'],
  ['NZ','新西兰','NZD'], ['MX','墨西哥','MXN'], ['AR','阿根廷','USD'], ['CL','智利','USD'],
  ['CO','哥伦比亚','USD'], ['PE','秘鲁','USD'], ['ES','西班牙','EUR'], ['IT','意大利','EUR'],
  ['NL','荷兰','EUR'], ['BE','比利时','EUR'], ['IE','爱尔兰','EUR'], ['PT','葡萄牙','EUR'],
  ['AT','奥地利','EUR'], ['CH','瑞士','CHF'], ['SE','瑞典','SEK'], ['NO','挪威','NOK'],
  ['DK','丹麦','DKK'], ['FI','芬兰','EUR'], ['PL','波兰','PLN'], ['CZ','捷克','CZK'],
  ['RO','罗马尼亚','RON'], ['HU','匈牙利','HUF'], ['GR','希腊','EUR'], ['SG','新加坡','SGD'],
  ['MY','马来西亚','MYR'], ['TH','泰国','THB'], ['PH','菲律宾','PHP'], ['ID','印度尼西亚','IDR'],
  ['IN','印度','INR'], ['KR','韩国','KRW'], ['TW','中国台湾','USD'], ['HK','中国香港','HKD'],
  ['IL','以色列','ILS'], ['AE','阿联酋','AED'], ['ZA','南非','ZAR']
];

function paypalOaicsCountry(code){
  return paypalOaicsCountries.find(item => item[0] === code) || paypalOaicsCountries[0];
}

function populatePaypalOaicsCountries(){
  const options = paypalOaicsCountries.map(([code,name,currency]) =>
    `<option value="${code}">${code} · ${name} · ${currency}</option>`
  ).join('');
  $('paypalOaicsProxyCountry').innerHTML = options;
  $('paypalOaicsBillingCountry').innerHTML = options;
  $('paypalOaicsProxyCountry').value = 'BR';
  $('paypalOaicsBillingCountry').value = 'DE';
}

function formStateSnapshot(){
  const state = {};
  form.querySelectorAll('input, textarea, select').forEach(node => {
    if ((!node.id && !node.name) || node.readOnly || ['button', 'submit', 'file'].includes(node.type)) return;
    const key = node.id || `name:${node.name}`;
    if (node.type === 'radio') {
      if (node.checked) state[key] = node.value;
    } else if (node.type === 'checkbox') state[key] = node.checked;
    else state[key] = node.value;
  });
  return state;
}

function saveFormState(){
  clearTimeout(formStateSaveTimer);
  formStateSaveTimer = setTimeout(() => {
    try { sessionStorage.setItem(FORM_STATE_KEY, JSON.stringify(formStateSnapshot())); } catch (_) {}
  }, 80);
}

function restoreFormState(){
  let state = {};
  try { state = JSON.parse(sessionStorage.getItem(FORM_STATE_KEY) || '{}'); } catch (_) {}
  form.querySelectorAll('input, textarea, select').forEach(node => {
    if ((!node.id && !node.name) || node.readOnly) return;
    const key = node.id || `name:${node.name}`;
    if (!Object.prototype.hasOwnProperty.call(state, key)) return;
    if (node.type === 'radio') node.checked = state[key] === node.value;
    else if (node.type === 'checkbox') node.checked = Boolean(state[key]);
    else node.value = String(state[key] ?? '');
  });
  document.querySelectorAll('.choice').forEach(label => {
    label.classList.toggle('active', Boolean(label.querySelector('input')?.checked));
  });
}

form.addEventListener('input', saveFormState);
form.addEventListener('change', saveFormState);
const countryCurrency = {US:'USD',DE:'EUR',FR:'EUR',NL:'EUR',IN:'INR',BR:'BRL',VN:'VND',GB:'GBP',JP:'JPY',KR:'KRW',PH:'PHP',AU:'AUD',CA:'CAD'};

const proxyProfiles = {
  hosted: {
    summary: '单池线路：代理池 1 同时负责 Checkout、所选账单地区、优惠更新和后续金额校验。',
    promoPool: 1,
    entry: {role:'Checkout / 账单 / 优惠', country:'账号常用地区或所选账单地区', description:'创建官方 Checkout，并沿用同一出口完成优惠更新与 Stripe 金额校验。'},
    exit: {role:'当前路径不使用', country:'无需填写', description:'Hosted 全程复用代理池 1，不读取代理池 2。'}
  },
  ph_short: {
    summary: '双池线路：代理池 1 创建菲律宾账单，代理池 2 负责 Plus 优惠地区。',
    promoPool: 2,
    entry: {role:'Checkout 入口', country:'US · 账单固定 PH/PHP', description:'使用美国出口创建 PH/PHP OpenAI Checkout，并生成菲律宾支付短链。'},
    exit: {role:'优惠地区', country:'TR · 关闭优惠时可与池 1 同为 US', description:'用于活动识别和优惠更新；不改变最终 PH/PHP 账单地区。'}
  },
  paypal: {
    summary: '双池线路：代理池 1 是优惠地区；代理池 2 决定 PayPal 支付出口、资料地区及 OpenAI 账单回退。',
    promoPool: 1,
    entry: {role:'优惠地区', country:'TR / JP；巴西特殊流程使用 BR', description:'用于 Plus 活动识别与优惠更新，不决定 PayPal 支付资料地区。'},
    exit: {role:'PayPal 支付 / 资料地区', country:'推荐 DE；巴西流程使用 BR', description:'决定 PayPal 出口和资料。US/GB/DE/FR/IE/NL/ES/IT/AT 使用原生账单，其他地区回退 DE/EUR Checkout。'}
  },
  paypal_oaics: {
    summary: '单池线路：代理池 1 承担 OAICS 全流程；代理出口与账单国家可独立手动设置。',
    promoPool: 1,
    entry: {role:'OAICS 全流程', country:'默认 BR · 可手动选择', description:'预检、Checkout、税费、PayPal confirm 与 BA 提取全程复用代理池 1；裸代理默认按 SOCKS5 解析。'},
    exit: {role:'当前路径不使用', country:'无需填写', description:'PayPal OAICS 强制单池复用代理池 1。'}
  },
  ideal: {
    summary: '双池线路：代理池 1 负责优惠地区，代理池 2 负责 NL/EUR 账单和 iDEAL 支付。',
    promoPool: 1,
    entry: {role:'优惠地区', country:'推荐 NL', description:'用于活动识别与优惠更新；使用同为 NL 的线路最容易保持地区一致。'},
    exit: {role:'账单 / 支付地区', country:'必须 NL · 账单 NL/EUR', description:'创建 NL/EUR Checkout，并贯穿 Stripe 与 iDEAL 银行支付处理。'}
  },
  upi: {
    summary: '双池线路：代理池 1 负责优惠地区，代理池 2 负责 IN/INR 账单和 UPI 支付。',
    promoPool: 1,
    entry: {role:'优惠地区', country:'TR / JP / BR', description:'用于活动资格识别与优惠预检，不决定印度账单地区。'},
    exit: {role:'账单 / 支付地区', country:'IN · 账单 IN/INR', description:'创建印度 Checkout，并处理 UPI PaymentMethod、确认和二维码结果。'}
  },
  pix: {
    summary: '单池线路：代理池 1 固定承担 BR/BRL 账单、优惠、PIX 支付和确认。',
    promoPool: 1,
    entry: {role:'账单 / 优惠 / 支付地区', country:'BR · 账单 BR/BRL', description:'同一条巴西出口完成 Checkout、优惠更新、Stripe、PIX PaymentMethod 与 approval。'},
    exit: {role:'当前路径不使用', country:'无需填写', description:'PIX 强制单链路，系统直接复用代理池 1。'}
  },
  momo: {
    summary: '单池线路：代理池 1 固定承担 VN/VND 账单、优惠、MoMo 支付和确认。',
    promoPool: 1,
    entry: {role:'账单 / 优惠 / 支付地区', country:'VN · 账单 VN/VND', description:'同一条越南出口完成 Checkout、优惠更新、Stripe、MoMo 支付与 approval。'},
    exit: {role:'当前路径不使用', country:'无需填写', description:'MoMo 强制单链路，系统直接复用代理池 1。'}
  },
  gcash: {
    summary: '双池线路：代理池 1 创建 PH/PHP 账单并处理 GCash，代理池 2 只负责优惠地区。',
    promoPool: 2,
    entry: {role:'Checkout / GCash 支付', country:'US · 账单固定 PH/PHP', description:'使用美国出口创建菲律宾账单，并完成 GCash 支付方式确认与跳转。'},
    exit: {role:'优惠地区', country:'推荐 VN', description:'用于 Plus 活动识别和优惠更新；不改变 PH/PHP 账单或 GCash 支付地区。'}
  },
  kakao: {
    summary: '双池线路：代理池 1 负责优惠地区，代理池 2 负责 KR/KRW 账单和 Kakao Pay 支付。',
    promoPool: 1,
    entry: {role:'优惠地区', country:'VN', description:'用于活动识别与优惠更新，不承担韩国支付处理。'},
    exit: {role:'账单 / 支付地区', country:'KR · 账单 KR/KRW', description:'创建韩国 Checkout，并处理 Kakao Pay、confirm 与 Nicepay 跳转。'}
  },
  twint: {
    summary: '双池线路：代理池 1 负责优惠地区，代理池 2 负责 CH/CHF 账单和 TWINT 支付。',
    promoPool: 1,
    entry: {role:'优惠地区', country:'推荐 CH', description:'用于活动识别与优惠更新。'},
    exit: {role:'账单 / 支付地区', country:'必须 CH · 账单 CH/CHF', description:'创建瑞士 Checkout，并贯穿 TWINT 支付确认。'}
  }
};

function proxyLines(node){
  return node.value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
}
function updateProxyCount(node, counter){
  const count = proxyLines(node).length;
  counter.textContent = `${count} / 500`;
  counter.classList.toggle('over-limit', count > 500);
  node.setCustomValidity(count > 500 ? '每个代理池最多填写 500 条' : '');
  return count;
}
function setProxySaveState(text, failed=false){
  const node = $('proxySaveState');
  node.textContent = text;
  node.classList.toggle('save-failed', failed);
}
function saveProxyPools(){
  clearTimeout(proxySaveTimer);
  proxySaveTimer = setTimeout(() => {
    try {
      localStorage.setItem(PROXY_STORAGE_KEYS.entry, $('entryProxy').value);
      localStorage.setItem(PROXY_STORAGE_KEYS.exit, $('exitProxy').value);
      setProxySaveState('已保存到本机');
    } catch (error) {
      setProxySaveState('本地保存失败', true);
    }
  }, 220);
}
function restoreProxyPools(){
  try {
    const entry = localStorage.getItem(PROXY_STORAGE_KEYS.entry);
    const exit = localStorage.getItem(PROXY_STORAGE_KEYS.exit);
    if (entry !== null) $('entryProxy').value = entry;
    if (exit !== null) $('exitProxy').value = exit;
    setProxySaveState(entry !== null || exit !== null ? '已恢复本地代理' : '本地自动保存');
  } catch (error) {
    setProxySaveState('本地保存不可用', true);
  }
}

function selected(name){
  const checked = form.querySelector(`input[name="${name}"]:checked`);
  if (checked) return checked.value;
  return form.elements.namedItem(name)?.value || '';
}
function paypalOaicsCountrySelection(){
  const manual = $('paypalOaicsManualEnabled').checked;
  return {
    manual,
    proxy: manual ? $('paypalOaicsProxyCountry').value : 'BR',
    billing: manual ? $('paypalOaicsBillingCountry').value : 'DE'
  };
}
function syncPaypalOaicsCountryControls(){
  const selection = paypalOaicsCountrySelection();
  const proxy = paypalOaicsCountry(selection.proxy);
  const billing = paypalOaicsCountry(selection.billing);
  $('paypalOaicsCountryFields').hidden = !selection.manual;
  $('paypalOaicsManualToggle').setAttribute('aria-expanded', String(selection.manual));
  $('paypalOaicsManualToggle').textContent = selection.manual
    ? '恢复默认 BR / DE'
    : '手动设置代理与账单国家';
  $('paypalOaicsCountrySummary').textContent = selection.manual
    ? `手动设置：${proxy[0]} 代理出口，${billing[0]}/${billing[2]} 零元账单。`
    : '默认使用 BR 代理出口与 DE/EUR 零元账单。';
  return selection;
}
function setPaypalProxyCheck(text, state=''){
  const node = $('paypalOaicsProxyCheck');
  node.hidden = !text || selected('link_type') !== 'paypal_oaics';
  node.textContent = text || '';
  node.className = `proxy-check-status${state ? ` is-${state}` : ''}`;
}
function schedulePaypalProxyCheck(delay=700){
  clearTimeout(paypalProxyCheckTimer);
  paypalProxyCheckSequence += 1;
  if (paypalProxyCheckController) paypalProxyCheckController.abort();
  paypalProxyCheckController = null;
  if (selected('link_type') !== 'paypal_oaics') {
    $('entryProxy').setCustomValidity('');
    setPaypalProxyCheck('');
    return;
  }
  const proxies = proxyLines($('entryProxy'));
  const expectedCountry = paypalOaicsCountrySelection().proxy;
  if (!proxies.length) {
    setPaypalProxyCheck(`填写代理后会自动检查格式和 ${expectedCountry} 出口连通性。`);
    return;
  }
  const sequence = paypalProxyCheckSequence;
  setPaypalProxyCheck('等待检查代理…', 'checking');
  paypalProxyCheckTimer = setTimeout(async () => {
    const controller = new AbortController();
    paypalProxyCheckController = controller;
    setPaypalProxyCheck(`正在快速检查格式和出口（最多 ${Math.min(5, proxies.length)} 条）…`, 'checking');
    try {
      const response = await fetch(`${API_BASE}/paypal-oaics/proxy-check`, {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({proxies, expected_country: expectedCountry}),
        signal: controller.signal,
      });
      const data = await response.json();
      if (sequence !== paypalProxyCheckSequence) return;
      if (!response.ok) {
        $('entryProxy').setCustomValidity(data.error || '代理格式不正确');
        setPaypalProxyCheck(data.error || '代理格式不正确', 'error');
        return;
      }
      $('entryProxy').setCustomValidity('');
      const suffix = data.truncated ? `；代理池共 ${data.total} 条，仅抽检前 ${data.checked} 条` : '';
      if (data.country_compatible > 0) {
        const extra = data.failed ? `，${data.failed} 条连接失败` : '';
        setPaypalProxyCheck(`检查完成：${data.country_compatible}/${data.checked} 条可用 ${data.expected_country} 出口${extra}${suffix}`, data.failed ? 'warning' : 'ok');
      } else if (data.reachable > 0) {
        const countries = [...new Set((data.results || []).map(item => item.country).filter(Boolean))].join('/');
        setPaypalProxyCheck(`代理可以连接，但出口为 ${countries || '未知'}，当前设置要求 ${data.expected_country} 出口${suffix}`, 'warning');
      } else {
        setPaypalProxyCheck(`代理连接失败，请检查地址、协议、账号密码或节点状态${suffix}`, 'error');
      }
    } catch (error) {
      if (error.name !== 'AbortError' && sequence === paypalProxyCheckSequence) {
        setPaypalProxyCheck('后台代理检查暂时未完成；提交任务时仍会执行严格预检。', 'warning');
      }
    } finally {
      if (paypalProxyCheckController === controller) paypalProxyCheckController = null;
    }
  }, delay);
}
function bindChoices(group, onChange){
  group.querySelectorAll('label').forEach(label => label.addEventListener('click', () => {
    group.querySelectorAll('label').forEach(x => x.classList.remove('active'));
    label.classList.add('active');
    setTimeout(onChange, 0);
  }));
}
bindChoices($('planGrid'), () => syncFields(false));
$('runMode').addEventListener('change', () => syncFields(true));

const runModeHints = {
  hosted: '返回官方 Checkout 长链，不提交支付方式。',
  ph_short: '生成菲律宾 Stripe 支付短链。',
  paypal: '用于 cs_live_* / Stripe Checkout，生成 PayPal Approve 跳转。',
  paypal_oaics: '使用 link-pp 核心：代理出口与账单国家可独立选择，提取 PayPal BA 链后停止。',
  ideal: '生成荷兰 iDEAL 银行支付跳转。',
  upi: '生成印度 UPI 支付结果或二维码。',
  pix: '生成巴西 PIX 支付结果或二维码。',
  momo: '生成越南 MoMo 电子钱包跳转。',
  gcash: '生成菲律宾 GCash 电子钱包跳转。',
  kakao: '生成韩国 Kakao Pay / Nicepay 跳转。'
};

function syncFields(applyRailDefault=false){
  const plan = selected('plan'), rail = selected('link_type');
  const oaicsCountries = syncPaypalOaicsCountryControls();
  $('runModeHint').textContent = runModeHints[rail] || '';
  $('teamFields').hidden = plan !== 'team';
  $('codexFields').hidden = plan !== 'codex_low';
  $('idealOptions').hidden = rail !== 'ideal';
  $('paypalOptions').hidden = rail !== 'paypal';
  $('paypalOaicsOptions').hidden = rail !== 'paypal_oaics';
  $('pixOptions').hidden = rail !== 'pix';
  $('regionFields').hidden = ['paypal', 'paypal_oaics'].includes(rail);
  $('regionAutoHint').hidden = !['paypal', 'paypal_oaics'].includes(rail);
  $('regionAutoHint').textContent = rail === 'paypal_oaics'
    ? `${oaicsCountries.proxy} 代理出口，${oaicsCountries.billing}/${paypalOaicsCountry(oaicsCountries.billing)[2]} 零元账单。`
    : 'PayPal 的国家、地区和币种会根据代理池 2 自动识别。';
  $('pixTaxId').required = false;
  const promoSupported = plan === 'plus';
  $('promoLine').style.display = promoSupported ? 'flex' : 'none';
  $('plusPromoFields').hidden = !promoSupported || !$('usePromo').checked;
  const needsExit = rail !== 'hosted' && rail !== 'paypal_oaics' && rail !== 'pix' && rail !== 'momo';
  $('proxyGrid').classList.toggle('single', !needsExit);
  $('exitProxyField').hidden = !needsExit;
  $('exitProxy').required = needsExit;
  $('copyEntryProxy').hidden = !needsExit;
  let profile = proxyProfiles[rail] || proxyProfiles.hosted;
  if (rail === 'paypal_oaics') {
    const proxy = paypalOaicsCountry(oaicsCountries.proxy);
    const billing = paypalOaicsCountry(oaicsCountries.billing);
    profile = {
      ...profile,
      summary: `单池线路：代理池 1 使用 ${proxy[0]} 出口，创建 ${billing[0]}/${billing[2]} 零元 OAICS Checkout 并提取 PayPal BA 链。`,
      entry: {...profile.entry, country: `${proxy[0]} · ${proxy[1]}`}
    };
  }
  const promoEnabled = plan === 'plus' && $('usePromo').checked;
  const promoState = !promoEnabled && profile.promoPool
    ? ` 当前未启用优惠：代理池 ${profile.promoPool} 的优惠职责本次不执行，但仍按页面要求填写。`
    : '';
  const recommendation = `${profile.summary}${promoState}`;
  $('proxyRecommendation').textContent = recommendation;
  $('proxyFootHint').textContent = recommendation;
  $('entryProxyRole').textContent = profile.entry.role;
  $('entryProxyCountry').textContent = profile.entry.country;
  $('entryProxyDescription').textContent = profile.entry.description;
  $('entryProxyLabel').textContent = `代理池 1 · ${profile.entry.role}`;
  $('entryProxyInputNote').textContent = `推荐：${profile.entry.country}。每行一个代理，系统每轮自动选择。`;
  $('exitProxyRole').textContent = profile.exit.role;
  $('exitProxyCountry').textContent = profile.exit.country;
  $('exitProxyDescription').textContent = profile.exit.description;
  $('exitProxyLabel').textContent = `代理池 2 · ${profile.exit.role}`;
  $('exitProxyInputNote').textContent = `推荐：${profile.exit.country}。每行一个代理，系统每轮自动选择。`;
  $('exitProxyRoleCard').classList.toggle('is-unused', !needsExit);
  if (applyRailDefault && providerDefaults[rail]) {
    $('country').value = providerDefaults[rail].country;
    $('currency').value = providerDefaults[rail].currency;
  }
  schedulePaypalProxyCheck(rail === 'paypal_oaics' ? 120 : 0);
}
$('country').addEventListener('change', () => $('currency').value = countryCurrency[$('country').value] || 'USD');
$('paypalOaicsManualToggle').addEventListener('click', () => {
  $('paypalOaicsManualEnabled').checked = !$('paypalOaicsManualEnabled').checked;
  syncFields(false);
  saveFormState();
});
$('paypalOaicsProxyCountry').addEventListener('change', () => syncFields(false));
$('paypalOaicsBillingCountry').addEventListener('change', () => syncFields(false));
$('usePromo').addEventListener('change', () => syncFields(false));
$('entryProxy').addEventListener('input', () => {
  $('entryProxy').setCustomValidity('');
  updateProxyCount($('entryProxy'), $('entryProxyCount'));
  saveProxyPools();
  schedulePaypalProxyCheck();
});
$('exitProxy').addEventListener('input', () => { updateProxyCount($('exitProxy'), $('exitProxyCount')); saveProxyPools(); });
$('copyEntryProxy').addEventListener('click', () => {
  $('exitProxy').value = $('entryProxy').value.trim();
  updateProxyCount($('exitProxy'), $('exitProxyCount'));
  saveProxyPools();
  $('exitProxy').focus();
});

function paintProgress(value){
  const p = Math.max(0, Math.min(100, value));
  $('progressValue').textContent = `${Math.round(p)}%`;
  $('orbitValue').style.strokeDashoffset = String(320.44 * (1 - p / 100));
  $('progressBar').style.width = `${p}%`;
}
function animateProgress(timestamp){
  const dt = Math.min(.08, Math.max(.001, (timestamp - (progressLastTick || timestamp)) / 1000));
  progressLastTick = timestamp;
  if (progressStatus === 'running' && targetProgress < 96) {
    targetProgress = Math.min(96, targetProgress + dt * .28);
  }
  const diff = targetProgress - displayedProgress;
  if (Math.abs(diff) > .02) {
    const rate = progressStatus === 'done' ? 42 : Math.max(7, Math.abs(diff) * 1.35);
    displayedProgress += Math.sign(diff) * Math.min(Math.abs(diff), rate * dt);
    paintProgress(displayedProgress);
  } else {
    displayedProgress = targetProgress;
    paintProgress(displayedProgress);
  }
  if (progressStatus === 'running' || Math.abs(targetProgress - displayedProgress) > .02) {
    progressFrame = requestAnimationFrame(animateProgress);
  } else {
    progressFrame = 0;
    progressLastTick = 0;
  }
}
function resetProgress(){
  if (progressFrame) cancelAnimationFrame(progressFrame);
  displayedProgress = 0;
  targetProgress = 0;
  progressStatus = 'idle';
  progressFrame = 0;
  progressLastTick = 0;
  paintProgress(0);
}
function setProgress(percent, text, status='running'){
  const p = Math.max(0, Math.min(100, Number(percent)||0));
  const retryReset = status === 'running' && p <= 10 && Math.max(displayedProgress, targetProgress) >= 20;
  if (retryReset) {
    displayedProgress = p;
    targetProgress = p;
    paintProgress(p);
  } else if (status === 'running') {
    targetProgress = Math.max(targetProgress, p);
  } else {
    targetProgress = p;
  }
  progressStatus = status;
  $('progressText').textContent = text || '处理中';
  const badge = $('statusBadge'); badge.className = `status-badge ${status}`;
  badge.textContent = status === 'done' ? '完成' : status === 'error' ? '异常' : status === 'cancelled' ? '已停止' : status === 'queued' ? '排队中' : status === 'running' ? '运行中' : '等待';
  $('progressStage').textContent = status === 'done' ? '任务完成' : status === 'error' ? '任务异常' : status === 'cancelled' ? '任务已停止' : status === 'queued' ? '等待执行' : status === 'running' ? '正在处理' : '等待开始';
  if (!progressFrame) progressFrame = requestAnimationFrame(animateProgress);
}
function renderLogs(logs){
  const box = $('logBox');
  if (!logs?.length) return;
  const nextKey = logs.map(x => `${x.time}|${x.message}`).join('\n');
  if (nextKey === renderedLogKey) return;
  const previousTop = box.scrollTop;
  const wasFollowing = logAutoFollow;
  box.innerHTML = logs.map(x => `<div class="log-row"><time>${escapeHtml(x.time)}</time><span>${escapeHtml(x.message)}</span></div>`).join('');
  renderedLogKey = nextKey;
  if (wasFollowing) box.scrollTop = box.scrollHeight;
  else box.scrollTop = previousTop;
}
function escapeHtml(v){ return String(v ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
function setRunning(running){ $('submitButton').disabled = running; $('cancelButton').hidden = !running; }
$('logBox').addEventListener('scroll', () => {
  const box = $('logBox');
  logAutoFollow = box.scrollHeight - box.clientHeight - box.scrollTop < 28;
});

function showResult(result){
  $('resultPanel').hidden = false;
  const resultMode = String(result.requested_link_type || result.link_type || '').toUpperCase();
  $('resultType').textContent = `${String(result.plan||'').toUpperCase()} · ${resultMode}`;
  $('resultEmail').textContent = result.account_email || '—';
  $('resultRegion').textContent = `${result.country || '—'} / ${result.currency || '—'}`;
  $('resultPromo').textContent = !result.promo_requested ? '未请求' : result.promo_applied === true ? '已生效 · 今日应付 0' : result.promo_applied === false ? '未生效' : '打开结账页确认';
  $('resultSession').textContent = result.checkout_session_id || '—';
  const resultProvider = String(result.link_type || result.provider || '').toLowerCase();
  const isIdeal = resultProvider === 'ideal';
  const isKakao = resultProvider === 'kakao';
  const isCodexLow = String(result.plan || '').toLowerCase() === 'codex_low';
  const codexShortLink = isCodexLow && result.checkout_session_id
    ? (result.short_link || result.verification_url || `https://chatgpt.com/checkout/openai_llc/${result.checkout_session_id}`)
    : '';
  const providerUrl = result.paypal_approve_url || result.paypal_link || result.provider_redirect_url || '';
  const finalValue = codexShortLink || result.short_link || result.verification_url || ((isIdeal || isKakao)
    ? (providerUrl || result.checkout_url || result.qr_data || '')
    : (result.qr_data || providerUrl || result.checkout_url || ''));
  $('resultValue').value = finalValue;
  const openUrl = codexShortLink || result.short_link || result.verification_url || providerUrl || result.checkout_url || '';
  $('openResult').href = openUrl || '#';
  $('openResult').style.display = openUrl ? 'inline-flex' : 'none';
  const verifyUrl = result.verification_url || '';
  $('verifyResult').href = verifyUrl || '#';
  $('verifyResult').hidden = !verifyUrl;
  const qr = isIdeal
    ? (result.qr_image_svg || result.qr_image_png || '')
    : (result.qr_image_png || result.qr_image_svg || '');
  $('qrWrap').hidden = !qr;
  if (qr) $('qrImage').src = qr;
  startCountdown(result.expires_at);
  $('resultPanel').scrollIntoView({behavior:'smooth',block:'nearest'});
}
function startCountdown(expiresAt){
  clearInterval(countdownTimer); const node = $('qrCountdown');
  if (!expiresAt) { node.textContent = ''; return; }
  const render = () => { const remain = Math.max(0, Number(expiresAt)*1000-Date.now()); const m=Math.floor(remain/60000),s=Math.floor(remain%60000/1000); node.textContent=remain?`二维码剩余 ${m}:${String(s).padStart(2,'0')}`:'二维码已到期'; };
  render(); countdownTimer=setInterval(render,1000);
}

async function poll(){
  if (!jobId) return;
  try{
    const r = await fetch(`${API_BASE}/checkout-progress?job_id=${encodeURIComponent(jobId)}`, {cache:'no-store'});
    const data = await r.json(); if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    setProgress(data.percent, data.text, data.status);
    renderLogs(data.logs);
    if (data.status === 'done') { clearInterval(pollTimer); setRunning(false); showResult(data.result || {}); }
    if (data.status === 'error' || data.status === 'cancelled') { clearInterval(pollTimer); setRunning(false); if(data.error) renderLogs([...(data.logs||[]),{time:'ERROR',message:data.error}]); }
  }catch(e){ clearInterval(pollTimer); setRunning(false); setProgress(100, e.message || String(e), 'error'); }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault(); $('resultPanel').hidden = true; $('logBox').innerHTML = '<div class="empty-log">正在创建任务…</div>';
  renderedLogKey = '';
  logAutoFollow = true;
  resetProgress();
  setRunning(true); setProgress(3, '提交任务', 'running');
  const plan = selected('plan');
  const body = {
    token: $('token').value, plan, link_type: selected('link_type'), country: $('country').value,
    currency: $('currency').value, entry_proxies: proxyLines($('entryProxy')), exit_proxies: proxyLines($('exitProxy')),
    retry_count: Math.max(1, Math.min(50, Number($('retryCount').value || 10))),
    use_sen: $('useSentinel').checked,
    use_so: $('useSentinel').checked,
    use_promo: plan === 'plus' && $('usePromo').checked,
    promo_campaign: plan === 'plus' ? $('promoCampaign').value.trim() : '',
    promo_code: plan === 'team' ? $('promoCode').value.trim() : '',
    workspace_name: plan === 'codex_low' ? $('codexWorkspaceName').value.trim() : $('workspaceName').value.trim(),
    workspace_id: $('workspaceId').value.trim(), seat_quantity: Number($('seatQuantity').value || 5),
    price_interval: $('priceInterval').value, credit_quantity: Number($('creditQuantity').value || 13),
    ideal_bank: '',
    pix_tax_id: selected('link_type') === 'pix' ? $('pixTaxId').value.trim() : '',
    pix_auto_kind: selected('link_type') === 'pix' ? $('pixAutoKind').value : 'cpf'
  };
  if (body.link_type === 'paypal_oaics') {
    const oaicsCountries = paypalOaicsCountrySelection();
    body.proxy_country = oaicsCountries.proxy;
    body.billing_country = oaicsCountries.billing;
    body.country = oaicsCountries.proxy;
    body.currency = paypalOaicsCountry(oaicsCountries.proxy)[2];
    body.exit_proxies = [];
    body.use_promo = true;
    body.promo_campaign = 'plus-1-month-free';
    body.provider_attempts = 10;
  }
  try{
    const r = await fetch(`${API_BASE}/checkout`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data = await r.json(); if(!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    jobId = data.job_id;
    sessionStorage.setItem(JOB_STATE_KEY, jobId);
    if (data.internal) setProgress(4, '私有直通任务已进入独立执行池', 'running');
    else if (data.queue_position > 0) setProgress(2, `任务已进入队列，当前第 ${data.queue_position} 位`, 'queued');
    clearInterval(pollTimer); await poll(); pollTimer=setInterval(poll,1200);
  }catch(e){ setRunning(false); setProgress(100,e.message||String(e),'error'); }
});

$('cancelButton').addEventListener('click', async () => {
  if(!jobId) return;
  setRunning(false);
  setProgress(100,'任务已停止','cancelled');
  await fetch(`${API_BASE}/checkout-cancel`,{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id:jobId})
  });
});
$('copyResult').addEventListener('click', async () => { await navigator.clipboard.writeText($('resultValue').value || ''); const old=$('copyResult').textContent; $('copyResult').textContent='已复制'; setTimeout(()=>$('copyResult').textContent=old,1200); });

function applyTheme(dark){
  document.documentElement.classList.toggle('dark',dark);
  localStorage.setItem('pay153-theme',dark?'dark':'light');
  $('themeToggle').textContent = dark ? '☀' : '☾';
  $('themeToggle').setAttribute('aria-label', dark ? '切换到浅色模式' : '切换到深色模式');
}
const requestedTheme = new URLSearchParams(location.search).get('theme');
const saved=localStorage.getItem('pay153-theme');
applyTheme(requestedTheme ? requestedTheme === 'dark' : (saved ? saved==='dark' : matchMedia('(prefers-color-scheme: dark)').matches));
$('themeToggle').addEventListener('click',()=>applyTheme(!document.documentElement.classList.contains('dark')));
if (privateMode) {
  document.body.classList.add('private-mode');
  document.title = 'PAY.153 · 私有直通提链';
  const brand = document.querySelector('.brand');
  if (brand) brand.href = '/pay153/private-checkout';
  const modeLabel = document.querySelector('.form-panel .panel-heading .quiet');
  if (modeLabel) modeLabel.textContent = '私有直通工作台';
  const modeNote = document.querySelector('.local-mode-note');
  if (modeNote) modeNote.innerHTML = '<b>私有直通通道</b><span>使用独立任务执行池，与本地工作台任务互不占用并发槽位。</span>';
  const rateCard = document.querySelector('.hero-board > div:nth-child(2)');
  if (rateCard) rateCard.innerHTML = '<small>PRIVATE LANE</small><strong>DIRECT</strong><span>独立执行池</span>';
}
restoreProxyPools();
populatePaypalOaicsCountries();
restoreFormState();
syncFields(false);
updateProxyCount($('entryProxy'), $('entryProxyCount'));
updateProxyCount($('exitProxy'), $('exitProxyCount'));
const savedJobId = sessionStorage.getItem(JOB_STATE_KEY) || '';
if (savedJobId) {
  jobId = savedJobId;
  setRunning(true);
  poll().then(() => {
    if ($('submitButton').disabled) {
      clearInterval(pollTimer);
      pollTimer = setInterval(poll, 1200);
    }
  });
}
