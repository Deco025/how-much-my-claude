"use strict";

// ── 小工具 ───────────────────────────────────────────────

const $ = (sel) => document.querySelector(sel);

// 只用 textContent 插入文本：模型名等都来自本地日志，不能当 HTML 解析
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// 图表 tooltip 是 HTML 字符串，插入日志来的文本前先转义
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch { /* 不是 JSON */ }
    const err = new Error(typeof detail === "string" && detail ? detail : `${path} → HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body ?? {}) });

function usd(v, digits) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const d = digits ?? (Math.abs(v) >= 100 ? 0 : Math.abs(v) >= 10 ? 1 : 2);
  return "$" + v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function compact(n) {
  if (!n) return "0";
  for (const [base, unit] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]]) {
    if (Math.abs(n) >= base) return (n / base).toFixed(n / base >= 100 ? 0 : 1) + unit;
  }
  return String(Math.round(n));
}

const pad = (n) => String(n).padStart(2, "0");
function when(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function day(ts) {
  const d = new Date(ts * 1000);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
function date(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
function clock(ts) {
  const d = new Date(ts * 1000);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : when(ts);
}
function span(seconds) {
  let s = Math.max(0, seconds);
  const d = Math.floor(s / 86400); s -= d * 86400;
  const hh = Math.floor(s / 3600); s -= hh * 3600;
  const mm = Math.floor(s / 60);
  if (d) return t("{d} 天 {h} 小时", { d, h: hh });
  if (hh) return t("{h} 小时 {m} 分", { h: hh, m: mm });
  return t("{m} 分钟", { m: mm });
}
function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 90) return t("刚刚");
  if (s < 3600) return t("{n} 分钟前", { n: Math.round(s / 60) });
  if (s < 86400) return t("{n} 小时前", { n: Math.round(s / 3600) });
  return t("{n} 天前", { n: Math.round(s / 86400) });
}
const pctText = (v) => `${v > 0 ? "+" : ""}${(v * 100).toFixed(0)}%`;
const now = () => Date.now() / 1000;

const WINDOW_SHORT = { five_hour: "5 小时", seven_day: "每周", thirty_day: "30 天" };
const TOOL_NAMES = { codex: "Codex", claude: "Claude" };
const STATE_NAMES = { stable: "稳定", tighter: "可能被收紧", looser: "可能被放宽", insufficient: "历史基线不足",
  not_eligible: "不可判断", model_changed: "主力模型变了", ignored: "不参与分析" };
// 「额度有没有被调」卡片上的检验章
const STAMPS = { stable: "未见调整", tighter: "疑似收紧", looser: "疑似放宽", insufficient: "基线不足",
  not_eligible: "不可判断", model_changed: "换了模型" };
const stateName = (s) => t(STATE_NAMES[s] || s);

// 窗口的估值条件。只有「可作条件估计」显示容量估值；其余只显示本机 API 等价金额、额度比例和原因
const QUALITY_NAMES = { incomplete: "采集不完整", source_unknown: "来源待确认", aligning: "正在对齐",
  waiting: "等待更多消耗", unconfirmed: "覆盖待确认", conditional: "可作条件估计" };
const qualityName = (q) => t(QUALITY_NAMES[q] || q);

function reasonText(r) {
  const pct = (v) => Math.round(v ?? 0);
  const names = Array.isArray(r.names) ? r.names.map((n) => t(n)).join(t("、")) : "";
  switch (r.code) {
    case "collect_gap": return t("这段时间采集有缺口（日志读取失败或断档），中间可能漏了记录");
    case "no_breakdown": return t("没拿到这一周的来源分项");
    case "breakdown_invalid": return t("这一周的来源分项读不懂");
    case "breakdown_stale": return t("来源分项停在 {when}，之后的使用来自哪里无法确认", { when: when(r.as_of) });
    case "unknown_sources": return names ? t("来源不明的使用约占已用的 {pct}%（{names}）", { pct: pct(r.share), names })
      : t("来源不明的使用约占已用的 {pct}%", { pct: pct(r.share) });
    case "uncollected_sources": return t("{names} 约占已用的 {pct}%，这些使用本项目采集不到", { pct: pct(r.share), names });
    case "noncode_in_window": return t("这段时间有 Claude Code 以外的使用（网页、App 或 Cowork）");
    case "noncode_unlocated": return t("来源分项不能说明这段时间有没有 Claude Code 以外的使用");
    case "aligning": return t("日志和额度快照可能还没同步，稍后再看");
    case "suspect_unrecorded": return t("额度涨得比本机记录多（疑似未记录消耗约 {pct}%，只是疑点，没有扣除）", { pct: pct(r.pct) });
    case "waiting": return t("用量还太少，变化不足以估算");
    case "no_claim": return t("没有打开覆盖声明，不能确认这个账号的使用都被采集到了");
    case "account_unknown": return t("读不到当前账号，覆盖声明不适用");
    case "account_changed": return t("这个窗口期间切换过账号");
    case "claim_other_account": return t("覆盖声明属于别的账号");
    case "claim_after_start": return t("覆盖声明在这个窗口开始之后才打开");
    case "claim_ended": return t("这个窗口开始时覆盖声明已经关闭");
    case "claim_conflict": return t("你声明了覆盖完整，但数据显示有别的来源，以数据为准");
    case "too_little": return t("用量还太少，变化不足以估算");
    case "code_share_low": return t("Claude Code 占比太低，推测部分太大，不给估值");
    case "no_split": return t("这个 5 小时窗口附近没有来源分项，分不出 Claude Code 占多少");
    case "unsupported": return t("这个窗口不支持估值");
    case "no_estimate": return t("这个窗口没有估值");
    case "model_unpriced": return t("这个窗口里没有汇率的模型花费太多，折算不成主力模型，不参与比较");
    case "estimate_conflict": return t("额度涨幅超出本机和其他来源能解释的部分，估值可能偏低，不参与比较");
    default: return r.code;
  }
}

// 5 小时窗口刚开始时，周额度只涨了几个整数点，取整误差占比大，区间会宽得没意义：只给中间值，等用量多了再给区间
const RANGE_MIN_WEEKLY = 5;
const RANGE_MAX_SPREAD = 1.5;
const rangeTooWide = (w, e) =>
  w.cap_high > w.cap_low * RANGE_MAX_SPREAD ||
  (e && e.basis === "window_delta" && e.weekly_delta < RANGE_MIN_WEEKLY);
const capRange = (w, e = w.estimate) => rangeTooWide(w, e) && w.cap
  ? t("约 {usd}", { usd: usd(w.cap) })
  : t("约 {low}～{high}", { low: usd(w.cap_low), high: usd(w.cap_high) });
const RANGE_EARLY_TIP = "窗口刚开始，用量还少，误差区间偏宽（{low}～{high}），只显示中间值；继续使用后会给出区间";
const rangeTip = (w, e = w.estimate) => rangeTooWide(w, e)
  ? t(RANGE_EARLY_TIP, { low: usd(w.cap_low), high: usd(w.cap_high) }) : null;

// 像素小图标，16×10 格：X 是主色，o 是眼睛。Claude 是吉祥物小章鱼（和应用图标同一只），Codex 是终端提示符
const SPRITES = {
  claude: [
    "..XXXXXXXXXXXX..",
    "..XXXXXXXXXXXX..",
    "..XXoXXXXXXoXX..",
    "..XXoXXXXXXoXX..",
    "XXXXXXXXXXXXXXXX",
    "XXXXXXXXXXXXXXXX",
    "..XXXXXXXXXXXX..",
    "..XXXXXXXXXXXX..",
    "...X.X....X.X...",
    "...X.X....X.X...",
  ],
  codex: [
    "XX..............",
    "XXX.............",
    ".XXX............",
    "..XXX...........",
    "...XX...........",
    "..XXX...........",
    ".XXX............",
    "XXX.............",
    "XX....XXXXXXXXXX",
    "......XXXXXXXXXX",
  ],
};

function sprite(tool) {
  const NS = "http://www.w3.org/2000/svg";
  const rows = SPRITES[tool];
  const svg = document.createElementNS(NS, "svg");
  for (const [k, v] of Object.entries({ viewBox: `0 0 ${rows[0].length} ${rows.length}`, class: `sprite sprite-${tool}`,
    "aria-hidden": "true", "shape-rendering": "crispEdges" })) svg.setAttribute(k, v);
  // 同一行里连续的同色格合成一个矩形
  rows.forEach((row, y) => {
    for (const m of row.matchAll(/X+|o+/g)) {
      const r = document.createElementNS(NS, "rect");
      for (const [k, v] of Object.entries({ x: m.index, y, width: m[0].length, height: 1, class: m[0][0] === "o" ? "px-eye" : "px" })) {
        r.setAttribute(k, v);
      }
      svg.append(r);
    }
  });
  return svg;
}

function windowLabel(w) {
  const base = WINDOW_SHORT[w.window] ? t(WINDOW_SHORT[w.window]) : w.window;
  const shared = (w.tool === "codex" && w.scope === "codex") || (w.tool === "claude" && w.scope === "all");
  return shared ? base : `${base} · ${w.scope}`;
}

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const toolColor = (tool) => cssVar(tool === "claude" ? "--series-2" : "--series-1");
// 超过两周没有新数据的分组（例如以前用过的别的账号、旧方案），以及在「数据管理」里设为只保留的，不在监控里显示
const isLive = (g) => now() - g.last_seen < 14 * 86400 && g.status !== "ignored";

// 线条小图标（16×16）：金额用一摞钱币，饼图备用。内容是写死的常量，不含外部数据
const ICONS = {
  coins: '<ellipse cx="8" cy="4" rx="5.5" ry="2"/><path d="M2.5 4v8c0 1.1 2.5 2 5.5 2s5.5-.9 5.5-2V4"/>' +
    '<path d="M2.5 8c0 1.1 2.5 2 5.5 2s5.5-.9 5.5-2"/>',
  pie: '<circle cx="8" cy="8" r="5.5"/><path d="M8 8V2.5A5.5 5.5 0 0 1 13.5 8Z" fill="currentColor"/>',
};
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [k, v] of Object.entries({ viewBox: "0 0 16 16", class: "ico", fill: "none", stroke: "currentColor",
    "stroke-width": "1.3", "aria-hidden": "true" })) svg.setAttribute(k, v);
  svg.innerHTML = ICONS[name];
  return svg;
}

// ── 图表公共 ─────────────────────────────────────────────

const state = { range: "7d", tool: "all", history: "codex", priceView: "used", priceQuery: "" };
let lastPrices = null;
const charts = new Map();

// 在 node 上画图（整体替换配置）。每 60 秒整体重绘一次，入场动画只会闪；
// 页面在后台时动画还会卡在第一帧（点不显示），所以一律关掉动画
function render(node, option) {
  let c = charts.get(node);
  if (!c) {
    c = echarts.init(node, null, { renderer: "svg" });
    charts.set(node, c);
  }
  c.setOption({ animation: false, ...option }, true);
  return c;
}

function disposeDetached() {
  for (const [node, c] of charts) {
    if (!node.isConnected) { c.dispose(); charts.delete(node); }
  }
}

function base() {
  const muted = cssVar("--text-muted");
  return {
    muted,
    textStyle: { fontFamily: getComputedStyle(document.body).fontFamily, color: cssVar("--text-secondary") },
    axis: {
      axisLine: { lineStyle: { color: cssVar("--axis") } },
      axisTick: { show: false },
      axisLabel: { color: muted, hideOverlap: true, fontFamily: cssVar("--mono"), fontSize: 11 },
      splitLine: { lineStyle: { color: cssVar("--grid"), type: "solid" } },
    },
    tooltip: {
      backgroundColor: cssVar("--surface-1"),
      borderColor: cssVar("--border"),
      textStyle: { color: cssVar("--text-primary"), fontSize: 12 },
      confine: true,
    },
    legend: { top: 0, right: 0, itemWidth: 10, itemHeight: 10, textStyle: { color: cssVar("--text-secondary"), fontSize: 12 } },
  };
}

// ── 连接状态：别人装好后第一次打开，告诉他还差什么 ──────────

const CREDENTIAL_HINTS = {
  claude: {
    missing: "找不到 Claude Code 的登录信息（{where}），所以查不到 Claude 的额度百分比。在终端运行 claude 并登录一次，然后点「立即同步」。",
    expired: "Claude Code 的登录已过期，额度百分比暂时查不到。在终端随便运行一次 claude，它会自己续上，然后点「立即同步」。",
    error: "读取 Claude Code 的登录信息失败（{where}）。",
  },
  codex: {
    missing: "找不到 Codex 的 ChatGPT 登录（{where}）。用量照样能从日志里读，只是空闲时查不了额度；运行 codex login 可以补上。",
    expired: "Codex 的登录太久没刷新了。运行一次 codex，它会自己续上。",
    error: "读取 Codex 的登录信息失败（{where}）。",
  },
};

function renderConnections(conns) {
  const box = $("#connect");
  box.replaceChildren();
  const used = conns.filter((c) => c.log_files > 0);
  if (!used.length) {
    // 两个工具的日志都没找到：多半是刚装好、还没用过，或者日志不在默认位置
    box.append(h("div", { class: "connect-card" },
      h("strong", {}, t("还没找到 Codex 或 Claude Code 的使用记录")),
      h("p", {}, t("这个工具读的是它们在本机留下的日志，自己不登录任何账号、不上传数据。装好 Codex CLI 或 Claude Code、登录并用过一次后，点右上角「立即同步」。")),
      h("ul", {}, conns.map((c) => h("li", {}, t("{tool} 的日志目录：{dir}", { tool: TOOL_NAMES[c.tool], dir: c.log_dir }))))));
    return;
  }
  for (const c of used) {
    const hint = CREDENTIAL_HINTS[c.tool][c.credentials];
    // Codex 的额度信息日志里就有，登录问题只是小提示；Claude 的额度百分比全靠登录
    if (hint && (c.tool === "claude" || c.credentials !== "missing")) {
      box.append(h("div", { class: `connect-card ${c.tool === "claude" ? "warn" : ""}` },
        sprite(c.tool), h("span", {}, t(hint, { where: c.credentials_where }))));
    }
  }
}

// ── 告警横幅 ─────────────────────────────────────────────

function renderBanner(monitor) {
  const box = $("#alert-banner");
  box.replaceChildren();
  // 只有够资格的结论才上横幅；不在覆盖声明下的（估值里有推测成分）标「推测」
  for (const g of monitor.groups.filter((x) => isLive(x) && x.eligible && (x.status === "tighter" || x.status === "looser"))) {
    box.append(h("div", { class: `banner ${g.status}`, role: "alert" },
      h("span", { class: "icon" }, g.status === "tighter" ? "▼" : "▲"),
      h("div", {},
        h("strong", {}, t("{tool} {window}窗口{state} {pct}%", { tool: TOOL_NAMES[g.tool], window: windowLabel(g),
          state: stateName(g.status), pct: Math.abs((g.ratio - 1) * 100).toFixed(0) }),
          g.declared ? null : h("span", { class: "tag", title: t("没有覆盖声明：估值里推测了其他来源的花费，结论仅供参考") }, t("推测"))),
        h("div", { class: "sub" }, t("折合 {model}：最近 {rn} 个窗口 {recent}，之前 {bn} 个窗口 {baseline}", {
          model: g.ref_model, rn: g.recent_n, recent: usd(g.recent_median), bn: g.baseline_n, baseline: usd(g.baseline_median) })))));
  }
}

// ── 现在：各窗口进度 ─────────────────────────────────────

// tNow 是「现在」，默认取当前时间
function renderWindowRow(w, tNow = now()) {
  const label = h("span", { class: "win-label" }, windowLabel(w));
  if (!w.active) {
    return h("div", { class: "win idle" },
      h("div", { class: "win-row" }, label,
        h("div", { class: "gauge" }, h("div", { class: "meter" })),
        h("span", { class: "win-pct muted" }, "0", h("small", {}, "%"))),
      h("div", { class: "win-meta" }, h("span", {}, t("已重置，下次使用时开始新窗口")),
        h("span", { class: "left" }, t("剩余 {pct}%", { pct: 100 }))));
  }
  const pct = w.used_percent || 0;
  const elapsed = Math.min(1, Math.max(0, (tNow - w.start) / (w.end - w.start)));
  const meter = pct >= 95 ? "meter crit" : pct >= 80 ? "meter warn" : "meter";
  // 按目前速度推算：到重置时会用到多少、什么时候用完。窗口刚开始的一成时间里速度还不准，先不推算
  const projected = pct >= 2 && elapsed >= 0.1 ? pct / elapsed : null;
  let forecast = null;
  if (projected !== null) {
    const eta = w.start + (tNow - w.start) * (100 / pct);
    forecast = eta < w.end
      ? h("span", { class: "forecast bad" }, t("预计 {when} 用完", { when: clock(eta) }))
      : h("span", { class: "forecast good", title: t("照目前速度，重置时约用到 {pct}%", { pct: Math.round(projected) }) },
        t("预计可撑到重置"));
  }
  const proj = projected !== null && projected > pct
    ? h("div", { class: projected > 100 ? "proj over" : "proj",
      style: `left:${Math.min(100, pct)}%;width:${(Math.min(100, projected) - Math.min(100, pct)).toFixed(1)}%` })
    : null;
  // 金额取额度快照那一刻的累计，和百分比同一时间点；快照之后的新花费单独提示
  const later = Math.max(0, (w.cost_so_far || 0) - (w.cost_at_snapshot || 0));
  const estimable = w.quality === "conditional" && w.cap;
  const reasons = (w.reasons || []).map(reasonText);
  const capTip = t("按当前使用构成估算，整个窗口用满大约值多少") + (w.claimed ? t("；基于你的覆盖声明") : "") +
    (w.bias_pct ? t("；别的来源约占 {pct}%，估值可能偏低", { pct: Math.round(w.bias_pct) }) : "") +
    (rangeTip(w) ? "。" + rangeTip(w) : "");
  const facts = [
    h("span", { class: "fact", title: t("本机日志里的用量按官方 API 价折算，截至 {when} 的额度快照。不是实际扣费，也不含别的设备",
      { when: clock(w.snapshot_at || tNow) }) },
      icon("coins"), t("本机 API 等价"), h("strong", {}, usd(w.cost_at_snapshot || 0)),
      estimable ? h("span", { class: "of", title: capTip }, t("/ 用满{range}", { range: capRange(w) })) : null),
    w.estimate ? estimateFact(w.estimate) : null,
    later >= 0.01 ? h("span", { class: "fact muted", title: t("额度快照之后的新花费，等额度更新后再算进比例") },
      t("+{usd} 待额度更新", { usd: usd(later) })) : null,
    w.quality && !estimable
      ? h("span", { class: `fact quality ${w.quality}`, title: reasons.join("\n") }, qualityName(w.quality)) : null,
    w.status ? h("span", { class: `fact state ${w.status}`,
      title: w.eligible && !w.declared ? t("没有覆盖声明：估值里推测了其他来源的花费，结论仅供参考") : null },
      stateName(w.status) + (w.eligible && !w.declared && (w.status === "tighter" || w.status === "looser")
        ? t("（推测）") : "")) : null,
  ];
  const sources = w.sources ? sourceList(w.sources) : null;
  return h("div", { class: "win" },
    h("div", { class: "win-row" }, label,
      h("div", { class: "gauge", role: "meter", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100,
        title: t("已用 {pct}%，窗口时间已过 {elapsed}%（小三角）", { pct: pct.toFixed(0), elapsed: (elapsed * 100).toFixed(0) }) +
          (projected !== null ? t("，照目前速度到重置时约 {pct}%（斜纹）", { pct: Math.round(projected) }) : "") },
        h("div", { class: meter },
          h("div", { class: "used", style: `width:${Math.min(100, pct)}%` }),
          proj),
        h("div", { class: "now", style: `left:${(elapsed * 100).toFixed(1)}%` })),
      h("span", { class: "win-pct" }, pct.toFixed(0), h("small", {}, "%"))),
    h("div", { class: "win-meta" }, h("span", {}, t("{span}后重置", { span: span(w.end - tNow) })), forecast,
      h("span", { class: "left" }, t("剩余 {pct}%", { pct: Math.max(0, 100 - pct).toFixed(0) }))),
    h("div", { class: "win-foot" }, facts), sources);
}

// Claude 窗口：总花费 = 确定（本机 Code）+ 推测（其他来源按同样比例折算）；5 小时窗口的 Code 占比由窗口期间的周分项变化拆出
function estimateFact(e) {
  const tip = [
    t("确定：本机 Claude Code 日志按官方 API 价 {usd}（占已用部分的 {pct}%）", { usd: usd(e.local), pct: Math.round(e.code_pct) }),
    e.basis === "window_delta"
      ? t("推测：这段时间周额度涨了 {dw}%，其中非 Code 约 {dn} 个百分点，所以 Code 约占 {low}%～{high}%；其他来源按同样的额度/美元比例折算 ≈ {usd}",
        { dw: Math.round(e.weekly_delta), dn: Math.max(0, e.noncode_delta).toFixed(1), low: Math.round(e.code_low),
          high: Math.round(e.code_high), usd: usd(e.inferred) })
      : t("推测：其他来源占 {pct}%，按同样的额度/美元比例折算 ≈ {usd}", { usd: usd(e.inferred), pct: Math.round(e.noncode_pct) }),
    t("前提：Claude Code 只在这台电脑上用；网页、App、Cowork 每 1% 额度和 Code 等价"),
    e.conflict_pct ? t("注意：约 {pct}% 的额度涨幅既没有本机日志、也不是非 Code 来源，Code 可能在别的设备上用过，估值偏低",
      { pct: Math.round(e.conflict_pct) }) : null,
  ].filter(Boolean).join("\n");
  return h("span", { class: "fact", title: tip },
    t("含推测 ≈ {usd}", { usd: usd(e.total) }) + (e.conflict_pct ? " ⚠" : ""),
    h("span", { class: "of", title: rangeTip(e, e) || tip }, t("/ 用满{range}", { range: capRange(e, e) })));
}

// Claude 周窗口：已用部分来自哪些产品（官方分项，占已用部分的比例）
function sourceList(src) {
  const rows = (src.rows || []).filter((r) => r.percent > 0);
  if (!rows.length) return null;
  return h("details", { class: "sources" },
    h("summary", {}, t("已用部分来源"),
      h("span", { class: "muted" }, " · " + t("{when}更新", { when: ago(src.last_as_of || src.as_of) }))),
    h("ul", {}, rows.map((r) => h("li", {}, h("span", {}, t(r.display_name || r.key)), h("strong", {}, `${Math.round(r.percent)}%`)))),
    h("small", { class: "muted" }, t("Claude Code 一项分不出是哪台设备；只有本机的 Claude Code 能被采集。")));
}

function renderNow(overview) {
  const box = $("#now");
  box.replaceChildren();
  for (const tool of overview.tools) {
    const card = h("div", { class: `card tool-${tool.tool}` },
      h("div", { class: "card-head" }, h("h3", {}, sprite(tool.tool), TOOL_NAMES[tool.tool]),
        tool.plan ? h("span", { class: "tag" }, tool.plan) : null));
    if (tool.windows.length) {
      tool.windows.forEach((w) => card.append(renderWindowRow(w)));
    } else {
      const msg = tool.tool === "claude" ? ({
        missing: "没找到 Claude Code 的登录信息",
        expired: "Claude Code 登录已过期，运行一次 claude 即可",
        error: "读取 Claude Code 登录信息失败",
      }[tool.credential_status] || "等待第一次额度查询…") : "用一次 Codex 后就会出现";
      card.append(h("p", { class: "empty" }, t(msg)));
    }
    box.append(card);
  }
}

// ── 额度监控：小图组 ─────────────────────────────────────

function renderMonitor(monitor) {
  const box = $("#monitor");
  box.replaceChildren();
  const groups = monitor.groups.filter(isLive);
  for (const g of groups) {
    const status = g.status || "insufficient";
    const card = h("div", { class: `card tool-${g.tool}` },
      h("div", { class: "card-head" },
        h("h3", {}, sprite(g.tool), `${TOOL_NAMES[g.tool]} · ${windowLabel(g)}`),
        h("span", { class: `stamp ${status}`, title: stateName(status) }, t(STAMPS[status] || status))));
    // 没有能估值的窗口：偏离仪和小图都是金额估值，一并不显示，只说明原因
    if (!g.estimated_n) {
      card.append(h("p", { class: "note" }, t("分析起点之后还没有能估值的窗口，暂不判断。")));
      const note = monitorNote(g, status);
      if (note) card.append(note);
      box.append(card);
      continue;
    }
    if (g.recent_median != null) {
      const value = g.recent_median;
      // 收紧是坏消息（红），放宽是好消息（绿），稳定不着色
      const deltaClass = { tighter: "delta bad", looser: "delta good" }[status] || "delta";
      card.append(
        h("div", { class: "figure" },
          h("span", { class: "big" }, usd(value)),
          // 换了模型时这个比值没有意义（回归把差异吸收了），不显示
          g.ratio != null && status !== "model_changed"
            ? h("span", { class: deltaClass }, t("较之前 {pct}", { pct: pctText(g.ratio - 1) }))
            : null),
        h("div", { class: "caption" }, t("一个窗口用满约值（API 价）") +
          (g.recent_n === 1 ? t("（最近 1 个窗口）") : t("（最近 {n} 个窗口中位）", { n: g.recent_n }))));
    }
    // 不在覆盖声明下：估值含推测成分（其他来源按 Code 等价折算），结论只在这里显示，不弹通知
    if (g.eligible && !g.declared && (status === "tighter" || status === "looser")) {
      card.append(h("p", { class: "note" },
        t("这个结论基于推测：其他来源（网页、App、Cowork）的消耗按和 Claude Code 等价折算。只在页面显示，不弹通知。")));
    }
    card.append(deviationGauge(g, status));
    const note = monitorNote(g, status);
    if (note) card.append(note);
    if (g.trend?.length) {
      const node = h("div", { class: "mini-chart" });
      card.append(node);
      requestAnimationFrame(() => drawTrend(node, g, monitor.events));
    } else {
      card.append(h("p", { class: "empty" }, t("已记录 {n} 个窗口，攒到 2 个用量 5% 以上的窗口后开始判断", { n: g.windows_n })));
    }
    box.append(card);
  }
  if (!groups.length) box.append(h("p", { class: "empty" }, t("还没有足够的窗口数据。")));
}

// 跨断档、换代时，说清楚这次是和什么比的
function monitorNote(g, status) {
  if (status === "not_eligible") {
    const why = Object.keys(g.blocked || {}).map((code) => reasonText({ code })).join(t("；"));
    return h("p", { class: "note warn" }, t("最近或基线窗口不满足估值条件，这次不判断有没有被调。"),
      why ? t("原因：{why}", { why }) : "");
  }
  if (status === "model_changed") {
    const rough = g.raw_ratio != null
      ? t("按 API 等价金额粗看，一个窗口之前约 {before}、现在约 {after}（{pct}），仅供参考。",
        { before: usd(g.raw_baseline), after: usd(g.raw_recent), pct: pctText(g.raw_ratio - 1) })
      : "";
    return h("p", { class: "note warn" },
      t("前后主力模型不同（{before} → {after}），额度变化和模型本身的差异分不开，这次不下结论。",
        { before: g.model_before, after: g.model_after }), rough,
      t("等新模型用满几周，会自动和它自己比。"));
  }
  if (g.cross_gap && g.baseline_to) {
    const range = t("{from}～{to}", { from: day(g.baseline_from), to: day(g.baseline_to) });
    if (g.gap_days >= 7) {
      return h("p", { class: "note" }, t("跨断档对比：中间停了 {days} 天，和停用前（{range}）的 {n} 个窗口比。断档期间如果被调过，这里能看出来。",
        { days: Math.round(g.gap_days), range, n: g.baseline_n }));
    }
    return h("p", { class: "note" }, t("近期可比的窗口不够，基线往前取到 {range}。", { range }));
  }
  return null;
}

// 偏离仪：最近几个窗口的中位数比之前变了多少。横轴 ±50%，中间是正常波动范围（判定阈值），
// 指针落在左边红区是收紧、右边黄区是放宽。超出 ±50% 的指针停在两端，读数照实显示
function deviationGauge(g, status) {
  const R = 0.5, th = g.threshold || 0.2;
  // 换代时指针换成虚线，读数是 API 等价金额的粗比，不是判断
  const ratio = status === "model_changed" ? g.raw_ratio : g.ratio;
  const pos = (x) => ((Math.max(-R, Math.min(R, x)) + R) / (2 * R)) * 100;
  const ticks = [[-R, t("◀ 收紧")], [-th, `-${Math.round(th * 100)}%`], [0, "0"], [th, `+${Math.round(th * 100)}%`], [R, t("放宽 ▶")]];
  const el = h("div", { class: ratio == null ? "dev off" : "dev" },
    h("div", { class: "dev-track" },
      h("i", { class: "tight", style: `left:0;width:${pos(-th)}%` }),
      h("i", { class: "ok", style: `left:${pos(-th)}%;width:${pos(th) - pos(-th)}%` }),
      h("i", { class: "loose", style: `left:${pos(th)}%;right:0` })),
    // translateX(-x%)：左端的字左对齐、右端的右对齐、中间的居中
    h("div", { class: "dev-ticks" }, ticks.map(([x, label]) => {
      const p = pos(x).toFixed(1);
      return h("span", { style: `left:${p}%;transform:translateX(-${p}%)` }, label);
    })));
  if (ratio != null) {
    el.append(h("div", { class: `dev-needle ${status}`, style: `left:${pos(ratio - 1)}%`,
      title: status === "model_changed" ? t("前后模型不同，这是按 API 等价金额的粗比")
        : t("最近 {rn} 个窗口中位 {recent}，之前 {bn} 个窗口中位 {baseline}", {
          rn: g.recent_n, recent: usd(g.recent_median), bn: g.baseline_n, baseline: usd(g.baseline_median) }) },
      h("b", {}, (status === "model_changed" ? t("粗比") + " " : "") + pctText(ratio - 1))));
  } else {
    // 还差多少样本：5 小时窗口要最近 72 小时 3 个 + 之前 3 周 5 个，长窗口要最新 1 个 + 之前 2 个
    const [needRecent, needBase] = g.window === "five_hour" ? [3, 5] : [1, 2];
    const dots = (label, have, need) => h("span", { class: "samples" }, label,
      h("span", {}, Array.from({ length: need }, (_, i) => h("i", { class: i < have ? "on" : null }))),
      `${Math.min(have || 0, need)}/${need}`);
    el.append(h("div", { class: "dev-wait", title: t("参与判断的窗口：用量 10% 以上、满足估值条件") },
      dots(g.window === "five_hour" ? t("最近 3 天") : t("最新"), g.recent_n, needRecent),
      dots(t("之前"), g.baseline_n, needBase)));
  }
  return el;
}

// 断档折叠：相邻两点之间空了很久时，把这段空白压成一小截。断档前后的点都放得下，又看得出中间停过。
// 返回 fold（真实时间 → 横轴位置）、unfold（反过来，落在断档里的标记出来）和各断档在横轴上的位置
function foldTime(times, minGap) {
  const ts = [...new Set(times)].sort((a, b) => a - b);
  const gaps = [];
  for (let i = 1; i < ts.length; i++) if (ts[i] - ts[i - 1] > minGap) gaps.push([ts[i - 1], ts[i]]);
  const kept = ts.length ? ts.at(-1) - ts[0] - gaps.reduce((a, [x, y]) => a + (y - x), 0) : 0;
  const width = Math.max(minGap / 3, kept * 0.06);
  const fold = (v) => {
    let shift = 0;
    for (const [x, y] of gaps) {
      if (v <= x) break;
      if (v < y) return x - shift + ((v - x) / (y - x)) * width;
      shift += y - x - width;
    }
    return v - shift;
  };
  const unfold = (v) => {
    let shift = 0;
    for (const [x, y] of gaps) {
      const from = x - shift;
      if (v <= from) break;
      if (v < from + width) return { t: x + ((v - from) / width) * (y - x), inGap: true };
      shift += y - x - width;
    }
    return { t: v + shift, inGap: false };
  };
  return { fold, unfold, gaps: gaps.map(([x, y]) => ({ from: fold(x), to: fold(y), days: (y - x) / 86400e3 })) };
}

function drawTrend(node, g, events) {
  const b = base();
  const color = toolColor(g.tool);
  const times = g.trend.map((p) => p.last_seen * 1000);
  const axis = foldTime(times, (g.window === "five_hour" ? 7 : 14) * 86400e3);
  const toPoint = (p) => [axis.fold(p.last_seen * 1000), +p.value.toFixed(2), p];
  const used = g.trend.filter((p) => p.in_trend).map(toPoint);
  // 纵轴按参与判断的点定范围，离群的未参与点不拉伸坐标
  const ref = (used.length ? used : g.trend.map(toPoint)).map((d) => d[1]);
  const lo = Math.max(0, Math.min(...ref) * 0.75), hi = Math.max(...ref) * 1.25;
  const inRange = (d) => d[1] >= lo && d[1] <= hi;
  const skipped = g.trend.filter((p) => !p.in_trend && !p.excluded).map(toPoint).filter(inRange);
  const excluded = g.trend.filter((p) => p.excluded).map(toPoint).filter(inRange);
  const xMin = Math.min(...times), xMax = Math.max(...times);
  const margin = Math.max((axis.fold(xMax) - axis.fold(xMin)) * 0.03, 3600e3);
  const marks = events
    .filter((e) => e.tool === g.tool && (!e.window || e.window === g.window) && e.ts * 1000 >= xMin)
    .map((e) => {
      const { title, detail } = eventText(e);
      return { xAxis: axis.fold(e.ts * 1000), name: t("{title}：{detail}", { title, detail }) };
    });
  const bm = g.baseline_median, th = g.threshold || 0.2;
  const tip = (p) => {
    const why = p.excluded ? t("你手动排除了，不参与判断")
      : p.quality && p.quality !== "conditional" ? t("{quality}，未参与判断", { quality: qualityName(p.quality) })
        : !p.in_trend ? t("用量太少，未参与判断")
          : p.role === "baseline" && g.cross_gap ? t("断档前的基线窗口") : "";
    return `<strong>${usd(p.value)}</strong><br>${esc(t("{start} 起 · 已用 {pct}%", { start: when(p.start), pct: p.pct.toFixed(0) }))}` +
      (why ? `<br>${esc(why)}` : "");
  };
  const short = xMax - xMin < 2 * 86400e3;
  const label = (v) => {
    const u = axis.unfold(v);
    if (u.inGap || u.t > Date.now() + 3600e3) return "";  // 断档里、还没到的日期都不标
    const d = new Date(u.t);
    return short ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  };
  const names = { used: t("参与判断"), skipped: t("未参与"), excluded: t("你排除的") };
  const chart = render(node, {
    textStyle: b.textStyle,
    grid: { left: 44, right: 22, top: 26, bottom: 22 },
    legend: { ...b.legend, icon: "rect", data: [names.used, names.skipped, ...(excluded.length ? [names.excluded] : [])] },
    tooltip: { ...b.tooltip, trigger: "item",
      formatter: (it) => it.componentType === "markLine" || it.componentType === "markArea" ? esc(it.name) : tip(it.data[2]) },
    xAxis: { type: "value", min: axis.fold(xMin) - margin, max: axis.fold(xMax) + margin, ...b.axis, splitLine: { show: false },
      // 两端是留白处，标签会和旁边的挤在一起、或者显示成还没到的日期，不标
      axisLabel: { ...b.axis.axisLabel, formatter: label, showMinLabel: false, showMaxLabel: false } },
    yAxis: { type: "value", min: +lo.toFixed(2), max: +hi.toFixed(2), splitNumber: 3, ...b.axis, axisLine: { show: false },
      axisLabel: { ...b.axis.axisLabel, formatter: (v) => "$" + Math.round(v) } },
    series: [
      { name: names.used, type: "scatter", symbol: "rect", symbolSize: 8, data: used, silent: true,
        itemStyle: { color, borderColor: cssVar("--surface-1"), borderWidth: 2 },
        markArea: { silent: false, data: [
          ...(bm ? [[{ yAxis: bm * (1 - th), itemStyle: { color: cssVar("--band") }, name: t("正常波动范围"), label: { show: false } },
            { yAxis: bm * (1 + th) }]] : []),
          ...axis.gaps.map((gp) => [{ xAxis: gp.from, itemStyle: { color: cssVar("--fold") },
            name: t("停用 {days} 天（已折叠）", { days: Math.round(gp.days) }),
            label: { show: true, position: "insideTop", color: b.muted, fontSize: 10,
              formatter: t("停 {days} 天", { days: Math.round(gp.days) }) } },
          { xAxis: gp.to }]),
        ] },
        markLine: { symbol: "none", animation: false,
          data: [
            ...(bm ? [{ yAxis: bm, name: t("之前中位 {value}", { value: usd(bm) }),
              lineStyle: { color: b.muted, width: 1, type: "solid" }, label: { show: false } }] : []),
            ...marks.map((m) => ({ ...m, lineStyle: { color: cssVar("--axis"), width: 1, type: "solid" }, label: { show: false } })),
          ] } },
      { name: names.skipped, type: "scatter", symbol: "rect", symbolSize: 7, data: skipped, silent: true,
        itemStyle: { color: "transparent", borderColor: b.muted, borderWidth: 1.5 } },
      { name: names.excluded, type: "scatter", symbol: "rect", symbolSize: 7, data: excluded, silent: true,
        itemStyle: { color: "transparent", borderColor: b.muted, borderWidth: 1.5, borderType: "dashed", opacity: 0.6 } },
      // 透明的大点只负责悬停和点击：8px 的点太难点中，命中区域放大到 24px
      { name: "hit", type: "scatter", symbolSize: 24, data: [...used, ...skipped, ...excluded], cursor: "pointer",
        itemStyle: { color: "rgba(0,0,0,0)" }, emphasis: { disabled: true } },
    ],
  });
  chart.off("click");
  chart.on("click", (it) => {
    if (it.seriesName === "hit") {
      showDetail({ ...it.data[2], tool: g.tool, scope: g.scope, window: g.window })
        .then(() => $("#detail").scrollIntoView({ behavior: "smooth", block: "nearest" }));
    }
  });
}

// ── 窗口详情 ─────────────────────────────────────────────

async function showDetail(w) {
  const q = new URLSearchParams({ tool: w.tool, scope: w.scope, window: w.window, end: w.end });
  const d = await api(`/api/window?${q}`);
  const panel = $("#detail");
  panel.hidden = false;
  const b = base();
  const color = toolColor(w.tool);
  $("#detail-title").textContent = t("{tool} {window}窗口 · {start} 起", { tool: TOOL_NAMES[w.tool], window: windowLabel(w), start: when(d.start) });
  const jumps = d.steps.filter((s) => s.external > 0).map((s) => {
    const p = d.points.find((x) => x.ts === s.ts);
    return [p.pct, +p.cost.toFixed(4), s.ts, s.external];
  });
  const ext = jumps.reduce((a, j) => a + j[3], 0);
  // 分子、分母、截止时间都给出来，用户可以自己复算
  $("#detail-note").textContent = t("截至 {when}：本机 API 等价 {cost} ÷ 已用 {pct}%",
    { when: when(d.snapshot_at || d.last_seen), cost: usd(d.cost_at_snapshot), pct: d.used_percent.toFixed(0) }) +
    (d.quality === "conditional" && d.cap ? t(" ≈ 用满{range}", { range: capRange(d) }) : "") +
    (d.unsegmented_pct >= 1 ? t(" · 开始监测前已用的 {pct}% 没有分步记录", { pct: d.unsegmented_pct.toFixed(0) }) : "") +
    (jumps.length ? t(" · 疑似未记录消耗约 {pct}%（未扣除）", { pct: ext.toFixed(0) }) : "");
  const quality = $("#detail-quality");
  quality.replaceChildren();
  if (d.quality) {
    quality.append(h("strong", { class: `quality ${d.quality}` }, qualityName(d.quality)),
      d.claimed && d.quality === "conditional" ? h("span", { class: "muted" }, " · " + t("基于你的覆盖声明")) : null,
      d.reasons?.length ? h("ul", {}, d.reasons.map((r) => h("li", {}, reasonText(r)))) : null);
  }
  const btn = $("#detail-exclude");
  btn.textContent = d.excluded ? t("恢复参与判断") : t("不让这个窗口参与判断");
  btn.title = t("比如这个窗口里你在别的设备上也用过、或者有别的异常。数据本身不删，随时可以恢复");
  btn.onclick = async () => {
    await toggleWindow(w.tool, d.key, !d.excluded);
    await showDetail(w);
  };
  const names = { cost: t("累计花费"), jump: t("疑似未记录消耗") };
  render($("#detail-chart"), {
    textStyle: b.textStyle,
    grid: { left: 52, right: 56, top: 30, bottom: 28 },
    legend: jumps.length ? { ...b.legend, data: [names.cost, names.jump] } : undefined,
    tooltip: { ...b.tooltip, trigger: "item",
      formatter: (it) => {
        const [pct, cost, ts, extPct] = it.data;
        return `<strong>${usd(cost)}</strong><br>${esc(t("已用 {pct}% · {when}", { pct, when: when(ts) }))}` +
          (extPct ? `<br>${esc(t("额度多涨约 {pct}%，本机没有对应记录", { pct: extPct.toFixed(0) }))}` : "");
      } },
    xAxis: { type: "value", min: 0, max: Math.max(10, Math.ceil((d.used_percent || 0) / 10) * 10), ...b.axis,
      name: t("已用"), nameTextStyle: { color: b.muted }, axisLabel: { ...b.axis.axisLabel, formatter: "{value}%" } },
    yAxis: { type: "value", ...b.axis, axisLine: { show: false },
      axisLabel: { ...b.axis.axisLabel, formatter: (v) => "$" + v } },
    series: [
      { name: names.cost, type: "line", step: "end", showSymbol: false, symbolSize: 8,
        data: d.points.map((p) => [p.pct, +p.cost.toFixed(4), p.ts]),
        lineStyle: { width: 2, color }, itemStyle: { color }, areaStyle: { color, opacity: 0.1 },
        endLabel: { show: true, color: cssVar("--text-primary"), formatter: () => usd(d.cost_at_snapshot) } },
      { name: names.jump, type: "scatter", symbol: "rect", data: jumps, symbolSize: 10,
        itemStyle: { color: cssVar("--critical"), borderColor: cssVar("--surface-1"), borderWidth: 2 } },
    ],
  });
}

// ── 模型汇率 ─────────────────────────────────────────────

function renderRates(monitor) {
  const box = $("#rates");
  box.replaceChildren();
  for (const g of monitor.groups.filter(isLive)) {
    const rows = (g.rates || []).filter((r) => r.unit === "usd" && r.cap_if_only).sort((a, b) => a.cap_if_only - b.cap_if_only);
    if (!rows.length) {
      // 汇率要靠至少 2 个用量 5% 以上的窗口回归；说清楚还差什么、大概什么时候有
      const next = g.latest_end > now()
        ? t("当前窗口 {when} 结束，下一个窗口用到 5% 以后就能算出来。", { when: clock(g.latest_end) })
        : t("下一个窗口用到 5% 以后就能算出来。");
      box.append(h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h3", {}, `${TOOL_NAMES[g.tool]} · ${windowLabel(g)}`),
          h("span", { class: "pill" }, t("{n} 个窗口", { n: g.windows_n }))),
        h("p", { class: "empty" }, t("至少要 2 个窗口才能分出各模型的汇率，现在只有 {n} 个。", { n: g.windows_n }), h("br"), next)));
      continue;
    }
    const node = h("div", { class: "chart", style: `height:${Math.max(120, rows.length * 34 + 40)}px` });
    box.append(h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h3", {}, `${TOOL_NAMES[g.tool]} · ${windowLabel(g)}`),
        h("span", { class: "pill" }, t("{n} 个窗口", { n: g.fit_windows }))),
      node));
    requestAnimationFrame(() => drawRates(node, g, rows));
  }
  if (!box.children.length) box.append(h("p", { class: "empty" }, t("至少需要 2 个用量 5% 以上的干净窗口才能回归。")));
}

function drawRates(node, g, rows) {
  const b = base();
  const color = toolColor(g.tool);
  render(node, {
    textStyle: b.textStyle,
    grid: { left: 8, right: 64, top: 4, bottom: 4, containLabel: true },
    tooltip: { ...b.tooltip, trigger: "item",
      formatter: (it) => `<strong>${usd(it.value)}</strong><br>${esc(it.name)} · ` +
        esc(t("出现在 {n} 个窗口", { n: rows[it.dataIndex].windows })) +
        (rows[it.dataIndex].windows < 4 ? esc(t("（数据少）")) : "") },
    xAxis: { type: "value", show: false },
    yAxis: { type: "category", data: rows.map((r) => r.model), ...b.axis, axisLine: { show: false },
      axisLabel: { color: cssVar("--text-secondary"), fontSize: 12 } },
    series: [{
      type: "bar", barMaxWidth: 16,
      data: rows.map((r) => ({ value: +r.cap_if_only.toFixed(2),
        itemStyle: { color, opacity: r.windows < 4 ? 0.35 : 1 } })),
      label: { show: true, position: "right", color: cssVar("--text-primary"), formatter: (it) => usd(it.value) },
    }],
  });
}

// ── 用量 ─────────────────────────────────────────────────

function tile(label, value, hint, sparkData) {
  const spark = sparkData ? h("div", { class: "spark" }) : null;
  const el = h("div", { class: "tile" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value),
    hint ? h("div", { class: "hint" }, hint) : null, spark);
  if (spark) requestAnimationFrame(() => drawSpark(spark, sparkData));
  return el;
}

function drawSpark(node, data) {
  const color = cssVar("--accent");
  render(node, {
    grid: { left: 0, right: 0, top: 4, bottom: 2 },
    xAxis: { type: "category", show: false, data: data.map((d) => d[0]) },
    yAxis: { type: "value", show: false, min: 0 },
    tooltip: { ...base().tooltip, trigger: "axis", valueFormatter: (v) => usd(v) },
    series: [{ type: "line", data: data.map((d) => d[1]), showSymbol: false, smooth: false,
      lineStyle: { width: 2, color }, areaStyle: { color, opacity: 0.1 } }],
  });
}

function renderUsage(report) {
  const s = report.summary;
  const byBucket = new Map();
  report.series.forEach((r) => byBucket.set(r.bucket, (byBucket.get(r.bucket) || 0) + r.cost_usd));
  const sparkData = [...byBucket.entries()].map(([k, v]) => [k, +v.toFixed(2)]);
  $("#usage-tiles").replaceChildren(
    tile(t("等价 API 花费"), usd(s.cost_usd), null, sparkData.length > 1 ? sparkData : null),
    tile(t("请求数"), s.requests.toLocaleString(), s.unpriced_requests ? t("{n} 次无价格", { n: s.unpriced_requests }) : null),
    tile(t("缓存命中率"), s.cache_hit_rate == null ? "—" : `${(s.cache_hit_rate * 100).toFixed(1)}%`,
      t("缓存读 {n} token", { n: compact(s.cache_read_tokens) })),
    tile(t("输出"), compact(s.output_tokens) + " token", s.reasoning_tokens ? t("其中推理 {n}", { n: compact(s.reasoning_tokens) }) : null),
  );

  // 花费随时间：按工具堆叠
  const b = base();
  const buckets = [...byBucket.keys()];
  const tools = state.tool === "all" ? ["codex", "claude"] : [state.tool];
  const surface = cssVar("--surface-1");
  const series = tools.map((tool) => ({
    name: TOOL_NAMES[tool], type: "bar", stack: "cost", barMaxWidth: 24,
    itemStyle: { color: toolColor(tool), borderColor: surface, borderWidth: 1 },
    data: buckets.map((bk) => {
      const row = report.series.find((r) => r.bucket === bk && r.tool === tool);
      return row ? +row.cost_usd.toFixed(4) : 0;
    }),
  }));
  $("#cost-chart-title").textContent = state.range === "today" ? t("等价 API 花费（按小时）") : t("等价 API 花费（按天）");
  render($("#cost-chart"), {
    textStyle: b.textStyle,
    grid: { left: 48, right: 12, top: 30, bottom: 26 },
    legend: tools.length > 1 ? { ...b.legend, icon: "rect" } : undefined,
    tooltip: { ...b.tooltip, trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: (v) => usd(v) },
    xAxis: { type: "category", data: buckets.map((bk) => bk.slice(5)), ...b.axis, splitLine: { show: false } },
    yAxis: { type: "value", ...b.axis, axisLine: { show: false },
      axisLabel: { ...b.axis.axisLabel, formatter: (v) => "$" + v } },
    series,
  });

  // 花在哪些模型上：有价格的订阅模型，按花费排序
  const priced = report.models.filter((m) => m.on_plan && m.cost_usd > 0).sort((a, c) => a.cost_usd - c.cost_usd).slice(-8);
  const unpricedReq = report.models.filter((m) => m.unpriced_requests === m.requests).reduce((a, m) => a + m.requests, 0);
  const shownTools = [...new Set(priced.map((m) => m.tool))];
  render($("#model-chart"), {
    textStyle: b.textStyle,
    grid: { left: 8, right: 60, top: shownTools.length > 1 ? 28 : 4, bottom: 4, containLabel: true },
    legend: shownTools.length > 1 ? { ...b.legend, icon: "rect", data: shownTools.map((x) => TOOL_NAMES[x]) } : undefined,
    tooltip: { ...b.tooltip, trigger: "item", formatter: (it) => `<strong>${usd(it.value)}</strong><br>${esc(it.name)}` },
    xAxis: { type: "value", show: false },
    yAxis: { type: "category", data: priced.map((m) => m.model), ...b.axis, axisLine: { show: false },
      axisLabel: { color: cssVar("--text-secondary"), fontSize: 12, width: 120, overflow: "truncate" } },
    series: shownTools.map((tool) => ({
      name: TOOL_NAMES[tool], type: "bar", stack: "m", barMaxWidth: 16,
      itemStyle: { color: toolColor(tool) },
      data: priced.map((m) => (m.tool === tool ? +m.cost_usd.toFixed(2) : null)),
      label: { show: true, position: "right", color: cssVar("--text-primary"),
        formatter: (it) => (it.value ? usd(it.value) : "") },
    })),
  });
  $("#model-note").textContent = unpricedReq ? t("另有 {n} 次请求转发到第三方或没有价格，未计入。", { n: unpricedReq.toLocaleString() }) : "";

  renderModelTable(report.models);
}

function renderModelTable(models) {
  const table = $("#model-table");
  table.replaceChildren(h("tr", {}, h("th", {}, t("工具")), h("th", { class: "text" }, t("模型")), h("th", { class: "text" }, t("计费")),
    h("th", {}, t("请求")), h("th", {}, t("输入")), h("th", {}, t("缓存读")), h("th", {}, t("缓存写")), h("th", {}, t("输出")),
    h("th", {}, t("命中率")), h("th", {}, t("等价花费"))));
  for (const m of models) {
    table.append(h("tr", {},
      h("td", {}, TOOL_NAMES[m.tool]), h("td", { class: "text" }, m.model),
      h("td", { class: "text" }, m.on_plan ? t("订阅") : t("第三方")),
      h("td", {}, m.requests.toLocaleString()), h("td", {}, compact(m.input_tokens)),
      h("td", {}, compact(m.cache_read_tokens)), h("td", {}, compact(m.cache_write_tokens)),
      h("td", {}, compact(m.output_tokens)),
      h("td", {}, m.cache_hit_rate == null ? "—" : (m.cache_hit_rate * 100).toFixed(0) + "%"),
      h("td", {}, m.unpriced_requests === m.requests ? t("无价格") : usd(m.cost_usd))));
  }
}

// ── 折叠区：事件、窗口历史、状态 ─────────────────────────

// 规则变化：中文直接用后端写好的说明；英文用事件里的原始数据重新组句
function eventText(e) {
  const p = e.params || {};
  if (LANG !== "en" || !Object.keys(p).length) return { title: e.title, detail: e.detail };
  if (e.kind === "plan") return { title: t("订阅方案变化"), detail: `${p.from} → ${p.to}` };
  if (e.kind === "window") return { title: t("出现新的限额窗口"), detail: `${p.scope} · ${p.window}` };
  if (e.kind === "reset") {
    return { title: t("窗口提前重置"), detail: `${p.scope} · ${p.window}: ` +
      t("上个窗口用到 {pct}%，原定 {due} 重置", { pct: Math.round(p.pct), due: when(p.due) }) +
      (p.maybe_switch ? t("，可能用了重置券或切换了账号") : "") };
  }
  if (e.kind === "schema") {
    const where = `${TOOL_NAMES[e.tool]} ${p.source === "api" ? t("额度接口") : t("日志")}`;
    return { title: t(p.change === "added" ? "{where}出现新字段" : "{where}的字段不再出现", { where }), detail: p.fields.join(", ") };
  }
  return { title: e.title, detail: e.detail };
}

function renderEvents(events) {
  $("#events-count").textContent = events.length ? t("（近 90 天 {n} 条）", { n: events.length }) : "";
  const box = $("#events");
  box.replaceChildren(...events.slice(0, 50).map((e) => {
    const { title, detail } = eventText(e);
    return h("div", { class: "event" },
      h("div", { class: "when" }, when(e.ts)),
      h("div", {}, h("strong", {}, `${TOOL_NAMES[e.tool]} · ${title}`), detail ? h("div", { class: "detail" }, detail) : null));
  }));
  if (!events.length) box.append(h("p", { class: "empty" }, t("近 90 天没有发现规则变化。")));
}

function renderWindowTable(rows) {
  const table = $("#window-table");
  table.replaceChildren(h("tr", {},
    h("th", {}, t("窗口")), h("th", {}, t("开始")), h("th", {}, t("已用")), h("th", {}, t("估值条件")), h("th", {}, t("本机 API 等价")),
    h("th", {}, t("用满估算")), h("th", {}, t("折合主力模型")), h("th", { class: "text" }, t("参与判断")), h("th", {}, "")));
  const shown = rows.filter((r) => r.supported && r.used_percent > 0);
  for (const r of shown) {
    const why = r.excluded ? t("否（你排除了）") : r.in_trend ? t("是")
      : r.quality && r.quality !== "conditional" ? t("否（{quality}）", { quality: qualityName(r.quality) }) : t("否");
    table.append(h("tr", { class: r.excluded ? "clickable excluded" : "clickable",
      onclick: () => showDetail(r).then(() => $("#detail").scrollIntoView({ behavior: "smooth" })) },
      h("td", {}, `${windowLabel(r)}${r.plan_type ? " · " + r.plan_type : ""}`), h("td", {}, when(r.start)),
      h("td", {}, `${r.used_percent.toFixed(0)}%`),
      h("td", { class: "text", title: (r.reasons || []).map(reasonText).join("\n") }, r.quality ? qualityName(r.quality) : "—"),
      h("td", {}, usd(r.cost_at_snapshot)), h("td", {}, r.quality === "conditional" && r.cap ? capRange(r) : "—"),
      h("td", {}, r.value ? usd(r.value) : "—"),
      h("td", { class: "text" }, why),
      h("td", {}, h("button", { type: "button", class: "mini", title: t("数据不删，只是不参与汇率、趋势和告警"),
        onclick: (e) => { e.stopPropagation(); toggleWindow(r.tool, r.key, !r.excluded); } },
      r.excluded ? t("恢复") : t("排除")))));
  }
  if (!shown.length) table.append(h("tr", {}, h("td", { colspan: 9, class: "text" }, t("还没有窗口数据。"))));
}

async function toggleWindow(tool, key, ignore) {
  await post("/api/data/rule", { tool, kind: "window", target: key, ignore });
  await loadAll();
}

// ── 数据管理 ─────────────────────────────────────────────

const PLAN_MODES = { keep: "参与分析", ignore: "只保留" };

function renderData(d) {
  const box = $("#data-panel");
  const backups = t("每天自动备份一份，留最近 14 份") +
    (d.backups.before_delete ? t("；另有 {n} 份删除数据前的备份", { n: d.backups.before_delete }) : "") + t("。");
  const intro = h("p", { class: "data-intro" },
    h("strong", {}, t("数据库 {mb} MB，永久保存，不会自动清理。", { mb: (d.db_bytes / 1e6).toFixed(1) })),
    t("Claude Code 默认会删掉 30 天前的日志，这里的副本不受影响。"), backups,
    h("br"), t("数据目录："), h("code", {}, d.data_dir));
  const sources = Object.entries(d.logs).map(([tool, l]) => h("div", { class: "data-source" },
    h("strong", {}, TOOL_NAMES[tool]), t("：已存 {n} 次请求", { n: l.requests.toLocaleString() }) +
      (l.first ? t("（{date} 起）", { date: date(l.first) }) : "") + t("。"),
    l.pending ? t("本机还有 {n} 个更早的日志没导入（最早 {date}）。", { n: l.pending, date: date(l.pending_oldest) })
      : t("本机的日志都已导入。")));
  const pending = Object.values(d.logs).some((l) => l.pending);
  const importBtn = pending ? h("button", { type: "button", onclick: importHistory }, t("导入全部历史日志")) : null;

  const table = h("table", {}, h("tr", {}, h("th", { class: "text" }, t("数据")), h("th", { class: "text" }, t("时间")),
    h("th", {}, t("窗口")), h("th", { class: "text" }, t("要不要留")), h("th", {}, "")));
  for (const g of d.groups) {
    const name = `${TOOL_NAMES[g.tool]} · ${g.plan || t("未记录方案")}`;
    if (g.mode === "deleted") {
      table.append(h("tr", { class: "deleted" }, h("td", { class: "text" }, name),
        h("td", { class: "text", colspan: 4 }, t("已于 {date} 删除；以后再用这个方案，新数据照常记录", { date: date(g.deleted_at) }))));
      continue;
    }
    const seg = g.plan ? h("div", { class: "seg" }, Object.entries(PLAN_MODES).map(([mode, text]) =>
      h("button", { type: "button", class: g.mode === mode ? "on" : null,
        title: mode === "ignore" ? t("数据留着，但不参与汇率、趋势和告警（比如以前用过的别的账号）") : t("默认"),
        onclick: () => setPlanMode(g.tool, g.plan, mode) }, t(text)))) : h("span", { class: "muted" }, "—");
    table.append(h("tr", {},
      h("td", { class: "text" }, name),
      h("td", { class: "text" }, t("{from}～{to}", { from: date(g.first), to: date(g.last) })),
      h("td", {}, g.windows + (g.ignored_windows ? t("（排除 {n}）", { n: g.ignored_windows }) : "")),
      h("td", { class: "text" }, seg),
      h("td", {}, g.plan ? h("button", { type: "button", class: "mini danger", onclick: () => deletePlan(g) }, t("删除…")) : null)));
  }
  box.replaceChildren(intro, ...sources, importBtn ? h("div", { class: "data-actions" }, importBtn) : "",
    h("div", { class: "table-wrap" }, table),
    h("p", { class: "muted small" }, t("「只保留」：数据不动，只是不参与分析；单个窗口可以在「窗口历史」或窗口详情里排除。「删除」前会自动把整个数据库备份一份。")));
}

async function loadData() {
  renderData(await api("/api/data"));
}

async function setPlanMode(tool, plan, mode) {
  await post("/api/data/rule", { tool, kind: "plan", target: plan, ignore: mode === "ignore" });
  await Promise.all([loadData(), loadAll()]);
}

async function deletePlan(g) {
  const what = g.tool === "codex"
    ? t("它的额度快照，以及只属于这些窗口的请求记录")
    : t("它的额度快照（Claude 的请求记录不分方案，留在用量统计里）");
  const ok = confirm(t("删除「{name}」的 {n} 个窗口？", { name: `${TOOL_NAMES[g.tool]} · ${g.plan}`, n: g.windows }) + "\n\n" +
    t("会删掉{what}。删除前会自动把整个数据库备份一份。删掉的数据以后重读日志也不会再导回来。", { what }));
  if (!ok) return;
  const r = await post("/api/data/delete", { tool: g.tool, plan: g.plan });
  await Promise.all([loadData(), loadAll()]);
  $("#data-panel").prepend(h("p", { class: "data-done" },
    t("已删除 {w} 个窗口、{s} 条额度快照、{r} 条请求。删除前的备份：{path}", { w: r.windows, s: r.snapshots, r: r.requests, path: r.backup })));
}

async function importHistory(e) {
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.textContent = t("导入中…");
  try {
    const r = await post("/api/data/import-history");
    const rows = Object.values(r).reduce((a, x) => a + (x.rows || 0), 0);
    await Promise.all([loadData(), loadAll()]);
    $("#data-panel").prepend(h("p", { class: "data-done" }, t("导入完成，新增 {n} 次请求。", { n: rows.toLocaleString() })));
  } catch (err) {
    btn.disabled = false;
    btn.textContent = t("导入全部历史日志");
    alert(t("导入失败：") + err.message);
  }
}

// ── 模型价格 ─────────────────────────────────────────────

const SOURCE_NAMES = { "models.dev": "models.dev", seed: "内置快照", override: "你手填的" };

function price(v) {
  if (v === null || v === undefined) return "—";
  return "$" + (v >= 10 ? v.toFixed(1) : v >= 1 ? v.toFixed(2) : v.toFixed(3)).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
}

function renderPrices(prices) {
  const byKey = new Map(prices.catalog.map((c) => [c.model, c]));
  const usedByKey = new Map();
  for (const m of prices.models) {
    if (m.price_key) usedByKey.set(m.price_key, (usedByKey.get(m.price_key) || 0) + m.requests);
  }
  const q = state.priceQuery.trim().toLowerCase();
  let rows;
  if (state.priceView === "used") {
    // 用过的模型（按日志里的原名），查到的价格；第三方转发的不计价，不列
    rows = prices.models.filter((m) => m.on_plan).map((m) => ({ name: m.model, key: m.price_key,
      entry: m.price_key ? byKey.get(m.price_key) : null, requests: m.requests, tool: m.tool }));
  } else {
    rows = prices.catalog.map((c) => ({ name: c.model, key: c.model, entry: c, requests: usedByKey.get(c.model) || 0 }));
  }
  rows = rows.filter((r) => !q || r.name.toLowerCase().includes(q));
  const table = $("#price-table");
  table.replaceChildren(h("tr", {}, h("th", { class: "text" }, t("模型")), h("th", {}, t("输入")), h("th", {}, t("缓存读")),
    h("th", {}, t("缓存写")), h("th", {}, t("输出")), h("th", { class: "text" }, t("说明")), h("th", {}, t("你的请求"))));
  for (const r of rows) {
    const e = r.entry;
    const notes = [];
    if (!e) notes.push(t("没有价格，不计入金额"));
    else {
      if (r.key !== r.name) notes.push(t("按 {model} 计价", { model: r.key }));
      if (e.alias_of) notes.push(t("同 {model}", { model: e.alias_of }));
      if (e.tiers?.length) notes.push(t("超过 {n} token 上下文加价", { n: compact(e.tiers[0].size) }));
      if (e.fast) notes.push(t("fast 模式另价"));
      if (e.source && e.source !== "models.dev") notes.push(t(SOURCE_NAMES[e.source] || e.source));
    }
    // Claude 的 1 小时缓存写入按输入价 2 倍计（和计价逻辑一致），表里没写时补上
    const cw1h = e?.cache_write_1h ?? (e && r.name.startsWith("claude-") ? e.input * 2 : null);
    const cw = e && e.cache_write != null ? price(e.cache_write) + (cw1h != null ? ` / 1h ${price(cw1h)}` : "") : "—";
    table.append(h("tr", { class: e ? null : "unpriced" },
      h("td", { class: "text" }, r.name), h("td", {}, price(e?.input)), h("td", {}, price(e?.cache_read)),
      h("td", {}, cw), h("td", {}, price(e?.output)),
      h("td", { class: "text muted" }, notes.join(t("，"))),
      h("td", {}, r.requests ? r.requests.toLocaleString() : "—")));
  }
  if (!rows.length) table.append(h("tr", {}, h("td", { colspan: 7, class: "text" }, t("没有匹配的模型。"))));
  $("#price-count").textContent = t("（{n} 个模型）", { n: prices.catalog.length });
  $("#price-note").textContent = t("单位：美元 / 百万 token。价格来自 models.dev，每天自动更新（最近一次：{when}）。", { when: prices.fetched_at || "—" }) +
    t("要改价或给没价格的模型补价，编辑 {path}（格式见项目里的 prices_override.example.json），然后点「立即同步」。", { path: prices.override_path });
}

// ── 设置 ─────────────────────────────────────────────────

const SETTING_FIELDS = [
  ["language", "界面语言", "select"],
  ["notify", "发现额度可能被调时弹系统通知", "checkbox"],
  ["claude_poll_minutes", "Claude Code 空闲时多久查一次额度（分钟）", "number", "使用中固定 3 分钟一次"],
  ["min_change_pct", "偏离超过多少才判为「被调」（%）", "number", "这是下限：你的数据本身波动大时会自动放宽"],
  ["model_overlap_pct", "前后共同在用的模型低于多少，算「换了模型」（%）", "number", "按最近窗口的花费占比算"],
  ["history_days", "首次启动导入多久以内的日志（天）", "number", "之后可以在「数据管理」里导入全部"],
  ["analysis_since", "判断额度有没有被调，从什么时候开始算", "datetime", "之前开始的窗口照常显示，但不参与比较。留空 = 从正式观察开始"],
];
// 本地时间 <-> datetime-local 输入框的值
const toLocalInput = (ts) => {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};
const LANGUAGES = { auto: "跟随系统", zh: "中文", en: "English" };

function renderSettings(s) {
  const form = h("form", { class: "settings", onsubmit: saveSettings });
  for (const [key, label, kind, hint] of SETTING_FIELDS) {
    let input;
    if (kind === "select") {
      input = h("select", { name: key }, Object.entries(LANGUAGES).map(([v, text]) =>
        h("option", { value: v, selected: s.values[key] === v ? "" : null }, t(text))));
    } else if (kind === "checkbox") {
      input = h("input", { type: "checkbox", name: key, checked: s.values[key] ? "" : null });
    } else if (kind === "datetime") {
      input = h("input", { type: "datetime-local", name: key, value: toLocalInput(s.values[key]),
        placeholder: toLocalInput(s.default_since) });
      form.append(h("label", { class: `field ${kind}` }, h("span", {}, t(label)), input,
        h("small", {}, t(hint) + (s.default_since ? t("（{when}）", { when: when(s.default_since) }) : ""))));
      continue;
    } else {
      const [lo, hi] = s.limits[key];
      input = h("input", { type: "number", name: key, value: s.values[key], min: lo, max: hi, step: 1 });
    }
    form.append(h("label", { class: `field ${kind}` }, h("span", {}, t(label)), input,
      hint ? h("small", {}, t(hint) + (s.defaults[key] !== undefined ? t("（默认 {v}）", { v: s.defaults[key] }) : "")) : null));
  }
  form.append(h("div", { class: "settings-actions" }, h("button", { type: "submit" }, t("保存")),
    h("span", { id: "settings-saved", class: "muted" })));
  $("#settings-panel").replaceChildren(form, h("div", { id: "coverage-panel", class: "coverage" }));
  loadCoverage();
}

// 覆盖声明：每个工具、每个账号分别声明，从打开时起生效，不追认过去
const COVERAGE_HELP = {
  claude: "如果这个账号只在这台电脑上用 Claude Code，不用 claude.ai 网页、手机 App、桌面版聊天或 Cowork，也没有别的电脑或云任务在用，就可以打开。",
  codex: "如果这个账号只在这台电脑上用 Codex，没有别的电脑、云任务或网页版在用，就可以打开。",
};

async function loadCoverage() {
  const cov = await api("/api/coverage");
  const box = $("#coverage-panel");
  if (!box) return;
  box.replaceChildren(h("h3", {}, t("覆盖声明")),
    h("p", { class: "muted" }, t("声明「这个账号在声明期间，所有消耗这份额度的使用都会被本项目采集到」。打开后，之后开始的窗口可以显示容量估值和收紧提示；之前的窗口不受影响。数据显示有别的来源时以数据为准，不估值。")));
  for (const tool of Object.keys(TOOL_NAMES)) {
    const c = cov[tool];
    if (!c) continue;
    const state = !c.account_known ? t("读不到当前账号，暂时不能声明。")
      : c.on ? t("已打开，{when} 起生效。", { when: when(c.since) })
        : c.other_account ? t("之前的声明属于别的账号，切换账号后需要重新确认。")
          : t("未打开。");
    const input = h("input", { type: "checkbox", checked: c.on ? "" : null, disabled: !c.account_known && !c.on ? "" : null,
      onchange: async (e) => {
        e.target.disabled = true;
        try {
          await post("/api/coverage", { tool, on: e.target.checked });
        } catch (err) {
          alert(t(err.message));
        } finally {
          await loadCoverage();
          loadAll();
        }
      } });
    box.append(h("label", { class: "field checkbox" },
      h("span", {}, t("{tool}：这个账号的使用都被本项目采集", { tool: TOOL_NAMES[tool] })), input,
      h("small", {}, t(COVERAGE_HELP[tool]) + " " + state)));
  }
}

async function saveSettings(e) {
  e.preventDefault();
  const form = e.currentTarget;
  const changes = {};
  for (const [key, , kind] of SETTING_FIELDS) {
    const el = form.elements[key];
    changes[key] = kind === "checkbox" ? el.checked : kind === "number" ? Number(el.value)
      : kind === "datetime" ? (el.value ? Math.floor(new Date(el.value).getTime() / 1000) : 0) : el.value;
  }
  const r = await post("/api/settings", changes);
  setLang(r.values.language);
  await init();
  $("#settings-saved").textContent = t("已保存");
}

// ── 运行状态 ─────────────────────────────────────────────

const POLL_STATUS = { ok: "正常", missing: "没找到登录信息", expired: "登录过期", error: "出错", rate_limited: "被限流，稍后再试" };

function renderStatus(overview, prices, conns) {
  const st = overview.status;
  const poll = (p) => (p ? t("{when}，{status}", { when: ago(p.at), status: t(POLL_STATUS[p.status] || p.status) }) +
    (p.error ? t("（{error}）", { error: p.error }) : "") : t("还没有"));
  const lines = [
    t("日志扫描：") + (st.last_scan ? ago(st.last_scan) : t("还没有")) +
      Object.entries(st.scan || {}).map(([k, v]) => ` · ${TOOL_NAMES[k]} ` + t("{n} 个文件", { n: v.files })).join(""),
    t("Claude 额度：") + poll(st.claude_quota) + t("（使用中每 3 分钟查一次）"),
    t("Codex 额度：") + poll(st.codex_quota) + t("（只在本地空闲时查）"),
    ...conns.map((c) => t("{tool} 登录信息：{status}（{where}）",
      { tool: TOOL_NAMES[c.tool], status: t(POLL_STATUS[c.credentials] || c.credentials), where: c.credentials_where })),
    t("价格表：models.dev，更新于 {when}", { when: prices.fetched_at || "—" }),
    t("备份：") + (st.backup ? `${ago(st.backup.at)} · ${st.backup.path}` : t("还没有")),
  ];
  const box = $("#status");
  box.replaceChildren(...lines.map((l) => h("div", {}, l)));
  const unpriced = prices.models.filter((m) => m.on_plan && !m.price_key);
  if (unpriced.length) {
    box.append(h("div", {}, t("没有价格的订阅模型：{models}（可以在「模型价格」说明的文件里补价）",
      { models: unpriced.map((m) => m.model).join(t("、")) })));
  }
  for (const e of (st.errors || []).slice(0, 5)) box.append(h("div", { class: "err" }, `${when(e.at)} ` + t("{where}：{error}", { where: t(e.where), error: e.error })));
  box.append(h("div", { class: "stop-row" },
    h("button", { type: "button", onclick: stopService }, t("停止服务")),
    h("span", { class: "muted" }, t("停止后不再采集额度数据；再打开一次 How much my Claude 即可恢复。"))));
}

async function stopService() {
  if (!confirm(t("停止后台服务？停止期间 Claude 的额度百分比无法补录。"))) return;
  clearInterval(refreshTimer);
  await api("/api/shutdown", { method: "POST" }).catch(() => {});
  document.body.replaceChildren(h("main", {}, h("p", { class: "empty" }, t("服务已停止。再打开一次 How much my Claude 即可恢复。"))));
}

// ── 加载 ─────────────────────────────────────────────────

async function loadUsage() {
  renderUsage(await api(`/api/usage?range=${state.range}&tool=${state.tool}`));
}

async function loadHistory() {
  renderWindowTable(await api(`/api/windows?tool=${state.history}`));
}

async function loadAll() {
  const [overview, monitor, prices, conns] = await Promise.all(
    [api("/api/overview"), api("/api/monitor"), api("/api/prices"), api("/api/connections")]);
  renderConnections(conns);
  renderBanner(monitor);
  renderNow(overview);
  renderMonitor(monitor);
  renderRates(monitor);
  renderEvents(monitor.events);
  renderStatus(overview, prices, conns);
  lastPrices = prices;
  renderPrices(prices);
  $("#sync-info").textContent = overview.status.last_scan ? t("同步于 {when}", { when: ago(overview.status.last_scan) }) : t("正在首次同步…");
  if (!overview.status.last_scan || !overview.status.claude_quota) setTimeout(() => loadAll().catch(() => {}), 4000);
  await Promise.all([loadUsage(), $("#history-details").open ? loadHistory() : null,
    $("#data-details").open ? loadData() : null]);
  disposeDetached();
}

// 先拿设置（决定界面语言），再翻译固定文字、加载数据
async function init() {
  const s = await api("/api/settings");
  setLang(s.values.language);
  translateStatic();
  renderSettings(s);
  await loadAll();
}

function bindSeg(id, key, onChange) {
  const seg = document.getElementById(id);
  seg.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    seg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === btn));
    state[key] = btn.dataset.v;
    onChange();
  });
}

bindSeg("f-range", "range", loadUsage);
bindSeg("f-tool", "tool", loadUsage);
bindSeg("f-history", "history", loadHistory);
bindSeg("f-price", "priceView", () => lastPrices && renderPrices(lastPrices));
$("#price-search").addEventListener("input", (e) => {
  state.priceQuery = e.target.value;
  if (lastPrices) renderPrices(lastPrices);
});
$("#history-details").addEventListener("toggle", (e) => { if (e.target.open) loadHistory(); });
$("#data-details").addEventListener("toggle", (e) => { if (e.target.open) loadData().catch(() => {}); });

$("#btn-sync").addEventListener("click", async () => {
  const btn = $("#btn-sync");
  btn.disabled = true;
  btn.textContent = t("同步中…");
  try {
    await api("/api/sync", { method: "POST" });
    await loadAll();
  } finally {
    btn.disabled = false;
    btn.textContent = t("立即同步");
  }
});

// 左上角标志：点击在 Claude / OpenAI 之间切换，记住上次的选择（浏览器存储不可用时就用默认的 Claude）
const corner = $(".corner-logos");
try {
  const saved = localStorage.getItem("ql-logo");
  if (saved === "claude" || saved === "openai") corner.dataset.logo = saved;
} catch { /* 隐私模式等情况下读不到，用默认值 */ }
$("#logo-toggle").addEventListener("click", () => {
  corner.dataset.logo = corner.dataset.logo === "claude" ? "openai" : "claude";
  try { localStorage.setItem("ql-logo", corner.dataset.logo); } catch { /* 忽略 */ }
});

window.addEventListener("resize", () => charts.forEach((c) => c.resize()));
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => loadAll());

init().catch((e) => $("#now").replaceChildren(h("p", { class: "empty" }, t("加载失败：") + e.message)));
const refreshTimer = setInterval(() => loadAll().catch(() => {}), 60_000);
