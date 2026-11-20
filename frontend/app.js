/* WiFiWeb 前端逻辑：状态轮询、扫描、连接、已保存网络管理 */
"use strict";

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const SCREEN_LABELS = { overview: "概览", scan: "扫描并加入网络", saved: "已保存网络" };

let scanList = [];
let savedNames = new Set();   // 已保存的 WLAN 配置文件名
let autoScanEnabled = true;   // 自动刷新开关（持久化在服务端 app.properties）
let joinTarget = null;
let curStatus = null;
let statusTimer = null;
let confirmAction = null;

/* ================= 通用 ================= */
async function api(path, opts) {
  const r = await fetch(path, opts);
  let d = {};
  try { d = await r.json(); } catch (e) { /* ignore */ }
  if (!r.ok) throw new Error(d.detail || d.error || r.statusText || "请求失败");
  return d;
}

function goto(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.screen === name));
  document.querySelectorAll(".screen").forEach((s) => s.classList.toggle("active", s.id === "screen-" + name));
  $("hdrTitle").textContent = SCREEN_LABELS[name] || "";
  document.body.classList.remove("drawer-open");
  // 用 hash 记录当前页签，F5 刷新后停留在当前页
  try { history.replaceState(null, "", "#/" + name); } catch (e) { /* 忽略 */ }
  if (name === "scan") {
    if (scanList.length === 0) {
      doScan();
    } else {
      renderScan();
    }
    loadSavedNames().then(() => { if ($("screen-scan").classList.contains("active")) renderScan(); });
  }
  if (name === "saved") renderSaved();
  if (name === "overview") loadStatus(true);
}
document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => goto(t.dataset.screen)));

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove("show"), 2600);
}
function openModal(id) { $(id).classList.add("show"); }
function closeModal(id) { $(id).classList.remove("show"); }

/* ESC 关闭当前打开的弹窗（网卡切换、加入网络、确认等） */
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  document.querySelectorAll(".modal-mask.show").forEach((m) => m.classList.remove("show"));
});

function sigHtml(dbm, pct) {
  // pct 为真实链路质量（0-100，与概览同口径）；未提供时按 dBm 估算
  const p = pct != null ? Math.round(pct) : (dbm != null ? Math.round((dbm + 100) * 2) : null);
  const level = p != null ? (p >= 75 ? 4 : p >= 55 ? 3 : p >= 35 ? 2 : 1) : 1;
  const text = dbm != null ? `${dbm} dBm（${p}%）` : (p != null ? `${p}%` : "未知");
  return `<span class="sig s${level}"><i></i><i></i><i></i><i></i></span><span class="sig-badge">${text}</span>`;
}

function askConfirm(html, action) {
  $("cfText").innerHTML = html;
  confirmAction = action;
  openModal("confirmModal");
}
$("cfOk").onclick = () => {
  closeModal("confirmModal");
  if (confirmAction) { const a = confirmAction; confirmAction = null; a(); }
};

/* ================= 概览 / 状态 ================= */
function renderStatus(st) {
  curStatus = st;
  const pill = $("connPill");
  pill.classList.toggle("off", !st.connected);
  $("pillText").textContent = st.connected ? `已连接：${st.ssid}` : "未连接";
  $("ovIfName").textContent = st.interface ? `接口：${st.interface}` : "";
  $("ovConnected").style.display = st.connected ? "" : "none";
  $("ovDisconnected").style.display = st.connected ? "none" : "";
  $("btnDisconnect").disabled = !st.connected;
  $("ovSsid").textContent = st.ssid || "-";
  $("ovBssid").textContent = st.bssid || "-";
  $("ovSig").innerHTML = st.connected ? (st.signal ? sigHtml(st.dbm, st.signal) : '<span class="muted">受系统位置策略限制</span>') : "-";
  const chTxt = [st.channel != null ? st.channel : null, st.band]
    .filter(Boolean)
    .join("（") + (st.band ? "）" : "");
  $("ovCh").textContent = st.connected ? (chTxt || "-") : "-";
  $("ovEnc").textContent = st.connected ? (st.auth || "-") : "-";
  $("ovIp").textContent = st.ip || "-";
  $("ovIpv6").textContent = st.ipv6 || "-";
  const hi = $("hdrIface");
  if (hi) {
    const sub = [st.ifaceBus, st.ifaceDesc].filter(Boolean).join(" - ");
    hi.textContent = st.interface ? `${st.interface}${sub ? `（${sub}）` : ""}` : "";
  }
  const note = $("ovLimitNote");
  if (note) note.style.display = st.connected && st.location_limited ? "" : "none";
  // /api/status 携带配置信息：开关/间隔/倒计时随心跳自动同步
  syncScanConfigFromStatus(st);
}

function syncScanConfigFromStatus(st) {
  if (st.autoScanInterval != null) {
    const v = Math.min(600, Math.max(3, parseInt(st.autoScanInterval, 10) || autoScanInterval));
    const inp = $("autoScanSec");
    if (v !== autoScanInterval) {
      autoScanInterval = v;
      // 用户正在输入时不覆盖输入框
      if (document.activeElement !== inp) inp.value = v;
    }
  }
  if (st.nextScanIn != null) {
    // 下次扫描时间由后端计算下发
    nextScanAt = Date.now() + Math.max(0, st.nextScanIn) * 1000;
  } else {
    nextScanAt = 0;   // 关闭/空闲：不显示倒计时
  }
  if (st.autoScanEnabled != null) {
    const enabled = !!st.autoScanEnabled;
    if (enabled !== autoScanEnabled) {
      autoScanEnabled = enabled;
      $("autoScanSwitch").checked = enabled;
    }
  }
}

async function loadStatus(notify) {
  try {
    const st = await api("/api/status");
    renderStatus(st);
    // 连接状态变化时同步刷新扫描列表的“已连接”标记
    if ($("screen-scan").classList.contains("active") && scanList.length) renderScan();
    if (notify) toast("已刷新当前无线状态");
  } catch (e) {
    $("pillText").textContent = "状态获取失败";
    if (notify) toast("获取状态失败：" + e.message);
  }
}

function confirmDisconnect() {
  askConfirm(`确定要断开与 <b>${curStatus && curStatus.ssid ? curStatus.ssid : ""}</b> 的连接吗？<br><span style="font-size:12px;color:#8a95a3">断开后可通过“已保存网络”一键重连。</span>`, async () => {
    try {
      await api("/api/disconnect", { method: "POST" });
      await loadStatus();
      toast("已断开连接");
    } catch (e) {
      toast("断开失败：" + e.message);
    }
  });
}

/* ================= 扫描 ================= */
let hasScanned = false;
let renderedRows = {};   // bssid -> 已渲染的行 HTML（用于增量比较）

function scanRowHtml(n) {
  const isCur = curStatus && curStatus.connected && n.ssid === curStatus.ssid;
  const saved = savedNames.has(n.ssid) && !isCur;   // 已连接的网络不再显示（已保存）
  return `<tr data-bssid="${n.bssid}">
      <td data-label="信号">${sigHtml(n.dbm, n.signal)}</td>
      <td data-label="SSID">${n.hidden ? '<span class="hidden-ssid">隐藏</span>' : n.ssid}${saved ? ' <span class="muted">（已保存）</span>' : ""}${isCur ? ' <span class="cur-conn">（已连接）</span>' : ""}</td>
      <td data-label="频段">${n.band || "-"}</td>
      <td data-label="信道">${n.channel != null ? n.channel : "-"}</td>
      <td data-label="模式">${n.radio || "-"}</td>
      <td data-label="BSSID">${n.bssid}</td>
      <td data-label="加密">${n.auth || "-"}</td>
      <td class="actions"><button class="btn primary small w-fixed" data-act="${saved ? "connect" : "join"}" ${isCur ? "disabled" : ""}>${isCur ? "已连接" : (saved ? "连接" : "加入网络")}</button></td>
    </tr>`;
}

async function loadSavedNames() {
  try {
    const d = await api("/api/profiles");
    savedNames = new Set((d.profiles || []).map(p => p.name));
  } catch (e) { /* 加载失败不影响扫描列表 */ }
}

async function connectSavedFromScan(ssid) {
  toast(`正在连接 ${ssid} ……`);
  try {
    await api("/api/profiles/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssid }),
    });
    await pollConnect(ssid);
    toast("已连接到 " + ssid);
  } catch (e) {
    toast("连接失败：" + e.message);
  }
  renderScan();
}

let scanFetching = false;   // 是否有 /api/scan 请求在途（renderScan 据此决定是否显示“扫描中”）

function renderScan() {
  const tbody = $("scanRows");
  // 清除占位行（无 data-bssid）
  tbody.querySelectorAll("tr.scanning-row").forEach(tr => tr.remove());
  // 筛选（SSID 包含 / 频段 / BSSID 包含，大小写不敏感）
  const kwSsid = ($("fltSsid").value || "").trim().toLowerCase();
  const kwBssid = ($("fltBssid").value || "").trim().toLowerCase();
  const fltBand = $("fltBand").value;
  // 按信号降序排列 + 过滤
  const sorted = [...scanList].sort((a, b) => (b.dbm ?? -999) - (a.dbm ?? -999))
    .filter(n => (!kwSsid || (!n.hidden && n.ssid.toLowerCase().includes(kwSsid)))
             && (!kwBssid || n.bssid.toLowerCase().includes(kwBssid))
             && (!fltBand || n.band === fltBand));
  const newBssids = new Set(sorted.map(n => n.bssid));

  // 收集现有行（bssid -> tr），用于增量对比
  const oldTrs = {};
  tbody.querySelectorAll("tr[data-bssid]").forEach(tr => oldTrs[tr.dataset.bssid] = tr);
  const oldHtml = { ...renderedRows };

  // 删除已消失的网络
  let removed = 0;
  for (const [bssid, tr] of Object.entries(oldTrs)) {
    if (!newBssids.has(bssid)) { tr.remove(); removed++; }
  }

  // 插入新行 / 更新有变化的行（末尾 append 顺序即完成排序重排，无闪烁）
  let added = 0, updated = 0;
  const newRendered = {};
  for (const n of sorted) {
    const html = scanRowHtml(n);
    newRendered[n.bssid] = html;
    const tr = oldTrs[n.bssid];
    if (!tr) {
      tbody.insertAdjacentHTML("beforeend", html);
      added++;
    } else if (oldHtml[n.bssid] !== html) {
      tr.outerHTML = html;
      updated++;
    } else {
      tbody.appendChild(tr);  // 重新排序
    }
  }
  renderedRows = newRendered;

  // 空列表占位
  if (scanList.length === 0) {
    tbody.innerHTML = scanFetching
      ? `<tr class="scanning-row"><td colspan="8"><div class="spinner" style="margin-bottom:10px;"></div>正在扫描附近的无线网络……</td></tr>`
      : hasScanned
        ? `<tr class="scanning-row"><td colspan="8">未发现任何无线网络</td></tr>`
        : `<tr class="scanning-row"><td colspan="8">点击「重新扫描」搜索附近的无线网络</td></tr>`;
  } else if (sorted.length === 0) {
    tbody.innerHTML = `<tr class="scanning-row"><td colspan="8">无匹配的网络（请调整筛选条件）</td></tr>`;
  }
  return { added, removed, updated };
}

async function doScan(force = false) {
  if (scanFetching) return;
  scanFetching = true;
  const emptyList = scanList.length === 0;
  // 界面统一按 force 的呈现：强制重扫、以及切网卡后列表为空时，都禁用按钮、暂停倒计时、显示“扫描中”
  if (force || emptyList) {
    $("btnScan").disabled = true;
    nextScanAt = 0;   // 扫描完成后由后端下发新的倒计时
    $("scanCountdown").textContent = "";
    $("scanStatus").textContent = emptyList ? "扫描中……" : "扫描中……（列表保留，仅增量更新）";
    if (emptyList) renderScan();   // 列表为空时立即显示“正在扫描”占位行
  }
  try {
    const d = await api("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // force 参数始终只由形参决定；切网卡后进入扫描页传的就是 false（走缓存路径，不触发强制 WlanScan）
      body: JSON.stringify({ force: !!force }),
    });
    scanList = d.networks || [];
    hasScanned = true;
    const stats = renderScan();
    const parts = [];
    if (stats.added) parts.push(`新增 ${stats.added}`);
    if (stats.removed) parts.push(`消失 ${stats.removed}`);
    if (stats.updated) parts.push(`更新 ${stats.updated}`);
    $("scanStatus").textContent = `共 ${scanList.length} 个网络` + (parts.length ? `（${parts.join("，")}）` : "（无变化）");
    if (force) toast(`扫描完成，共发现 ${scanList.length} 个网络`);
  } catch (e) {
    $("scanStatus").textContent = "";
    toast("扫描失败：" + e.message);
  } finally {
    scanFetching = false;
    if (force || emptyList) {
      $("btnScan").disabled = false;
      // 临时按当前间隔预估倒计时，下次心跳会用后端计算的值覆盖
      if (autoScanEnabled) nextScanAt = Date.now() + autoScanInterval * 1000;
      renderScan();   // 请求已结束：空结果/失败时把占位行从“扫描中”切回真实提示
    }
  }
}

/* openwrt 式自动刷新：开关 + 可调间隔；倒计时由后端计算下发（nextScanIn） */
let autoScanInterval = 15;
let nextScanAt = 0;   // 下次扫描的本地时间戳（ms），由 /api/status 下发

async function loadConfig() {
  // 从服务端 app.properties 读取配置（开关状态 + 刷新间隔）
  try {
    const c = await api("/api/config");
    autoScanEnabled = !!c.autoScanEnabled;
    autoScanInterval = Math.min(600, Math.max(3, parseInt(c.autoScanInterval, 10) || 15));
  } catch (e) { /* 服务不可用时使用默认值 */ }
  $("autoScanSec").value = autoScanInterval;
  $("autoScanSwitch").checked = autoScanEnabled;
}

async function saveConfig() {
  try {
    await api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ autoScanEnabled, autoScanInterval }),
    });
  } catch (e) { /* 保存失败不影响本次会话 */ }
}

function onAutoScanSecChange(input) {
  let v = parseInt(input.value, 10);
  if (isNaN(v)) v = autoScanInterval;
  v = Math.min(600, Math.max(3, v));
  input.value = v;
  autoScanInterval = v;
  saveConfig();
  toast(`自动刷新间隔已设为 ${v} 秒`);
  if (autoScanEnabled) nextScanAt = Date.now() + v * 1000;   // 预估倒计时，心跳会用后端计算的值覆盖
}

// 每秒渲染一次倒计时（时间基准来自后端 /api/status 下发的 nextScanIn）
function scanTick() {
  const cd = $("scanCountdown");
  if (!nextScanAt) {
    cd.textContent = "";
    return;
  }
  const left = Math.round((nextScanAt - Date.now()) / 1000);
  if (left <= 0) {
    nextScanAt = 0;
    cd.textContent = "";
    if (autoScanEnabled && hasScanned) doScan(false);   // 到 0 获取后端最新列表
    return;
  }
  cd.textContent = `下次刷新 ${left}s`;
}

function onAutoScanSwitch(on) {
  autoScanEnabled = on;
  saveConfig();
  if (on) {
    // 后端下个心跳会下发准确的倒计时，这里先按间隔预估
    nextScanAt = Date.now() + autoScanInterval * 1000;
    toast(`已开启自动刷新（每 ${autoScanInterval} 秒）`);
  } else {
    nextScanAt = 0;
    toast("已关闭自动刷新");
  }
}

function clearScanFilters() {
  $("fltSsid").value = "";
  $("fltBssid").value = "";
  $("fltBand").value = "";
  renderScan();
}
$("fltSsid").addEventListener("input", () => renderScan());
$("fltBssid").addEventListener("input", () => renderScan());
$("fltBand").addEventListener("change", () => renderScan());

$("scanRows").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-act='join'], button[data-act='connect']");
  if (!btn || btn.disabled) return;
  const tr = btn.closest("tr");
  const net = tr && scanList.find(n => n.bssid === tr.dataset.bssid);
  if (!net) return;
  if (btn.dataset.act === "connect") {
    connectSavedFromScan(net.ssid);
  } else {
    openJoin(net);
  }
});

/* ================= 加入网络 ================= */
function fillJoinModal(target) {
  joinTarget = target;
  $("jmSsid").textContent = target.hidden ? "隐藏" : target.ssid;
  $("jmBssid").textContent = target.bssid || "-";
  $("jmEnc").textContent = target.encText || (target.open ? "无（开放网络）" : "自动判断");
  $("jmPwdField").style.display = target.open ? "none" : "";
  $("jmOpenNote").style.display = target.open ? "" : "none";
  $("jmSsidField").style.display = target.ssidEditable ? "" : "none";
  $("jmSsidHint").textContent = target.manual
    ? "密码留空将按开放网络添加。"
    : "隐藏网络不会出现在扫描列表中，请输入准确的网络名称。";
  $("jmPwd").value = "";
  $("jmSsidInput").value = target.ssid || "";
  $("jmAuto").checked = true;
  jmValidate();
  openModal("joinModal");
}

function openJoin(net) {
  fillJoinModal({
    ssid: net.ssid,
    bssid: net.bssid,
    open: !!net.open,
    hidden: !!net.hidden,
    auth_hint: net.auth,
    ssidEditable: !!net.hidden,
  });
}

function openManual() {
  fillJoinModal({ ssid: "", bssid: "-", open: false, hidden: true, manual: true, ssidEditable: true });
}

function jmValidate() {
  const pwd = $("jmPwd").value;
  const ssidShown = $("jmSsidField").style.display !== "none";
  const pwdShown = $("jmPwdField").style.display !== "none";
  const ssidOk = !ssidShown || $("jmSsidInput").value.trim() !== "";
  const pwdOk = !pwdShown || (joinTarget && joinTarget.manual && pwd === "") || pwd.length >= 8;
  $("jmJoinBtn").disabled = !ssidOk || !pwdOk;
}

function togglePwd() {
  const p = $("jmPwd");
  const eye = document.querySelector(".pwd-eye");
  const show = p.type === "password";
  p.type = show ? "text" : "password";
  eye.textContent = show ? "隐藏" : "显示";
}

// 输入信息后按回车 = 点击“加入网络”（仅在信息有效时）
for (const id of ["jmPwd", "jmSsidInput"]) {
  $(id).addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !$("jmJoinBtn").disabled) {
      ev.preventDefault();
      startJoin();
    }
  });
}

/* 轮询连接结果 */
async function pollConnect(ssid, timeoutMs = 30000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    await sleep(1500);
    try {
      const st = await api("/api/status");
      renderStatus(st);
      if (st.connected && st.ssid === ssid) return st;
    } catch (e) { /* 轮询中单次失败忽略 */ }
  }
  throw new Error("连接超时：请检查密码是否正确、信号是否良好");
}

async function startJoin() {
  const ssid = joinTarget.ssidEditable ? $("jmSsidInput").value.trim() : joinTarget.ssid;
  const pwd = $("jmPwd").value;
  const keep = $("jmAuto").checked;
  const open = joinTarget.open || (joinTarget.manual && pwd === "");
  closeModal("joinModal");

  $("pgSsid").textContent = ssid;
  $("pgRunning").style.display = "";
  $("pgOk").style.display = "none";
  $("pgFail").style.display = "none";
  $("pgClose").style.display = "none";
  $("pgRetry").style.display = "none";
  $("pgGo").style.display = "none";
  openModal("progressModal");

  try {
    await api("/api/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ssid,
        password: open ? null : pwd,
        auto: keep,
        hidden: joinTarget.hidden || joinTarget.manual,
        auth_hint: joinTarget.auth_hint,
      }),
    });
    const st = await pollConnect(ssid);
    $("pgRunning").style.display = "none";
    $("pgOk").style.display = "";
    $("pgOkSsid").textContent = st.ssid;
    $("pgOkSig").textContent = st.dbm != null ? `${st.dbm} dBm（${st.signal}%）` : "-";
    $("pgGo").style.display = "";
  } catch (e) {
    $("pgRunning").style.display = "none";
    $("pgFail").style.display = "";
    $("pgFailReason").textContent = e.message;
    $("pgRetry").style.display = "";
    $("pgClose").style.display = "";
  }
}

function backToJoin() {
  closeModal("progressModal");
  openModal("joinModal");
}
function afterJoin() {
  closeModal("progressModal");
  loadSavedNames();
  goto("overview");
  toast("连接成功：" + curStatus.ssid);
}

/* ================= 已保存网络 ================= */
async function renderSaved() {
  $("savedRows").innerHTML = `<tr class="scanning-row"><td colspan="5">加载中…</td></tr>`;
  try {
    const d = await api("/api/profiles");
    const list = d.profiles || [];
    $("savedRows").innerHTML =
      list
        .map(
          (p) => `<tr>
        <td data-label="SSID">${p.name}</td>
        <td data-label="加密">${p.auth || "-"}</td>
        <td data-label="自动连接"><label class="switch"><input type="checkbox" data-auto="${p.name}" ${p.auto ? "checked" : ""}><span class="slider"></span></label></td>
        <td data-label="状态">${p.connected ? '<span class="cur-conn">已连接</span>' : '<span class="muted">未连接</span>'}</td>
        <td class="actions">
          ${p.connected ? "" : `<button class="btn primary small" data-connect="${p.name}">连接</button>`}
          <button class="btn danger small" data-forget="${p.name}">忘记</button>
        </td>
      </tr>`
        )
        .join("") || savedEmptyHtml(d.interface);
  } catch (e) {
    $("savedRows").innerHTML = `<tr class="scanning-row"><td colspan="5">${e.message}</td></tr>`;
  }
}

/* 已保存网络为空时的提示：Windows 按适配器分开存储，需说明当前网卡与查看方式 */
function savedEmptyHtml(ifname) {
  const name = ifname || "当前无线网卡";
  return `<tr class="scanning-row"><td colspan="5">
      <div style="margin-bottom:6px;">「${name}」上没有已保存的无线网络</div>
      <div style="font-size:12px;color:#8a95a3;line-height:1.7;">
        Windows 的无线配置按网卡分开保存，其他网卡上保存过的网络不会显示在这里。<br>
        可切换到对应的无线网卡查看，或在「扫描并加入网络」中加入要连接的热点。
      </div>
    </td></tr>`;
}

$("savedRows").addEventListener("click", async (ev) => {
  const connectBtn = ev.target.closest("button[data-connect]");
  const forgetBtn = ev.target.closest("button[data-forget]");
  if (connectBtn) {
    const ssid = connectBtn.dataset.connect;
    toast(`正在连接 ${ssid} ……`);
    try {
      await api("/api/profiles/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ssid }),
      });
      await pollConnect(ssid);
      toast("已连接到 " + ssid);
    } catch (e) {
      toast("连接失败：" + e.message);
    }
    renderSaved();
  } else if (forgetBtn) {
    const ssid = forgetBtn.dataset.forget;
    askConfirm(`确定要忘记网络 <b>${ssid}</b> 吗？<br><span style="font-size:12px;color:#8a95a3">将删除其保存的密码，重新连接时需再次输入。</span>`, async () => {
      try {
        await api("/api/profiles/" + encodeURIComponent(ssid), { method: "DELETE" });
        toast("已忘记网络 " + ssid);
      } catch (e) {
        toast("操作失败：" + e.message);
      }
      renderSaved();
    });
  }
});

$("savedRows").addEventListener("change", async (ev) => {
  const chk = ev.target.closest("input[data-auto]");
  if (!chk) return;
  const ssid = chk.dataset.auto;
  try {
    await api("/api/profiles/auto", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssid, auto: chk.checked }),
    });
    toast(`已${chk.checked ? "开启" : "关闭"}「${ssid}」自动连接`);
  } catch (e) {
    toast("操作失败：" + e.message);
    chk.checked = !chk.checked;
  }
});

/* ================= 无线网卡选择 ================= */
async function checkInterfaces() {
  try {
    const d = await api("/api/interfaces");
    const list = d.list || [];
    if (list.length < 2 || d.selected) return;   // 单网卡或已选择过 → 不弹
    renderIfaceModal(list);
    openModal("ifaceModal");
  } catch (e) { /* 接口列表失败不影响主流程 */ }
}

async function openIfaceModal() {
  try {
    const d = await api("/api/interfaces");
    renderIfaceModal(d.list || []);
    const cur = (d.list || []).find(x => x.selected) || (d.list || [])[0];
    if (cur) {
      const r = document.querySelector(`input[name=ifaceSel][value="${cur.guid}"]`);
      if (r) r.checked = true;
    }
    openModal("ifaceModal");
  } catch (e) {
    toast("获取网卡列表失败：" + e.message);
  }
}

function renderIfaceModal(list) {
  // 内置网卡排在 USB 前面
  const order = { "内置 (PCI)": 0, "USB": 1 };
  const sorted = [...list].sort((a, b) => (order[a.bus] ?? 9) - (order[b.bus] ?? 9));
  $("ifaceList").innerHTML = sorted.map(it => {
    const sub = [it.bus, it.desc].filter(Boolean).join(" - ");
    return `
    <label class="iface-item">
      <input type="radio" name="ifaceSel" value="${it.guid}" ${it.selected ? "checked" : ""}>
      <div class="iface-info">
        <b>${it.name || "?"}</b><span class="muted" style="font-size:12px;">（${sub}）</span>
        <div class="muted" style="font-size:12px;">${it.state}${it.ssid ? " · 已连接 " + it.ssid : ""}</div>
      </div>
    </label>`;
  }).join("");
}

async function selectInterface() {
  const sel = document.querySelector("input[name=ifaceSel]:checked");
  if (!sel) return;
  try {
    await api("/api/interfaces/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ guid: sel.value }),
    });
    scanList = [];
    renderedRows = {};
    hasScanned = false;
    // 切换网卡后清空筛选（残留筛选会隐藏新网卡扫到的网络）
    $("fltSsid").value = "";
    $("fltBssid").value = "";
    $("fltBand").value = "";
    closeModal("ifaceModal");
    toast("已切换无线网卡");
    loadStatus(true);
    // 已保存配置按适配器分开存储，切换网卡后同步刷新扫描列表的"（已保存）"标记
    loadSavedNames().then(() => { if ($("screen-scan").classList.contains("active")) renderScan(); });
    const active = document.querySelector(".screen.active").id;
    if (active === "screen-scan") doScan(true);
    else if (active === "screen-saved") renderSaved();   // 已保存网络按网卡分开存储，切换后需刷新
  } catch (e) {
    toast("切换失败：" + e.message);
  }
}

/* ================= 初始化 ================= */
// 每秒渲染一次倒计时；时间基准由后端 /api/status 下发（nextScanIn）
setInterval(scanTick, 1000);
// 从服务端加载配置，再按 URL hash 恢复页签（F5 刷新保留）
loadConfig().then(() => {
  const hashScreen = (location.hash || "").replace(/^#\/?/, "");
  goto(SCREEN_LABELS[hashScreen] ? hashScreen : "overview");
  loadStatus();
  checkInterfaces();   // 多网卡时弹窗选择
});
// 全局心跳：每 4 秒轮询状态（后端据此判断有无客户端，决定是否继续定时扫描）
setInterval(() => {
  loadStatus();
}, 4000);
