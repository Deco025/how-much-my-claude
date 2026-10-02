"use strict";

// 宣传动画：吉祥物在 Claude 卡片的进度条上敲代码，演一遍「额度被悄悄收紧，被监控发现」。
//   /?story            自动循环播放
//   /?story&paused     不自动播放，录制脚本（tools/record_story.py）逐帧调用 __story.seek(t) 再截图；&t=秒 停在某一刻
//   &lang=zh|en        界面语言，不写就跟浏览器
// 数据都是虚构的，不请求任何接口。卡片每一行用的是真实界面的 renderWindowRow，
// 所以进度条、斜纹预测、「预计 HH:MM 用完」、已花 / 约值的算法和样子都和真实界面一致。
(() => {
  const params = new URLSearchParams(location.search);
  setLang(params.get("lang") || "auto");
  document.body.classList.add("story");

  const DURATION = 20;
  const H5 = 5 * 3600;
  const WEEK = 7 * 86400;
  const CAP = 40.9;        // 平常一个 5 小时窗口约值多少（等价 API 美元）
  const TIGHT = 0.7;       // 第二轮被悄悄收紧到七成
  const WEEK_CAP = 340;
  const WEEK_BASE = 102;   // 开场时这周已经花掉的
  // 两轮敲代码：同样敲一下、花同样的钱，第一轮涨 2%（一格），第二轮涨 2% ÷ 0.7
  const ROUNDS = [
    { t0: 2.0, t1: 6.5, n: 47, from: 6, step: 2, cap: CAP },
    { t0: 12.0, t1: 15.2, n: 35, from: 0, step: 2 / TIGHT, cap: CAP * TIGHT },
  ];
  const DETECT = 16.8;     // 盖章
  const FADE = 19.4;       // 章和横幅淡出
  const RESET = 19.7;      // 数据回到开头

  const day = new Date();
  day.setHours(13, 0, 0, 0);
  const S1 = day.getTime() / 1000;          // 第一个 5 小时窗口 13:00 开始
  const S2 = S1 + H5 + 120;                 // 第二个在第一个重置两分钟后开始
  const W0 = S1 + 0.1 * H5 - 0.5 * WEEK;    // 开场时这周已过一半

  const seg = (t, a, b) => Math.min(1, Math.max(0, (t - a) / (b - a)));
  const lerp = (a, b, k) => a + (b - a) * k;

  const strokes = (r, t) => t < r.t0 ? 0 : Math.min(r.n, Math.floor((t - r.t0) / ((r.t1 - r.t0) / r.n)) + 1);
  const roundPct = (r, t) => Math.min(100, r.from + r.step * strokes(r, t));
  const strokeLen = (r) => (r.t1 - r.t0) / r.n;

  // 正在按下的那一下：左右手轮流，落点在几个键之间换；没在按的那一刻返回 null
  function pressAt(r, t) {
    if (t < r.t0 || t >= r.t1) return null;
    const i = Math.floor((t - r.t0) / strokeLen(r));
    if ((t - r.t0 - i * strokeLen(r)) / strokeLen(r) > 0.6) return null;
    return { hand: i % 2, key: Math.floor(i / 2) };
  }
  // 第二轮写小说：每一下写两格字，右页写满（33 格）就翻页，翻页用 0.2 秒
  const CELLS = 2;
  const PAGE = 33;
  function writingAt(r, t) {
    const written = strokes(r, t) * CELLS;
    for (const n of [PAGE, 2 * PAGE]) {
      const flipAt = r.t0 + (Math.ceil(n / CELLS) - 1) * strokeLen(r);
      const flip = seg(t, flipAt, flipAt + 0.2);
      if (flip > 0 && flip < 1) return { written: n - 1, flip };
    }
    return { written, flip: 0 };
  }

  // 画面里的时钟：干活时每敲一下往前跳一截，休息时快进到重置。
  // 时钟和用量在同一时刻跳，「预计」才会只翻一次；时钟要是连续走，用量一格格跳，
  // 两者比值在 100% 上下来回，「预计用完 / 可撑到重置」会跟着闪
  function clockAt(t) {
    const [r1, r2] = ROUNDS;
    if (t < r1.t1) return S1 + (0.1 + 0.89 * strokes(r1, t) / r1.n) * H5;
    if (t < 8.2) return S1 + 0.99 * H5;
    if (t < 10.4) return S1 + (0.99 + 0.01 * seg(t, 8.2, 10.4)) * H5;
    if (t < r2.t0) return S1 + H5 + 120 * seg(t, 10.4, r2.t0);
    // 第二轮时钟走得和第一轮一样快，用量却涨得快，「预计」一开始就是红的
    return S2 + 0.89 * H5 * (r2.t1 - r2.t0) / (r1.t1 - r1.t0) * strokes(r2, t) / r2.n;
  }

  const WINDOW = { tool: "claude", scope: "all", external_pct: 0 };
  const active = (start, pct, cost) => ({ ...WINDOW, window: "five_hour", active: true, start, end: start + H5,
    used_percent: pct, cost_so_far: cost, cap: CAP });

  // 某一刻两条进度条的数据，字段和 /api/overview 里的窗口一样
  function windowsAt(t) {
    if (t >= RESET) t = 0;
    const [r1, r2] = ROUNDS;
    let five;
    let spent;  // 开场以后又花掉的
    if (t < 10.4) {
      const pct = roundPct(r1, t);
      five = active(S1, pct, pct / 100 * CAP);
      spent = (pct - r1.from) / 100 * CAP;
    } else {
      spent = (100 - r1.from) / 100 * CAP;
      if (t < 10.8) {             // 到点重置：进度条倒着清空
        const pct = 100 * (1 - seg(t, 10.4, 10.8));
        five = active(S1, pct, pct / 100 * CAP);
      } else if (t < r2.t0) {
        five = { ...WINDOW, window: "five_hour", active: false };
      } else {
        const pct = roundPct(r2, t);
        five = active(S2, pct, pct / 100 * r2.cap);
        spent += pct / 100 * r2.cap;
      }
    }
    five.status = t >= DETECT ? "tighter" : "stable";
    const cost = WEEK_BASE + spent;
    const week = { ...WINDOW, window: "seven_day", active: true, start: W0, end: W0 + WEEK,
      used_percent: cost / WEEK_CAP * 100, cost_so_far: cost, cap: WEEK_CAP, status: "stable" };
    return { five, week, tNow: clockAt(t) };
  }

  // 吉祥物在哪、什么姿势。g 里是三个落脚点（脚底中点，相对卡片）：标题小图标的位置（家）、
  // 5 小时进度条上干活的位置、每周进度条上休息的位置。干活时站在固定的地方，不跟着进度条走
  function mascotAt(t, g) {
    const [r1, r2] = ROUNDS;
    // 抛物线：往下跳时也先往上蹿，弧顶要越过两条进度条之间的文字
    const jump = (a, b, k) => ({ x: lerp(a.x, b.x, k),
      y: lerp(a.y, b.y, k) - (24 + Math.abs(b.y - a.y) * 0.6 + Math.abs(b.x - a.x) * 0.08) * 4 * k * (1 - k) });
    const { home, work, rest } = g;
    const cursor = Math.floor(t * 4) % 2 === 0;
    const steam = t * 1.2;
    const wobble = Math.floor(t * 5) % 2;
    if (t < 0.5) return { ...home, spec: { pose: "stand", eyes: t > 0.25 && t < 0.33 ? "blink" : "normal" } };
    if (t < 0.7) return { ...home, spec: { pose: "crouch" } };
    if (t < 1.3) return { ...jump(home, work, seg(t, 0.7, 1.3)), spec: { pose: "air" } };
    if (t < 1.45) return { ...work, spec: { pose: "squash" } };
    // 第一轮：掏出笔记本电脑写代码，屏幕上一行行冒出代码
    if (t < r1.t0) return { ...work, spec: { pose: "type", eyes: "happy", laptop: seg(t, 1.45, 1.9), cursor } };
    if (t < r1.t1) {
      return { ...work, spec: { pose: "type", eyes: "happy", typed: strokes(r1, t) * 2, press: pressAt(r1, t), cursor } };
    }
    // 用满了：合上电脑，跳到每周那条上躺沙滩椅、戴墨镜喝咖啡，等 5 小时重置
    const typed = r1.n * 2;
    if (t < 6.85) return { ...work, spec: { pose: "type", typed, laptop: 1 - seg(t, 6.5, 6.85) } };
    if (t < 7.0) return { ...work, spec: { pose: "crouch" } };
    if (t < 7.6) return { ...jump(work, rest, seg(t, 7.0, 7.6)), spec: { pose: "air" } };
    if (t < 7.7) return { ...rest, spec: { pose: "squash" } };
    if (t < 10.8) {
      const sip = (t > 8.8 && t < 9.3) || (t > 9.9 && t < 10.4);
      return { ...rest, spec: { pose: "lounge", chair: seg(t, 7.7, 8.1), glasses: seg(t, 8.05, 8.3),
        glint: t > 8.3 && t < 8.55, sip, steam, note: t > 8.6 ? ((t - 8.6) / 1.1) % 1 : null } };
    }
    if (t < 11.1) return { ...rest, spec: { pose: "lounge", chair: 1 - seg(t, 10.9, 11.1), glasses: 1 - seg(t, 10.8, 10.95), steam } };
    // 重置了，回去干活。第二轮换成写小说：摊开本子拿铅笔写，AI 小星星在旁边闪
    if (t < 11.25) return { ...rest, spec: { pose: "crouch" } };
    if (t < 11.75) return { ...jump(rest, work, seg(t, 11.25, 11.75)), spec: { pose: "air" } };
    if (t < 11.85) return { ...work, spec: { pose: "squash" } };
    if (t < r2.t0) return { ...work, spec: { pose: "write", eyes: "happy", book: seg(t, 11.85, 12.0) } };
    if (t < r2.t1) {
      return { ...work, spec: { pose: "write", eyes: "happy", ...writingAt(r2, t), zig: Math.floor(t * 12) % 2,
        sparkle: Math.floor(t * 3) % 4 } };
    }
    // 同样的工作量却提前用满：停笔、冒问号，掏放大镜看进度条，发现被收紧
    const notebook = { notebook: true, written: writingAt(r2, r2.t1).written };
    if (t < 15.45) return { ...work, spec: { pose: "stand", eyes: "wide", ...notebook, bubble: t > 15.3 ? "?" : null } };
    if (t < DETECT) return { ...work, spec: { pose: "inspect", eyes: "wide", ...notebook, bubble: "?", wobble } };
    if (t < 19.0) {
      const blink = Math.floor((t - DETECT) * 3) % 3 === 2;
      return { ...work, spec: { pose: "inspect", eyes: "wide", ...notebook, bubble: blink ? null : "!", wobble } };
    }
    if (t < 19.1) return { ...work, spec: { pose: "crouch" } };
    if (t < 19.6) return { ...jump(work, home, seg(t, 19.1, 19.6)), spec: { pose: "air" } };
    if (t < RESET) return { ...home, spec: { pose: "squash" } };
    return { ...home, spec: { pose: "stand" } };
  }

  // ── 舞台：横幅 + 一张 Claude 卡片 ──
  const headSprite = sprite("claude");
  const rows = h("div", { class: "story-rows" });
  const svg = createMascot();
  const stamp = h("span", { class: "stamp tighter story-stamp" }, t(STAMPS.tighter));
  const card = h("div", { class: "card tool-claude story-card" },
    h("div", { class: "card-head" }, h("h3", {}, headSprite, TOOL_NAMES.claude), h("span", { class: "tag" }, "pro")),
    rows, stamp, svg);
  $("#now").replaceChildren(card);
  renderBanner({ groups: [{ tool: "claude", scope: "all", window: "five_hour", status: "tighter", ratio: TIGHT,
    ref_model: "claude-opus-5-5", recent_n: 3, recent_median: CAP * TIGHT, baseline_n: 12, baseline_median: CAP,
    last_seen: now() }] });
  const banner = $("#alert-banner");

  // 标题小图标和两条进度条的位置（相对卡片），每帧重新量：行是整个重画的
  function geometry() {
    const c = card.getBoundingClientRect();
    const rel = (el) => {
      const r = el.getBoundingClientRect();
      return { left: r.left - c.left, right: r.right - c.left, top: r.top - c.top, bottom: r.bottom - c.top, width: r.width };
    };
    const [m5, mw] = [...rows.querySelectorAll(".meter")].map(rel);
    const sp = rel(headSprite);
    // 干活的位置离进度条左端留出放大镜的地方；休息的位置在每周那条中间
    return { home: { x: sp.left + 16 * MASCOT_CELL, y: sp.top + 20 * MASCOT_CELL },
      work: { x: m5.left + 84, y: m5.top }, rest: { x: mw.left + mw.width * 0.5, y: mw.top } };
  }

  function seek(time) {
    const tt = ((time % DURATION) + DURATION) % DURATION;
    const { five, week, tNow } = windowsAt(tt);
    rows.replaceChildren(renderWindowRow(five, tNow), renderWindowRow(week, tNow));
    // 吉祥物拿放大镜看的时候，「已花」那一项闪一闪
    if (tt >= 15.6 && tt < DETECT && Math.floor(tt * 4) % 2 === 0) rows.querySelector(".fact")?.classList.add("story-flag");
    // 结尾淡一下再回到开头的数据，循环播放时接得上
    rows.style.opacity = tt < FADE ? 1 : tt < RESET ? 1 - 0.85 * seg(tt, FADE, RESET) : 0.15 + 0.85 * seg(tt, RESET, DURATION);

    const m = mascotAt(tt, geometry());
    drawMascot(svg, m.spec);
    svg.style.left = `${Math.round(m.x - (16 - MASCOT_BOX.x) * MASCOT_CELL)}px`;
    svg.style.top = `${Math.round(m.y - (20 - MASCOT_BOX.y) * MASCOT_CELL)}px`;

    // 盖章：从大到小砸下来，卡片抖一下；随后横幅滑进来
    const out = seg(tt, FADE, RESET);
    const k = seg(tt, DETECT, DETECT + 0.18);
    stamp.style.opacity = tt < DETECT ? 0 : k * (1 - out);
    stamp.style.transform = `rotate(-8deg) scale(${(1 + 1.6 * (1 - k) ** 2).toFixed(3)})`;
    const shake = seg(tt, DETECT + 0.18, DETECT + 0.5);
    card.style.transform = shake > 0 && shake < 1 ? `translateX(${(3 * Math.sin(shake * 18) * (1 - shake)).toFixed(2)}px)` : "";
    const b = seg(tt, DETECT + 0.2, DETECT + 0.5);
    banner.style.opacity = b * (1 - out);
    banner.style.transform = `translateY(${((1 - b) * -8).toFixed(1)}px)`;
  }

  window.__story = { duration: DURATION, seek };
  if (params.has("paused")) {
    seek(Number(params.get("t")) || 0);
  } else {
    const t0 = performance.now();
    const tick = (ms) => {
      seek((ms - t0) / 1000);
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
})();
