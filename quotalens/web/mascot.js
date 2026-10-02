"use strict";

// 像素吉祥物：宣传动画（story.js）里的那只小章鱼——在进度条上敲代码、躺沙滩椅喝咖啡、写小说、拿放大镜查额度。
// 和卡片标题里的小图标是同一只（app.js 的 SPRITES.claude）。身体每格拆成 2×2 的细格，道具按细格画，细节才画得出来。
// 坐标以细格为单位：身体占 x 0–31（两侧各 4 格是鳍）、y 0–19，脚底在 y=20；道具可以画到身体外面，
// 整张图的范围是 MASCOT_BOX。

const MASCOT_CELL = 2;  // 一个细格画成 2px
const MASCOT_BOX = { x: -24, y: -26, w: 92, h: 47 };

const MASCOT_COLORS = {
  X: "var(--mascot)", Y: "#b9603f", o: "var(--mascot-eye)", W: "#ffffff",
  // 笔记本电脑：屏幕边框、屏幕、转轴、机身、键、触控板、前沿
  B: "#2d2d2b", P: "#17212c", h: "#55534f", A: "#cfccc4", k: "#8f8c84", T: "#b9b6ae", E: "#9d9a92",
  // 屏幕上的代码高亮
  b: "#5aa9e6", e: "#7ccf7c", y: "#f0b75a", p: "#b48ef0", i: "#d9d6cc",
  // 小说本：纸、纸边、书脊阴影、封皮、字；铅笔：笔芯、木头、黄漆、铁箍、橡皮；AI 小星星
  N: "#f6efdc", O: "#c9bd9f", J: "#ddd0b0", C: "#8c3b2e", R: "#5e584e",
  G: "#333333", n: "#e3c08d", U: "#f2c230", F: "#b5b5b5", Z: "#e88a9a", a: "#a98bf0",
  // 沙滩椅：木头、木头暗面、木头亮面、两色条纹布
  w: "#b07a43", x: "#7e532a", z: "#d39a5e", S: "#2f9b8f", s: "#f1ebdd",
  // 墨镜；马克杯：杯身、杯壁、咖啡、奶泡；热气（近处浓、远处淡）
  g: "#121212", m: "#4f9fe0", M: "#2a6fb8", c: "#6b3f1f", f: "#ead6b3",
  v: "color-mix(in srgb, var(--text-muted) 75%, transparent)", u: "color-mix(in srgb, var(--text-muted) 35%, transparent)",
  // 放大镜：镜框、木柄、镜片；头上的 ？ 和 ！
  K: "var(--text-secondary)", H: "#7a5230", l: "rgba(120, 180, 240, 0.35)", q: "var(--text-primary)", r: "var(--critical)",
};

// 几种站姿：头顶在哪一行、身体几行（剩下的是腿）、压扁时往两边胖几格、腾空时鳍抬高几格
const MASCOT_SHAPES = {
  stand: { top: 0, height: 16 },
  crouch: { top: 4, height: 14 },
  squash: { top: 6, height: 13, wide: 2 },
  air: { top: -3, height: 16, lift: 3 },
};

const MASCOT_GLYPHS = {
  "?": [".qqqq.", "qq..qq", "....qq", "...qq.", "..qq..", "..qq..", "......", "..qq..", "..qq.."],
  "!": ["rrr", "rrr", "rrr", "rrr", "rrr", ".r.", "...", "rrr", "rrr"],
};

function mascotCanvas() {
  const rows = new Map();
  const set = (x, y, c) => {
    if (!rows.has(y)) rows.set(y, new Map());
    rows.get(y).set(x, c);
  };
  const rect = (x0, y0, x1, y1, c) => { for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) set(x, y, c); };
  const grid = (lines, x0, y0) => lines.forEach((line, dy) =>
    [...line].forEach((c, dx) => { if (c !== ".") set(x0 + dx, y0 + dy, c); }));
  const line = (x0, y0, x1, y1, c) => {
    const n = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0), 1);
    for (let i = 0; i <= n; i++) set(Math.round(x0 + (x1 - x0) * i / n), Math.round(y0 + (y1 - y0) * i / n), c);
  };
  const get = (x, y) => rows.get(y)?.get(x);
  return { rows, set, get, rect, grid, line };
}

// 把另一张画布 src 绕它的 (px, py) 转 angle（弧度，负数是往后仰、逆时针）画到 cv 上，转轴落在 (tx, ty)
function mascotBlitRotated(cv, src, angle, px, py, tx, ty) {
  const cos = Math.cos(angle), sin = Math.sin(angle);
  let r = 0;
  for (const [y, row] of src.rows) for (const x of row.keys()) r = Math.max(r, Math.hypot(x - px, y - py));
  r = Math.ceil(r) + 1;
  for (let y = ty - r; y <= ty + r; y++) {
    for (let x = tx - r; x <= tx + r; x++) {
      const dx = x - tx, dy = y - ty;
      const c = src.get(Math.round(px + cos * dx + sin * dy), Math.round(py - sin * dx + cos * dy));
      if (c) cv.set(x, y, c);
    }
  }
}

// 身体：turned 时身子朝右侧过来一点（左边一溜暗面，眼睛和腿往右挪），腿一直画到脚底
function mascotBody(cv, { top = 0, height = 16, wide = 0 }, turned, legs = true) {
  cv.rect(4 - wide, top, 27 + wide, top + height - 1, "X");
  if (turned) cv.rect(4, top, 5, top + height - 1, "Y");
  if (legs) for (const x of turned ? [7, 11, 21, 25] : [6, 10, 20, 24]) cv.rect(x, top + height, x + 1, 19, "X");
}

function mascotEyes(cv, kind, top, turned) {
  const y = top + 4;
  for (const x of turned ? [11, 23] : [8, 22]) {
    if (kind === "happy") {           // 弯弯的 ∩ ∩
      cv.rect(x, y + 1, x + 1, y + 1, "o");
      cv.rect(x - 1, y + 2, x - 1, y + 3, "o");
      cv.rect(x + 2, y + 2, x + 2, y + 3, "o");
    } else if (kind === "wide") {     // 瞪大，带一点高光
      cv.rect(x - 1, y - 1, x + 1, y + 3, "o");
      cv.set(x - 1, y - 1, "W");
    } else if (kind === "blink") {
      cv.rect(x, y + 3, x + 1, y + 3, "o");
    } else {
      cv.rect(x, y, x + 1, y + 3, "o");
    }
  }
}

function mascotFins(cv, top, { left = true, right = true, lift = 0, spread = 0 } = {}) {
  const y = top + 8 - lift;
  if (left) cv.rect(-spread, y, 3, y + 3, "X");
  if (right) cv.rect(28, y, 31 + spread, y + 3, "X");
}

// 墨镜：圆角镜片、鼻梁、镜腿，镜片左上两点反光。amount 0→1 从头顶上方滑下来；glint 时右镜片角上闪一颗星
function mascotGlasses(cv, amount, top, glint) {
  if (amount <= 0) return;
  const y = top + 3 - Math.round((1 - amount) * 14);
  for (const [x0, x1] of [[5, 12], [19, 26]]) {
    cv.rect(x0 + 1, y, x1 - 1, y, "g");
    cv.rect(x0, y + 1, x1, y + 3, "g");
    cv.rect(x0 + 1, y + 4, x1 - 1, y + 4, "g");
    cv.set(x0 + 2, y + 1, "W");
    cv.set(x0 + 1, y + 2, "W");
  }
  cv.rect(13, y + 1, 18, y + 1, "g");
  cv.set(4, y + 1, "g");
  cv.set(27, y + 1, "g");
  if (glint) {
    for (const [dx, dy] of [[0, 0], [1, 0], [-1, 0], [0, 1], [0, -1], [2, 0], [-2, 0], [0, 2], [0, -2]]) {
      cv.set(28 + dx, y - 1 + dy, Math.abs(dx) + Math.abs(dy) > 1 ? "y" : "W");
    }
  }
}

// ── 笔记本电脑 ──
// 正面看：上面是亮着的屏幕，下面是往前变宽的键盘机身。宽 28、高 18
const MASCOT_LAPTOP = (() => {
  const rows = [];
  const row = (cells) => rows.push(Array.from({ length: 28 }, (_, x) => cells(x) || ".").join(""));
  row((x) => (x >= 4 && x <= 23 ? "B" : null));
  for (let i = 0; i < 8; i++) row((x) => (x === 4 || x === 23 ? "B" : x > 4 && x < 23 ? "P" : null));
  row((x) => (x >= 4 && x <= 23 ? "B" : null));
  row((x) => (x >= 5 && x <= 22 ? "h" : null));
  row((x) => (x >= 3 && x <= 24 ? "A" : null));
  for (const shift of [0, 1]) row((x) => (x >= 2 && x <= 25 ? (x > 2 && x < 25 && (x + shift) % 3 ? "k" : "A") : null));
  row((x) => (x >= 1 && x <= 26 ? "A" : null));
  row((x) => (x >= 1 && x <= 26 ? (x >= 10 && x <= 17 ? "T" : "A") : null));
  row((x) => (x >= 10 && x <= 17 ? "T" : "A"));
  row(() => "E");
  return rows;
})();

// 屏幕上的代码：每行 [缩进, [[长度, 颜色], ...]]，词之间空一格。敲一下出一个字符，满四行往上滚
const MASCOT_CODE = [
  [0, [[3, "p"], [6, "b"], [2, "i"]]],
  [2, [[5, "b"], [1, "i"], [5, "e"]]],
  [2, [[2, "p"], [4, "i"], [3, "y"]]],
  [4, [[6, "b"], [1, "i"], [4, "e"]]],
  [4, [[3, "p"], [7, "i"]]],
  [2, [[4, "y"], [2, "i"], [5, "b"]]],
  [0, [[2, "i"]]],
  [0, [[3, "p"], [5, "b"], [3, "i"]]],
  [2, [[6, "e"], [1, "i"], [3, "y"]]],
  [2, [[2, "p"], [5, "b"], [2, "i"]]],
  [4, [[4, "i"], [1, "i"], [6, "e"]]],
  [2, [[5, "p"], [4, "b"]]],
];
const MASCOT_CODE_CELLS = MASCOT_CODE.map(([indent, tokens]) => {
  const cells = [];
  let x = indent;
  tokens.forEach(([len, c], k) => {
    if (k) cells.push([x++, null]);
    for (let j = 0; j < len; j++) cells.push([x++, c]);
  });
  return cells;
});

// open 0→1：先放下机身，再从下往上翻开屏幕。typed 是一共敲了几个字符，cursor 是光标这一刻亮不亮
function mascotLaptop(cv, x0, y0, open, typed, cursor) {
  if (open <= 0) return;
  const shown = open < 0.5 ? 8 : 8 + Math.round((open - 0.5) / 0.5 * 10);
  MASCOT_LAPTOP.slice(-shown).forEach((line, i) => [...line].forEach((c, dx) => {
    if (c !== ".") cv.set(x0 + dx, y0 + 18 - shown + i, c);
  }));
  if (open < 1) return;
  // 把敲过的字符一行行排出来，只显示最后四行
  const lines = [];
  let left = typed;
  for (let i = 0; ; i++) {
    const cells = MASCOT_CODE_CELLS[i % MASCOT_CODE_CELLS.length];
    lines.push(cells.slice(0, Math.min(left, cells.length)));
    if (left < cells.length) break;
    left -= cells.length;
  }
  const visible = lines.slice(-4);
  visible.forEach((cells, k) => cells.forEach(([dx, c]) => { if (c) cv.set(x0 + 6 + dx, y0 + 2 + 2 * k, c); }));
  if (cursor) {
    const last = visible[visible.length - 1];
    const cx = last.length ? last[last.length - 1][0] + 1 : MASCOT_CODE[(lines.length - 1) % MASCOT_CODE.length][0];
    cv.set(x0 + 6 + Math.min(cx, 15), y0 + 2 + 2 * (visible.length - 1), "W");
  }
}

// 敲键盘的两只手：从身体右侧伸出两条短手臂，按下去的那只落在键上，另一只抬在键盘上方
const MASCOT_LAPTOP_X = 26;  // 电脑紧挨着身体右侧
const MASCOT_KEYS = [[30, 32, 35, 37, 33], [42, 45, 47, 43, 49]];
function mascotTypingArms(cv, top, press) {
  [0, 1].forEach((hand) => {
    const down = press && press.hand === hand;
    const hx = MASCOT_KEYS[hand][press ? press.key % 5 : 2];
    const hy = down ? 13 : 9 + hand;
    cv.line(29, top + 9 + hand * 2, hx, hy, "X");
    cv.rect(hx, hy, hx + 1, hy + 1, "X");
  });
}

// ── 小说本 ──
// 摊开的本子捧在身前（挡住下半身，眼睛从本子上面露出来）。左页是上一页写满的，右页三行、每行 11 格，
// 写满一页就翻页
const MASCOT_BOOK = { x: 1, y: 8 };
const MASCOT_PAGE_LINES = ["RRR.RRRRR.R", "RR.RRRR.RRR", "RRRRR.RR.RR"];
const MASCOT_PAGE = 33;
function mascotNotebook(cv, x0, y0, open, written, flip) {
  if (open <= 0) return;
  if (open < 0.5) {  // 合着的本子
    cv.rect(x0 + 8, y0 + 3, x0 + 21, y0 + 10, "C");
    cv.rect(x0 + 9, y0 + 9, x0 + 21, y0 + 9, "O");
    return;
  }
  cv.rect(x0, y0 + 1, x0 + 29, y0 + 10, "C");
  cv.rect(x0 + 1, y0, x0 + 13, y0 + 8, "N");
  cv.rect(x0 + 16, y0, x0 + 28, y0 + 8, "N");
  cv.rect(x0 + 1, y0 + 9, x0 + 13, y0 + 9, "O");
  cv.rect(x0 + 16, y0 + 9, x0 + 28, y0 + 9, "O");
  cv.rect(x0 + 14, y0, x0 + 15, y0 + 9, "J");
  const page = written % MASCOT_PAGE;
  for (let cell = 0; cell < MASCOT_PAGE; cell++) {
    const line = Math.floor(cell / 11);
    if (MASCOT_PAGE_LINES[line][cell % 11] !== "R") continue;
    if (written >= MASCOT_PAGE) cv.set(x0 + 2 + (cell % 11), y0 + 2 + 2 * line, "R");  // 左页：上一页
    if (cell < page) cv.set(x0 + 17 + (cell % 11), y0 + 2 + 2 * line, "R");
  }
  if (flip > 0 && flip < 1) {  // 翻页：右页翘起来、立在书脊上、落到左边
    const f = Math.floor(flip * 3);
    if (f === 0) cv.rect(x0 + 17, y0 - 2, x0 + 27, y0 + 5, "N");
    else if (f === 1) cv.rect(x0 + 13, y0 - 7, x0 + 16, y0 + 7, "N");
    else cv.rect(x0 + 2, y0 - 2, x0 + 12, y0 + 5, "N");
  }
}

// 正在写的那一格（笔尖落在这里）
function mascotPenPoint(written) {
  const cell = written % MASCOT_PAGE;
  return [MASCOT_BOOK.x + 17 + (cell % 11), MASCOT_BOOK.y + 2 + 2 * Math.floor(cell / 11)];
}

// 铅笔：笔尖朝下，往右上斜着，握在右鳍里
function mascotPencil(cv, tx, ty) {
  ["G", "n", "U", "U", "U", "U", "U", "U", "F", "Z", "Z"].forEach((c, i) => {
    cv.set(tx + i, ty - i, c);
    if (i) cv.set(tx + i - 1, ty - i, c);
  });
}

function mascotSparkle(cv, x, y, big) {
  cv.set(x, y, "W");
  for (let d = 1; d <= (big ? 2 : 1); d++) for (const [dx, dy] of [[d, 0], [-d, 0], [0, d], [0, -d]]) cv.set(x + dx, y + dy, "a");
}

// ── 沙滩椅、咖啡 ──
// 侧面看的沙滩椅（朝右）：斜着的条纹布靠背、往前略微翘起的椅面、木头扶手和交叉的椅腿。
// open 0→1：先出椅腿和椅面，再从下往上出靠背，最后是扶手
function mascotChair(cv, open) {
  if (open <= 0) return;
  for (const d of [0, 1]) {
    cv.line(-3 + d, 15, -8 + d, 19, "x");
    cv.line(27 + d, 12, 32 + d, 19, "x");
    cv.line(-5 + d, 19, 24 + d, 13, "w");
  }
  for (let x = -2; x <= 28; x++) {
    const y = Math.round(14 - 2 * (x + 2) / 30 + 1.5 * Math.sin(Math.PI * (x + 2) / 30));
    const c = Math.floor((x + 2) / 3) % 2 ? "S" : "s";
    cv.set(x, y, c);
    cv.set(x, y + 1, c);
  }
  if (open < 0.4) return;
  const rows = Math.round((open - 0.4) / 0.6 * 25);
  for (let k = 24; k >= 25 - rows && k >= 0; k--) {
    const x0 = Math.round(-12 + 10 * k / 24);
    for (let x = x0; x <= x0 + 4; x++) cv.set(x, -10 + k, Math.floor(k / 3) % 2 ? "S" : "s");
    cv.set(x0 - 1, -10 + k, "w");
    cv.set(x0 - 2, -10 + k, "x");
  }
  if (rows >= 25) cv.rect(-15, -12, -9, -11, "z");
  if (open < 0.85) return;
  cv.line(-8, 5, 25, 5, "w");
  cv.line(-8, 4, 25, 4, "z");
  cv.line(25, 5, 28, 12, "w");
}

// 蓝色马克杯（把手在左），奶泡上一点咖啡色
const MASCOT_MUG = ["..MMMMMM", "..MffcfM", "MMMmmmmM", "M.MmmmmM", "M.MmmmmM", "MMMmmmmM", "..MmmmmM", "...MMMM."];
function mascotMug(cv, mx, my, phase) {
  cv.grid(MASCOT_MUG, mx, my);
  // 两缕热气：细细的虚线，弯弯曲曲往上飘，越高越淡
  [0, 1].forEach((k) => {
    const drift = Math.floor(phase * 8);
    for (let j = 0; j < 8; j++) {
      if ((j + drift + k * 2) % 4 === 3) continue;
      const x = mx + 4 + k * 2 + Math.round(Math.sin(j * 0.8 - phase * Math.PI * 2 + k * Math.PI));
      cv.set(x, my - 2 - j, j < 4 ? "v" : "u");
    }
  });
}

// 惬意时飘起来的小音符，phase 0→1 从扶手边往上飘
const MASCOT_NOTE = ["..pp", "..pp.p", "..p..p", "..p", "ppp", "ppp"];
function mascotNote(cv, phase) {
  if (phase == null || phase < 0 || phase >= 1) return;
  cv.grid(MASCOT_NOTE, -18 + Math.round(Math.sin(phase * 6) * 2), -4 - Math.round(phase * 16));
}

// 躺在沙滩椅上：椅子撑开前先站在旁边；撑开后整只往后仰（转 17°）躺进去，墨镜和眼睛跟着转，
// 杯子一直是正的：平时托在右鳍上，抿一口时举到嘴边
const MASCOT_RECLINE = -0.3;
function mascotLounge(cv, spec) {
  const chair = spec.chair ?? 1;
  mascotChair(cv, chair);
  const glasses = spec.glasses ?? 0;
  if (chair < 1) {
    mascotBody(cv, MASCOT_SHAPES.stand);
    mascotFins(cv, 0);
    mascotEyes(cv, spec.eyes || "happy", 0);
    return;
  }
  const body = mascotCanvas();
  mascotBody(body, MASCOT_SHAPES.stand, false, false);  // 腿收在椅子的布兜里
  mascotFins(body, 0, { right: false });
  if (glasses < 1) mascotEyes(body, spec.eyes || "happy", 0);
  mascotGlasses(body, glasses, 0, spec.glint);
  mascotBlitRotated(cv, body, MASCOT_RECLINE, 16, 16, 12, 13);
  mascotNote(cv, spec.note);
  if (spec.sip) {  // 举到嘴边
    mascotMug(cv, 6, 2, spec.steam || 0);
    cv.line(23, 4, 13, 7, "X");
    cv.rect(12, 6, 14, 8, "X");
  } else {          // 托在右鳍上，鳍包着杯底
    mascotMug(cv, 22, -6, spec.steam || 0);
    cv.rect(21, 2, 30, 4, "X");
  }
}

// 放大镜：左鳍握着木柄，镜片挨着身体左下方、贴着进度条
const MASCOT_LENS = ["..KKKKK..", ".KlllllK.", "KllWllllK", "KlWlllllK", "KlllllllK", "KlllllllK", "KlllllllK", ".KlllllK.", "..KKKKK.."];
function mascotMagnifier(cv, top, wobble) {
  const dx = wobble ? -1 : 0;
  cv.grid(MASCOT_LENS, -13 + dx, 9);
  cv.line(-4 + dx, 11, -1, 10, "H");
  cv.line(-4 + dx, 12, -1, 11, "H");
  cv.rect(-2, top + 8, 3, top + 12, "X");  // 左鳍握着木柄
}

// spec：pose（stand / crouch / squash / air / type / write / lounge / inspect）、eyes（normal / happy / wide / blink）
//   type：laptop（0–1 打开程度）、typed（敲了几个字符）、press（{ hand: 0 | 1, key }）、cursor
//   write：book（0–1）、written（写了几格）、flip（0–1 翻页进度）、zig（笔尖上下抖）、sparkle（0–3）
//   lounge：chair、glasses（0–1）、glint、sip、steam（热气飘的相位）、note（音符飘到哪了）
//   inspect：wobble、notebook（本子留在旁边）；任何姿势都可以带 bubble（"?" / "!"）
function drawMascot(svg, spec) {
  const cv = mascotCanvas();
  const pose = spec.pose;
  if (pose === "lounge") {
    mascotLounge(cv, spec);
  } else {
    const shape = MASCOT_SHAPES[pose] || MASCOT_SHAPES.stand;
    const turned = pose === "type";
    mascotBody(cv, shape, turned);
    // 电脑挡在身体右下角前面，像是抱在跟前
    if (pose === "type") mascotLaptop(cv, MASCOT_LAPTOP_X, 2, spec.laptop ?? 1, spec.typed || 0, spec.cursor);
    if (pose !== "write" && spec.notebook) mascotNotebook(cv, MASCOT_BOOK.x, MASCOT_BOOK.y, 1, spec.written || 0, 0);
    if (pose === "type") {
      mascotFins(cv, shape.top, { right: false });
      if ((spec.laptop ?? 1) >= 1) mascotTypingArms(cv, shape.top, spec.press);
      else mascotFins(cv, shape.top, { left: false });
    } else if (pose === "write") {
      mascotNotebook(cv, MASCOT_BOOK.x, MASCOT_BOOK.y, spec.book ?? 1, spec.written || 0, spec.flip || 0);
      cv.rect(-1, 10, 2, 13, "X");  // 左鳍扶着本子边
      if ((spec.book ?? 1) >= 1 && !(spec.flip > 0 && spec.flip < 1)) {
        const [px, py] = mascotPenPoint(spec.written || 0);
        const ty = py - (spec.zig ? 1 : 0);
        mascotPencil(cv, px, ty);
        cv.line(29, shape.top + 10, px + 6, ty - 6, "X");
        cv.rect(px + 5, ty - 7, px + 7, ty - 5, "X");
        // AI 帮忙：笔尖旁边时不时闪两颗小星星
        const sp = spec.sparkle || 0;
        if (sp === 1 || sp === 2) mascotSparkle(cv, px + 11, ty - 4, sp === 2);
        if (sp === 2 || sp === 3) mascotSparkle(cv, px - 4, ty - 7, sp === 3);
      } else {
        mascotFins(cv, shape.top, { left: false });
      }
    } else if (pose === "inspect") {
      mascotFins(cv, shape.top, { left: false });
      mascotMagnifier(cv, shape.top, spec.wobble);
    } else {
      mascotFins(cv, shape.top, { lift: shape.lift || 0, spread: shape.wide || 0 });
    }
    mascotEyes(cv, spec.eyes || "normal", shape.top, turned);
  }
  if (spec.bubble) cv.grid(MASCOT_GLYPHS[spec.bubble], spec.bubble === "!" ? 27 : 26, -14);

  // 同一行里连续的同色格合成一个矩形
  const NS = "http://www.w3.org/2000/svg";
  const frag = document.createDocumentFragment();
  for (const [y, row] of cv.rows) {
    const xs = [...row.keys()].sort((a, b) => a - b);
    for (let i = 0; i < xs.length;) {
      let j = i;
      while (j + 1 < xs.length && xs[j + 1] === xs[j] + 1 && row.get(xs[j + 1]) === row.get(xs[i])) j++;
      const r = document.createElementNS(NS, "rect");
      r.setAttribute("x", xs[i]);
      r.setAttribute("y", y);
      r.setAttribute("width", j - i + 1);
      r.setAttribute("height", 1);
      r.style.fill = MASCOT_COLORS[row.get(xs[i])];
      frag.append(r);
      i = j + 1;
    }
  }
  svg.replaceChildren(frag);
}

function createMascot() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const b = MASCOT_BOX;
  for (const [k, v] of Object.entries({ viewBox: `${b.x} ${b.y} ${b.w} ${b.h}`, width: b.w * MASCOT_CELL,
    height: b.h * MASCOT_CELL, class: "mascot", "aria-hidden": "true", "shape-rendering": "crispEdges" })) {
    svg.setAttribute(k, v);
  }
  return svg;
}
