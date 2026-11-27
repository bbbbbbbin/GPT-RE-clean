# -*- coding: utf-8 -*-
"""单页界面（无外部 CDN，直接内联）。"""

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PlusVCC · 短链 + 开卡</title>
<style>
:root{
  --bg:#0f1115; --panel:#171a21; --panel2:#1e222b; --line:#2a2f3a;
  --text:#e6e9ef; --muted:#98a1b3; --accent:#4f8cff; --ok:#35c48a;
  --warn:#f5a524; --err:#f2555a; --mono:ui-monospace,SFMono-Regular,Consolas,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{display:flex;align-items:center;gap:14px;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:5}
header h1{font-size:16px;margin:0;font-weight:800;letter-spacing:.3px}
header .spacer{flex:1}
.pill{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:3px 10px;background:var(--panel2)}
.pill b{color:var(--text);font-variant-numeric:tabular-nums}
nav{display:flex;gap:6px;padding:10px 20px 0;flex-wrap:wrap}
nav button{background:transparent;border:1px solid transparent;color:var(--muted);padding:8px 14px;border-radius:8px 8px 0 0;cursor:pointer;font-size:14px;font-weight:600}
nav button:hover{color:var(--text)}
nav button.on{color:var(--text);background:var(--panel);border-color:var(--line);border-bottom-color:var(--panel)}
main{padding:18px 20px 60px;max-width:1180px;margin:0 auto}
section{display:none}
section.on{display:block}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
.card h2{margin:0 0 12px;font-size:14px;font-weight:800;color:var(--text);display:flex;align-items:center;gap:8px}
.card h2 small{font-weight:500;color:var(--muted);font-size:12px}
label.fld{display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--muted);font-weight:600}
input,select,textarea{background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:9px 10px;font-size:13px;font-family:inherit;outline:none;width:100%}
input:focus,select:focus,textarea:focus{border-color:var(--accent)}
textarea{resize:vertical;min-height:84px;font-family:var(--mono);font-size:12px}
.row{display:flex;gap:12px;flex-wrap:wrap}
.row > *{flex:1;min-width:170px}
.row.tight > *{flex:0 0 auto;min-width:0}
button.btn{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:9px 16px;font-size:13px;font-weight:700;cursor:pointer}
button.btn:hover{filter:brightness(1.08)}
button.btn:disabled{opacity:.55;cursor:not-allowed}
button.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--text)}
button.btn.small{padding:5px 10px;font-size:12px;border-radius:6px}
button.btn.danger{background:var(--err)}
.actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:12px}
.hint{color:var(--muted);font-size:12px}
.mono{font-family:var(--mono)}
.result{display:none;margin-top:12px;border:1px dashed var(--line);border-radius:10px;padding:12px;background:var(--panel2)}
.result.on{display:block}
.result .url{font-family:var(--mono);font-size:13px;word-break:break-all;color:#9ecbff}
.status{margin-top:10px;font-size:13px;color:var(--muted);white-space:pre-wrap}
.status.ok{color:var(--ok)}
.status.err{color:var(--err)}
.status.warn{color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{color:var(--muted);font-size:12px;font-weight:700;white-space:nowrap}
tr:hover td{background:#1b1f27}
td.num{font-variant-numeric:tabular-nums}
.tag{display:inline-block;font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
.tag.ok{color:var(--ok);border-color:rgba(53,196,138,.4)}
.tag.warn{color:var(--warn);border-color:rgba(245,165,36,.4)}
.tag.err{color:var(--err);border-color:rgba(242,85,90,.4)}
.otp-code{font-family:var(--mono);font-size:22px;font-weight:800;letter-spacing:3px;color:#9ecbff}
.otp-wrap{display:flex;align-items:center;gap:10px}
.otp-bar{width:110px;height:5px;border-radius:999px;background:var(--panel2);overflow:hidden;border:1px solid var(--line)}
.otp-bar i{display:block;height:100%;background:var(--ok);transition:width .9s linear}
.otp-bar.low i{background:var(--warn)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:840px){.grid2{grid-template-columns:1fr}}
.toast-wrap{position:fixed;right:18px;bottom:18px;display:flex;flex-direction:column;gap:8px;z-index:99}
.toast{background:var(--panel2);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:8px;padding:10px 14px;font-size:13px;box-shadow:0 8px 24px rgba(0,0,0,.35);animation:in .18s ease-out}
.toast.err{border-left-color:var(--err)}
.toast.ok{border-left-color:var(--ok)}
@keyframes in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.modal-mask{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;z-index:50;padding:20px}
.modal-mask.on{display:flex}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:14px;max-width:560px;width:100%;padding:18px}
.modal h3{margin:0 0 12px;font-size:15px}
.kv{display:grid;grid-template-columns:88px 1fr auto;gap:8px;align-items:center;padding:9px 0;border-bottom:1px dashed var(--line)}
.kv:last-of-type{border-bottom:0}
.kv .k{color:var(--muted);font-size:12px}
.kv .v{font-family:var(--mono);font-size:15px;letter-spacing:.5px;word-break:break-all}
.banner{border:1px solid var(--line);border-left:3px solid var(--warn);background:#1d1c14;border-radius:8px;padding:10px 12px;font-size:12px;color:#e8d9a8;margin-top:12px}
.tabs-sub{display:flex;gap:8px;margin-bottom:10px}
.tabs-sub button{background:var(--panel2);border:1px solid var(--line);color:var(--muted);border-radius:6px;padding:5px 10px;font-size:12px;cursor:pointer}
.tabs-sub button.on{color:var(--text);border-color:var(--accent)}
.empty{color:var(--muted);font-size:13px;padding:14px 0;text-align:center}
</style>
</head>
<body>
<header>
  <h1>PlusVCC</h1>
  <span class="pill">余额 <b id="hdrBalance">--</b></span>
  <span class="pill" id="hdrCfg">未配置密钥</span>
  <span class="spacer"></span>
  <button class="btn ghost small" onclick="openSession()">打开 /api/auth/session</button>
  <button class="btn ghost small" onclick="quitApp()">退出程序</button>
</header>

<nav>
  <button class="on" data-tab="link">① 短链</button>
  <button data-tab="apply">② 开卡</button>
  <button data-tab="cards">③ 卡片 / 授权</button>
  <button data-tab="2fa">④ 2FA</button>
  <button data-tab="settings">⚙ 设置</button>
</nav>

<main>
  <!-- ============================ 短链 ============================ -->
  <section id="tab-link" class="on">
    <div class="card">
      <h2>生成 Plus 支付短链 <small>先查 plus-1-month-free 优惠，有优惠才生成</small></h2>
      <div class="grid2">
        <label class="fld">session（/api/auth/session 的 JSON 或 accessToken）
          <textarea id="session" placeholder='{"accessToken":"eyJ...","user":{...}}'></textarea>
        </label>
        <div style="display:flex;flex-direction:column;gap:12px">
          <label class="fld">代理（URL / host:port / host:port:user:pass）
            <input id="proxy" placeholder="socks5h://user:pass@host:port" spellcheck="false">
          </label>
          <div class="row">
            <label class="fld">代理类型
              <select id="proxyType">
                <option value="auto">自动</option>
                <option value="http">HTTP</option>
                <option value="socks5">SOCKS5</option>
              </select>
            </label>
            <label class="fld">国家（可空）
              <input id="country" placeholder="自动识别，如 US" spellcheck="false">
            </label>
            <label class="fld">币种（可空）
              <input id="currency" placeholder="自动识别，如 USD" spellcheck="false">
            </label>
          </div>
        </div>
      </div>
      <div class="row" style="margin-top:12px">
        <label class="fld">链式上游代理（默认 http://127.0.0.1:7890）
          <input id="chainUpstream" placeholder="http://127.0.0.1:7890" spellcheck="false">
        </label>
        <label class="fld" style="justify-content:flex-end">
          <span><input type="checkbox" id="chainOnly" checked style="width:auto"> 只走链式（跳过直连）</span>
        </label>
      </div>
      <div class="actions">
        <button class="btn" id="btnGen" onclick="genShort()">生成短链</button>
        <button class="btn ghost" onclick="openSession()">浏览器打开 session</button>
        <button class="btn ghost" id="btnSentinel" onclick="testSentinel()">自检 sentinel</button>
        <span class="hint">Ctrl+Enter 也可提交；链式通常 2-5 秒</span>
      </div>
      <div class="status" id="linkStatus"></div>
      <div class="result" id="linkResult">
        <div class="hint" style="margin-bottom:6px">短链（需该 session 对应账号登录后打开付款）</div>
        <div class="url mono" id="shortUrl"></div>
        <div class="actions">
          <button class="btn small" onclick="copyText($('shortUrl').textContent)">复制</button>
          <button class="btn ghost small" onclick="openUrl($('shortUrl').textContent)">在浏览器打开</button>
          <span class="hint" id="linkMeta"></span>
        </div>
      </div>
    </div>
  </section>

  <!-- ============================ 开卡 ============================ -->
  <section id="tab-apply">
    <div class="card">
      <h2>开卡配额 / 可用 BIN <small>来自 /openapi/v1/card-bins/query</small></h2>
      <div class="actions" style="margin-top:0">
        <button class="btn ghost small" onclick="loadBins()">刷新 BIN 和配额</button>
        <span class="hint" id="quotaText">--</span>
      </div>
      <div id="binsWrap" style="margin-top:10px"><div class="empty">点击「刷新 BIN 和配额」加载</div></div>
    </div>

    <div class="card">
      <h2>申请开卡 <small>储值卡 PREPAID，开卡后自动查询任务状态</small></h2>
      <div class="row">
        <label class="fld">卡 BIN
          <select id="applyBin"></select>
        </label>
        <label class="fld">开卡张数
          <input id="applyCount" type="number" min="1" max="20" value="1">
        </label>
        <label class="fld">每张首充金额（USD）
          <input id="applyAmount" value="10.00" spellcheck="false">
        </label>
        <label class="fld">持卡人 ID（可空）
          <input id="applyHolder" placeholder="留空用系统默认持卡人" spellcheck="false">
        </label>
      </div>
      <div class="row" style="margin-top:12px">
        <label class="fld">requestNo（商户请求号，8-64 位，字母+数字）
          <input id="applyRequestNo" spellcheck="false">
        </label>
        <label class="fld">备注（最长 40 字符）
          <input id="applyRemark" placeholder="例如：plus-卡片1" maxlength="40">
        </label>
      </div>
      <div class="actions">
        <button class="btn" id="btnApply" onclick="applyCard()">提交开卡</button>
        <button class="btn ghost" onclick="$('applyRequestNo').value = makeRequestNo()">重新生成 requestNo</button>
      </div>
      <div class="banner">注意：开卡会从 AiLink 账户扣款（开卡手续费 + 首充金额）。提交后仍以 AiLink 返回的开卡任务状态为准。</div>
      <div class="status" id="applyStatus"></div>
      <div id="applyTaskWrap" style="margin-top:12px"></div>
    </div>
  </section>

  <!-- ============================ 卡片 ============================ -->
  <section id="tab-cards">
    <div class="card">
      <h2>我的卡片</h2>
      <div class="row">
        <label class="fld">批次号 batchNo
          <input id="qBatch" placeholder="开卡后自动带入" spellcheck="false">
        </label>
        <label class="fld">备注关键字
          <input id="qRemark" placeholder="模糊匹配" spellcheck="false">
        </label>
        <label class="fld">卡状态
          <select id="qStatus">
            <option value="">全部</option>
            <option value="ACTIVE">ACTIVE 正常</option>
            <option value="FROZEN">FROZEN 冻结</option>
            <option value="OPENING">OPENING 开卡中</option>
            <option value="CANCELLED">CANCELLED 已销卡</option>
            <option value="EXPIRED">EXPIRED 已过期</option>
          </select>
        </label>
        <label class="fld" style="justify-content:flex-end">
          <button class="btn" onclick="loadCards()">查询卡片</button>
        </label>
      </div>
      <div id="cardsWrap" style="margin-top:12px"><div class="empty">暂无数据</div></div>
    </div>

    <div class="card" id="txCard" style="display:none">
      <h2>授权 / 交易 <small id="txTitle"></small></h2>
      <div class="row">
        <label class="fld">交易类型
          <select id="txType">
            <option value="">全部</option>
            <option value="AUTH">AUTH 授权</option>
            <option value="CAPTURE">CAPTURE 扣款</option>
            <option value="REFUND">REFUND 退款</option>
            <option value="REVERSE">REVERSE 撤销</option>
            <option value="RECHARGE">RECHARGE 充值</option>
          </select>
        </label>
        <label class="fld">交易状态
          <select id="txStatus">
            <option value="">全部</option>
            <option value="AUTHORIZED">AUTHORIZED 已授权</option>
            <option value="SUCCESS">SUCCESS 成功</option>
            <option value="SETTLED">SETTLED 已清算</option>
            <option value="PROCESSING">PROCESSING 处理中</option>
            <option value="DECLINED">DECLINED 已拒绝</option>
            <option value="FAILED">FAILED 失败</option>
            <option value="REVERSED">REVERSED 已撤销</option>
            <option value="REFUNDED">REFUNDED 已退款</option>
          </select>
        </label>
        <label class="fld">开始时间
          <input id="txBegin" placeholder="2026-06-01 00:00:00" spellcheck="false">
        </label>
        <label class="fld">结束时间
          <input id="txEnd" placeholder="2026-12-31 23:59:59" spellcheck="false">
        </label>
        <label class="fld" style="justify-content:flex-end">
          <button class="btn ghost" onclick="loadTx(currentTxIdx)">刷新</button>
        </label>
      </div>
      <div id="txWrap" style="margin-top:12px"><div class="empty">选择一张卡查看</div></div>
    </div>
  </section>

  <!-- ============================ 2FA ============================ -->
  <section id="tab-2fa">
    <div class="card">
      <h2>ChatGPT 账号 2FA <small>本地按 RFC 6238 生成，30 秒刷新，密钥不出本机</small></h2>
      <div class="row">
        <label class="fld">备注（可空）
          <input id="otpLabel" placeholder="例如：主号 / plus1" maxlength="60">
        </label>
        <label class="fld" style="flex:2.2">2FA 密钥（Base32）或 otpauth:// 链接
          <input id="otpSecret" placeholder="JBSWY3DPEHPK3PXP 或 otpauth://totp/...?secret=..." spellcheck="false" autocomplete="off">
        </label>
        <label class="fld" style="justify-content:flex-end">
          <button class="btn" onclick="addTotp()">添加账号</button>
        </label>
      </div>
      <div class="row" style="margin-top:12px">
        <label class="fld">手动指定周期 / 位数（可空，默认 30 秒 6 位）
          <input id="otpPeriod" placeholder="30" spellcheck="false">
        </label>
        <div style="display:flex;align-items:flex-end;gap:10px">
          <button class="btn ghost" onclick="previewTotp()">先试算一次</button>
          <button class="btn ghost" onclick="refreshTotp()">刷新验证码</button>
        </div>
      </div>
      <div class="status" id="otpStatus"></div>
      <div id="totpWrap" style="margin-top:12px"><div class="empty">还没有账号，粘贴 2FA 密钥后点「添加账号」</div></div>
    </div>

    <div class="card">
      <h2>怎么拿 ChatGPT 的 2FA 密钥</h2>
      <div class="hint" style="line-height:1.9">
        1. 登录 chatgpt.com → 左下角头像 → 设置 → 安全 → 多重要素验证 → 开启「验证器应用」。<br>
        2. 弹窗里二维码下方那串字符就是密钥；也可以复制 otpauth:// 链接整段粘进来。<br>
        3. 验证码每 30 秒变一次，本页自动倒计时，过期瞬间自动取新码。<br>
        4. 密钥保存在本机 <span class="mono">%APPDATA%\\PlusVCC\\config.json</span>，用 Windows DPAPI 加密（仅当前用户可解密）。
      </div>
    </div>
  </section>

  <!-- ============================ 设置 ============================ -->
  <section id="tab-settings">
    <div class="card">
      <h2>AiLink VCC 凭据 <small>只保存在本机</small></h2>
      <div class="row">
        <label class="fld">接口域名
          <input id="cfgBaseUrl" placeholder="https://card.ailinkvcc.com" spellcheck="false">
        </label>
        <label class="fld">API Key
          <input id="cfgApiKey" placeholder="ak_xxx" spellcheck="false">
        </label>
      </div>
      <div class="grid2" style="margin-top:12px">
        <label class="fld">API Secret
          <input id="cfgApiSecret" type="password" placeholder="留空表示不修改" spellcheck="false">
        </label>
        <label class="fld">Webhook Secret（解密卡号 / 有效期 / CVV 必需）
          <input id="cfgWebhookSecret" type="password" placeholder="留空表示不修改" spellcheck="false">
        </label>
      </div>
      <div class="row" style="margin-top:12px">
        <label class="fld">接口代理（可空，例 http://127.0.0.1:7890）
          <input id="cfgApiProxy" placeholder="留空自动探测 127.0.0.1:7890 / 10808" spellcheck="false">
        </label>
        <label class="fld">超时秒数
          <input id="cfgTimeout" type="number" min="5" max="120" value="40">
        </label>
        <label class="fld">默认链式上游
          <input id="cfgChainUpstream" placeholder="http://127.0.0.1:7890" spellcheck="false">
        </label>
      </div>
      <div class="actions">
        <button class="btn" onclick="saveConfig()">保存</button>
        <button class="btn ghost" onclick="testConfig()">测试连接（查余额）</button>
        <span class="hint" id="cfgStatus"></span>
      </div>
      <div class="banner" id="cfgPathNote"></div>
    </div>

    <div class="card">
      <h2>使用说明</h2>
      <div class="hint" style="line-height:1.9">
        1. 先在这里填 API Key / API Secret / Webhook Secret，点「保存」再「测试连接」。<br>
        2. 「短链」页填 session 和代理，生成 checkout 短链并可直接打开。<br>
        3. 「开卡」页选 BIN、填首充金额提交，任务成功后会跳到「卡片」。<br>
        4. 「卡片」页点「卡号/CVV」用 Webhook Secret 本地解密，接口只给密文。<br>
        5. 卡片列表点「授权」按卡号后四位查该卡的授权/扣款/退款记录。<br>
        6. 「2FA」页粘贴 ChatGPT 的 2FA 密钥，本地生成 6 位动态码并自动倒计时。<br>
        7. 若接口返回 HTTP 403 且是 openresty 页面，通常是商户后台配了 API IP 白名单，换「接口代理」或找客服加白。<br>
        8. 短链生成需要 <b>Node.js</b>（checkout 必须带 sentinel token，靠 node 跑 sdk.js）。生成前可先点「自检 sentinel」，
        显示「sentinel 正常」就说明环境没问题；报错会直接给出原因（没装 node / 模块缺失）。<br>
        9. 要换新版 exe：点右上角「退出程序」（或运行 <code>vcc_tool\\stop.ps1</code>）→ 等 2 秒端口释放 →
        覆盖 <code>dist\\PlusVCC.exe</code> → 重新双击，用新弹出的页面。
      </div>
    </div>
  </section>
</main>

<div class="toast-wrap" id="toasts"></div>

<div class="modal-mask" id="revealMask" onclick="if(event.target===this)closeReveal()">
  <div class="modal">
    <h3>卡信息 <span class="mono hint" id="revealCardId"></span></h3>
    <div class="kv"><span class="k">卡号</span><span class="v" id="rvNumber">--</span><button class="btn small ghost" onclick="copyText($('rvNumber').textContent)">复制</button></div>
    <div class="kv"><span class="k">有效期</span><span class="v" id="rvExpiry">--</span><button class="btn small ghost" onclick="copyText($('rvExpiry').textContent)">复制</button></div>
    <div class="kv"><span class="k">CVV</span><span class="v" id="rvCvv">--</span><button class="btn small ghost" onclick="copyText($('rvCvv').textContent)">复制</button></div>
    <div class="actions">
      <button class="btn small" onclick="copyText(revealText())">复制全部（卡号/有效期/CVV）</button>
      <button class="btn ghost small" onclick="closeReveal()">关闭</button>
    </div>
    <div class="banner" id="revealNote">卡号/CVV 属于敏感信息。确认商户后台已配置 Webhook Secret，否则解密会失败。</div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let CARDS = [];
let REVEAL = null;

/* ---------------------------------- 基础 ---------------------------------- */
function toast(msg, type) {
  const div = document.createElement('div');
  div.className = 'toast ' + (type || '');
  div.textContent = msg;
  $('toasts').appendChild(div);
  setTimeout(() => div.remove(), 4200);
}

async function api(path, body) {
  const opt = { method: 'POST', headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const resp = await fetch(path, opt);
  let data = null;
  try { data = await resp.json(); } catch (e) { data = null; }
  if (!data) throw new Error('HTTP ' + resp.status + ' 返回非 JSON');
  return data;
}

function setStatus(id, text, type) {
  const el = $(id);
  el.className = 'status ' + (type || '');
  el.textContent = text || '';
}

async function copyText(text) {
  const value = (text || '').trim();
  if (!value || value === '--') return toast('没有可复制的内容', 'err');
  try {
    await navigator.clipboard.writeText(value);
    toast('已复制', 'ok');
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = value;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
    toast('已复制', 'ok');
  }
}

function openUrl(url) {
  if (!url) return;
  window.open(url, '_blank');
}

function openSession() {
  window.open('https://chatgpt.com/api/auth/session', '_blank');
}

async function quitApp() {
  if (!confirm('确定退出 PlusVCC？退出后端口会释放，可以直接替换 exe。')) return;
  try { await fetch('/api/quit', { method: 'POST' }); } catch (e) {}
  document.body.innerHTML = '<div style="padding:60px;text-align:center;font-size:16px;line-height:2">'
    + '程序已退出，端口已释放，现在可以替换 / 删除 exe 了。<br>'
    + '<span style="color:#666;font-size:14px">重新双击 exe 会新开一个页面，以新页面为准。</span></div>';
}

document.querySelectorAll('nav button').forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll('nav button').forEach((b) => b.classList.toggle('on', b === btn));
    const tab = btn.dataset.tab;
    document.querySelectorAll('main section').forEach((s) => s.classList.toggle('on', s.id === 'tab-' + tab));
    if (tab === 'cards' && !CARDS.length) loadCards();
    if (tab === '2fa') loadTotp();
  };
});

/* ---------------------------------- 短链 ---------------------------------- */
function rememberLinkForm() {
  const keys = ['proxy', 'proxyType', 'country', 'currency', 'chainUpstream'];
  const state = {};
  keys.forEach((k) => { state[k] = $(k).value; });
  state.chainOnly = $('chainOnly').checked;
  localStorage.setItem('plusvcc.link', JSON.stringify(state));
}

function restoreLinkForm() {
  try {
    const state = JSON.parse(localStorage.getItem('plusvcc.link') || '{}');
    Object.keys(state).forEach((k) => {
      if (k === 'chainOnly') $('chainOnly').checked = !!state[k];
      else if ($(k)) $(k).value = state[k];
    });
  } catch (e) {}
}

async function genShort() {
  const session = $('session').value.trim();
  const proxy = $('proxy').value.trim();
  if (!session) return setStatus('linkStatus', '请先填 session', 'err');
  if (!proxy) return setStatus('linkStatus', '请先填代理', 'err');
  rememberLinkForm();
  const btn = $('btnGen');
  btn.disabled = true;
  $('linkResult').classList.remove('on');
  setStatus('linkStatus', '正在检测代理并查询优惠（链式通常 2-5 秒）...', 'warn');
  try {
    const data = await api('/api/shortlink', {
      session,
      proxy,
      proxy_type: $('proxyType').value,
      country: $('country').value.trim(),
      currency: $('currency').value.trim(),
      chain_upstream: $('chainUpstream').value.trim(),
      chain_only: $('chainOnly').checked,
    });
    if (!data.ok) {
      if (data.code === 'no_promo') {
        setStatus('linkStatus', '无优惠（state=' + (data.state || 'ineligible') + '），已停止生成短链。', 'warn');
      } else if (data.code === 'session_expired') {
        setStatus('linkStatus', 'session 已失效，请重新获取 /api/auth/session。', 'err');
      } else {
        setStatus('linkStatus', data.error || '生成失败', 'err');
      }
      return;
    }
    $('shortUrl').textContent = data.short_url;
    const meta = [];
    if (data.proxy_ip) meta.push('出口 ' + data.proxy_ip);
    if (data.country) meta.push(data.country + '/' + data.currency);
    if (data.chained) meta.push('链式');
    if (data.chain_note) meta.push(data.chain_note);
    if (data.sentinel_used) meta.push('sentinel' + (data.sentinel_so_used ? '+SO' : ''));
    $('linkMeta').textContent = meta.join(' · ');
    $('linkResult').classList.add('on');
    setStatus('linkStatus', '生成成功' + (data.sentinel_used ? '' : '（注意：本次没带 sentinel）'), data.sentinel_used ? 'ok' : 'warn');
  } catch (e) {
    setStatus('linkStatus', '请求异常: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

async function testSentinel() {
  const btn = $('btnSentinel');
  btn.disabled = true;
  setStatus('linkStatus', '正在生成 sentinel token（会调用 node 跑 sdk.js）...', 'warn');
  try {
    const data = await api('/api/sentinel/selftest', {
      proxy: $('proxy').value.trim(),
      chain_upstream: $('chainUpstream').value.trim(),
      chain_only: $('chainOnly').checked,
    });
    if (!data.ok) {
      setStatus('linkStatus', 'sentinel 自检失败: ' + (data.error || '未知错误'), 'err');
      return;
    }
    setStatus('linkStatus',
      'sentinel 正常：flow=' + data.flow + '，token ' + data.token_len + ' 字节' +
      (data.has_so ? '（含 SO）' : '（无 SO）') + '，用时 ' + data.elapsed + 's' +
      (data.proxy ? '，出口 ' + data.proxy : '') +
      (data.proxy_note ? '（' + data.proxy_note + '）' : ''), 'ok');
  } catch (e) {
    setStatus('linkStatus', 'sentinel 自检异常: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

$('session').addEventListener('keydown', (e) => {
  if (e.ctrlKey && e.key === 'Enter') genShort();
});

/* ---------------------------------- 开卡 ---------------------------------- */
function makeRequestNo() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const stamp = '' + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds());
  const tail = Math.random().toString(16).slice(2, 8);
  return 'kaika' + stamp + tail;
}

let BINS = [];

async function loadBins() {
  try {
    const data = await api('/api/vcc/bins', {});
    if (!data.ok) throw new Error(data.error || '查询失败');
    BINS = (data.data && data.data.list) || [];
    const quota = (data.data && data.data.quota) || {};
    $('quotaText').textContent = '配额 已开 ' + (quota.issuedCardCount != null ? quota.issuedCardCount : '?') +
      ' / 上限 ' + (quota.maxCardCount != null ? quota.maxCardCount : '?') +
      '，剩余 ' + (quota.remainingCardCount != null ? quota.remainingCardCount : '?');
    if (!BINS.length) {
      $('binsWrap').innerHTML = '<div class="empty">没有可用 BIN（可能未配置或配额已满）</div>';
      $('applyBin').innerHTML = '';
      return;
    }
    const rows = BINS.map((b) => '<tr>' +
      '<td class="mono">' + b.binId + '</td>' +
      '<td class="mono">' + (b.bin || '') + '</td>' +
      '<td>' + (b.cardOrg || '') + '</td>' +
      '<td>' + (b.cardType || '') + '</td>' +
      '<td>' + (b.currency || '') + '</td>' +
      '<td class="num">' + (b.issuingCountry || '') + '</td>' +
      '<td class="num">' + (b.minOpenCardAmount || '') + '</td>' +
      '<td class="num">' + (b.minRechargeAmount || '') + '</td>' +
      '<td class="num">' + (b.maxOpenCardCount != null ? b.maxOpenCardCount : '') + '</td>' +
      '</tr>').join('');
    $('binsWrap').innerHTML = '<table><thead><tr><th>binId</th><th>BIN</th><th>卡组织</th><th>类型</th><th>币种</th><th>发卡地</th><th>最低开卡额</th><th>最低充值</th><th>单次上限</th></tr></thead><tbody>' + rows + '</tbody></table>';
    $('applyBin').innerHTML = BINS.map((b) => '<option value="' + b.binId + '">' +
      b.binId + ' · ' + (b.bin || '') + ' · ' + (b.cardOrg || '') + ' ' + (b.currency || '') + '</option>').join('');
    const amount = BINS[0].minOpenCardAmount || BINS[0].minRechargeAmount;
    if (amount && !$('applyAmount').value) $('applyAmount').value = amount;
  } catch (e) {
    $('binsWrap').innerHTML = '<div class="empty">' + e.message + '</div>';
    toast(e.message, 'err');
  }
}

async function applyCard() {
  const binId = $('applyBin').value;
  if (!binId) return toast('请先加载 BIN', 'err');
  const payload = {
    cardBinId: binId,
    cardCount: Number($('applyCount').value || 1),
    rechargeAmount: $('applyAmount').value.trim(),
    holderId: $('applyHolder').value.trim(),
    remark: $('applyRemark').value.trim(),
    requestNo: $('applyRequestNo').value.trim(),
  };
  if (!payload.rechargeAmount) return toast('请填首充金额', 'err');
  if (!payload.requestNo) $('applyRequestNo').value = makeRequestNo();
  payload.requestNo = $('applyRequestNo').value.trim();

  const btn = $('btnApply');
  btn.disabled = true;
  $('applyTaskWrap').innerHTML = '';
  setStatus('applyStatus', '正在提交开卡申请...', 'warn');
  try {
    const data = await api('/api/vcc/apply', payload);
    if (!data.ok) {
      setStatus('applyStatus', '开卡失败: ' + (data.error || ''), 'err');
      return;
    }
    const task = data.data || {};
    setStatus('applyStatus', '已提交：taskId=' + task.taskId + '，batchNo=' + task.batchNo + '，开始查询任务状态...', 'warn');
    renderTask(task);
    pollApply(task.taskId, task.batchNo);
  } catch (e) {
    setStatus('applyStatus', '请求异常: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

function renderTask(task) {
  const status = task.applyStatus || task.taskStatus || '';
  const cls = status === 'SUCCESS' ? 'ok' : (status === 'FAILED' || status === 'CANCELED') ? 'err' : 'warn';
  $('applyTaskWrap').innerHTML = '<table><tbody>' +
    '<tr><th style="width:140px">taskId</th><td class="mono">' + (task.taskId != null ? task.taskId : '') + '</td></tr>' +
    '<tr><th>batchNo</th><td class="mono">' + (task.batchNo || '') + '</td></tr>' +
    '<tr><th>requestNo</th><td class="mono">' + (task.requestNo || '') + '</td></tr>' +
    '<tr><th>状态</th><td><span class="tag ' + cls + '">' + status + ' ' + (task.applyStatusText || task.taskStatusText || '') + '</span></td></tr>' +
    (task.successCount != null ? '<tr><th>成功 / 总数</th><td>' + task.successCount + ' / ' + task.totalCount + '</td></tr>' : '') +
    '</tbody></table>';
}

async function pollApply(taskId, batchNo) {
  for (let i = 0; i < 20; i++) {
    await new Promise((r) => setTimeout(r, 3000));
    try {
      const data = await api('/api/vcc/apply-status', { taskId });
      if (!data.ok) { setStatus('applyStatus', '状态查询失败: ' + data.error, 'err'); return; }
      const task = data.data || {};
      renderTask(task);
      const status = task.applyStatus || task.taskStatus || '';
      if (status === 'SUCCESS' || status === 'PARTIAL_SUCCESS') {
        setStatus('applyStatus', '开卡成功，正在加载该批次卡片...', 'ok');
        $('qBatch').value = batchNo || task.batchNo || '';
        CARDS = [];
        document.querySelector('nav button[data-tab="cards"]').click();
        await loadCards();
        return;
      }
      if (status === 'FAILED' || status === 'CANCELED') {
        setStatus('applyStatus', '开卡任务结束：' + status, 'err');
        return;
      }
      setStatus('applyStatus', '任务处理中（' + status + '），已等待 ' + ((i + 1) * 3) + ' 秒...', 'warn');
    } catch (e) {
      setStatus('applyStatus', '状态查询异常: ' + e.message, 'err');
      return;
    }
  }
  setStatus('applyStatus', '任务仍未完成，可稍后手动查询 applyStatus。', 'warn');
}

/* ---------------------------------- 卡片 ---------------------------------- */
function statusTag(status) {
  const map = { ACTIVE: 'ok', FROZEN: 'warn', OPENING: 'warn', CANCELLED: 'err', EXPIRED: 'err', CANCEL_REQUESTED: 'warn' };
  const key = String(status || '').toUpperCase();
  return '<span class="tag ' + (map[key] || '') + '">' + (status || '-') + '</span>';
}

async function loadCards() {
  const payload = {
    pageNum: 1,
    pageSize: 50,
    batchNo: $('qBatch').value.trim(),
    remark: $('qRemark').value.trim(),
    cardStatus: $('qStatus').value,
  };
  $('cardsWrap').innerHTML = '<div class="empty">查询中...</div>';
  try {
    const data = await api('/api/vcc/cards', payload);
    if (!data.ok) throw new Error(data.error || '查询失败');
    CARDS = (data.data && data.data.list) || [];
    if (!CARDS.length) {
      $('cardsWrap').innerHTML = '<div class="empty">没有找到卡片</div>';
      return;
    }
    const rows = CARDS.map((c, idx) => '<tr>' +
      '<td class="mono">' + (c.cardId || '') + '</td>' +
      '<td class="mono">' + (c.cardNoMask || '') + '</td>' +
      '<td>' + statusTag(c.cardStatus) + '</td>' +
      '<td class="num">' + (c.balance != null ? c.balance : '') + '</td>' +
      '<td>' + (c.holderName || '') + '</td>' +
      '<td>' + (c.remark || '') + '</td>' +
      '<td class="mono">' + (c.batchNo || '') + '</td>' +
      '<td class="num">' + (c.createTime || '') + '</td>' +
      '<td><div class="row tight">' +
        '<button class="btn small" onclick="revealCard(' + idx + ')">卡号/CVV</button>' +
        '<button class="btn small ghost" onclick="loadTx(' + idx + ')">授权</button>' +
        '<button class="btn small ghost" onclick="loadOtp(' + idx + ')">3DS码</button>' +
      '</div></td>' +
      '</tr>').join('');
    $('cardsWrap').innerHTML = '<table><thead><tr><th>cardId</th><th>卡号</th><th>状态</th><th>余额</th><th>持卡人</th><th>备注</th><th>批次</th><th>创建时间</th><th>操作</th></tr></thead><tbody>' + rows + '</tbody></table>' +
      '<div class="hint" style="margin-top:8px">共 ' + ((data.data && data.data.total) || CARDS.length) + ' 张。卡号/CVV 由接口返回密文，本地用 Webhook Secret 解密。</div>';
  } catch (e) {
    $('cardsWrap').innerHTML = '<div class="empty">' + e.message + '</div>';
  }
}

async function revealCard(idx) {
  const card = CARDS[idx];
  if (!card) return;
  try {
    const data = await api('/api/vcc/reveal', { cardId: card.cardId });
    if (!data.ok) {
      REVEAL = null;
      $('revealCardId').textContent = card.cardId || '';
      $('rvNumber').textContent = '--';
      $('rvExpiry').textContent = '--';
      $('rvCvv').textContent = '--';
      $('revealNote').textContent = data.error || '解密失败';
      $('revealMask').classList.add('on');
      return;
    }
    REVEAL = data.data || {};
    $('revealCardId').textContent = REVEAL.cardId || card.cardId;
    $('rvNumber').textContent = formatCardNumber(REVEAL.cardNumber);
    $('rvExpiry').textContent = REVEAL.expiryDate || '--';
    $('rvCvv').textContent = REVEAL.cvv || '--';
    $('revealNote').textContent = '卡号/CVV 属于敏感信息，仅在本机解密显示。';
    $('revealMask').classList.add('on');
  } catch (e) {
    toast('请求异常: ' + e.message, 'err');
  }
}

function formatCardNumber(value) {
  const raw = String(value || '').replace(/[^0-9]/g, '');
  if (!raw) return '--';
  return raw.replace(/(.{4})/g, '$1 ').trim();
}

function revealText() {
  if (!REVEAL) return '';
  return [REVEAL.cardNumber, REVEAL.expiryDate, REVEAL.cvv].filter(Boolean).join(' | ');
}

function closeReveal() {
  $('revealMask').classList.remove('on');
  REVEAL = null;
  $('rvNumber').textContent = '--';
  $('rvExpiry').textContent = '--';
  $('rvCvv').textContent = '--';
  $('revealNote').textContent = '卡号/CVV 属于敏感信息。确认商户后台已配置 Webhook Secret，否则解密会失败。';
}

let currentTxIdx = -1;

async function loadTx(idx) {
  const card = CARDS[idx];
  if (!card) return;
  currentTxIdx = idx;
  $('txCard').style.display = 'block';
  $('txTitle').textContent = card.cardId + ' · ' + (card.cardNoMask || '');
  $('txWrap').innerHTML = '<div class="empty">查询中...</div>';
  try {
    const data = await api('/api/vcc/transactions', {
      pageNum: 1,
      pageSize: 50,
      cardNo: card.last4 || '',
      keyword: '',
      transactionType: $('txType').value,
      transactionStatus: $('txStatus').value,
      beginTime: $('txBegin').value.trim(),
      endTime: $('txEnd').value.trim(),
    });
    if (!data.ok) {
      $('txWrap').innerHTML = '<div class="empty">' + (data.error || '查询失败') + '</div>';
      return;
    }
    const list = (data.data && data.data.list) || [];
    if (!list.length) {
      $('txWrap').innerHTML = '<div class="empty">暂无授权/交易记录（该卡还没有被扣款或授权）</div>';
      return;
    }
    const rows = list.map((t) => '<tr>' +
      '<td class="num">' + (t.txnTime || '') + '</td>' +
      '<td>' + (t.transactionType || '') + '</td>' +
      '<td>' + txTag(t.transactionStatus) + '</td>' +
      '<td>' + (t.settlementStatus || '') + '</td>' +
      '<td class="num">' + (t.originalAmount || '') + ' ' + (t.originalCurrency || '') + '</td>' +
      '<td class="num">' + (t.usdAmount || '') + '</td>' +
      '<td class="num">' + (t.feeAmount || '') + '</td>' +
      '<td>' + (t.transactionInfo || '') + '</td>' +
      '<td>' + (t.merchantCountry || '') + ' ' + (t.merchantMcc || '') + '</td>' +
      '<td>' + (t.displayFailReason || '') + '</td>' +
      '</tr>').join('');
    $('txWrap').innerHTML = '<table><thead><tr><th>时间</th><th>类型</th><th>状态</th><th>结算</th><th>原始金额</th><th>USD</th><th>手续费</th><th>商户/说明</th><th>国家/MCC</th><th>失败原因</th></tr></thead><tbody>' + rows + '</tbody></table>' +
      '<div class="hint" style="margin-top:8px">共 ' + ((data.data && data.data.total) || list.length) + ' 条。AUTH=授权，CAPTURE=扣款，REVERSE=撤销，REFUND=退款。</div>';
  } catch (e) {
    $('txWrap').innerHTML = '<div class="empty">' + e.message + '</div>';
  }
}

function txTag(status) {
  const key = String(status || '').toUpperCase();
  const map = {
    AUTHORIZED: 'ok', SUCCESS: 'ok', SETTLED: 'ok',
    PROCESSING: 'warn', DECLINED: 'err', FAILED: 'err',
    REVERSED: 'warn', REFUNDED: 'warn', DISPUTED: 'warn',
  };
  return '<span class="tag ' + (map[key] || '') + '">' + (status || '-') + '</span>';
}

async function loadOtp(idx) {
  const card = CARDS[idx];
  if (!card) return;
  toast('正在查询 3DS 验证码...');
  try {
    const data = await api('/api/vcc/otp', { cardId: card.cardId });
    if (!data.ok) return toast(data.error || '查询失败', 'err');
    const code = data.data && data.data.verificationCode;
    toast(code ? ('3DS 验证码: ' + code) : '暂无验证码（found=false）', code ? 'ok' : '');
    if (code) copyText(code);
  } catch (e) {
    toast('请求异常: ' + e.message, 'err');
  }
}

/* ---------------------------------- 2FA ---------------------------------- */
let TOTP = [];
let TOTP_TICK = null;

function escapeHtml(text) {
  return String(text == null ? '' : text).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function fmtOtpCode(code) {
  const raw = String(code || '');
  if (!raw) return '------';
  if (raw.length === 6) return raw.slice(0, 3) + ' ' + raw.slice(3);
  if (raw.length === 8) return raw.slice(0, 4) + ' ' + raw.slice(4);
  return raw;
}

function setOtpStatus(text, type) {
  setStatus('otpStatus', text || '', type);
}

async function loadTotp() {
  try {
    const data = await api('/api/totp/codes', {});
    if (!data.ok) throw new Error(data.error || '读取失败');
    TOTP = (data.data || []).map((item) => Object.assign({}, item, {
      deadline: Date.now() + Math.max(0, Number(item.remaining || 0)) * 1000,
    }));
    renderTotp();
    setOtpStatus(TOTP.length ? ('共 ' + TOTP.length + ' 个账号，验证码每 ' + (TOTP[0].period || 30) + ' 秒刷新') : '', '');
  } catch (e) {
    setOtpStatus('读取 2FA 失败: ' + e.message, 'err');
  }
}

async function refreshTotp() {
  await loadTotp();
}

function renderTotp() {
  const wrap = $('totpWrap');
  if (!TOTP.length) {
    wrap.innerHTML = '<div class="empty">还没有账号，粘贴 2FA 密钥后点「添加账号」</div>';
    return;
  }
  const rows = TOTP.map((a) => {
    const period = Math.max(1, Number(a.period || 30));
    const pct = Math.max(0, Math.min(100, (a.deadline - Date.now()) / (period * 1000) * 100));
    return '<tr data-id="' + escapeHtml(a.id) + '">' +
      '<td>' + escapeHtml(a.label || 'ChatGPT') + '</td>' +
      '<td><span class="otp-code" data-code>' + fmtOtpCode(a.code) + '</span>' +
        (a.error ? ' <span class="tag err">' + escapeHtml(a.error) + '</span>' : '') + '</td>' +
      '<td style="min-width:140px">' +
        '<div class="otp-wrap"><div class="otp-bar' + (pct < 25 ? ' low' : '') + '"><i style="width:' + pct.toFixed(1) + '%"></i></div>' +
        '<span class="hint" data-remain>' + Math.max(0, Math.ceil((a.deadline - Date.now()) / 1000)) + 's</span></div>' +
      '</td>' +
      '<td><div class="row tight">' +
        '<button class="btn small" data-copy="1">复制</button>' +
        '<button class="btn small ghost" data-del="1">删除</button>' +
      '</div></td>' +
      '</tr>';
  }).join('');
  wrap.innerHTML = '<table><thead><tr><th>账号</th><th>验证码</th><th>剩余</th><th>操作</th></tr></thead><tbody>' + rows + '</tbody></table>';

  wrap.querySelectorAll('tr[data-id]').forEach((tr) => {
    const account = TOTP.find((a) => a.id === tr.dataset.id);
    if (!account) return;
    const copyBtn = tr.querySelector('[data-copy]');
    if (copyBtn) copyBtn.onclick = () => copyText(account.code || '');
    const delBtn = tr.querySelector('[data-del]');
    if (delBtn) delBtn.onclick = () => removeTotp(account.id, account.label);
  });
}

function tickTotp() {
  if (!TOTP.length) return;
  let expire = false;
  TOTP.forEach((a) => {
    const period = Math.max(1, Number(a.period || 30));
    const left = (a.deadline - Date.now()) / 1000;
    if (left <= 0) { expire = true; return; }
    a.remaining = Math.ceil(left);
    const tr = document.querySelector('#totpWrap tr[data-id="' + a.id + '"]');
    if (!tr) return;
    const bar = tr.querySelector('.otp-bar');
    const remain = tr.querySelector('[data-remain]');
    const pct = Math.max(0, Math.min(100, left / period * 100));
    if (bar) {
      bar.querySelector('i').style.width = pct.toFixed(1) + '%';
      bar.classList.toggle('low', pct < 25);
    }
    if (remain) remain.textContent = Math.ceil(left) + 's';
  });
  if (expire) loadTotp();
}

async function addTotp() {
  const secret = $('otpSecret').value.trim();
  if (!secret) return setOtpStatus('请先粘贴 2FA 密钥或 otpauth:// 链接', 'err');
  const period = $('otpPeriod').value.trim();
  const payload = { secret, label: $('otpLabel').value.trim() };
  if (period) {
    const num = Number(period);
    if (!Number.isFinite(num) || num < 10 || num > 120) return setOtpStatus('周期必须是 10-120 的秒数', 'err');
    if (!secret.toLowerCase().startsWith('otpauth://')) {
      payload.secret = 'otpauth://totp/ChatGPT?secret=' + encodeURIComponent(secret) + '&period=' + num;
    }
  }
  try {
    const data = await api('/api/totp/add', payload);
    if (!data.ok) return setOtpStatus(data.error || '添加失败', 'err');
    $('otpSecret').value = '';
    $('otpLabel').value = '';
    $('otpPeriod').value = '';
    setOtpStatus('已添加 ' + ((data.data && data.data.label) || 'ChatGPT'), 'ok');
    await loadTotp();
  } catch (e) {
    setOtpStatus('请求异常: ' + e.message, 'err');
  }
}

async function previewTotp() {
  const secret = $('otpSecret').value.trim();
  if (!secret) return setOtpStatus('请先粘贴 2FA 密钥', 'err');
  try {
    const data = await api('/api/totp/preview', { secret });
    if (!data.ok) return setOtpStatus(data.error || '试算失败', 'err');
    const d = data.data || {};
    setOtpStatus('试算成功：' + (d.code || '') + '（剩余 ' + (d.remaining || 0) + 's）。确认无误再点「添加账号」。', 'ok');
  } catch (e) {
    setOtpStatus('请求异常: ' + e.message, 'err');
  }
}

async function removeTotp(id, label) {
  if (!confirm('删除账号「' + (label || 'ChatGPT') + '」的 2FA 密钥？')) return;
  try {
    const data = await api('/api/totp/remove', { id });
    if (!data.ok) return toast(data.error || '删除失败', 'err');
    toast('已删除', 'ok');
    await loadTotp();
  } catch (e) {
    toast('请求异常: ' + e.message, 'err');
  }
}

$('otpSecret').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') addTotp();
});

/* ---------------------------------- 设置 ---------------------------------- */
async function loadConfig() {
  try {
    const data = await api('/api/config', {});
    if (!data.ok) return;
    const c = data.data || {};
    $('cfgBaseUrl').value = c.base_url || '';
    $('cfgApiKey').value = c.api_key || '';
    $('cfgApiSecret').value = '';
    $('cfgWebhookSecret').value = '';
    $('cfgTimeout').value = c.timeout || 40;
    $('cfgApiProxy').value = c.api_proxy || '';
    $('cfgChainUpstream').value = c.chain_upstream || 'http://127.0.0.1:7890';
    $('cfgApiSecret').placeholder = c.api_secret_set ? ('已保存 ' + (c.api_secret_masked || '') + '，留空不修改') : '未设置';
    $('cfgWebhookSecret').placeholder = c.webhook_secret_set ? ('已保存 ' + (c.webhook_secret_masked || '') + '，留空不修改') : '未设置';
    $('hdrCfg').textContent = c.api_key && c.api_secret_set ? '密钥已配置' : '未配置密钥';
    $('cfgPathNote').textContent = '配置文件：' + (c.config_path || '') + (c.encrypted ? '（Secret 已用 Windows DPAPI 加密）' : '（当前平台仅 base64 存放）');
    if (!$('chainUpstream').value && c.chain_upstream) $('chainUpstream').value = c.chain_upstream;
    if (c.api_key && c.api_secret_set) refreshBalance();
  } catch (e) {}
}

async function saveConfig() {
  const payload = {
    base_url: $('cfgBaseUrl').value.trim(),
    api_key: $('cfgApiKey').value.trim(),
    timeout: Number($('cfgTimeout').value || 40),
    api_proxy: $('cfgApiProxy').value.trim(),
    chain_upstream: $('cfgChainUpstream').value.trim(),
  };
  const secret = $('cfgApiSecret').value.trim();
  const webhook = $('cfgWebhookSecret').value.trim();
  if (secret) payload.api_secret = secret;
  if (webhook) payload.webhook_secret = webhook;
  try {
    const data = await api('/api/config', payload);
    if (!data.ok) return toast(data.error || '保存失败', 'err');
    toast('已保存', 'ok');
    await loadConfig();
  } catch (e) {
    toast('请求异常: ' + e.message, 'err');
  }
}

async function testConfig() {
  setStatus('cfgStatus', '正在请求 AiLink 查询余额...', 'warn');
  try {
    const data = await api('/api/vcc/account-balance', {});
    if (!data.ok) {
      setStatus('cfgStatus', '连接失败: ' + (data.error || ''), 'err');
      return;
    }
    const rows = ((data.data && data.data.balances) || []).map((b) =>
      b.currency + ' 可用 ' + b.availableBalance + '（余额 ' + b.balance + '，冻结 ' + b.frozenAmount + '）').join('；');
    setStatus('cfgStatus', '连接成功：' + rows +
      (data.proxy_note ? '（' + data.proxy_note + '）' :
        (data.auto_proxy ? '（直连被重置，已自动改走 ' + (data.proxy || '本机代理') + '）' : '')), 'ok');
    refreshBalance();
  } catch (e) {
    setStatus('cfgStatus', '请求异常: ' + e.message, 'err');
  }
}

async function refreshBalance() {
  try {
    const data = await api('/api/vcc/account-balance', {});
    if (!data.ok) { $('hdrBalance').textContent = '--'; return; }
    const list = (data.data && data.data.balances) || [];
    const usd = list.find((b) => b.currency === 'USD');
    $('hdrBalance').textContent = usd ? (usd.availableBalance + ' USD') : '--';
  } catch (e) { $('hdrBalance').textContent = '--'; }
}

/* ---------------------------------- 启动 ---------------------------------- */
restoreLinkForm();
if (!$('chainUpstream').value) $('chainUpstream').value = 'http://127.0.0.1:7890';
$('applyRequestNo').value = makeRequestNo();
loadConfig();
loadTotp();
TOTP_TICK = setInterval(tickTotp, 1000);
</script>
</body>
</html>
"""
