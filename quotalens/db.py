"""SQLite 存储。

usage          每次模型调用一行（已去重），token 统一成「新鲜输入 + 缓存读 + 缓存写 + 输出」口径
quota_snapshot 订阅额度百分比快照（Codex 来自日志，Claude 来自轮询接口）
usage_window   请求与额度窗口的对应关系（仅 Codex）
file_cursor    日志文件增量读取游标
meta           杂项（当前入库费用所用的价格表版本）
raw_response   额度接口的原始响应归档（相同内容只延长时间范围），留作证据
schema_seen    额度数据的字段结构指纹及首末出现时间，用来发现接口 / 日志格式变化
alert          额度变化告警
data_rule      用户在页面上对数据做的选择：某个订阅方案 / 某个窗口不参与分析，或某个方案的数据已删除
account_seen   查额度时看到的登录账号（不可逆摘要）
source_breakdown Claude 周额度的来源分项（Claude Code / Chats / Cowork / 其他）
collect_gap    采集器发现的缺口（读取或解析失败、文件截断、超出导入期限）
coverage_claim 用户的覆盖声明（绑定账号和生效时间）

数据默认永久保存，不会自动清理；Claude Code 默认 30 天后删掉自己的日志，这里的副本不受影响。
"""
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from .paths import DATA_DIR, DB_PATH

# 归档前从原始响应里去掉的身份信息
PII_KEYS = {"email", "user_id", "account_id"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id                    TEXT PRIMARY KEY,
    tool                  TEXT NOT NULL,              -- claude | codex
    ts                    REAL NOT NULL,              -- epoch 秒
    model                 TEXT NOT NULL,              -- 日志里的原始模型名
    on_plan               INTEGER NOT NULL,           -- 1 = 官方模型、计入订阅额度；0 = 经转发的第三方模型
    session_id            TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0, -- 不含缓存的新鲜输入
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0, -- 含推理 token
    reasoning_tokens      INTEGER NOT NULL DEFAULT 0,
    speed                 TEXT,
    cost_usd              REAL                        -- NULL = 没有价格
);
CREATE INDEX IF NOT EXISTS usage_tool_ts ON usage(tool, ts);

CREATE TABLE IF NOT EXISTS quota_snapshot (
    tool           TEXT NOT NULL,
    scope          TEXT NOT NULL,   -- codex: limit_id；claude: all / opus / sonnet / fable / 其他接口键名
    window         TEXT NOT NULL,   -- five_hour / seven_day / ...
    ts             REAL NOT NULL,
    used_percent   REAL NOT NULL,
    resets_at      REAL,
    window_seconds INTEGER,
    plan_type      TEXT,
    source         TEXT NOT NULL,   -- log | api
    PRIMARY KEY (tool, scope, window, ts)
);

-- 请求 → 它所消耗的额度窗口。Codex 每个 token_count 事件同时带用量和该账号的窗口
-- resets_at，据此精确归属；切换账号、窗口提前重置都不会串。Claude 没有这层信息，按时间归属。
CREATE TABLE IF NOT EXISTS usage_window (
    usage_id  TEXT NOT NULL,
    tool      TEXT NOT NULL,
    scope     TEXT NOT NULL,
    window    TEXT NOT NULL,
    resets_at REAL NOT NULL,
    PRIMARY KEY (usage_id, scope, window)
);
CREATE INDEX IF NOT EXISTS usage_window_lookup ON usage_window(tool, scope, window, resets_at);

CREATE TABLE IF NOT EXISTS file_cursor (
    path     TEXT PRIMARY KEY,
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    offset   INTEGER NOT NULL,
    state    TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS raw_response (
    id        INTEGER PRIMARY KEY,
    tool      TEXT NOT NULL,
    first_ts  REAL NOT NULL,
    last_ts   REAL NOT NULL,
    body_hash TEXT NOT NULL,
    body      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_seen (
    tool        TEXT NOT NULL,
    source      TEXT NOT NULL,   -- log / api
    fingerprint TEXT NOT NULL,
    first_ts    REAL NOT NULL,
    last_ts     REAL NOT NULL,
    shape       TEXT NOT NULL,   -- 字段路径列表（JSON）
    PRIMARY KEY (tool, source, fingerprint)
);

CREATE TABLE IF NOT EXISTS alert (
    id        INTEGER PRIMARY KEY,
    ts        REAL NOT NULL,
    tool      TEXT NOT NULL,
    window    TEXT NOT NULL,
    direction TEXT NOT NULL,     -- tighter / looser
    ratio     REAL NOT NULL,
    detail    TEXT,
    analysis_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS data_rule (
    tool   TEXT NOT NULL,
    kind   TEXT NOT NULL,        -- plan：整个订阅方案；window：单个窗口
    target TEXT NOT NULL,        -- plan_type，或窗口键 scope:window:首次重置时间
    mode   TEXT NOT NULL,        -- ignore：保留但不参与分析；deleted：已删除
    until  REAL,                 -- deleted：删除时刻。重读日志时跳过这之前属于该方案的数据，删掉的不会回来
    ts     REAL NOT NULL,
    PRIMARY KEY (tool, kind, target)
);

-- 每次查额度时看到的登录账号（只存不可逆摘要）。窗口的账号从这里按时间取，覆盖声明据此绑定账号
CREATE TABLE IF NOT EXISTS account_seen (
    tool    TEXT NOT NULL,
    ts      REAL NOT NULL,
    account TEXT NOT NULL,
    PRIMARY KEY (tool, ts)
);

-- Claude 周额度的来源分项（seven_day_breakdown）：已用部分里各产品的占比
CREATE TABLE IF NOT EXISTS source_breakdown (
    tool          TEXT NOT NULL,
    window        TEXT NOT NULL,   -- seven_day
    as_of         REAL NOT NULL,   -- 分项的统计时刻
    window_start  REAL,            -- 分项所属窗口的开始时刻
    fetched_at    REAL NOT NULL,
    key           TEXT NOT NULL,   -- claude_code / chats / cowork / other / 未知新产品原样保存
    display_name  TEXT,
    percent       REAL,            -- 占已用部分的百分比；NULL = 字段无法解析
    last_as_of    REAL,            -- 相同分项最后一次被确认的时刻
    PRIMARY KEY (tool, window, as_of, key)
);

-- 采集器发现的缺口：读取失败、解析失败、文件被截断或轮换、超出导入期限。缺口期间的窗口不能证明记录完整
CREATE TABLE IF NOT EXISTS collect_gap (
    id      INTEGER PRIMARY KEY,
    tool    TEXT NOT NULL,
    start   REAL NOT NULL,
    end     REAL NOT NULL,
    kind    TEXT NOT NULL,       -- read_error / parse_error / truncated / horizon
    detail  TEXT,
    ts      REAL NOT NULL,
    UNIQUE (tool, start, end, kind, detail)
);

-- 覆盖声明：用户声明「这个账号在此期间所有消耗该额度的使用都会被本项目采集到」。
-- 绑定账号和生效时间；关闭时写入 end，不删除记录，历史估值的条件可追溯
CREATE TABLE IF NOT EXISTS coverage_claim (
    id      INTEGER PRIMARY KEY,
    tool    TEXT NOT NULL,
    account TEXT NOT NULL,
    start   REAL NOT NULL,
    end     REAL
);
"""

# 后台同步线程和 API 请求线程共用；写操作串行化，读走 WAL 不受影响
write_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


# 给已有表补列：(表, 列, 定义)。只加列，不改不删
COLUMNS = [
    ("alert", "analysis_version", "INTEGER NOT NULL DEFAULT 1"),   # 产生告警的分析算法版本；旧告警是 1
    ("source_breakdown", "last_as_of", "REAL"),                    # 相同分项最后一次被确认的时刻
    ("source_breakdown", "issues", "TEXT"),                        # 解析时发现的问题（JSON 列表），同组每行相同
]

SAME_WINDOW = 900   # 同一窗口的开始时间在相邻两次查询间的最大漂移（秒），与 calibrate.RESET_JITTER 一致


def round_reset(ts):
    """重置时间取整到分钟（接口返回的值每次有亚秒级漂移），用于判断轮询结果有没有变化。"""
    return None if ts is None else round(ts / 60) * 60


def _migrate(conn) -> None:
    for table, column, decl in COLUMNS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()


@contextmanager
def writer():
    with write_lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


@contextmanager
def reader():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _scrub(obj):
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items() if k not in PII_KEYS}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def archive_response(conn, tool: str, ts: float, body) -> None:
    """归档一次接口响应；和该工具上一条内容相同就只延长它的时间范围。"""
    text = json.dumps(_scrub(body), sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(text.encode()).hexdigest()
    last = conn.execute("SELECT id, body_hash FROM raw_response WHERE tool = ? ORDER BY last_ts DESC LIMIT 1",
                        (tool,)).fetchone()
    if last and last["body_hash"] == digest:
        conn.execute("UPDATE raw_response SET last_ts = ? WHERE id = ?", (ts, last["id"]))
    else:
        conn.execute("INSERT INTO raw_response (tool, first_ts, last_ts, body_hash, body) VALUES (?, ?, ?, ?, ?)",
                     (tool, ts, ts, digest, text))
    note_schema(conn, tool, "api", body, ts, ts)


def shape_of(obj, prefix=""):
    """字段路径列表，例如 rate_limit.primary_window.used_percent:int。值本身不参与。"""
    if isinstance(obj, dict):
        paths = []
        for k, v in obj.items():
            paths.extend(shape_of(v, f"{prefix}.{k}" if prefix else k))
        return paths or [f"{prefix}:{{}}"]
    if isinstance(obj, list):
        return shape_of(obj[0], prefix + "[]") if obj else [f"{prefix}:[]"]
    kind = "null" if obj is None else type(obj).__name__
    return [f"{prefix}:{kind}"]


def schema_fingerprint(obj):
    """(指纹, 字段路径列表)。

    只看路径不看类型，并且每条路径的上级路径也算在内：字段在 null 和对象之间切换
    （credits: null ↔ credits.balance …）时，上级字段始终存在，不会误报「字段消失」。
    """
    paths = set()
    for p in shape_of(obj):
        parts = p.rsplit(":", 1)[0].split(".")
        paths.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
    paths = sorted(paths)
    return hashlib.sha1("|".join(paths).encode()).hexdigest()[:16], paths


def note_schema(conn, tool: str, source: str, obj, first_ts: float, last_ts: float, fingerprint=None) -> None:
    """记录字段结构指纹的首末出现时间（与处理顺序无关，适合乱序导入的日志）。"""
    fp, paths = fingerprint or schema_fingerprint(obj)
    conn.execute(
        "INSERT INTO schema_seen (tool, source, fingerprint, first_ts, last_ts, shape) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(tool, source, fingerprint) DO UPDATE SET "
        "first_ts = MIN(first_ts, excluded.first_ts), last_ts = MAX(last_ts, excluded.last_ts)",
        (tool, source, fp, first_ts, last_ts, json.dumps(paths)))


def data_rules(conn, tool: str) -> dict:
    """{"plans": {plan_type: (mode, until)}, "windows": {窗口键, ...}}"""
    out = {"plans": {}, "windows": set()}
    for r in conn.execute("SELECT kind, target, mode, until FROM data_rule WHERE tool = ?", (tool,)):
        if r["kind"] == "plan":
            out["plans"][r["target"]] = (r["mode"], r["until"])
        elif r["kind"] == "window" and r["mode"] == "ignore":
            out["windows"].add(r["target"])
    return out


def note_account(conn, tool: str, ts: float, account) -> None:
    """只在账号和上一次记录的不同时写一行（账号变化的时间点就足够判断窗口归属）。"""
    if not account:
        return
    last = conn.execute("SELECT account FROM account_seen WHERE tool = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                        (tool, ts)).fetchone()
    if not last or last["account"] != account:
        conn.execute("INSERT OR IGNORE INTO account_seen (tool, ts, account) VALUES (?, ?, ?)", (tool, ts, account))


def accounts_seen(conn, tool: str):
    """[(ts, 账号摘要)]，按时间排序。"""
    return [(r["ts"], r["account"]) for r in
            conn.execute("SELECT ts, account FROM account_seen WHERE tool = ? ORDER BY ts", (tool,))]


def latest_account(conn, tool: str):
    r = conn.execute("SELECT account FROM account_seen WHERE tool = ? ORDER BY ts DESC LIMIT 1", (tool,)).fetchone()
    return r["account"] if r else None


def get_meta(conn, key: str, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def set_meta(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))


def note_gap(conn, tool: str, start: float, end: float, kind: str, detail=None, now=None) -> None:
    conn.execute("INSERT OR IGNORE INTO collect_gap (tool, start, end, kind, detail, ts) VALUES (?, ?, ?, ?, ?, ?)",
                 (tool, start, end, kind, detail, now or time.time()))


def clear_read_gaps(conn, tool: str, name: str) -> None:
    """文件后来读成功了：读失败时游标没有前进，之前漏读的内容这次已经补上，删掉它的读取失败缺口。"""
    prefix = f"{name}: "
    conn.execute("DELETE FROM collect_gap WHERE tool = ? AND kind = 'read_error' AND substr(detail, 1, ?) = ?",
                 (tool, len(prefix), prefix))


def collect_gaps(conn, tool: str):
    return [dict(r) for r in conn.execute(
        "SELECT start, end, kind, detail FROM collect_gap WHERE tool = ? ORDER BY start", (tool,))]


def save_breakdown(conn, tool: str, window: str, as_of: float, window_start, rows, now=None, issues=()) -> bool:
    """保存一组来源分项。和上一组内容相同（同一窗口、同样的分项）只把上一组的 last_as_of 延长到 as_of。
    返回内容是否有变化（新窗口或占比变了），用来让分析缓存失效。"""
    now = now or time.time()
    last = conn.execute("SELECT as_of, window_start, last_as_of FROM source_breakdown WHERE tool = ? AND window = ?"
                        " ORDER BY as_of DESC LIMIT 1", (tool, window)).fetchone()
    if last and as_of <= max(last["as_of"], last["last_as_of"] or last["as_of"]):
        return False   # 旧的或重复的（回填时会遇到）
    same_window = last and (last["window_start"] == window_start or (
        last["window_start"] is not None and window_start is not None
        and abs(last["window_start"] - window_start) <= SAME_WINDOW))
    if same_window:
        prev = [(r["key"], r["display_name"], r["percent"]) for r in conn.execute(
            "SELECT key, display_name, percent FROM source_breakdown WHERE tool = ? AND window = ? AND as_of = ?"
            " ORDER BY key", (tool, window, last["as_of"]))]
        cur = sorted((r["key"], r.get("display_name"), r.get("percent")) for r in rows)
        if prev == cur:
            conn.execute("UPDATE source_breakdown SET last_as_of = ? WHERE tool = ? AND window = ? AND as_of = ?",
                         (as_of, tool, window, last["as_of"]))
            return False
    conn.executemany(
        "INSERT OR IGNORE INTO source_breakdown"
        " (tool, window, as_of, window_start, fetched_at, key, display_name, percent, last_as_of, issues)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(tool, window, as_of, window_start, now, r["key"], r.get("display_name"), r.get("percent"), as_of,
          json.dumps(list(issues)) if issues else None) for r in rows])
    return True


def breakdowns(conn, tool: str, window: str = "seven_day"):
    """[{"as_of", "last_as_of", "window_start", "rows": [{key, display_name, percent}]}]，按 as_of 排序。
    同一组的占比在 [as_of, last_as_of] 期间每次查询都没变。"""
    out = {}
    for r in conn.execute("SELECT as_of, last_as_of, window_start, key, display_name, percent, issues"
                          " FROM source_breakdown WHERE tool = ? AND window = ? ORDER BY as_of, key", (tool, window)):
        item = out.setdefault(r["as_of"], {"as_of": r["as_of"], "last_as_of": r["last_as_of"] or r["as_of"],
                                           "window_start": r["window_start"], "rows": [],
                                           "issues": json.loads(r["issues"]) if r["issues"] else []})
        item["rows"].append({"key": r["key"], "display_name": r["display_name"], "percent": r["percent"]})
    return list(out.values())


def coverage_claims(conn, tool: str):
    return [dict(r) for r in conn.execute(
        "SELECT id, account, start, end FROM coverage_claim WHERE tool = ? ORDER BY start", (tool,))]


def set_coverage_claim(conn, tool: str, account, on: bool, now=None) -> None:
    """打开：为该账号新开一段声明（已有未结束的就不动）；关闭：结束该工具所有未结束的声明。"""
    now = now or time.time()
    open_rows = conn.execute("SELECT id, account FROM coverage_claim WHERE tool = ? AND end IS NULL", (tool,)).fetchall()
    if on:
        if not account:
            raise ValueError("account unknown")
        if any(r["account"] == account for r in open_rows):
            return
        conn.execute("UPDATE coverage_claim SET end = ? WHERE tool = ? AND end IS NULL", (now, tool))
        conn.execute("INSERT INTO coverage_claim (tool, account, start, end) VALUES (?, ?, ?, NULL)", (tool, account, now))
    else:
        conn.execute("UPDATE coverage_claim SET end = ? WHERE tool = ? AND end IS NULL", (now, tool))


def set_rule(conn, tool: str, kind: str, target: str, mode, until=None) -> None:
    """mode 为 None 表示取消这条选择（恢复默认：保留并参与分析）。"""
    if mode is None:
        conn.execute("DELETE FROM data_rule WHERE tool = ? AND kind = ? AND target = ?", (tool, kind, target))
    else:
        conn.execute("INSERT OR REPLACE INTO data_rule (tool, kind, target, mode, until, ts) VALUES (?, ?, ?, ?, ?, ?)",
                     (tool, kind, target, mode, until, time.time()))


def delete_plan_data(conn, tool: str, plan: str, windows) -> dict:
    """删掉一个订阅方案的数据：它的额度快照，以及（Codex）只属于这些窗口的请求。

    windows 是这个方案的窗口（含 _reset_min / _reset_max）。Claude 的请求不分方案，保留在用量统计里。
    记一条 deleted 规则，以后重读日志时跳过，删掉的数据不会再导回来。
    """
    snapshots = requests = 0
    usage_ids = set()
    for w in windows:
        lo, hi = w["_reset_min"] - 1, w["_reset_max"] + 1
        # 没记方案的旧快照跟着窗口走；同一时段别的账号的快照（方案不同）不动
        snapshots += conn.execute(
            "DELETE FROM quota_snapshot WHERE tool = ? AND scope = ? AND window = ? AND resets_at BETWEEN ? AND ? "
            "AND (plan_type = ? OR plan_type IS NULL)", (tool, w["scope"], w["window"], lo, hi, plan)).rowcount
        args = (tool, w["scope"], w["window"], lo, hi)
        usage_ids.update(r[0] for r in conn.execute(
            "SELECT usage_id FROM usage_window WHERE tool = ? AND scope = ? AND window = ? AND resets_at BETWEEN ? AND ?",
            args))
        conn.execute("DELETE FROM usage_window WHERE tool = ? AND scope = ? AND window = ? AND resets_at BETWEEN ? AND ?",
                     args)
        conn.execute("DELETE FROM data_rule WHERE tool = ? AND kind = 'window' AND target = ?", (tool, w["key"]))
    snapshots += conn.execute("DELETE FROM quota_snapshot WHERE tool = ? AND plan_type = ?", (tool, plan)).rowcount
    for uid in usage_ids:
        if not conn.execute("SELECT 1 FROM usage_window WHERE usage_id = ? LIMIT 1", (uid,)).fetchone():
            requests += conn.execute("DELETE FROM usage WHERE id = ?", (uid,)).rowcount
    set_rule(conn, tool, "plan", plan, "deleted", until=time.time())
    return {"snapshots": snapshots, "requests": requests, "windows": len(windows)}


def backup(keep: int = 14, label=None) -> str:
    """把数据库备份到 data/backups/，保留最近 keep 份每日备份。

    label 用于改动前的一次性备份（例如删除数据前），文件名带时间，不参与轮换、不会被自动删掉。
    """
    folder = DATA_DIR / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (time.strftime(f"{label}-%Y%m%d-%H%M%S.db") if label else time.strftime("quotalens-%Y%m%d.db"))
    src = connect()
    try:
        dst = sqlite3.connect(target)
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()
    for old in sorted(folder.glob("quotalens-*.db"))[:-keep]:
        old.unlink()
    return str(target)
