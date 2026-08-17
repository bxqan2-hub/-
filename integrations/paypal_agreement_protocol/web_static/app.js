const API_BASE = '/paypal-pay/api';
const GROK_API_BASE = '/api/grok-trial';
const $ = (id) => document.getElementById(id);
const PRIVATE_BRAINTREE_SLUG = 'bt-vault-8f1d2e4c9a7b6d3e';
const privateBraintreeEnabled = location.pathname.includes(`/${PRIVATE_BRAINTREE_SLUG}`);

const state = {
  jobId: '',
  job: null,
  pollTimer: null,
  jobsTimer: null,
  browserTimer: null,
  browserFrameUrl: '',
  logPinned: true,
  renderedLogCount: 0,
  batchJobIds: [],
  batchJobs: [],
  batchPollTimer: null,
};

const vaultState = {
  session: null,
  paypalCheckout: null,
  rendered: false,
  generated: null,
};

const PROTOCOL_FORM_STATE_KEY = `paypal.protocol.form.${privateBraintreeEnabled ? 'private' : 'standard'}.v1`;
let protocolFormStateTimer = 0;
function protocolFormStateSnapshot() {
  const saved = {};
  $('protocolForm').querySelectorAll('input, textarea, select').forEach(node => {
    if ((!node.id && !node.name) || node.readOnly || ['button', 'submit', 'file'].includes(node.type)) return;
    const key = node.id || `name:${node.name}`;
    if (node.type === 'radio') {
      if (node.checked) saved[key] = node.value;
    } else if (node.type === 'checkbox') saved[key] = node.checked;
    else saved[key] = node.value;
  });
  return saved;
}

function saveProtocolFormState() {
  clearTimeout(protocolFormStateTimer);
  protocolFormStateTimer = setTimeout(() => {
    try { sessionStorage.setItem(PROTOCOL_FORM_STATE_KEY, JSON.stringify(protocolFormStateSnapshot())); } catch (_) {}
  }, 80);
}

function restoreProtocolFormState() {
  let saved = {};
  try { saved = JSON.parse(sessionStorage.getItem(PROTOCOL_FORM_STATE_KEY) || '{}'); } catch (_) {}
  $('protocolForm').querySelectorAll('input, textarea, select').forEach(node => {
    if ((!node.id && !node.name) || node.readOnly) return;
    const key = node.id || `name:${node.name}`;
    if (!Object.prototype.hasOwnProperty.call(saved, key)) return;
    if (node.type === 'radio') node.checked = saved[key] === node.value;
    else if (node.type === 'checkbox') node.checked = Boolean(saved[key]);
    else node.value = String(saved[key] ?? '');
  });
}

$('protocolForm').addEventListener('input', saveProtocolFormState);
$('protocolForm').addEventListener('change', saveProtocolFormState);

if (privateBraintreeEnabled) {
  $('privateBraintreeMode').hidden = false;
} else {
  $('protocolModeSwitch').style.gridTemplateColumns = '1fr';
}
$('vaultAccessToken').value = sessionStorage.getItem('pay153-braintree-access-token') || '';
$('vaultProxy').value = sessionStorage.getItem('pay153-braintree-proxy') || '';
$('vaultAccessToken').addEventListener('input', () => {
  sessionStorage.setItem('pay153-braintree-access-token', $('vaultAccessToken').value);
});
$('vaultProxy').addEventListener('input', () => {
  sessionStorage.setItem('pay153-braintree-proxy', $('vaultProxy').value.trim());
});

function applyTheme(mode) {
  const dark = mode === 'dark' || (mode === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.classList.toggle('dark', dark);
  $('themeToggle').textContent = dark ? '☀' : '◐';
}

const savedTheme = localStorage.getItem('pay153-theme') || 'system';
applyTheme(savedTheme);
$('themeToggle').addEventListener('click', () => {
  const next = document.documentElement.classList.contains('dark') ? 'light' : 'dark';
  localStorage.setItem('pay153-theme', next);
  applyTheme(next);
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if ((localStorage.getItem('pay153-theme') || 'system') === 'system') applyTheme('system');
});

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'same-origin',
    headers: options.body ? {'Content-Type': 'application/json'} : {},
    ...options,
  });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) { const error = new Error(data.error || `HTTP ${response.status}`); error.status = response.status; throw error; }
  return data;
}

function proxyLines() {
  return $('proxies').value.split(/\r?\n/).map(v => v.trim()).filter(v => v && !v.startsWith('#'));
}
function updateProxyCount() {
  const count = proxyLines().length;
  $('proxyCount').textContent = `${count} / 500`;
  $('proxyCount').classList.toggle('over-limit', count > 500);
}
$('proxies').addEventListener('input', updateProxyCount);

function baPoolLines() {
  return $('baToken').value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
}
function phonePoolLines() {
  return $('phone').value.split(/\r?\n/).map(value => normalizePhone(value)).filter(Boolean);
}
function updatePairCounts() {
  const baCount = baPoolLines().length;
  const phoneCount = phonePoolLines().length;
  $('baPoolCount').textContent = `${baCount} / 20`;
  $('phonePoolCount').textContent = `${phoneCount} / 20`;
  $('baPoolCount').classList.toggle('over-limit', baCount > 20);
  $('phonePoolCount').classList.toggle('over-limit', phoneCount > 20);
}
$('baToken').addEventListener('input', updatePairCounts);
$('phone').addEventListener('input', updatePairCounts);

let paypalCountries = [];
let dynamicCountriesEnabled = false;

async function updateCountrySchemaHint(country) {
  const hint = $('countrySchemaHint');
  if (!hint || !country) return;
  const selected = $('paypalCountry').selectedOptions[0];
  const cached = selected?.dataset.schemaCached === '1';
  if (!cached) {
    hint.textContent = '\u65b0\u56fd\u5bb6 \u00b7 \u4efb\u52a1\u5f00\u59cb\u65f6\u5b9e\u65f6\u89e3\u6790\u5730\u533a\u5b57\u6bb5\u4e0e\u5730\u5740\u89c4\u5219';
    return;
  }
  try {
    const data = await api(`/country-fields?country=${encodeURIComponent(country)}`);
    const schema = data.fields || {};
    const required = (schema.address_fields || []).filter(v => v.required).map(v => v.paypal_name).join('\u3001') || '\u6309\u9875\u9762\u914d\u7f6e';
    const status = schema.implementation_status === 'live' ? '\u5df2\u9a8c\u8bc1\u534f\u8bae' : '\u5df2\u6709\u5b57\u6bb5\u7f13\u5b58';
    hint.textContent = `${status} \u00b7 ${schema.locale || country} \u00b7 ${schema.currency || '\u2014'} \u00b7 \u5730\u5740\u5fc5\u586b\uff1a${required}`;
  } catch (_) {
    hint.textContent = '\u5b57\u6bb5\u7f13\u5b58\u672a\u547d\u4e2d\uff0c\u4efb\u52a1\u5f00\u59cb\u65f6\u5b9e\u65f6\u89e3\u6790';
  }
}

function updateCountryFields() {
  const select = $('paypalCountry');
  const country = select.value;
  const option = select.selectedOptions[0];
  if (!country || !option) return;
  const zh = option.dataset.zh || country;
  const calling = option.dataset.calling || '+';
  $('dynamicCountryNotice').hidden = option.dataset.live === '1';
  $('countryPickerValue').textContent = `${country} \u00b7 ${zh} \u00b7 ${option.dataset.en || ''}`;
  $('phoneLabel').textContent = `${zh}\u624b\u673a\u53f7\u6c60`;
  $('phone').placeholder = `\u6bcf\u884c\u4e00\u4e2a ${calling} \u624b\u673a\u53f7\uff0c\u987a\u5e8f\u4e0e BA \u94fe\u6c60\u4e00\u81f4`;
  $('phone').required = true;
  $('phone').disabled = false;
  $('proxyCountryHint').textContent = `\u63a8\u8350\u586b\u5199 ${country}`;
  updateCountrySchemaHint(country);
}

function populateCountrySelect() {
  const select = $('paypalCountry');
  select.replaceChildren();
  paypalCountries.forEach(item => {
    const option = document.createElement('option');
    option.value = item.code;
    option.textContent = `${item.code} \u00b7 ${item.name_zh} \u00b7 ${item.name_en}`;
    option.dataset.zh = item.name_zh;
    option.dataset.en = item.name_en;
    option.dataset.calling = item.calling_code || '+';
    option.dataset.live = item.verified ? '1' : '0';
    option.dataset.schemaCached = item.schema_cached ? '1' : '0';
    select.appendChild(option);
  });
  select.value = paypalCountries.some(v => v.code === 'BR') ? 'BR' : (paypalCountries[0]?.code || '');
}
function closeCountryPicker() { $('countryPickerMenu').hidden = true; $('countryPickerToggle').setAttribute('aria-expanded', 'false'); }
function openCountryPicker() { $('countryPickerMenu').hidden = false; $('countryPickerToggle').setAttribute('aria-expanded', 'true'); $('countrySearch').value = ''; renderCountryOptionList(''); requestAnimationFrame(() => $('countrySearch').focus()); }
function renderCountryOptionList(query = '') {
  const list = $('countryOptionList');
  const selectedCode = $('paypalCountry').value;
  const q = query.trim().toLowerCase();
  const exactCode = q && paypalCountries.find(item => item.code.toLowerCase() === q);
  const matches = exactCode ? [exactCode] : paypalCountries.filter(item => `${item.code} ${item.name_zh} ${item.name_en}`.toLowerCase().includes(q));
  list.replaceChildren();
  const addRows = (label, rows) => {
    if (!rows.length) return;
    const heading = document.createElement('div'); heading.className = 'country-option-heading'; heading.textContent = label; list.appendChild(heading);
    rows.forEach(item => {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'country-option'; button.setAttribute('role', 'option'); button.setAttribute('aria-selected', item.code === selectedCode ? 'true' : 'false'); button.dataset.code = item.code;
      const main = document.createElement('span');
      const code = document.createElement('b'); code.textContent = item.code;
      const chinese = document.createElement('strong'); chinese.textContent = item.name_zh;
      main.append(code, chinese);
      const english = document.createElement('small'); english.textContent = item.name_en;
      button.append(main, english);
      button.addEventListener('click', () => { $('paypalCountry').value = item.code; updateCountryFields(); saveProtocolFormState(); closeCountryPicker(); });
      list.appendChild(button);
    });
  };
  addRows('\u5df2\u9a8c\u8bc1\u534f\u8bae\u652f\u4ed8', matches.filter(v => v.verified));
  addRows('\u6309\u9700\u5b9e\u65f6\u89e3\u6790', matches.filter(v => !v.verified));
  if (!matches.length) { const empty = document.createElement('div'); empty.className = 'country-option-empty'; empty.textContent = '\u6ca1\u6709\u5339\u914d\u7684\u56fd\u5bb6'; list.appendChild(empty); }
}
async function loadPayPalCountries() { const data = await api('/supported-countries'); paypalCountries = Array.isArray(data.countries) ? data.countries : []; dynamicCountriesEnabled = data.dynamic_countries_enabled === true; populateCountrySelect(); renderCountryOptionList(''); updateCountryFields(); }
$('countryPickerToggle').addEventListener('click', () => { if ($('countryPickerMenu').hidden) openCountryPicker(); else closeCountryPicker(); });
$('countrySearch').addEventListener('input', () => renderCountryOptionList($('countrySearch').value));
$('countrySearch').addEventListener('keydown', event => { if (event.key === 'Escape') { closeCountryPicker(); $('countryPickerToggle').focus(); } });
$('paypalCountry').addEventListener('change', updateCountryFields);
document.addEventListener('click', event => { if (!$('countryPicker').contains(event.target)) closeCountryPicker(); });
const paypalCountriesReady = loadPayPalCountries().catch(() => { $('countrySchemaHint').textContent = '\u56fd\u5bb6\u76ee\u5f55\u52a0\u8f7d\u5931\u8d25'; });

$('buyerMode').addEventListener('change', () => {
  const elevated = $('buyerMode').value === 'identity_elevation';
  $('buyerModeHint').value = elevated
    ? '注册后提升 Guest 身份并绑定当前 EC，再提交授权'
    : '沿用当前协议流程，兼容性优先';
});

function normalizePhone(value) {
  return value.replace(/[\s().-]+/g, '');
}
function extractBa(value) {
  const match = value.trim().match(/BA-[A-Za-z0-9]{8,80}/);
  return match ? match[0] : value.trim();
}

function statusMeta(status) {
  const map = {
    queued: ['排队', 'queued'], running: ['运行中', 'running'], awaiting_otp: ['等待验证码', 'awaiting'], awaiting_captcha: ['等待手动验证', 'awaiting'],
    cancelling: ['正在停止', 'queued'], cancelled: ['已停止', 'cancelled'], completed: ['已完成', 'done'], failed: ['失败', 'error'],
  };
  return map[status] || ['等待', 'idle'];
}

function progressFor(job) {
  if (!job) return 0;
  if (job.status === 'completed') return 100;
  if (job.status === 'queued') return 5;
  if (job.status === 'cancelled') return Math.max(5, progressFromStage(job.stage));
  if (job.status === 'failed') return Math.max(8, progressFromStage(job.stage));
  if (job.status === 'awaiting_captcha') return 16;
  if (job.status === 'awaiting_otp') return 78;
  return progressFromStage(job.stage);
}
function progressFromStage(stage = '') {
  if (/Phase 4|最终授权/.test(stage)) return 92;
  if (/Phase 3|短信验证|注册/.test(stage)) return 72;
  if (/Phase 2|创建账号/.test(stage)) return 54;
  if (/Phase 1|风控|指纹/.test(stage)) return 36;
  if (/Phase 0|协议页/.test(stage)) return 18;
  if (/生成/.test(stage)) return 10;
  if (/收到输入/.test(stage)) return 82;
  return 8;
}

function setProgress(percent, text, stage) {
  const p = Math.max(0, Math.min(100, Math.round(percent)));
  $('progressValue').textContent = `${p}%`;
  $('progressText').textContent = text || '处理中';
  $('progressStage').textContent = stage || '运行中';
  $('progressBar').style.width = `${p}%`;
  $('orbitValue').style.strokeDashoffset = `${320.44 * (1 - p / 100)}`;
}

function isMajorLog(log) {
  const level = String(log.level || '').toUpperCase();
  if (level === 'ERROR' || level === 'WARNING' || level === 'WARN' || level === 'SUCCESS') return true;
  const message = String(log.message || '');
  return /Phase\s*[0-4]|协议支付任务|SMS|OTP|verification code|account|authorization|authorize|completed|success|failed|验证码|短信|授权|注册/.test(message);
}
function formatTime(ts) {
  if (!ts) return '--:--:--';
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', {hour12: false});
}
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function renderLogs(logs = []) {
  const box = $('logBox');
  const keepTop = box.scrollTop;
  const wasPinned = state.logPinned || (box.scrollHeight - box.scrollTop - box.clientHeight < 32);
  const major = logs.filter(isMajorLog).slice(-100);
  if (!major.length) {
    box.innerHTML = '<div class="empty-log">任务已开始，正在等待主要步骤。</div>';
    return;
  }
  box.innerHTML = major.map(item => {
    const level = String(item.level || '').toLowerCase();
    const cls = level === 'error' ? 'error' : (level === 'warning' || level === 'warn' ? 'warn' : '');
    return `<div class="log-row ${cls}"><time>${formatTime(item.time)}</time><span>${escapeHtml(item.message)}</span></div>`;
  }).join('');
  if (wasPinned) box.scrollTop = box.scrollHeight;
  else box.scrollTop = keepTop;
}
$('logBox').addEventListener('scroll', () => {
  const box = $('logBox');
  state.logPinned = box.scrollHeight - box.scrollTop - box.clientHeight < 32;
});

function renderResult(job) {
  const terminal = ['completed', 'failed', 'cancelled'].includes(job.status);
  $('resultPanel').hidden = !terminal;
  if (!terminal) return;
  const result = job.result || {};
  $('resultStatus').textContent = job.status === 'completed' ? '授权完成' : (job.status === 'cancelled' ? '任务已停止' : '执行失败');
  $('paymentAction').textContent = result.payment_action || result.status || '—';
  const settlementLabels = {
    confirmed: '已确认到账',
    pending_verification: '等待到账确认',
    authorization_only: '仅协议授权',
  };
  $('settlementStatus').textContent = settlementLabels[result.settlement_status] || '待检查';
  $('billingCountry').textContent = result.billing_country || result.region || job.country || '—';
  const verificationUrl = String(result.pending_url || result.verification_url || '');
  const verifyLink = $('openVerification');
  if (/^https:\/\/(?:chatgpt\.com|chat\.openai\.com|pay\.openai\.com)\//i.test(verificationUrl)) {
    verifyLink.href = verificationUrl;
    verifyLink.textContent = '打开 OpenAI Pending 页面';
    verifyLink.hidden = false;
  } else {
    verifyLink.removeAttribute('href');
    verifyLink.hidden = true;
  }
  $('buyerId').textContent = result.user_id || result.buyer_id || '—';
  $('resultBa').textContent = result.ba_token || job.ba_token || '—';
  const payload = job.status === 'failed'
    ? {...(job.result || {}), status: job.status, error: job.error || job.result?.error, stage: job.stage}
    : (job.result || {status: job.status});
  $('resultValue').value = JSON.stringify(payload, null, 2);
  $('resultSeal').textContent = job.status === 'completed' ? 'READY' : (job.status === 'cancelled' ? 'STOP' : 'ERROR');
}

function renderJob(job) {
  state.job = job;
  state.jobId = job.id;
  sessionStorage.setItem('paypal-protocol-job', job.id);
  const [label, cls] = statusMeta(job.status);
  $('statusBadge').textContent = label;
  $('statusBadge').className = `status-badge ${cls}`;
  setProgress(progressFor(job), job.stage || label, job.status === 'awaiting_otp' ? '需要操作' : label);
  $('cancelButton').hidden = !job.cancellable;
  $('cancelButton').disabled = job.status === 'cancelling';
  $('submitButton').disabled = job.cancellable;
  $('otpPanel').hidden = !job.awaiting_otp;
  $('captchaPanel').hidden = !job.awaiting_captcha;
  if (job.awaiting_otp) {
    $('otpPrompt').textContent = job.awaiting_prompt || '请输入 6 位验证码，也可填写新手机号重新发送。';
  }
  if (job.awaiting_captcha) {
    const challengeUrl = String(job.challenge_url || '').trim();
    const captchaLink = $('captchaLink');
    $('captchaPrompt').textContent = job.awaiting_prompt || '打开真实验证地址完成验证，再粘贴 datadome Cookie 或 adsddtoken。';
    if (/^https:\/\/([a-z0-9-]+\.)*captcha-delivery\.com\/captcha(?:\/|\?|$)/i.test(challengeUrl)) {
      captchaLink.href = challengeUrl;
      captchaLink.hidden = false;
      captchaLink.textContent = '打开真实验证地址';
    } else {
      captchaLink.removeAttribute('href');
      captchaLink.hidden = true;
    }
    const browserActive = Boolean(job.browser_active);
    $('browserPanel').hidden = !browserActive;
    $('captchaFallback').hidden = browserActive;
    if (browserActive) {
      const browserState = job.browser_state || {};
      $('browserStatus').textContent = `${browserState.message || '服务器临时 Chromium 运行中'}${browserState.http_status ? ` · HTTP ${browserState.http_status}` : ''}`;
      scheduleBrowserFrame();
    } else {
      stopBrowserFrame();
    }
  } else {
    $('captchaLink').removeAttribute('href');
    $('captchaLink').hidden = true;
    $('browserPanel').hidden = true;
    $('captchaFallback').hidden = false;
    stopBrowserFrame();
  }
  renderLogs(job.logs || []);
  renderResult(job);
}

function stopBrowserFrame() {
  clearTimeout(state.browserTimer);
  state.browserTimer = null;
  if (state.browserFrameUrl) {
    URL.revokeObjectURL(state.browserFrameUrl);
    state.browserFrameUrl = '';
  }
}

function scheduleBrowserFrame() {
  clearTimeout(state.browserTimer);
  state.browserTimer = setTimeout(refreshBrowserFrame, 180);
}

async function refreshBrowserFrame() {
  if (!state.jobId || !state.job?.browser_active) return stopBrowserFrame();
  const image = $('browserFrame');
  image.src = `${API_BASE}/jobs/${encodeURIComponent(state.jobId)}/browser/frame?t=${Date.now()}`;
  state.browserTimer = setTimeout(refreshBrowserFrame, 700);
}

async function browserAction(payload) {
  if (!state.jobId || !state.job?.browser_active) return;
  const data = await api(`/jobs/${encodeURIComponent(state.jobId)}/browser/action`, {
    method: 'POST', body: JSON.stringify(payload),
  });
  if (data.job) renderJob(data.job);
}

$('browserFrame').addEventListener('click', async event => {
  const image = $('browserFrame');
  if (!image.naturalWidth || !image.naturalHeight) return;
  const rect = image.getBoundingClientRect();
  const x = (event.clientX - rect.left) * image.naturalWidth / rect.width;
  const y = (event.clientY - rect.top) * image.naturalHeight / rect.height;
  try { await browserAction({type: 'click', x, y}); } catch (error) { showClientError(error.message); }
});

document.querySelectorAll('[data-browser-action]').forEach(button => {
  button.addEventListener('click', async () => {
    const action = button.dataset.browserAction;
    const payload = action === 'reload'
      ? {type: 'reload'}
      : {type: 'scroll', delta_y: action === 'scroll-up' ? -600 : 600};
    try { await browserAction(payload); } catch (error) { showClientError(error.message); }
  });
});

document.querySelectorAll('[data-browser-key]').forEach(button => {
  button.addEventListener('click', async () => {
    try { await browserAction({type: 'key', key: button.dataset.browserKey}); }
    catch (error) { showClientError(error.message); }
  });
});

$('browserType').addEventListener('click', async () => {
  const value = $('browserText').value;
  if (!value) return;
  try {
    await browserAction({type: 'text', value});
    $('browserText').value = '';
  } catch (error) { showClientError(error.message); }
});

$('browserFinish').addEventListener('click', async () => {
  try { await browserAction({type: 'finish'}); }
  catch (error) { showClientError(error.message); }
});

function clearMissingJob(message = '') {
  clearTimeout(state.pollTimer);
  stopBrowserFrame();
  sessionStorage.removeItem('paypal-protocol-job');
  state.jobId = '';
  state.job = null;
  state.renderedLogCount = 0;
  $('otpPanel').hidden = true;
  $('captchaPanel').hidden = true;
  $('browserPanel').hidden = true;
  $('cancelButton').hidden = true;
  $('cancelButton').disabled = false;
  $('submitButton').disabled = false;
  $('resultPanel').hidden = true;
  $('logBox').innerHTML = '<div class="empty-log">\u5f53\u524d\u4efb\u52a1\u5df2\u7ed3\u675f\u6216\u670d\u52a1\u91cd\u542f\uff0c\u8bf7\u91cd\u65b0\u63d0\u4ea4\u3002</div>';
  $('statusBadge').textContent = '\u7b49\u5f85';
  $('statusBadge').className = 'status-badge idle';
  setProgress(0, message || '\u7b49\u5f85\u521b\u5efa\u65b0\u4efb\u52a1', 'IDLE');
}

async function loadJob(id, quiet = false) {
  if (!id) return;
  try {
    const job = await api(`/jobs/${encodeURIComponent(id)}?log_offset=0`);
    renderJob(job);
    if (!['completed','failed','cancelled'].includes(job.status)) schedulePoll();
    else clearTimeout(state.pollTimer);
  } catch (error) {
    if (Number(error.status) === 404) {
      clearMissingJob('\u65e7\u4efb\u52a1\u5df2\u7ecf\u5931\u6548\uff0c\u8bf7\u91cd\u65b0\u63d0\u4ea4');
      await refreshJobs();
      return;
    }
    if (!quiet) showClientError(error.message);
  }
}
function schedulePoll() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    await loadJob(state.jobId, true);
  }, 1000);
}

function showClientError(message) {
  $('statusBadge').textContent = '失败';
  $('statusBadge').className = 'status-badge error';
  $('progressText').textContent = message;
  $('progressStage').textContent = '请求失败';
  $('batchRunPanel').hidden = false;
  $('batchRunSummary').textContent = message || '请求失败';
  $('batchRunPercent').textContent = '错误';
  $('batchOverallBar').style.width = '0%';
}

function protocolMode() {
  return document.querySelector('input[name="protocolMode"]:checked')?.value || 'legacy';
}

function updateProtocolMode() {
  const vault = protocolMode() === 'braintree';
  $('braintreeVaultFields').hidden = !vault;
  $('legacyTokenField').hidden = vault;
  $('legacyCountryFields').hidden = vault;
  $('legacyProxyFields').hidden = vault;
  $('legacyActions').hidden = vault;
  document.querySelectorAll('#protocolModeSwitch label').forEach(label => {
    label.classList.toggle('active', label.querySelector('input')?.checked);
  });
  if (vault) {
    setProgress(0, '等待加载英国 Braintree Vault', 'Braintree 模式');
    $('statusBadge').textContent = 'VAULT';
    $('statusBadge').className = 'status-badge idle';
  }
}

document.querySelectorAll('input[name="protocolMode"]').forEach(input => input.addEventListener('change', updateProtocolMode));

function updateVaultRegion() {
  const region = $('vaultRegion').value;
  const locale = region === 'GB' ? 'en_GB' : 'en_US';
  $('vaultParameterBox').innerHTML = [
    `账单国家=${region}`, `代理出口=待检测`, `buyerCountry=${region}`, `locale=${locale}`,
    'vault=true', 'intent=tokenize', 'fundingSource=paypal',
  ].map(value => `<span>${value}</span>`).join('');
  $('loadVaultButton').querySelector('span').textContent = `生成${region === 'GB' ? '英国' : '美国'} Braintree BA 链接`;
}

$('vaultRegion').addEventListener('change', updateVaultRegion);
updateVaultRegion();
updateProtocolMode();

async function grokApi(path, options = {}) {
  const response = await fetch(`${GROK_API_BASE}${path}`, {
    headers: {'Content-Type': 'application/json', 'X-Grok-Access-Token': $('vaultAccessToken').value.trim()},
    ...options,
  });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data.result;
}

function renderVaultResult(result) {
  const verification = result?.verification || {};
  const active = Boolean(verification.activated);
  $('resultPanel').hidden = false;
  $('resultStatus').textContent = active ? 'SuperGrok 已到账' : '订阅已提交，等待同步';
  $('paymentAction').textContent = result?.payment_action || 'BRAINTREE_VAULT';
  $('settlementStatus').textContent = active ? '已确认到账' : '等待到账确认';
  $('billingCountry').textContent = result?.billing_country || $('vaultRegion').value || '—';
  $('buyerId').textContent = $('vaultAccountId').value.trim() || '—';
  $('resultBa').textContent = vaultState.session?.campaign_id || '—';
  $('resultValue').value = JSON.stringify(result, null, 2);
  $('resultSeal').textContent = active ? 'READY' : 'PENDING';
  $('statusBadge').textContent = active ? '完成' : '同步中';
  $('statusBadge').className = `status-badge ${active ? 'done' : 'awaiting'}`;
  setProgress(active ? 100 : 92, active ? 'Braintree PayPal 订阅已激活' : 'nonce 已提交，等待 Grok 同步', active ? '已到账' : '订阅同步');
}

$('loadVaultButton').addEventListener('click', async () => {
  const accountId = $('vaultAccountId').value.trim();
  const region = $('vaultRegion').value;
  const proxy = $('vaultProxy').value.trim();
  const accessToken = $('vaultAccessToken').value.trim();
  if (!accessToken) return showClientError('请填写私有访问令牌');
  if (!proxy) return showClientError('请填写与账单国家一致的协议生成代理');
  const button = $('loadVaultButton');
  button.disabled = true;
  $('vaultStatus').textContent = '正在校验代理并生成 Braintree BA 链接…';
  setProgress(20, '正在生成 Braintree BA 链接', 'Vault 初始化');
  try {
    const generated = await grokApi('/braintree-link', {method:'POST', body:JSON.stringify({account_id:accountId, region, proxy})});
    $('resultPanel').hidden = false;
    $('resultStatus').textContent = 'Braintree BA 链接已生成';
    $('paymentAction').textContent = 'BRAINTREE_VAULT_LINK';
    $('settlementStatus').textContent = '等待 PayPal 授权并回传 Grok';
    $('billingCountry').textContent = `${generated.billing_country || region} · 代理出口 ${generated.proxy_country || '未知'}${generated.proxy_city ? ' / ' + generated.proxy_city : ''}`;
    $('buyerId').textContent = generated.account_id || accountId || '—';
    if (generated.account_id) $('vaultAccountId').value = generated.account_id;
    $('resultBa').textContent = generated.billing_token || '—';
    $('resultValue').value = generated.approval_url || '';
    $('resultSeal').textContent = 'WAIT';
    vaultState.generated = generated;
    $('vaultReturnPanel').hidden = false;
    $('statusBadge').textContent = '等待授权';
    $('statusBadge').className = 'status-badge awaiting';
    const openLink = $('openVerification');
    openLink.href = generated.approval_url || '#';
    openLink.textContent = '打开 PayPal 授权链接';
    openLink.hidden = !generated.approval_url;
    $('vaultParameterBox').innerHTML = [
      `账单国家=${generated.billing_country || region}`,
      `代理出口=${generated.proxy_country || '未知'}${generated.proxy_city ? '/' + generated.proxy_city : ''}`,
      `buyerCountry=${region}`, 'vault=true', 'intent=tokenize'
    ].map(value => `<span>${value}</span>`).join('');
    $('vaultStatus').textContent = 'BA 链接已返回。点击右侧“打开 PayPal 授权链接”，或复制结果后自行打开。';
    setProgress(50, 'BA 链接已生成，等待 PayPal 授权结果', '等待授权');
  } catch (error) {
    $('vaultStatus').textContent = `生成失败：${error.message || error}`;
    showClientError(error.message || String(error));
  } finally {
    button.disabled = false;
  }
});


function extractVaultPayerId(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  try {
    const parsed = new URL(raw);
    return parsed.searchParams.get('PayerID') || parsed.searchParams.get('payerId') || parsed.searchParams.get('payer_id') || '';
  } catch (_) {
    const match = raw.match(/(?:PayerID|payerId|payer_id)=([^&#\s]+)/i);
    return match ? decodeURIComponent(match[1]) : raw;
  }
}

$('completeVaultButton').addEventListener('click', async () => {
  const generated = vaultState.generated;
  if (!generated?.billing_token || !generated?.account_id) return showClientError('请先生成 Braintree BA 链接');
  const payerId = extractVaultPayerId($('vaultReturnValue').value);
  if (!payerId) return showClientError('请粘贴 PayPal 授权完成后的 URL 或 Payer ID');
  const button = $('completeVaultButton');
  button.disabled = true;
  $('vaultStatus').textContent = '正在将 PayPal 授权回传 Braintree 并提交 Grok…';
  setProgress(72, '正在生成 payment method nonce', 'Braintree tokenize');
  try {
    const result = await grokApi('/braintree-complete', {method:'POST', body:JSON.stringify({
      account_id: generated.account_id,
      billing_token: generated.billing_token,
      payer_id: payerId,
      region: generated.region || $('vaultRegion').value,
      plan_id: generated.plan_id || 'supergrok_monthly',
      campaign_id: generated.campaign_id || '',
      proxy: $('vaultProxy').value.trim(),
    })});
    renderVaultResult(result);
    $('vaultStatus').textContent = result?.verification?.activated ? '已回传 Grok，订阅状态已激活。' : '已回传 Grok，正在等待订阅状态同步。';
  } catch (error) {
    $('vaultStatus').textContent = `回传失败：${error.message || error}`;
    showClientError(error.message || String(error));
  } finally {
    button.disabled = false;
  }
});

function phoneValidationError(phone, country) {
  if (!/^\+?\d{8,20}$/.test(phone)) return '手机号格式不正确';
  const rules = {
    BR: [/^\+?55\d{8,15}$/, '巴西号码需以 +55 开头'],
    GB: [/^\+?44\d{9,12}$/, '英国号码需以 +44 开头'],
    US: [/^\+?1\d{10}$/, '美国号码需为 +1 后 10 位'],
    CA: [/^\+?1[2-9]\d{9}$/, '加拿大号码需为 +1 后 10 位'],
    JP: [/^\+?81\d{9,11}$/, '日本号码需以 +81 开头'],
    TH: [/^\+?66[689]\d{8}$/, '泰国号码需为 +66 后 9 位'],
    ID: [/^\+?628\d{8,11}$/, '印尼号码需为 +62 8xx'],
    PH: [/^\+?639\d{9}$/, '菲律宾号码需为 +63 9xx'],
    TW: [/^\+?8869\d{8}$/, '中国台湾号码需为 +886 9xx'],
    MX: [/^\+?52\d{10}$/, '墨西哥号码需为 +52 后 10 位'],
    AE: [/^\+?9715[024568]\d{7}$/, '阿联酋号码需为 +971 5x'],
    AU: [/^\+?614\d{8}$/, '澳大利亚号码需为 +61 4xx'],
  };
  const rule = rules[country];
  return rule && !rule[0].test(phone) ? rule[1] : '';
}

function captureBatchInputValues() {
  const values = {};
  document.querySelectorAll('.batch-code-input').forEach(input => { values[input.dataset.jobId] = input.value; });
  return values;
}

function renderBatchJobs(jobs) {
  state.batchJobs = [...jobs].sort((a, b) => (a.batch_index || 0) - (b.batch_index || 0));
  const panel = $('batchRunPanel');
  panel.hidden = false;
  const terminal = job => ['completed', 'failed', 'cancelled'].includes(job.status);
  const finished = jobs.filter(terminal).length;
  const success = jobs.filter(job => job.status === 'completed').length;
  const failed = jobs.filter(job => job.status === 'failed').length;
  const progressValues = jobs.map(job => terminal(job) ? 100 : progressFor(job));
  const overall = jobs.length ? Math.round(progressValues.reduce((sum, value) => sum + value, 0) / jobs.length) : 0;
  $('batchRunPercent').textContent = `${overall}%`;
  $('batchOverallBar').style.width = `${overall}%`;
  $('batchRunSummary').textContent = `共 ${jobs.length} 组 · 已结束 ${finished} · 成功 ${success} · 失败 ${failed}`;
  const pendingValues = captureBatchInputValues();
  const activeInput = document.activeElement?.classList?.contains('batch-code-input') ? document.activeElement.dataset.jobId : '';
  const activeSelection = activeInput ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
  $('batchJobGrid').innerHTML = state.batchJobs.map((job, zeroIndex) => {
    const [label, cls] = statusMeta(job.status);
    const percent = progressFor(job);
    const index = job.batch_index || zeroIndex + 1;
    const waitingOtp = job.status === 'awaiting_otp';
    const waitingCaptcha = job.status === 'awaiting_captcha';
    const codeAction = waitingOtp ? 'otp' : (waitingCaptcha ? 'captcha' : '');
    const prompt = job.awaiting_prompt || (waitingOtp ? '填写短信验证码' : '填写验证结果');
    const codeValue = pendingValues[job.id] || '';
    const resultText = terminal(job) ? JSON.stringify(job.result || (job.error ? {error: job.error} : {status: job.status}), null, 2) : '';
    const latestLogs = (job.logs || []).filter(isMajorLog).slice(-3);
    return `<article class="batch-job-card ${escapeHtml(cls)}" data-job-card="${escapeHtml(job.id)}">
      <div class="batch-card-head"><div><b>#${index} · ${escapeHtml(job.ba_token || job.id)}</b><small>${escapeHtml(job.phone || '')}</small></div><span class="status-badge ${escapeHtml(cls)}">${escapeHtml(label)}</span></div>
      <div class="batch-card-progress"><i style="width:${percent}%"></i></div>
      <div class="batch-card-stage"><span>${percent}%</span><b>${escapeHtml(job.stage || label)}</b></div>
      ${codeAction ? `<div class="batch-code-row"><input class="batch-code-input" data-job-id="${escapeHtml(job.id)}" autocomplete="one-time-code" placeholder="${escapeHtml(prompt)}" value="${escapeHtml(codeValue)}"><button type="button" data-batch-code="${escapeHtml(codeAction)}" data-job-id="${escapeHtml(job.id)}">提交</button></div>` : ''}
      ${waitingCaptcha && /^https:\/\//i.test(job.challenge_url || '') ? `<a class="batch-challenge-link" href="${escapeHtml(job.challenge_url)}" target="_blank" rel="noopener noreferrer">打开验证地址</a>` : ''}
      ${latestLogs.length ? `<div class="batch-mini-log">${latestLogs.map(item => `<p>${escapeHtml(item.message)}</p>`).join('')}</div>` : ''}
      ${resultText ? `<details class="batch-result"><summary>查看结果</summary><pre>${escapeHtml(resultText)}</pre></details>` : ''}
      ${job.cancellable ? `<button class="batch-stop-one" type="button" data-batch-cancel="${escapeHtml(job.id)}">停止此任务</button>` : ''}
    </article>`;
  }).join('');
  document.querySelectorAll('[data-batch-code]').forEach(button => button.addEventListener('click', () => submitBatchCode(button.dataset.jobId, button.dataset.batchCode)));
  document.querySelectorAll('[data-batch-cancel]').forEach(button => button.addEventListener('click', () => cancelBatchJob(button.dataset.batchCancel)));
  if (activeInput) {
    const input = document.querySelector(`.batch-code-input[data-job-id="${CSS.escape(activeInput)}"]`);
    if (input) { input.focus(); if (activeSelection) input.setSelectionRange(...activeSelection); }
  }
  const hasActive = jobs.some(job => !terminal(job));
  $('cancelBatchButton').hidden = !hasActive;
  $('submitButton').disabled = hasActive;
}

async function refreshBatchJobs() {
  clearTimeout(state.batchPollTimer);
  if (!state.batchJobIds.length) return;
  const settled = await Promise.allSettled(state.batchJobIds.map(id => api(`/jobs/${encodeURIComponent(id)}?log_offset=0`)));
  const jobs = settled.filter(item => item.status === 'fulfilled').map(item => item.value);
  if (!jobs.length) {
    sessionStorage.removeItem('paypal-protocol-batch');
    state.batchJobIds = [];
    return;
  }
  renderBatchJobs(jobs);
  const active = jobs.some(job => !['completed', 'failed', 'cancelled'].includes(job.status));
  if (active) state.batchPollTimer = setTimeout(refreshBatchJobs, 1800);
  else {
    $('submitButton').disabled = false;
    $('cancelBatchButton').hidden = true;
    sessionStorage.removeItem('paypal-protocol-batch');
  }
}

function startBatch(jobs) {
  const list = Array.isArray(jobs) ? jobs : [];
  state.batchJobIds = list.map(job => typeof job === 'string' ? job : job.id).filter(Boolean);
  if (!state.batchJobIds.length) return;
  sessionStorage.setItem('paypal-protocol-batch', JSON.stringify(state.batchJobIds));
  if (list[0] && typeof list[0] === 'object') renderBatchJobs(list);
  refreshBatchJobs();
}

async function submitBatchCode(jobId, action) {
  const input = document.querySelector(`.batch-code-input[data-job-id="${CSS.escape(jobId)}"]`);
  const value = input?.value.trim();
  if (!value) return;
  try {
    await api(`/jobs/${encodeURIComponent(jobId)}/${action}`, {method:'POST', body:JSON.stringify({value})});
    if (input) input.value = '';
    await refreshBatchJobs();
  } catch (error) { showClientError(error.message); }
}

async function cancelBatchJob(jobId) {
  try {
    await api(`/jobs/${encodeURIComponent(jobId)}/cancel`, {method:'POST', body:'{}'});
    await refreshBatchJobs();
  } catch (error) { showClientError(error.message); }
}

$('cancelBatchButton').addEventListener('click', async () => {
  $('cancelBatchButton').disabled = true;
  await Promise.allSettled(state.batchJobs.filter(job => job.cancellable).map(job => api(`/jobs/${encodeURIComponent(job.id)}/cancel`, {method:'POST', body:'{}'})));
  $('cancelBatchButton').disabled = false;
  await refreshBatchJobs();
});

$('protocolForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (protocolMode() === 'braintree') return;
  const baValues = baPoolLines();
  const selectedCountryOption = $('paypalCountry').selectedOptions[0];
  if (selectedCountryOption?.dataset.live !== '1' && !dynamicCountriesEnabled) return showClientError('\u8be5\u56fd\u5bb6\u5df2\u8fdb\u5165\u5b9e\u65f6\u89e3\u6790\u76ee\u5f55\uff0c\u52a8\u6001\u56fd\u5bb6\u6267\u884c\u5f00\u5173\u5f53\u524d\u5173\u95ed');
  const phones = phonePoolLines();
  const country = $('paypalCountry').value;
  const proxies = proxyLines();
  if (!baValues.length) return showClientError('请填写 BA 链池');
  if (baValues.length > 20) return showClientError('BA 链池最多支持 20 条');
  if (phones.length > 20) return showClientError('手机号池最多支持 20 条');
  if (baValues.length !== phones.length) return showClientError(`BA 链池与手机号池数量必须一致（当前 ${baValues.length} / ${phones.length}）`);
  const baTokens = baValues.map(extractBa);
  const invalidBaIndex = baTokens.findIndex(value => !/^BA-[A-Za-z0-9]{8,80}$/.test(value));
  if (invalidBaIndex >= 0) return showClientError(`第 ${invalidBaIndex + 1} 条 BA 链格式不正确`);
  if (new Set(baTokens).size !== baTokens.length) return showClientError('BA 链池中存在重复 BA Token');
  const invalidPhoneIndex = phones.findIndex(phone => phoneValidationError(phone, country));
  if (invalidPhoneIndex >= 0) return showClientError(`第 ${invalidPhoneIndex + 1} 个手机号：${phoneValidationError(phones[invalidPhoneIndex], country)}`);
  if (!proxies.length) return showClientError(`请至少填写一条 ${country} 代理`);
  if (proxies.length > 500) return showClientError('代理池最多支持 500 条');
  $('submitButton').disabled = true;
  $('batchRunPanel').hidden = false;
  $('batchRunSummary').textContent = `正在创建 ${baValues.length} 个独立任务…`;
  $('batchRunPercent').textContent = '0%';
  $('batchOverallBar').style.width = '0%';
  $('batchJobGrid').innerHTML = '';
  setProgress(3, '正在创建独立任务', '提交中');
  try {
    const data = await api('/jobs', {method:'POST', body:JSON.stringify({
      ba_pool: baValues, phones, country, proxies, agreement_only: false,
      buyer_mode: $('buyerMode').value,
    })});
    startBatch(data.jobs || []);
    await refreshJobs();
  } catch (error) {
    $('submitButton').disabled = false;
    showClientError(error.message);
  }
});

$('cancelButton').addEventListener('click', async () => {
  if (!state.jobId) return;
  $('cancelButton').disabled = true;
  try {
    const data = await api(`/jobs/${encodeURIComponent(state.jobId)}/cancel`, {method:'POST', body:'{}'});
    renderJob(data.job);
    schedulePoll();
  } catch (error) {
    if (Number(error.status) === 404) clearMissingJob('\u4efb\u52a1\u5df2\u7ecf\u5931\u6548\uff0c\u8bf7\u91cd\u65b0\u63d0\u4ea4');
    else showClientError(error.message);
  }
});

$('captchaSubmit').addEventListener('click', submitCaptcha);
async function submitCaptcha() {
  const value = $('captchaValue').value.trim();
  if (!value || !state.jobId) return;
  $('captchaSubmit').disabled = true;
  try {
    const data = await api(`/jobs/${encodeURIComponent(state.jobId)}/captcha`, {method:'POST', body:JSON.stringify({value})});
    $('captchaValue').value = '';
    renderJob(data.job);
    schedulePoll();
  } catch (error) { showClientError(error.message); }
  finally { $('captchaSubmit').disabled = false; }
}

$('otpSubmit').addEventListener('click', submitOtp);
$('otpValue').addEventListener('keydown', event => { if (event.key === 'Enter') submitOtp(); });
async function submitOtp() {
  const value = $('otpValue').value.trim();
  if (!value || !state.jobId) return;
  $('otpSubmit').disabled = true;
  try {
    const data = await api(`/jobs/${encodeURIComponent(state.jobId)}/otp`, {method:'POST', body:JSON.stringify({value})});
    $('otpValue').value = '';
    renderJob(data.job);
    schedulePoll();
  } catch (error) { showClientError(error.message); }
  finally { $('otpSubmit').disabled = false; }
}

async function refreshJobs() {
  try {
    const data = await api('/jobs');
    const jobs = data.jobs || [];
    const wrap = $('recentJobs');
    if (!jobs.length) {
      wrap.innerHTML = '<div class="empty-log">当前浏览器还没有协议支付任务。</div>';
      return;
    }
    wrap.innerHTML = jobs.slice(0, 8).map(job => {
      const [label] = statusMeta(job.status);
      return `<button class="recent-job ${job.id === state.jobId ? 'active' : ''}" type="button" data-id="${escapeHtml(job.id)}"><span><b>${escapeHtml(job.ba_token || job.id)}</b><small>${escapeHtml(job.stage || '')}</small></span><em>${label}</em></button>`;
    }).join('');
    wrap.querySelectorAll('.recent-job').forEach(button => button.addEventListener('click', () => startBatch([button.dataset.id])));
  } catch (_) {}
}
$('refreshJobs').addEventListener('click', refreshJobs);

$('copyResult').addEventListener('click', async () => {
  const text = $('resultValue').value;
  if (!text) return;
  try { await navigator.clipboard.writeText(text); }
  catch (_) { $('resultValue').select(); document.execCommand('copy'); }
  $('copyResult').textContent = '已复制';
  setTimeout(() => $('copyResult').textContent = '复制结果', 1200);
});

async function checkHealth() {
  try {
    await api('/health');
    $('serviceState').innerHTML = '<i></i> 服务在线';
  } catch (_) {
    $('serviceState').innerHTML = '<i class="offline"></i> 服务异常';
  }
}

function formatDuration(seconds) {
  const value = Math.max(0, Math.round(Number(seconds) || 0));
  if (!value) return '—';
  if (value < 60) return `${value}s`;
  const minutes = Math.floor(value / 60);
  const remain = value % 60;
  return remain ? `${minutes}m ${remain}s` : `${minutes}m`;
}

function renderSuccessStats(stats) {
  if (!$('successTotal')) return;
  const events = Array.isArray(stats.success_events) ? stats.success_events : [];
  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  const todayCount = Number.isFinite(Number(stats.success_today))
    ? Number(stats.success_today)
    : events.filter(item => Number(item.ts || 0) >= todayStart).length;
  $('successTotal').textContent = String(stats.success_total || 0);
  $('successToday').textContent = String(todayCount);
  $('successAverage').textContent = formatDuration(stats.average_success_seconds);
  $('successLatest').textContent = stats.latest_success_at
    ? new Date(Number(stats.latest_success_at) * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})
    : '—';

  const serverBuckets = Array.isArray(stats.success_hourly_24h)
    ? stats.success_hourly_24h : [];
  let buckets;
  if (serverBuckets.length === 24) {
    buckets = serverBuckets.map(item => ({
      time: new Date(Number(item.start_ts || 0) * 1000),
      label: String(item.label || ''),
      count: Math.max(0, Number(item.count) || 0),
    }));
  } else {
    const endHour = new Date(now);
    endHour.setMinutes(0, 0, 0);
    const startMs = endHour.getTime() - 23 * 3600 * 1000;
    buckets = Array.from({length: 24}, (_, index) => ({
      time: new Date(startMs + index * 3600 * 1000), count: 0,
    }));
    for (const event of events) {
      const eventMs = Number(event.ts || 0) * 1000;
      const index = Math.floor((eventMs - startMs) / 3600000);
      if (index >= 0 && index < buckets.length) buckets[index].count += 1;
    }
  }
  const maxCount = Math.max(1, ...buckets.map(item => item.count));
  const chartWidth = 720;
  const plotHeight = 58;
  const step = chartWidth / 24;
  const barWidth = 18;
  const bars = buckets.map((item, index) => {
    const height = item.count ? Math.round(10 + item.count / maxCount * 48) : 2;
    const x = index * step + (step - barWidth) / 2;
    const y = plotHeight - height;
    const hour = item.label || item.time.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    const label = index % 4 === 0 || index === 23
      ? `<text class="chart-hour" x="${x + barWidth / 2}" y="78" text-anchor="middle">${hour.slice(0, 2)}</text>` : '';
    const count = item.count
      ? `<text class="chart-count" x="${x + barWidth / 2}" y="${Math.max(8, y - 4)}" text-anchor="middle">${item.count}</text>` : '';
    return `<g><title>${hour} · ${item.count} 次</title><rect class="chart-bar${item.count ? ' active' : ''}" x="${x}" y="${y}" width="${barWidth}" height="${height}" rx="5"></rect>${count}${label}</g>`;
  }).join('');
  $('successTimeline').innerHTML = `<svg class="success-chart-svg" viewBox="0 0 ${chartWidth} 82" preserveAspectRatio="none" role="img">${bars}</svg>`;
  const recentHour = buckets[buckets.length - 1].count;
  $('successTimelineCaption').textContent = stats.success_total
    ? `当前小时 ${recentHour} 次 · 累计 ${stats.success_total} 次`
    : '等待第一个成功信号';
}

async function refreshSuccessStats() {
  try {
    renderSuccessStats(await api('/stats'));
  } catch (_) {}
}

(async function init() {
  await paypalCountriesReady;
  restoreProtocolFormState();
  updateProtocolMode();
  updateVaultRegion();
  updateCountryFields();
  updateProxyCount();
  updatePairCounts();
  await checkHealth();
  if ($('successTotal')) await refreshSuccessStats();
  await refreshJobs();
  let savedBatch = [];
  try { savedBatch = JSON.parse(sessionStorage.getItem('paypal-protocol-batch') || '[]'); } catch (_) {}
  if (Array.isArray(savedBatch) && savedBatch.length) startBatch(savedBatch.slice(0, 20));
  else {
    const saved = sessionStorage.getItem('paypal-protocol-job');
    if (saved) startBatch([saved]);
  }
  state.jobsTimer = setInterval(refreshJobs, 5000);
  if ($('successTotal')) setInterval(refreshSuccessStats, 15000);
  setInterval(checkHealth, 15000);
})();
