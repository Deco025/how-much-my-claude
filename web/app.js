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
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`);
  return res.json();
}

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
  if (d) return `${d} 天 ${hh} 小时`;
  if (hh) return `${hh} 小时 ${mm} 分`;
  return `${mm} 分钟`;
}
function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "刚刚";
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.round(s / 3600)} 小时前`;
  return `${Math.round(s / 86400)} 天前`;
}
const pctText = (v) => `${v > 0 ? "+" : ""}${(v * 100).toFixed(0)}%`;
const now = () => Date.now() / 1000;

const WINDOW_SHORT = { five_hour: "5 小时", seven_day: "每周", thirty_day: "30 天" };
const TOOL_NAMES = { codex: "Codex", claude: "Claude" };
const STATE_NAMES = { stable: "稳定", tighter: "可能被收紧", looser: "可能被放宽", insufficient: "数据不足" };

function windowLabel(w) {
  const base = WINDOW_SHORT[w.window] || w.window;
  const shared = (w.tool === "codex" && w.scope === "codex") || (w.tool === "claude" && w.scope === "all");
  return shared ? base : `${base} · ${w.scope}`;
}

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const toolColor = (tool) => cssVar(tool === "claude" ? "--series-2" : "--series-1");
// 超过两周没有新数据的分组（例如以前用过的别的账号、旧方案）不再显示
const isLive = (g) => now() - g.last_seen < 14 * 86400;

// ── 图表公共 ─────────────────────────────────────────────

const state = { range: "7d", tool: "all", history: "codex" };
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
      axisLabel: { color: muted, hideOverlap: true },
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

// ── 告警横幅 ─────────────────────────────────────────────

function renderBanner(monitor) {
  const box = $("#alert-banner");
  box.replaceChildren();
  for (const g of monitor.groups.filter((x) => isLive(x) && (x.status === "tighter" || x.status === "looser"))) {
    box.append(h("div", { class: `banner ${g.status}`, role: "alert" },
      h("span", { class: "icon" }, g.status === "tighter" ? "▼" : "▲"),
      h("div", {},
        h("strong", {}, `${TOOL_NAMES[g.tool]} ${windowLabel(g)}窗口${STATE_NAMES[g.status]} ${Math.abs((g.ratio - 1) * 100).toFixed(0)}%`),
        h("div", { class: "sub" },
          `折合 ${g.ref_model}：最近 ${g.recent_n} 个窗口 ${usd(g.recent_median)}，之前 ${g.baseline_n} 个窗口 ${usd(g.baseline_median)}`))));
  }
}

// ── 现在：各窗口进度 ─────────────────────────────────────

function renderWindowRow(w) {
  const t = now();
  if (!w.active) {
    return h("div", { class: "win" },
      h("div", { class: "win-row" },
        h("span", { class: "win-label" }, windowLabel(w)),
        h("div", { class: "meter" }),
        h("span", { class: "win-pct muted" }, "0%")),
      h("div", { class: "win-sub" }, "已重置，下次使用时开始新窗口"));
  }
  const pct = w.used_percent || 0;
  const elapsed = Math.min(1, Math.max(0, (t - w.start) / (w.end - w.start)));
  const meter = pct >= 95 ? "meter crit" : pct >= 80 ? "meter warn" : "meter";
  // 按目前速度推算什么时候用完
  let pace = null;
  if (pct >= 2 && elapsed >= 0.02) {
    const eta = w.start + (t - w.start) * (100 / pct);
    pace = eta < w.end ? h("span", { class: "alarm" }, `按目前速度 ${clock(eta)} 用完`) : h("span", {}, "按目前速度用不完");
  }
  const sub = [
    h("span", {}, `${span(w.end - t)}后重置`),
    pace,
    h("span", {}, "已花 ", h("strong", {}, usd(w.cost_so_far)), w.cap ? ` / 约值 ${usd(w.cap)}` : ""),
    w.external_pct >= 1 ? h("span", { title: "本地日志以外的消耗：别的设备、云任务或网页聊天" }, `外部消耗 ${w.external_pct.toFixed(0)}%`) : null,
    w.status ? h("span", { class: `state ${w.status}` }, STATE_NAMES[w.status]) : null,
  ];
  return h("div", { class: "win" },
    h("div", { class: "win-row" },
      h("span", { class: "win-label" }, windowLabel(w)),
      h("div", { class: meter, role: "meter", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100,
        title: `已用 ${pct.toFixed(0)}%，窗口时间已过 ${(elapsed * 100).toFixed(0)}%（细线）` },
        h("div", { class: "fill", style: `width:${Math.min(100, pct)}%` }),
        h("div", { class: "pace", style: `left:calc(${(elapsed * 100).toFixed(1)}% - 1px)` })),
      h("span", { class: "win-pct" }, `${pct.toFixed(0)}%`)),
    h("div", { class: "win-sub" }, sub));
}

function renderNow(overview) {
  const box = $("#now");
  box.replaceChildren();
  for (const t of overview.tools) {
    const card = h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h3", {}, TOOL_NAMES[t.tool]), t.plan ? h("span", { class: "pill" }, t.plan) : null));
    if (t.windows.length) {
      t.windows.forEach((w) => card.append(renderWindowRow(w)));
    } else {
      const msg = t.tool === "claude" ? ({
        missing: "没找到 Claude Code 的登录凭据",
        expired: "Claude Code 登录已过期，运行一次 claude 即可",
        error: "读取 Claude Code 凭据失败",
      }[t.credential_status] || "等待第一次额度查询…") : "用一次 Codex 后就会出现";
      card.append(h("p", { class: "empty" }, msg));
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
    const card = h("div", { class: "card" },
      h("div", { class: "card-head" },
        h("h3", {}, `${TOOL_NAMES[g.tool]} · ${windowLabel(g)}`),
        h("span", { class: `state ${status}` }, STATE_NAMES[status])));
    if (g.ref_model) {
      const value = g.recent_median ?? g.ref_cap;
      // 收紧是坏消息（红），放宽是好消息（绿），稳定不着色
      const deltaClass = { tighter: "delta bad", looser: "delta good" }[status] || "delta";
      card.append(
        h("div", { class: "figure" },
          h("span", { class: "big" }, usd(value)),
          g.ratio != null
            ? h("span", { class: deltaClass }, `较之前 ${pctText(g.ratio - 1)}`)
            : null),
        h("div", { class: "caption" }, `全用 ${g.ref_model} 时一个窗口约值` +
          (g.recent_median != null ? `（最近 ${g.recent_n} 个窗口中位）` : "")));
    }
    if (g.trend?.length) {
      const node = h("div", { class: "mini-chart" });
      card.append(node);
      requestAnimationFrame(() => drawTrend(node, g, monitor.events));
    } else {
      card.append(h("p", { class: "empty" }, `已记录 ${g.windows_n} 个窗口，攒到 2 个用量 5% 以上的窗口后开始判断`));
    }
    box.append(card);
  }
  if (!groups.length) box.append(h("p", { class: "empty" }, "还没有足够的窗口数据。"));
}

function drawTrend(node, g, events) {
  const b = base();
  const color = toolColor(g.tool);
  const toPoint = (p) => [p.last_seen * 1000, +p.value.toFixed(2), p];
  const used = g.trend.filter((p) => p.in_trend).map(toPoint);
  // 纵轴按参与判断的点定范围，离群的未参与点不拉伸坐标
  const ref = (used.length ? used : g.trend.map(toPoint)).map((d) => d[1]);
  const lo = Math.max(0, Math.min(...ref) * 0.75), hi = Math.max(...ref) * 1.25;
  const skipped = g.trend.filter((p) => !p.in_trend).map(toPoint).filter((d) => d[1] >= lo && d[1] <= hi);
  const xMin = Math.min(...g.trend.map((p) => p.last_seen)) * 1000;
  const marks = events
    .filter((e) => e.tool === g.tool && (!e.window || e.window === g.window) && e.ts * 1000 >= xMin)
    .map((e) => ({ xAxis: e.ts * 1000, name: `${e.title}：${e.detail}` }));
  const bm = g.baseline_median, th = g.threshold || 0.2;
  const tip = (p) => {
    const why = p.contaminated ? `<br>外部消耗约 ${p.external_pct.toFixed(0)}%，未参与判断`
      : !p.in_trend ? "<br>用量太少，未参与判断" : "";
    return `<strong>${usd(p.value)}</strong><br>${when(p.start)} 起 · 已用 ${p.pct.toFixed(0)}%${why}`;
  };
  const chart = render(node, {
    textStyle: b.textStyle,
    grid: { left: 44, right: 22, top: 26, bottom: 22 },
    legend: { ...b.legend, data: ["参与判断", "未参与"] },
    tooltip: { ...b.tooltip, trigger: "item",
      formatter: (it) => it.componentType === "markLine" ? esc(it.name) : tip(it.data[2]) },
    xAxis: { type: "time", ...b.axis, splitLine: { show: false },
      axisLabel: { ...b.axis.axisLabel, formatter: "{MM}-{dd}" } },
    yAxis: { type: "value", min: +lo.toFixed(2), max: +hi.toFixed(2), splitNumber: 3, ...b.axis, axisLine: { show: false },
      axisLabel: { ...b.axis.axisLabel, formatter: (v) => "$" + Math.round(v) } },
    series: [
      { name: "参与判断", type: "scatter", symbolSize: 8, data: used, silent: true,
        itemStyle: { color, borderColor: cssVar("--surface-1"), borderWidth: 2 },
        markArea: bm ? { silent: true, itemStyle: { color: cssVar("--band") },
          data: [[{ yAxis: bm * (1 - th) }, { yAxis: bm * (1 + th) }]] } : undefined,
        markLine: { symbol: "none", animation: false,
          data: [
            ...(bm ? [{ yAxis: bm, name: `之前中位 ${usd(bm)}`, lineStyle: { color: b.muted, width: 1, type: "solid" }, label: { show: false } }] : []),
            ...marks.map((m) => ({ ...m, lineStyle: { color: cssVar("--axis"), width: 1, type: "solid" }, label: { show: false } })),
          ] } },
      { name: "未参与", type: "scatter", symbolSize: 8, data: skipped, silent: true,
        itemStyle: { color: "transparent", borderColor: b.muted, borderWidth: 1.5 } },
      // 透明的大点只负责悬停和点击：8px 的点太难点中，命中区域放大到 24px
      { name: "hit", type: "scatter", symbolSize: 24, data: [...used, ...skipped], cursor: "pointer",
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
  $("#detail-title").textContent = `${TOOL_NAMES[w.tool]} ${windowLabel(w)}窗口 · ${when(d.start)} 起`;
  const jumps = d.steps.filter((s) => s.external > 0).map((s) => {
    const p = d.points.find((x) => x.ts === s.ts);
    return [p.pct, +p.cost.toFixed(4), s.ts, s.external];
  });
  const ext = jumps.reduce((a, j) => a + j[3], 0);
  $("#detail-note").textContent = `已用 ${d.used_percent.toFixed(0)}% · 花费 ${usd(d.cost_at_snapshot)}` +
    (d.cap ? ` · 约值 ${usd(d.cap)}` : "") + (jumps.length ? ` · 外部消耗约 ${ext.toFixed(0)}%` : "");
  render($("#detail-chart"), {
    textStyle: b.textStyle,
    grid: { left: 52, right: 56, top: 30, bottom: 28 },
    legend: jumps.length ? { ...b.legend, data: ["累计花费", "外部消耗跳涨"] } : undefined,
    tooltip: { ...b.tooltip, trigger: "item",
      formatter: (it) => {
        const [pct, cost, ts, extPct] = it.data;
        return `<strong>${usd(cost)}</strong><br>已用 ${pct}% · ${when(ts)}` +
          (extPct ? `<br>约 ${extPct.toFixed(0)}% 来自本地日志以外` : "");
      } },
    xAxis: { type: "value", min: 0, max: Math.max(10, Math.ceil((d.used_percent || 0) / 10) * 10), ...b.axis,
      name: "已用", nameTextStyle: { color: b.muted }, axisLabel: { ...b.axis.axisLabel, formatter: "{value}%" } },
    yAxis: { type: "value", ...b.axis, axisLine: { show: false },
      axisLabel: { ...b.axis.axisLabel, formatter: (v) => "$" + v } },
    series: [
      { name: "累计花费", type: "line", step: "end", showSymbol: false, symbolSize: 8,
        data: d.points.map((p) => [p.pct, +p.cost.toFixed(4), p.ts]),
        lineStyle: { width: 2, color }, itemStyle: { color }, areaStyle: { color, opacity: 0.1 },
        endLabel: { show: true, color: cssVar("--text-primary"), formatter: () => usd(d.cost_at_snapshot) } },
      { name: "外部消耗跳涨", type: "scatter", data: jumps, symbolSize: 10,
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
        ? `当前窗口 ${clock(g.latest_end)} 结束，下一个窗口用到 5% 以后就能算出来。`
        : "下一个窗口用到 5% 以后就能算出来。";
      box.append(h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h3", {}, `${TOOL_NAMES[g.tool]} · ${windowLabel(g)}`),
          h("span", { class: "pill" }, `${g.windows_n} 个窗口`)),
        h("p", { class: "empty" }, `至少要 2 个窗口才能分出各模型的汇率，现在只有 ${g.windows_n} 个。`, h("br"), next)));
      continue;
    }
    const node = h("div", { class: "chart", style: `height:${Math.max(120, rows.length * 34 + 40)}px` });
    box.append(h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h3", {}, `${TOOL_NAMES[g.tool]} · ${windowLabel(g)}`),
        h("span", { class: "pill" }, `${g.fit_windows} 个窗口`)),
      node));
    requestAnimationFrame(() => drawRates(node, g, rows));
  }
  if (!box.children.length) box.append(h("p", { class: "empty" }, "至少需要 2 个用量 5% 以上的干净窗口才能回归。"));
}

function drawRates(node, g, rows) {
  const b = base();
  const color = toolColor(g.tool);
  render(node, {
    textStyle: b.textStyle,
    grid: { left: 8, right: 64, top: 4, bottom: 4, containLabel: true },
    tooltip: { ...b.tooltip, trigger: "item",
      formatter: (it) => `<strong>${usd(it.value)}</strong><br>${esc(it.name)} · 出现在 ${rows[it.dataIndex].windows} 个窗口` +
        (rows[it.dataIndex].windows < 4 ? "（数据少）" : "") },
    xAxis: { type: "value", show: false },
    yAxis: { type: "category", data: rows.map((r) => r.model), ...b.axis, axisLine: { show: false },
      axisLabel: { color: cssVar("--text-secondary"), fontSize: 12 } },
    series: [{
      type: "bar", barMaxWidth: 16,
      data: rows.map((r) => ({ value: +r.cap_if_only.toFixed(2),
        itemStyle: { color, opacity: r.windows < 4 ? 0.35 : 1, borderRadius: [0, 4, 4, 0] } })),
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
    tile("等价 API 花费", usd(s.cost_usd), null, sparkData.length > 1 ? sparkData : null),
    tile("请求数", s.requests.toLocaleString(), s.unpriced_requests ? `${s.unpriced_requests} 次无价格` : null),
    tile("缓存命中率", s.cache_hit_rate == null ? "—" : `${(s.cache_hit_rate * 100).toFixed(1)}%`,
      `缓存读 ${compact(s.cache_read_tokens)} token`),
    tile("输出", compact(s.output_tokens) + " token", s.reasoning_tokens ? `其中推理 ${compact(s.reasoning_tokens)}` : null),
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
  if (series.length) series[series.length - 1].itemStyle.borderRadius = [4, 4, 0, 0];
  $("#cost-chart-title").textContent = state.range === "today" ? "等价 API 花费（按小时）" : "等价 API 花费（按天）";
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
    legend: shownTools.length > 1 ? { ...b.legend, icon: "rect", data: shownTools.map((t) => TOOL_NAMES[t]) } : undefined,
    tooltip: { ...b.tooltip, trigger: "item", formatter: (it) => `<strong>${usd(it.value)}</strong><br>${esc(it.name)}` },
    xAxis: { type: "value", show: false },
    yAxis: { type: "category", data: priced.map((m) => m.model), ...b.axis, axisLine: { show: false },
      axisLabel: { color: cssVar("--text-secondary"), fontSize: 12, width: 120, overflow: "truncate" } },
    series: shownTools.map((tool) => ({
      name: TOOL_NAMES[tool], type: "bar", stack: "m", barMaxWidth: 16,
      itemStyle: { color: toolColor(tool), borderRadius: [0, 4, 4, 0] },
      data: priced.map((m) => (m.tool === tool ? +m.cost_usd.toFixed(2) : null)),
      label: { show: true, position: "right", color: cssVar("--text-primary"),
        formatter: (it) => (it.value ? usd(it.value) : "") },
    })),
  });
  $("#model-note").textContent = unpricedReq ? `另有 ${unpricedReq.toLocaleString()} 次请求转发到第三方或没有价格，未计入。` : "";

  renderModelTable(report.models);
}

function renderModelTable(models) {
  const table = $("#model-table");
  table.replaceChildren(h("tr", {}, h("th", {}, "工具"), h("th", { class: "text" }, "模型"), h("th", { class: "text" }, "计费"),
    h("th", {}, "请求"), h("th", {}, "输入"), h("th", {}, "缓存读"), h("th", {}, "缓存写"), h("th", {}, "输出"),
    h("th", {}, "命中率"), h("th", {}, "等价花费")));
  for (const m of models) {
    table.append(h("tr", {},
      h("td", {}, TOOL_NAMES[m.tool]), h("td", { class: "text" }, m.model),
      h("td", { class: "text" }, m.on_plan ? "订阅" : "第三方"),
      h("td", {}, m.requests.toLocaleString()), h("td", {}, compact(m.input_tokens)),
      h("td", {}, compact(m.cache_read_tokens)), h("td", {}, compact(m.cache_write_tokens)),
      h("td", {}, compact(m.output_tokens)),
      h("td", {}, m.cache_hit_rate == null ? "—" : (m.cache_hit_rate * 100).toFixed(0) + "%"),
      h("td", {}, m.unpriced_requests === m.requests ? "无价格" : usd(m.cost_usd))));
  }
}

// ── 折叠区：事件、窗口历史、状态 ─────────────────────────

function renderEvents(events) {
  $("#events-count").textContent = events.length ? `（近 90 天 ${events.length} 条）` : "";
  const box = $("#events");
  box.replaceChildren(...events.slice(0, 50).map((e) => h("div", { class: "event" },
    h("div", { class: "when" }, when(e.ts)),
    h("div", {}, h("strong", {}, `${TOOL_NAMES[e.tool]} · ${e.title}`), e.detail ? h("div", { class: "detail" }, e.detail) : null))));
  if (!events.length) box.append(h("p", { class: "empty" }, "近 90 天没有发现规则变化。"));
}

function renderWindowTable(rows) {
  const table = $("#window-table");
  table.replaceChildren(h("tr", {},
    h("th", {}, "窗口"), h("th", {}, "开始"), h("th", {}, "已用"), h("th", {}, "外部消耗"), h("th", {}, "等价花费"),
    h("th", {}, "推算上限"), h("th", {}, "折合主力模型"), h("th", { class: "text" }, "参与判断")));
  const shown = rows.filter((r) => r.supported && r.used_percent > 0);
  for (const r of shown) {
    table.append(h("tr", { class: "clickable", onclick: () => showDetail(r).then(() => $("#detail").scrollIntoView({ behavior: "smooth" })) },
      h("td", {}, `${windowLabel(r)}${r.plan_type ? " · " + r.plan_type : ""}`), h("td", {}, when(r.start)),
      h("td", {}, `${r.used_percent.toFixed(0)}%`),
      h("td", {}, r.external_pct >= 1 ? `${r.external_pct.toFixed(0)}%` : "—"),
      h("td", {}, usd(r.cost_at_snapshot)), h("td", {}, r.cap ? usd(r.cap) : "—"),
      h("td", {}, r.value ? usd(r.value) : "—"),
      h("td", { class: "text" }, r.in_trend ? "是" : r.contaminated ? "否（外部消耗多）" : "否")));
  }
  if (!shown.length) table.append(h("tr", {}, h("td", { colspan: 8, class: "text" }, "还没有窗口数据。")));
}

function renderStatus(overview, prices) {
  const st = overview.status;
  const poll = (p) => (p ? `${ago(p.at)}，${p.status}${p.error ? "（" + p.error + "）" : ""}` : "还没有");
  const lines = [
    `日志扫描：${st.last_scan ? ago(st.last_scan) : "还没有"}` +
      Object.entries(st.scan || {}).map(([k, v]) => ` · ${TOOL_NAMES[k]} ${v.files} 个文件`).join(""),
    `Claude 额度：${poll(st.claude_quota)}（使用中每 3 分钟查一次）`,
    `Codex 额度：${poll(st.codex_quota)}（只在本地空闲时查）`,
    `价格表：models.dev，更新于 ${prices.fetched_at || "—"}`,
    `备份：${st.backup ? `${ago(st.backup.at)} · ${st.backup.path}` : "还没有"}`,
  ];
  const box = $("#status");
  box.replaceChildren(...lines.map((l) => h("div", {}, l)));
  const unpriced = prices.models.filter((m) => m.on_plan && !m.price_key);
  if (unpriced.length) {
    box.append(h("div", {}, `没有价格的订阅模型：${unpriced.map((m) => m.model).join("、")}（可在 prices_override.json 补价）`));
  }
  for (const e of (st.errors || []).slice(0, 5)) box.append(h("div", { class: "err" }, `${when(e.at)} ${e.where}：${e.error}`));
}

// ── 加载 ─────────────────────────────────────────────────

async function loadUsage() {
  renderUsage(await api(`/api/usage?range=${state.range}&tool=${state.tool}`));
}

async function loadHistory() {
  renderWindowTable(await api(`/api/windows?tool=${state.history}`));
}

async function loadAll() {
  const [overview, monitor, prices] = await Promise.all([api("/api/overview"), api("/api/monitor"), api("/api/prices")]);
  renderBanner(monitor);
  renderNow(overview);
  renderMonitor(monitor);
  renderRates(monitor);
  renderEvents(monitor.events);
  renderStatus(overview, prices);
  $("#sync-info").textContent = overview.status.last_scan ? `同步于 ${ago(overview.status.last_scan)}` : "正在首次同步…";
  if (!overview.status.last_scan || !overview.status.claude_quota) setTimeout(() => loadAll().catch(() => {}), 4000);
  await Promise.all([loadUsage(), $("#history-details").open ? loadHistory() : null]);
  disposeDetached();
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
$("#history-details").addEventListener("toggle", (e) => { if (e.target.open) loadHistory(); });

$("#btn-sync").addEventListener("click", async () => {
  const btn = $("#btn-sync");
  btn.disabled = true;
  btn.textContent = "同步中…";
  try {
    await api("/api/sync", { method: "POST" });
    await loadAll();
  } finally {
    btn.disabled = false;
    btn.textContent = "立即同步";
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

loadAll().catch((e) => $("#now").replaceChildren(h("p", { class: "empty" }, "加载失败：" + e.message)));
setInterval(() => loadAll().catch(() => {}), 60_000);
