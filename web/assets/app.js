/* Dashboard app — vanilla JS, no dependencies. */
"use strict";

const TOKEN = window.__ADMIN_TOKEN__ || "";
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  route: "overview",
  overview: null,
  items: [],
  itemFilter: "all",
  itemQuery: "",
  selectedItem: null,
  detail: null,
  tasks: [],
  outbox: [],
  downstream: null,
  siteState: null,
  siteHealth: null,
  filters: { batch: "" },
  busy: false,
};

const STATE_LABEL = { active: "在用", quarantined: "待复核", retracted: "已撤回" };
const STATE_PILL = { active: "pill-active", quarantined: "pill-quarantined", retracted: "pill-retracted" };
const DECISION_LABEL = {
  retract: "撤回",
  quarantine: "进入待复核",
  restore: "恢复",
  keep: "维持",
  hold: "人工冻结",
};
const REASON_LABEL = {
  source_withdrawal_notice: "来源明确的撤回通知",
  multi_scan_multi_region_complete_absence: "多次完整扫描跨地区确认目录移除",
  insufficient_evidence: "证据不足，暂不删除",
  ambiguous_observation: "观测存在歧义（地区限制/错误页/抓取不全）",
  present_in_complete_scan: "完整扫描中仍可见",
  no_signal: "无变化",
  manual_hold: "运营人工冻结",
};
const BODY_LABEL = {
  ok: "完整",
  tombstone: "撤回墓碑页",
  withdrawn: "撤回",
  not_found: "404",
  blocked: "地区限制",
  error_page: "200 错误页",
  incomplete: "抓取不全",
  error: "请求失败",
  not_checked: "未探测",
  skipped: "跳过",
};

function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return esc(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtRelative(iso) {
  if (!iso) return "—";
  const d = new Date(iso).getTime();
  if (Number.isNaN(d)) return "—";
  const secs = Math.max(0, Math.round((Date.now() - d) / 1000));
  if (secs < 60) return `${secs} 秒前`;
  if (secs < 3600) return `${Math.floor(secs / 60)} 分钟前`;
  if (secs < 86400) return `${Math.floor(secs / 3600)} 小时前`;
  return `${Math.floor(secs / 86400)} 天前`;
}

function num(value) {
  if (value == null) return "—";
  return Number(value).toLocaleString("zh-CN");
}

function toast(message, bad = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast show" + (bad ? " bad" : "");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { el.className = "toast"; }, 3600);
}

async function api(path, options = {}) {
  const init = {
    method: options.method || "GET",
    headers: { "X-Admin-Token": TOKEN, "X-Requested-By": "dashboard" },
  };
  if (options.body !== undefined) {
    init.method = options.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const resp = await fetch(path, init);
  let data = null;
  try { data = await resp.json(); } catch (e) { data = null; }
  if (!resp.ok) {
    const msg = (data && (data.error || data.message)) || `请求失败 (${resp.status})`;
    throw new Error(msg);
  }
  return data;
}

async function act(label, fn) {
  if (state.busy) return;
  state.busy = true;
  $$("button").forEach((b) => { if (b.dataset.busy !== "off") b.disabled = true; });
  try {
    const result = await fn();
    if (label) toast(label);
    return result;
  } catch (err) {
    toast(err.message || String(err), true);
  } finally {
    state.busy = false;
    $$("button").forEach((b) => { b.disabled = false; });
  }
}

/* ------------------------------------------------------------------ routing */
const ROUTES = {
  overview: { title: "总览", sub: "删除结论的依据、补扫与下游状态", render: renderOverview },
  items: { title: "资料与依据", sub: "每条结论都能回到具体扫描与通知", render: renderItems },
  proof: { title: "证据工作台", sub: "逐条查看删除门槛、成本与误删风险", render: renderProof },
  tasks: { title: "补扫任务", sub: "失败扫描自动排队，手动补扫可追溯", render: renderTasks },
  delivery: { title: "下游投递", sub: "事件幂等、乱序防护与当前内容", render: renderDelivery },
  lab: { title: "故障演练台", sub: "切换地区与故障，验证判定不会误删", render: renderLab },
};

function parseRoute() {
  const hash = location.hash.replace(/^#\/?/, "");
  const [route, arg] = hash.split("/");
  return { route: ROUTES[route] ? route : "overview", arg: arg || "" };
}

async function navigate() {
  const { route, arg } = parseRoute();
  state.route = route;
  $$(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.nav === route));
  $("#page-title").textContent = ROUTES[route].title;
  $("#page-sub").textContent = ROUTES[route].sub;
  if (route === "items" && arg) {
    await openItem(arg);
    state.selectedItem = arg;
  }
  await refresh();
}

async function refresh() {
  const view = $("#view");
  try {
    const jobs = [api("/api/overview")];
    if (state.route === "tasks") jobs.push(api("/api/tasks"));
    if (state.route === "delivery") jobs.push(api("/api/outbox"), api("/api/downstream"));
    if (state.route === "lab") jobs.push(api("/api/site/state"), api("/api/site/health"));
    if (state.route === "items" || state.route === "proof") {
      jobs.push(api(`/api/items?state=${encodeURIComponent(state.itemFilter === "all" ? "" : state.itemFilter)}&q=${encodeURIComponent(state.itemQuery)}`));
    }
    const results = await Promise.all(jobs.map((p) => p.catch((e) => ({ __error: e.message }))));
    if (results[0].__error) throw new Error(results[0].__error);
    state.overview = results[0];
    let idx = 1;
    if (state.route === "tasks") state.tasks = results[idx++].tasks || [];
    if (state.route === "delivery") {
      state.outbox = results[idx++].outbox || [];
      state.downstream = results[idx++];
    }
    if (state.route === "lab") {
      state.siteState = results[idx++];
      state.siteHealth = results[idx++];
    }
    if (state.route === "items" || state.route === "proof") state.items = results[idx++].items || [];
    updateLive();
    view.innerHTML = ROUTES[state.route].render();
    hydrateSearch();
    if (state.route === "proof" && !state.selectedItem && state.items.length) {
      await selectProofItem(state.items[0].id);
    }
  } catch (err) {
    view.innerHTML = `<div class="banner"><div><strong>载入失败</strong>${esc(err.message)}</div></div>`;
  }
}

function updateLive() {
  const o = state.overview;
  const pill = $("#live-pill");
  const side = $("#side-status");
  if (!o) {
    pill.innerHTML = '<span class="dot dot-muted"></span><span id="live-text">未连接</span>';
    return;
  }
  const mine = o.regions.latest || {};
  const bad = Object.entries(mine).filter(([, v]) => v.status !== "ok").map(([k]) => k);
  const cls = bad.length ? "dot-warn" : "dot-good";
  const text = bad.length ? `扫描异常：${bad.join(", ")}` : "采集正常";
  pill.innerHTML = `<span class="dot ${cls}"></span><span id="live-text">${esc(text)}</span>`;
  side.innerHTML = `<span class="dot ${cls}"></span><span>${esc(text)}</span>`;
}

/* ------------------------------------------------------------------ bits */
function kpi(label, value, note, cls = "") {
  return `<div class="card kpi ${cls}">
    <div class="kpi-label">${label}</div>
    <div class="kpi-value">${value}</div>
    <div class="kpi-note">${note}</div>
  </div>`;
}

function meter(label, have, need, note = "", tone = "") {
  const pct = need > 0 ? Math.min(100, Math.round((have / need) * 100)) : (have > 0 ? 100 : 0);
  const done = need > 0 && have >= need;
  const toneCls = tone || (done ? "good" : have > 0 ? "warn" : "");
  return `<div class="meter">
    <div class="meter-top"><span class="meter-label">${label}</span>
      <span class="meter-value">${have}${need ? " / " + need : ""}</span></div>
    <div class="meter-track ${toneCls}-track"><div class="meter-fill ${toneCls}" style="width:${pct}%"></div></div>
    ${note ? `<div class="meter-note">${note}</div>` : ""}
  </div>`;
}

function statePill(value) {
  return `<span class="pill ${STATE_PILL[value] || "pill-neutral"}">${STATE_LABEL[value] || esc(value)}</span>`;
}

function reqChips(ev = {}, req = {}) {
  const scansNeed = (req && req.scans && (req.scans.need || req.scans.min_scans)) || req.min_scans || 3;
  const regionsNeed = (req && req.regions && req.regions.need) || req.min_regions || 2;
  const batchesNeed = (req && req.batches && req.batches.need) || req.min_batches || 2;
  const scansHave = ev.absent_complete_scans || 0;
  const regionsHave = (ev.absent_regions || []).length;
  const batchesHave = (ev.absent_batches || []).length;
  const chips = [];
  chips.push(`<span class="chip ${scansHave >= scansNeed ? "good" : ""}">扫描 ${scansHave}/${scansNeed}</span>`);
  chips.push(`<span class="chip ${regionsHave >= regionsNeed ? "good" : ""}">地区 ${regionsHave}/${regionsNeed}</span>`);
  chips.push(`<span class="chip ${batchesHave >= batchesNeed ? "good" : ""}">批次 ${batchesHave}/${batchesNeed}</span>`);
  if (ev.present_complete_scans) chips.push(`<span class="chip info">可见 ${ev.present_complete_scans}</span>`);
  if (ev.ambiguity_scans) chips.push(`<span class="chip warn">歧义 ${ev.ambiguity_scans}</span>`);
  return `<div class="chip-row">${chips.join("")}</div>`;
}

function scanStatus(status, complete) {
  if (status === "ok" && complete) return '<span class="chip good">完整</span>';
  if (status === "anomaly") return '<span class="chip crit">异常</span>';
  if (status === "running") return '<span class="chip info">进行中</span>';
  return '<span class="chip warn">不完整</span>';
}

/* ------------------------------------------------------------------ overview */
function renderOverview() {
  const o = state.overview;
  const c = o.counts;
  const pending = o.alerts || 0;
  const regionRows = Object.entries(o.regions.latest || {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([region, info]) => `<div class="region-row">
      <span class="dot ${info.status === "ok" ? "dot-good" : "dot-warn"}"></span>
      <b>${esc(region.toUpperCase())}</b>
      <span class="muted tiny">${info.status === "ok" ? "目录完整" : "最近扫描异常"} · ${fmtRelative(info.at)}</span>
      <span class="muted tiny nowrap">${num(info.count)} 条</span>
      <span class="chip ${info.status === "ok" ? "good" : "crit"}">${info.status === "ok" ? "正常" : esc(info.status)}</span>
    </div>`).join("");

  const scanRows = (o.scans || []).slice(0, 8).map((s) => `<tr>
      <td class="mono tiny">${esc(s.id.slice(0, 12))}</td>
      <td>${esc(s.region.toUpperCase())}</td>
      <td>${scanStatus(s.status, s.complete)}</td>
      <td class="num">${num(s.listing_count)}</td>
      <td class="num">${s.detail.population_ratio != null ? Math.round(s.detail.population_ratio * 100) + "%" : "—"}</td>
      <td class="muted nowrap tiny">${fmtRelative(s.started_at)}</td>
    </tr>`).join("") || `<tr><td colspan="6" class="empty-state">尚无扫描记录，先执行一次扫描。</td></tr>`;

  const decisions = (o.decisions || []).slice(0, 7).map((d) => `<tr>
      <td>${statePill(d.decision === "retract" ? "retracted" : d.decision === "quarantine" ? "quarantined" : "active")}</td>
      <td class="tiny">${esc(REASON_LABEL[d.basis] || d.basis)}</td>
      <td class="mono tiny">${esc(d.item_id)}</td>
      <td class="muted tiny nowrap">${fmtRelative(d.at)}</td>
    </tr>`).join("") || `<tr><td colspan="4" class="empty-state">暂无判定记录。</td></tr>`;

  const lastAuto = o.last_auto_scan_at ? fmtRelative(o.last_auto_scan_at) : "未开启自动扫描";
  return `
    ${pending ? `<div class="banner warn"><span class="dot dot-warn" style="margin-top:5px"></span><div>
      <strong>${pending} 条资料在近 3 天内进入过待复核</strong>
      请到“证据工作台”核对是真实撤回、暂时不可见，还是抓取不全。系统不会凭一次空结果删除。
    </div></div>` : ""}
    <div class="grid grid-kpi">
      ${kpi("目录资料", num(c.total), `已纳入台账 · ${esc(o.regions.configured.join(" / "))}`)}
      ${kpi("在用", num(c.active), "当前可发布")}
      ${kpi("待复核", num(c.quarantined), "证据不足或观测歧义", c.quarantined ? "is-accent" : "")}
      ${kpi("已撤回", num(c.retracted), "自动证据 + 来源通知", "is-crit")}
      ${kpi("需人工关注", num(c.review), `撤回通知 ${num(c.notices)} 条`)}
    </div>

    <div class="grid grid-2 mt">
      <div class="card">
        <div class="card-head"><div><h2 class="card-title">删除证据门槛</h2>
          <p class="card-sub">任一门槛未满足，就只会进入待复核，不会删除</p></div></div>
        <div class="card-body stack" style="gap:14px">
          <div class="evidence-grid">
            <div class="meter"><div class="meter-top"><span class="meter-label">完整扫描批次</span><span class="meter-value">≥ ${o.policy.min_scan_batches}</span></div><div class="meter-note">单次多地区扫描只算一个批次</div></div>
            <div class="meter"><div class="meter-top"><span class="meter-label">覆盖地区</span><span class="meter-value">≥ ${o.policy.min_regions}</span></div><div class="meter-note">至少两个地区同时看不到</div></div>
            <div class="meter"><div class="meter-top"><span class="meter-label">完整观测次数</span><span class="meter-value">≥ ${o.policy.min_scans}</span></div><div class="meter-note">目录被屏蔽/截断的扫描不计入</div></div>
            <div class="meter"><div class="meter-top"><span class="meter-label">异常时附加要求</span><span class="meter-value">+${o.policy.anomaly_extra_scans}</span></div><div class="meter-note">出现歧义或目录骤减时提高门槛</div></div>
          </div>
          <div class="reason"><p><strong>自动删除只发生在：</strong>连续多个批次、多个地区的完整目录扫描都确认条目消失，且没有任何一次看到正文或目录条目。</p>
          <p class="tiny muted">单次空结果、目录 200 错误页、列表截断、地区 451/403、错误地区缓存，都只会触发待复核或补扫。</p></div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><div><h2 class="card-title">地区与目录健康</h2>
          <p class="card-sub">站点 ${esc(o.regions.site_url)}</p></div>
          <button class="btn btn-sm" data-action="scan-all">立即扫描</button></div>
        <div class="card-body">
          ${regionRows || '<div class="empty-state">尚无地区扫描记录</div>'}
          <p class="tiny muted" style="margin:12px 0 0">自动扫描：${esc(lastAuto)}${o.auto_scan_interval ? ` · 周期 ${Math.round(o.auto_scan_interval / 3600)}h` : ""}</p>
        </div>
      </div>
    </div>

    <div class="grid grid-2b mt">
      <div class="card">
        <div class="card-head"><div><h2 class="card-title">最近扫描</h2>
          <p class="card-sub">“不完整”不会产生删除证据</p></div>
          <a class="btn btn-sm" href="#/tasks">补扫任务</a></div>
        <div class="card-body table-wrap">
          <table class="data"><thead><tr><th>扫描</th><th>地区</th><th>状态</th><th class="num">条目</th><th class="num">规模比</th><th>时间</th></tr></thead>
          <tbody>${scanRows}</tbody></table>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><div><h2 class="card-title">最近判定</h2>
          <p class="card-sub">每次判定都保存当时的证据快照</p></div>
          <a class="btn btn-sm" href="#/proof">查看依据</a></div>
        <div class="card-body table-wrap">
          <table class="data"><thead><tr><th>结论</th><th>原因</th><th>资料</th><th>时间</th></tr></thead>
          <tbody>${decisions}</tbody></table>
        </div>
      </div>
    </div>`;
}

/* ------------------------------------------------------------------ items */
function itemRowHtml(it) {
  const ev = it.evidence || {};
  return `<tr class="click" data-item-id="${esc(it.id)}">
      <td>
        <div class="strong">${esc(it.title)}</div>
        <div class="tiny muted mono">${esc(it.id)} · ${esc(it.category || "未分类")}</div>
      </td>
      <td>${statePill(it.state)}${it.manual_hold ? ' <span class="chip">人工冻结</span>' : ""}${it.review_needed ? ' <span class="chip warn">需关注</span>' : ""}</td>
      <td>${reqChips(ev, it.requirement)}</td>
      <td class="tiny secondary">${esc(REASON_LABEL[it.last_reason] || it.last_reason || "尚未判定")}</td>
      <td class="muted tiny nowrap">${fmtRelative(it.state_changed_at)}</td>
    </tr>`;
}

function renderItems() {
  const items = state.items || [];
  const rows = items.map(itemRowHtml).join("");

  const counts = state.overview.counts;
  return `
    <div class="card">
      <div class="card-head"><div><h2 class="card-title">资料台账</h2>
        <p class="card-sub">共 ${num(counts.total)} 条 · 在用 ${counts.active} · 待复核 ${counts.quarantined} · 已撤回 ${counts.retracted}</p></div>
        <input class="input" id="item-search" type="search" placeholder="搜索标题或 ID" value="${esc(state.itemQuery)}">
      </div>
      <div class="card-body" style="padding-top:12px">
        <div class="filterbar">
          <div class="seg" id="item-filter">
            ${["all", "active", "quarantined", "retracted"].map((f) => `<button data-filter="${f}" class="${state.itemFilter === f ? "on" : ""}">${f === "all" ? "全部" : STATE_LABEL[f]}</button>`).join("")}
          </div>
          <span class="tiny muted">点击任意行查看删除依据、扫描记录与下游内容</span>
        </div>
        <div class="table-wrap"><table class="data">
          <thead><tr><th>资料</th><th>状态</th><th>证据门槛</th><th>最近原因</th><th>变更时间</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="5" class="empty-state"><strong>没有匹配的资料</strong>调整筛选条件，或先执行一次完整扫描。</td></tr>'}</tbody>
        </table></div>
      </div>
    </div>`;
}

async function selectProofItem(itemId) {
  state.selectedItem = itemId;
  try {
    state.detail = await api(`/api/items/${encodeURIComponent(itemId)}`);
  } catch (e) {
    toast(e.message, true);
    return;
  }
  if (state.route === "proof") {
    const host = $("#proof-detail");
    if (host) host.innerHTML = renderDetailBody(state.detail);
    $$("#proof-list .proof-row").forEach((row) => {
      const on = row.dataset.itemId === itemId;
      row.classList.toggle("active", on);
      row.style.background = on ? "#f7faff" : "";
    });
  }
}

function renderProof() {
  const items = state.items || [];
  const selected = state.selectedItem;
  return `
    <div class="grid grid-2">
      <div class="card" id="proof-list-card">
        <div class="card-head"><div><h2 class="card-title">逐条核对</h2>
          <p class="card-sub">优先显示待复核与已撤回</p></div>
          <div class="seg" id="item-filter">
            ${["all", "quarantined", "retracted"].map((f) => `<button data-filter="${f}" class="${state.itemFilter === f ? "on" : ""}">${f === "all" ? "全部" : STATE_LABEL[f]}</button>`).join("")}
          </div></div>
        <div class="card-body" style="padding-top:8px">
          <div id="proof-list">
            ${items.map((it) => `<div class="region-row proof-row ${selected === it.id ? "active" : ""}" data-item-id="${esc(it.id)}" style="cursor:pointer;padding:10px 6px;border-radius:8px;${selected === it.id ? "background:#f7faff" : ""}">
              <div style="min-width:0;flex:1">
                <div class="strong" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(it.title)}</div>
                <div class="tiny muted mono">${esc(it.id)}</div>
              </div>
              ${statePill(it.state)}
            </div>`).join("") || '<div class="empty-state">暂无资料，请先扫描。</div>'}
          </div>
        </div>
      </div>
      <div class="card"><div class="card-body" id="proof-detail">
        ${state.detail && state.selectedItem ? renderDetailBody(state.detail) : '<div class="empty-state"><strong>选择左侧资料</strong>查看删除依据与补扫记录</div>'}
      </div></div>
    </div>`;
}

/* ------------------------------------------------------------------ detail */
function renderDetailBody(detail) {
  const item = detail.item;
  const ev = detail.evidence || {};
  const req = detail.requirement || {};
  const scans = detail.per_scan || [];
  const decisions = detail.decisions || [];
  const notices = detail.notices || [];
  const downstream = detail.downstream;
  const tasks = detail.tasks || [];
  const outbox = detail.outbox || [];

  const scansNeed = req.scans?.need || req.min_scans || 3;
  const decisionTone = item.state === "retracted" ? "crit" : item.state === "quarantined" ? "warn" : "good";

  const traceRows = scans.slice(0, 10).map((s) => {
    const bad = s.anomaly || (s.catalog === "unknown") || (s.catalog === "blocked");
    const cls = s.catalog === "present" ? "ok" : bad ? "warn" : "crit";
    const body = BODY_LABEL[s.body] || s.body;
    return `<li class="${cls}">
      <div class="trace-time">${fmtTime(s.observed_at)} · ${esc(String(s.region).toUpperCase())} · <span class="mono">${esc(s.scan_id.slice(0, 12))}</span></div>
      <div class="trace-body">
        目录：${s.catalog === "present" ? '<b>出现</b>' : s.catalog === "tombstone" ? '<b>撤回墓碑</b>' : '<b>未出现</b>'}
        · 正文：${esc(body)}
        ${s.anomaly ? ` <span class="chip warn">${esc(s.anomaly)}</span>` : ""}
      </div>
    </li>`;
  }).join("") || '<li><div class="trace-body muted">尚无扫描观测。</div></li>';

  const decisionRows = decisions.slice(0, 6).map((d) => `<tr>
      <td class="nowrap tiny">${fmtTime(d.at)}</td>
      <td>${esc(DECISION_LABEL[d.decision] || d.decision)}</td>
      <td class="tiny secondary">${esc(REASON_LABEL[d.basis] || d.basis)}</td>
      <td class="tiny">${reqChips(d.evidence || {})}</td>
    </tr>`).join("") || '<tr><td colspan="4" class="muted tiny">尚无判定记录</td></tr>';

  const noticeHtml = notices.length ? notices.map((n) => `<div class="reason crit" style="margin-bottom:8px">
      <p><strong>${esc(n.source)}</strong> · ${fmtTime(n.received_at)}</p>
      <p class="tiny">${esc(n.statement)}</p>
      <p class="tiny muted mono">notice_id ${esc(n.notice_id)}${n.duplicate ? " · 重复投递" : ""}</p>
    </div>`).join("") : '<p class="tiny muted">没有收到撤回通知。删除只能来自多批次完整扫描的证据。</p>';

  let downstreamHtml = '<p class="tiny muted">下游尚无该条目记录。</p>';
  if (downstream && downstream.present) {
    const status = downstream.status === "published" ? "已发布" : "已撤下";
    const permanent = Boolean(downstream.permanently_restricted);
    const restrictNote = downstream.status !== "published"
      ? permanent
        ? '<span class="chip crit">永久受限</span> 撤回结论不可被自动扫描解除，只有人工恢复（allow_restore）能重新发布。'
        : '<span class="chip warn">临时撤下</span> 待复核期间的保守处理；完整扫描恢复可见后会自动重新发布。'
      : "";
    downstreamHtml = `<dl class="kv">
      <dt>当前状态</dt><dd><span class="pill ${downstream.status === "published" ? "pill-active" : "pill-retracted"}">${status}</span> ${restrictNote}</dd>
      <dt>事件序号</dt><dd class="mono">${esc(downstream.seq)}</dd>
      <dt>原因</dt><dd class="tiny">${esc(downstream.reason || "—")}</dd>
      <dt>更新时间</dt><dd class="tiny">${fmtTime(downstream.updated_at)}</dd>
    </dl>
    <div class="table-wrap mt"><table class="data"><thead><tr><th>事件</th><th>类型</th><th class="num">序号</th><th>结果</th><th>时间</th></tr></thead><tbody>
      ${(downstream.events || []).slice(0, 5).map((e) => `<tr><td class="mono tiny">${esc(e.event_id)}</td><td class="tiny">${esc(e.event_type)}</td><td class="num tiny">${esc(e.seq)}</td><td class="tiny">${esc(e.outcome)}</td><td class="muted tiny nowrap">${fmtTime(e.received_at)}</td></tr>`).join("")}
    </tbody></table></div>`;
  } else if (downstream && downstream.present === false && item.state !== "active") {
    downstreamHtml = '<p class="tiny muted">下游还没有收到该条目的下架事件，可在“下游投递”页检查发件箱。</p>';
  }

  const taskRows = tasks.length ? tasks.slice(0, 5).map((t) => `<tr>
      <td class="tiny">${t.kind === "body_probe" ? "正文补探" : t.kind === "rescan_region" ? "地区补扫" : "全量补扫"}</td>
      <td>${esc(t.region || "—")}</td>
      <td><span class="chip ${t.state === "done" ? "good" : t.state === "failed" ? "crit" : "warn"}">${esc(t.state)}</span></td>
      <td class="muted tiny">${fmtRelative(t.next_run_at)}</td>
      <td class="tiny muted">${esc(t.last_error || "")}</td>
    </tr>`).join("") : '<tr><td colspan="5" class="muted tiny">没有补扫任务</td></tr>';

  const snapshotHtml = (detail.snapshots || []).slice(0, 3).map((s) => `<div class="tiny muted" style="margin-bottom:6px">
      <span class="mono">${esc(s.content_hash.slice(0, 12))}</span> · ${fmtTime(s.fetched_at)} · ${num(s.bytes)} 字节<br>
      <span class="secondary">${esc((s.excerpt || "").slice(0, 110))}…</span>
    </div>`).join("") || '<p class="tiny muted">没有正文快照（尚未抓取或正文不完整）。</p>';

  const actionButtons = [];
  actionButtons.push(`<button class="btn btn-sm" data-action="probe">补抓正文</button>`);
  actionButtons.push(`<button class="btn btn-sm" data-action="rescan">补扫地区</button>`);
  if (item.state !== "retracted") {
    actionButtons.push(`<button class="btn btn-sm btn-danger" data-action="hold">人工冻结</button>`);
  } else {
    actionButtons.push(`<button class="btn btn-sm" data-action="restore">人工恢复</button>`);
  }
  actionButtons.push(`<button class="btn btn-sm" data-action="review">标记关注</button>`);

  const reviewBanner = item.review_needed ? `<div class="banner warn" style="margin-bottom:14px">
      <div><strong>需要人工关注</strong>${esc(item.review_note || "该条目的判定存在冲突信号。")}</div></div>` : "";

  return `
    <div class="drawer-head" style="border:0;padding:0 0 10px">
      <div><h2 style="font-size:18px">${esc(item.title)}</h2>
        <p class="tiny muted mono" style="margin:0">${esc(item.id)} · ${esc(item.category || "未分类")} · v${esc(item.version)}</p></div>
      <div>${statePill(item.state)}</div>
    </div>
    ${reviewBanner}
    <div class="reason ${decisionTone}">
      <p><strong>${item.state === "retracted" ? "当前结论：已撤回" : item.state === "quarantined" ? "当前结论：待复核（未删除）" : "当前结论：在用"}</strong></p>
      <p>${esc(item.state_reason ? REASON_LABEL[item.state_reason] || item.state_reason : "尚无判定")}</p>
      <p class="tiny muted">依据：${item.state_basis === "source_notice" ? "来源明确的撤回通知" : item.state_basis === "auto_evidence" ? "多批次、跨地区完整扫描证据" : item.state_basis === "manual_hold" ? "运营人工冻结" : item.state_basis === "scan_ambiguous" ? "扫描结果存在歧义" : "最近一次完整扫描"} · ${fmtTime(item.state_changed_at)}</p>
    </div>

    <div class="drawer-section" style="margin-top:18px">
      <h3>删除门槛与证据</h3>
      <div class="evidence-grid">
        ${meter("完整观测次数", ev.absent_complete_scans || 0, scansNeed, "目录被屏蔽或截断的扫描不计入")}
        ${meter("覆盖地区数", (ev.absent_regions || []).length, req.regions?.need || 2, (ev.absent_regions || []).map((r) => esc(r.toUpperCase())).join("、") || "尚无")}
        ${meter("完整扫描批次", (ev.absent_batches || []).length, req.batches?.need || 2, "单次多地区扫描只算一个批次")}
        ${meter("最近完整扫描仍可见", ev.latest_present ? 1 : 0, 1, ev.latest_present ? "存在可见观测，不会删除" : "无可见观测", ev.latest_present ? "good" : "")}
      </div>
      ${ev.population_note ? `<p class="tiny muted" style="margin:10px 0 0">${esc(ev.population_note)}</p>` : ""}
      ${ev.ambiguity_scans ? `<div class="banner warn" style="margin:12px 0 0"><div>
        <strong>歧义观测 ${ev.ambiguity_scans} 次</strong>
        ${esc(Object.entries(ev.ambiguity_kinds || {}).map(([k, v]) => `${k}×${v}`).join("，") || "地区限制 / 错误页 / 抓取不全")}
        —— 这些扫描不会作为删除证据。</div></div>` : ""}
    </div>

    <div class="drawer-section">
      <h3>扫描观测（最近 ${Math.min(scans.length, 10)} 次）</h3>
      <ul class="trace">${traceRows}</ul>
    </div>

    <div class="drawer-section">
      <h3>判定历史</h3>
      <div class="table-wrap"><table class="data"><thead><tr><th>时间</th><th>结论</th><th>原因</th><th>当时证据</th></tr></thead>
        <tbody>${decisionRows}</tbody></table></div>
    </div>

    <div class="drawer-section">
      <h3>撤回通知</h3>
      ${noticeHtml}
    </div>

    <div class="drawer-section">
      <h3>下游当前内容</h3>
      ${downstreamHtml}
    </div>

    <div class="drawer-section">
      <h3>补扫与投递</h3>
      <div class="btn-row" style="margin-bottom:10px">${actionButtons.join("")}</div>
      <div class="table-wrap"><table class="data"><thead><tr><th>任务</th><th>地区</th><th>状态</th><th>下次执行</th><th>最近错误</th></tr></thead>
        <tbody>${taskRows}</tbody></table></div>
      <p class="tiny muted" style="margin:10px 0 4px">发件箱：</p>
      <div class="table-wrap"><table class="data"><thead><tr><th>事件</th><th class="num">序号</th><th>状态</th><th>投递时间</th></tr></thead><tbody>
        ${outbox.slice(0, 4).map((e) => `<tr><td class="tiny">${esc(e.event_type)}</td><td class="num tiny">${esc(e.seq)}</td><td><span class="chip ${e.state === "delivered" ? "good" : e.state === "dead" ? "crit" : "warn"}">${esc(e.state)}</span></td><td class="muted tiny nowrap">${e.delivered_at ? fmtTime(e.delivered_at) : "—"}</td></tr>`).join("") || '<tr><td colspan="4" class="muted tiny">暂无事件</td></tr>'}
      </tbody></table></div>
    </div>

    <div class="drawer-section">
      <h3>正文快照与成本</h3>
      <div class="two-col" style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div>${snapshotHtml}</div>
        <dl class="kv">
          <dt>首次发现</dt><dd class="tiny">${fmtTime(item.created_at)}</dd>
          <dt>最近出现</dt><dd class="tiny">${fmtTime(item.last_seen_at)}</dd>
          <dt>证据跨度</dt><dd class="tiny">${ev.span_seconds != null ? Math.round(ev.span_seconds / 60) + " 分钟" : "—"}</dd>
          <dt>证据扫描数</dt><dd class="tiny">${(ev.scan_ids || []).length}</dd>
          <dt>人工冻结</dt><dd class="tiny">${item.manual_hold ? "是 · " + esc(item.hold_reason) : "否"}</dd>
          <dt>正文哈希</dt><dd class="mono tiny">${esc((item.last_content_hash || "").slice(0, 16) || "—")}</dd>
        </dl>
      </div>
      <p class="tiny muted" style="margin:10px 0 0">误删风险提示：自动撤回要求跨批次、跨地区、多次完整扫描；仍可能因站点长期屏蔽同一资料而误判，因此撤回后保留正文快照与全部观测，可人工恢复。</p>
    </div>
    <div class="btn-row"><button class="btn" data-action="close">关闭</button></div>`;
}

/* ------------------------------------------------------------------ drawer shell */
function openItem(itemId) {
  state.selectedItem = itemId;
  const backdrop = $("#drawer-backdrop");
  const drawer = $("#drawer");
  if (!drawer) return;
  drawer.classList.add("open");
  backdrop.classList.add("open");
  drawer.innerHTML = '<div class="drawer-body"><div class="loading">载入依据…</div></div>';
  api(`/api/items/${encodeURIComponent(itemId)}`).then((detail) => {
    state.detail = detail;
    drawer.innerHTML = `<div class="drawer-head">
        <div><h2>${esc(detail.item.title)}</h2>
          <p class="tiny muted mono" style="margin:0">${esc(detail.item.id)}</p></div>
        <button class="close-x" data-close aria-label="关闭">×</button>
      </div>
      <div class="drawer-body">${renderDetailBody(detail)}</div>`;
    drawer.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closeDrawer));
    drawer.querySelectorAll("[data-action]").forEach((b) => {
      if (!b.dataset.bound) {
        b.dataset.bound = "1";
        b.addEventListener("click", () => handleAction(b.dataset.action));
      }
    });
  }).catch((e) => {
    drawer.innerHTML = `<div class="drawer-body"><div class="banner"><div><strong>载入失败</strong>${esc(e.message)}</div></div></div>`;
  });
}

function closeDrawer() {
  const drawer = $("#drawer");
  const backdrop = $("#drawer-backdrop");
  if (drawer) drawer.classList.remove("open");
  if (backdrop) backdrop.classList.remove("open");
}

/* ------------------------------------------------------------------ tasks */
function renderTasks() {
  const tasks = state.tasks || [];
  const counts = tasks.reduce((acc, t) => { acc[t.state] = (acc[t.state] || 0) + 1; return acc; }, {});
  const rows = tasks.map((t) => `<tr>
      <td class="tiny">${t.kind === "body_probe" ? "正文补探" : t.kind === "rescan_region" ? "地区补扫" : "全量补扫"}</td>
      <td class="mono tiny">${esc(t.item_id || "—")}</td>
      <td>${esc((t.region || "—").toUpperCase())}</td>
      <td><span class="chip ${t.state === "done" ? "good" : t.state === "failed" ? "crit" : t.state === "running" ? "info" : "warn"}">${esc(t.state)}</span></td>
      <td class="num tiny">${esc(t.attempts)}</td>
      <td class="muted tiny nowrap">${fmtTime(t.next_run_at)}</td>
      <td class="tiny secondary">${esc(t.last_error || t.payload?.reason || "")}</td>
    </tr>`).join("") || '<tr><td colspan="7" class="empty-state"><strong>没有补扫任务</strong>扫描失败、人工补抓与地区补扫都会出现在这里。</td></tr>';
  return `
    <div class="grid grid-kpi" style="grid-template-columns:repeat(4,minmax(0,1fr))">
      ${kpi("排队中", num(counts.queued || 0), "到期自动尝试")}
      ${kpi("执行中", num(counts.running || 0), "同一时间一个扫描")}
      ${kpi("已完成", num(counts.done || 0), "含成功补扫")}
      ${kpi("失败", num(counts.failed || 0), "已重试上限，需人工")}
    </div>
    <div class="card mt">
      <div class="card-head"><div><h2 class="card-title">补扫队列</h2>
        <p class="card-sub">失败扫描自动入队；补扫成功后才可能形成删除结论</p></div>
        <div class="btn-row">
          <button class="btn btn-sm" data-action="run-tasks">立即执行到期任务</button>
          <button class="btn btn-sm btn-primary" data-action="scan-all">全量扫描</button>
        </div></div>
      <div class="card-body table-wrap"><table class="data">
        <thead><tr><th>类型</th><th>资料</th><th>地区</th><th>状态</th><th class="num">尝试</th><th>下次执行</th><th>说明</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
    </div>
    <div class="reason" style="margin-top:14px"><p><strong>为什么需要补扫：</strong>一次扫描失败（网络、5xx、目录 200 错误页、地区屏蔽）时，条目“消失”不能作为删除证据。任务队列会在稍后重试，只有完整且地区匹配的扫描才计入门槛。</p></div>`;
}

/* ------------------------------------------------------------------ delivery */
function renderDelivery() {
  const outbox = state.outbox || [];
  const down = state.downstream;
  const counts = outbox.reduce((acc, e) => { acc[e.state] = (acc[e.state] || 0) + 1; return acc; }, {});
  const outRows = outbox.map((e) => `<tr>
      <td class="mono tiny">${esc(e.item_id)}</td>
      <td class="tiny">${esc(e.event_type)}</td>
      <td class="num tiny">${esc(e.seq)}</td>
      <td><span class="chip ${e.state === "delivered" ? "good" : e.state === "dead" ? "crit" : "warn"}">${esc(e.state)}</span></td>
      <td class="num tiny">${esc(e.attempts)}</td>
      <td class="muted tiny nowrap">${e.delivered_at ? fmtTime(e.delivered_at) : fmtTime(e.next_attempt_at)}</td>
      <td class="tiny secondary">${esc(e.last_error || "")}</td>
    </tr>`).join("") || '<tr><td colspan="7" class="empty-state"><strong>发件箱为空</strong>状态变化后会自动生成事件。</td></tr>';
  const downRows = (down?.items || []).map((i) => {
    const withheld = i.status !== "published";
    const restrict = !withheld
      ? '<span class="muted tiny">—</span>'
      : i.permanently_restricted
        ? '<span class="chip crit">永久受限</span>'
        : '<span class="chip warn">临时撤下</span>';
    return `<tr>
      <td><div class="strong">${esc(i.title || i.item_id)}</div><div class="tiny muted mono">${esc(i.item_id)}</div></td>
      <td><span class="pill ${i.status === "published" ? "pill-active" : "pill-retracted"}">${i.status === "published" ? "已发布" : "已撤下"}</span></td>
      <td>${restrict}</td>
      <td class="num tiny">${esc(i.seq)}</td>
      <td class="tiny secondary">${esc(i.reason || "")}</td>
      <td class="muted tiny nowrap">${fmtTime(i.updated_at)}</td>
    </tr>`;
  }).join("") || '<tr><td colspan="6" class="empty-state">下游暂无内容。</td></tr>';
  const downOk = down && !down.error;
  return `
    <div class="grid grid-kpi" style="grid-template-columns:repeat(4,minmax(0,1fr))">
      ${kpi("已投递", num(counts.delivered || 0), "下游已确认")}
      ${kpi("待投递", num((counts.queued || 0) + (counts.inflight || 0)), "自动重试")}
      ${kpi("投递失败", num(counts.dead || 0), "超过重试上限", counts.dead ? "is-crit" : "")}
      ${kpi("下游目录", downOk ? num(down.counts.published) : "不可达", downOk ? `已撤下 ${down.counts.withheld}` : "检查下游服务")}
    </div>
    <div class="grid grid-2 mt">
      <div class="card">
        <div class="card-head"><div><h2 class="card-title">发件箱</h2>
          <p class="card-sub">序号单调递增，重复投递幂等，乱序事件被拒绝</p></div>
          <button class="btn btn-sm" data-action="dispatch">立即投递</button></div>
        <div class="card-body table-wrap"><table class="data">
          <thead><tr><th>资料</th><th>事件</th><th class="num">序号</th><th>状态</th><th class="num">尝试</th><th>时间</th><th>错误</th></tr></thead>
          <tbody>${outRows}</tbody></table></div>
      </div>
      <div class="card">
        <div class="card-head"><div><h2 class="card-title">下游当前内容</h2>
          <p class="card-sub">${downOk ? "下游服务在线" : "下游服务不可达"}</p></div>
          <a class="btn btn-sm" href="__DOWNSTREAM_URL__/" target="_blank" rel="noopener">打开下游 ↗</a></div>
        <div class="card-body table-wrap"><table class="data">
          <thead><tr><th>条目</th><th>状态</th><th>约束</th><th class="num">序号</th><th>原因</th><th>更新时间</th></tr></thead>
          <tbody>${downRows}</tbody></table></div>
      </div>
    </div>
    <div class="reason good" style="margin-top:14px"><p><strong>重放保护：</strong>下游按序号保存状态，低于当前序号的事件被忽略；“撤回”只能由带 <code>allow_restore</code> 的人工恢复事件解除，扫描触发的发布事件永远不会让已撤回资料重新上架。</p></div>`;
}

/* ------------------------------------------------------------------ lab */
function renderLab() {
  const site = state.siteState && !state.siteState.error ? state.siteState : null;
  const health = state.siteHealth || {};
  const regions = ["us", "eu", "apac"];
  const faults = site?.faults || {};
  const items = site ? Object.entries(site.items).slice(0, 10) : [];

  const regionRows = site ? regions.map((r) => `<div class="region-row">
      <b style="width:44px">${r.toUpperCase()}</b>
      <select class="input" data-region-select="${r}">
        ${[["ok", "正常"], ["blocked403", "目录 403 屏蔽"], ["error200", "目录 200 错误页"]].map(([v, label]) => `<option value="${v}" ${(site.regions[r]?.catalog_mode || "ok") === v ? "selected" : ""}>${label}</option>`).join("")}
      </select>
      <span class="tiny muted">切换后立即影响该地区扫描</span>
    </div>`).join("") : '<div class="empty-state">站点不可达</div>';

  const itemRows = items.map(([id, it]) => `<tr>
      <td><div class="tiny strong">${esc(it.title.slice(0, 18))}</div><div class="tiny muted mono">${esc(id)}</div></td>
      <td><label class="switch"><input type="checkbox" data-site-item="${esc(id)}" data-field="withdrawn" ${it.withdrawn ? "checked" : ""}> 撤回</label></td>
      <td class="tiny">${regions.map((r) => `<label class="switch"><input type="checkbox" data-site-item="${esc(id)}" data-field="catalog_hidden" data-region="${r}" ${(it.catalog_hidden || []).includes(r) ? "checked" : ""}>${r}</label>`).join(" ")}</td>
      <td class="tiny">${regions.map((r) => `<label class="switch"><input type="checkbox" data-site-item="${esc(id)}" data-field="page_blocked" data-region="${r}" ${(it.page_blocked || {})[r] ? "checked" : ""}>${r}</label>`).join(" ")}</td>
    </tr>`).join("") || '<tr><td colspan="4" class="empty-state">站点暂无条目</td></tr>';

  return `
    <div class="banner ${health.reachable ? "info" : ""}" style="${health.reachable ? "" : "border-color:#edc4c4;background:#fbeaea"}">
      <span class="dot ${health.reachable ? "dot-good" : "dot-crit"}" style="margin-top:5px"></span>
      <div><strong>站点状态</strong>${health.reachable ? "本地发布站点在线。下面的开关会直接改变站点响应，用于验证采集端不会误删。" : esc(health.error || "站点不可达")}</div>
    </div>
    <div class="lab-grid">
      <div class="card">
        <div class="card-head"><div><h2 class="card-title">地区目录</h2>
          <p class="card-sub">按地区临时屏蔽目录，验证不会当成撤回</p></div></div>
        <div class="card-body">${regionRows}</div>
      </div>
      <div class="card">
        <div class="card-head"><div><h2 class="card-title">目录故障</h2>
          <p class="card-sub">制造“看起来正常”的异常响应</p></div></div>
        <div class="card-body stack" style="gap:10px">
          ${[
            ["listing_error_200", "列表返回 200 错误页", "状态码正常但内容是错误提示"],
            ["listing_truncate", "列表截断", "只返回部分条目，仍声明真实总数"],
            ["body_truncate", "正文截断", "正文缺少结束标记，不能作为内容更新"],
            ["wrong_region", "错误地区缓存", "请求 eu 却返回 us 内容"],
          ].map(([key, label, note]) => `<label class="switch" style="align-items:flex-start;gap:8px">
            <input type="checkbox" data-fault="${key}" ${faults[key] ? "checked" : ""} style="margin-top:2px">
            <span><b style="color:var(--text-primary)">${label}</b><br><span class="tiny muted">${note}</span></span>
          </label>`).join("")}
          <div class="form-row" style="margin-top:6px">
            <label class="field">错误率（0–1）<input class="input" id="fault-rate" type="number" min="0" max="1" step="0.05" value="${esc(faults.error_rate ?? 0)}" style="width:100px"></label>
            <label class="field">延迟（ms）<input class="input" id="fault-latency" type="number" min="0" max="3000" step="100" value="${esc(faults.latency_ms ?? 0)}" style="width:100px"></label>
            <button class="btn btn-sm" data-action="apply-faults">应用</button>
          </div>
        </div>
      </div>
    </div>

    <div class="card mt">
      <div class="card-head"><div><h2 class="card-title">一键演练场景</h2>
        <p class="card-sub">每个场景写入固定故障并触发扫描，然后到“证据工作台”核对结论</p></div></div>
      <div class="card-body">
        <div class="lab-grid">
          ${[
            ["single-empty", "单次消失", "隐藏 itm_020 后只扫一次：应只进入待复核"],
            ["region-block", "地区屏蔽", "eu 目录 403 后扫描：不得产生删除"],
            ["error-page", "200 错误页", "列表返回错误提示页后扫描：不得产生删除"],
            ["truncate", "列表截断", "目录截断后扫描：不得产生删除"],
            ["notice-first", "通知先到", "写入来源通知：正文仍可见也必须保持撤回"],
            ["region-flip", "地区切换", "错误地区缓存 + 地区错误页后扫描：不得产生删除"],
            ["genuine", "真实撤回", "撤回 itm_021 并连续三次完整扫描：应自动撤回"],
            ["recover", "恢复误判", "先撤回再人工恢复：记录审计并重新发布"],
          ].map(([id, title, desc]) => `<div class="scenario">
            <h3>${title}</h3><p>${desc}</p>
            <div><button class="btn btn-sm btn-primary" data-scenario="${id}">运行场景</button></div>
          </div>`).join("")}
        </div>
        <div class="btn-row" style="margin-top:14px">
          <button class="btn" data-action="scan-all">扫描全部地区</button>
          <button class="btn" data-action="run-tasks">执行补扫任务</button>
          <button class="btn" data-action="dispatch">投递下游</button>
          <button class="btn btn-danger" data-action="reset-site">重置站点</button>
        </div>
      </div>
    </div>

    <div class="card mt">
      <div class="card-head"><div><h2 class="card-title">站点条目（前 10 条）</h2>
        <p class="card-sub">撤回=真实下架；隐藏目录/页面 451=地区性暂时不可见</p></div></div>
      <div class="card-body table-wrap"><table class="data">
        <thead><tr><th>条目</th><th>真实撤回</th><th>目录隐藏（地区）</th><th>页面 451（地区）</th></tr></thead>
        <tbody>${itemRows}</tbody></table></div>
    </div>`;
}

/* ------------------------------------------------------------------ actions */
const SCENARIOS = {
  async "single-empty"() {
    const id = "itm_020";
    await api("/api/site/item", { body: { item_id: id, catalog_hidden: ["us", "eu", "apac"] } });
    await api("/api/scan", { body: { regions: ["us", "eu", "apac"], mode: "scenario", note: "场景：单次消失" } });
    toast("已隐藏并扫描一次：应只进入待复核");
  },
  async "region-block"() {
    await api("/api/site/region", { body: { region: "eu", catalog_mode: "blocked403" } });
    await api("/api/scan", { body: { mode: "scenario", note: "场景：地区屏蔽" } });
    toast("eu 目录已 403 屏蔽：查看后台结论不应变化");
  },
  async "error-page"() {
    await api("/api/site/faults", { body: { listing_error_200: true } });
    await api("/api/scan", { body: { mode: "scenario", note: "场景：200 错误页" } });
    toast("目录已返回 200 错误页：该扫描不应被采纳");
  },
  async truncate() {
    await api("/api/site/faults", { body: { listing_truncate: true } });
    await api("/api/scan", { body: { mode: "scenario", note: "场景：列表截断" } });
    toast("目录已截断：该扫描不应被采纳");
  },
  async "notice-first"() {
    await api("/api/notices", { body: { notice_id: `ntc_${Date.now()}`, item_id: "itm_004", source: "rights-office", statement: "权利方撤回通知（场景）" } });
    await api("/api/scan", { body: { mode: "scenario", note: "场景：通知先到" } });
    toast("通知已登记：正文仍可见也必须保持撤回");
  },
  async "region-flip"() {
    await api("/api/site/faults", { body: { wrong_region: true } });
    await api("/api/scan", { body: { mode: "scenario", note: "场景：错误地区缓存" } });
    await api("/api/site/faults", { body: { wrong_region: false } });
    toast("错误地区缓存已注入：该扫描不应被采纳");
  },
  async genuine() {
    const id = "itm_021";
    await api("/api/site/item", { body: { item_id: id, withdrawn: true } });
    for (let i = 0; i < 3; i++) {
      await api("/api/scan", { body: { mode: "scenario", note: `场景：真实撤回 ${i + 1}/3` } });
    }
    toast(`${id} 已连续三次完整扫描确认，应自动撤回`);
  },
  async recover() {
    const id = "itm_021";
    await api("/api/site/item", { body: { item_id: id, withdrawn: false } });
    await api("/api/items/" + id + "/restore", { body: { reason: "场景：发布方书面恢复函，人工复核通过" } });
    toast("已人工恢复并重新发布");
  },
};

async function runScenario(id) {
  const fn = SCENARIOS[id];
  if (!fn) return;
  await act("", fn);
  await refresh();
}

/* ------------------------------------------------------------------ events
   Wiring is installed once, delegated from the view container. Re-rendering
   replaces innerHTML; it must never re-attach handlers.
*/
function installDelegates() {
  const view = $("#view");

  view.addEventListener("click", async (event) => {
    const sc = event.target.closest("[data-scenario]");
    if (sc) { await runScenario(sc.dataset.scenario); return; }

    const filter = event.target.closest("#item-filter button");
    if (filter) {
      state.itemFilter = filter.dataset.filter;
      state.selectedItem = null;
      state.detail = null;
      await refresh();
      return;
    }

    const row = event.target.closest("tr[data-item-id], .proof-row[data-item-id]");
    if (row) {
      if (state.route === "proof") await selectProofItem(row.dataset.itemId);
      else openItem(row.dataset.itemId);
      return;
    }

    const btn = event.target.closest("[data-action]");
    if (btn) await handleAction(btn.dataset.action);
  });

  view.addEventListener("change", async (event) => {
    const region = event.target.closest("[data-region-select]");
    if (region) {
      await act("地区设置已更新", () => api("/api/site/region", { body: { region: region.dataset.regionSelect, catalog_mode: region.value } }));
      await refresh();
      return;
    }
    const fault = event.target.closest("[data-fault]");
    if (fault) {
      await act("故障开关已更新", () => api("/api/site/faults", { body: { [fault.dataset.fault]: fault.checked } }));
      await refresh();
      return;
    }
    const siteItem = event.target.closest("[data-site-item]");
    if (siteItem) {
      const id = siteItem.dataset.siteItem;
      const field = siteItem.dataset.field;
      const body = { item_id: id };
      if (field === "withdrawn") {
        body.withdrawn = siteItem.checked;
      } else {
        const current = state.siteState.items[id][field] || [];
        const list = Array.isArray(current) ? current.slice() : Object.keys(current);
        const rgn = siteItem.dataset.region;
        body[field] = siteItem.checked ? [...new Set([...list, rgn])] : list.filter((r) => r !== rgn);
      }
      await act("站点条目已更新", () => api("/api/site/item", { body }));
      await refresh();
    }
  });
}

async function handleAction(action) {
  if (action === "scan-all") {
    await act("扫描完成", () => api("/api/scan", { body: { regions: null, mode: "manual", note: "后台手动扫描" } }));
    await refresh();
  } else if (action === "dispatch") {
    await act("已尝试投递下游", () => api("/api/dispatch", { body: { limit: 100 } }));
    await refresh();
  } else if (action === "run-tasks") {
    await act("补扫任务已执行", () => api("/api/tasks/run", { body: { limit: 5 } }));
    await refresh();
  } else if (action === "reset-site") {
    if (!confirm("重置站点会清空站点的撤回/屏蔽状态，台账记录保留。继续？")) return;
    await act("站点已重置", () => api("/api/site/reset", { body: {} }));
    await refresh();
  } else if (action === "apply-faults") {
    const rate = Number($("#fault-rate").value || 0);
    const latency = Number($("#fault-latency").value || 0);
    await act("故障参数已应用", () => api("/api/site/faults", { body: { error_rate: rate, latency_ms: latency } }));
    await refresh();
  } else if (["probe", "rescan", "hold", "restore", "review"].includes(action)) {
    await handleDetailAction(action);
  } else if (action === "close") {
    closeDrawer();
  }
}

async function handleDetailAction(action) {
  const itemId = state.selectedItem || (state.detail && state.detail.item && state.detail.item.id);
  if (!itemId) return;
  if (action === "probe") {
    await act("已排队：正文补抓", () => api("/api/tasks", { body: { kind: "body_probe", item_id: itemId, region: (state.overview && state.overview.regions.configured[0]) || "us", note: "人工补抓正文" } }));
  } else if (action === "rescan") {
    const region = prompt("补扫地区（us / eu / apac）", "us");
    if (!region) return;
    await act(`已排队：${region} 地区补扫`, () => api("/api/tasks", { body: { kind: "rescan_region", item_id: itemId, region, note: "人工补扫" } }));
  } else if (action === "hold") {
    const reason = prompt("人工冻结原因（冻结后不会自动删除或发布）", "人工复核中");
    if (!reason) return;
    await act("已冻结，等待人工复核", () => api(`/api/items/${itemId}/hold`, { body: { held: true, reason } }));
  } else if (action === "restore") {
    const reason = prompt("人工恢复原因（必填，将写入审计日志）", "");
    if (!reason) return;
    await act("已人工恢复并重新发布", () => api(`/api/items/${itemId}/restore`, { body: { reason } }));
  } else if (action === "review") {
    const note = prompt("关注说明", "人工关注：请复核证据");
    if (!note) return;
    await act("已标记关注", () => api(`/api/items/${itemId}/review`, { body: { note } }));
  } else {
    return;
  }
  await refresh();
  if (state.route === "proof" && state.selectedItem) await selectProofItem(state.selectedItem);
}

function hydrateSearch() {
  const search = $("#item-search");
  if (!search || search.dataset.wired) return;
  search.dataset.wired = "1";
  let timer = null;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      state.itemQuery = search.value.trim();
      if (state.route !== "items") return;
      try {
        const data = await api(`/api/items?state=${encodeURIComponent(state.itemFilter === "all" ? "" : state.itemFilter)}&q=${encodeURIComponent(state.itemQuery)}`);
        state.items = data.items;
        const tbody = $("#view table.data tbody");
        if (tbody) {
          tbody.innerHTML = (state.items || []).map(itemRowHtml).join("") ||
            '<tr><td colspan="5" class="empty-state"><strong>没有匹配的资料</strong>调整筛选条件。</td></tr>';
        }
      } catch (e) { toast(e.message, true); }
    }, 260);
  });
}

/* ------------------------------------------------------------------ start */
window.addEventListener("hashchange", navigate);
window.addEventListener("DOMContentLoaded", () => {
  $("#btn-refresh").addEventListener("click", refresh);
  installDelegates();
  $("#drawer-backdrop")?.addEventListener("click", closeDrawer);
  if (!location.hash) location.hash = "#/overview";
  navigate();
  setInterval(async () => {
    if (state.route === "overview" || state.route === "delivery") {
      try {
        state.overview = await api("/api/overview");
        updateLive();
        if (state.route === "overview") { $("#view").innerHTML = renderOverview(); }
      } catch (e) { /* transient poll failure is fine */ }
    }
  }, 15000);
});
