"""Codex 会话日志 → usage 表 + quota_snapshot 表。

日志位置：~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl 和 ~/.codex/archived_sessions/。
  turn_context              → 当前模型
  event_msg / token_count   → info.last_token_usage 是单次调用用量；
                              rate_limits 带 5 小时 / 周窗口的已用百分比和重置时间
去重：同一用量会被重复写出（限额刷新时重发、子代理 / fork 的 rollout 复制父线程历史），
这些重复行的 (total_token_usage, last_token_usage) 完全相同，用它的哈希做主键，
INSERT OR IGNORE 天然去重，重读文件也不会双算。
归属：每次请求挂到同一事件（或本文件最近一次）rate_limits 报告的窗口上，见 usage_window 表。
删除：用户在页面上删掉某个订阅方案的数据后，重读日志时跳过删除时刻之前属于该方案的快照和请求。
"""
import hashlib
import json
import time

from . import db
from .paths import codex_dir
from .pricing import PriceTable, is_on_plan
from .util import file_birth, file_changed, iter_complete_lines, parse_ts, window_name

TOOL = "codex"
COUNTER_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens", "total_tokens")

INSERT_USAGE = """
INSERT OR IGNORE INTO usage (id, tool, ts, model, on_plan, session_id, input_tokens, cache_read_tokens,
                             cache_write_5m_tokens, cache_write_1h_tokens, output_tokens, reasoning_tokens,
                             speed, cost_usd)
VALUES (?, 'codex', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL, ?)
"""
INSERT_LINK = """
INSERT OR IGNORE INTO usage_window (usage_id, tool, scope, window, resets_at) VALUES (?, 'codex', ?, ?, ?)
"""
INSERT_SNAPSHOT = """
INSERT OR IGNORE INTO quota_snapshot (tool, scope, window, ts, used_percent, resets_at, window_seconds, plan_type, source)
VALUES ('codex', ?, ?, ?, ?, ?, ?, ?, 'log')
"""


def _counters(obj):
    if not isinstance(obj, dict):
        return None
    return [int(obj.get(k) or 0) for k in COUNTER_KEYS]


def rate_limit_snapshots(payload: dict, ts: float, schemas=None):
    """token_count 事件里的 rate_limits → 快照行参数；schemas 收集字段结构指纹的首末时间。"""
    rl = payload.get("rate_limits")
    if not isinstance(rl, dict):
        return []
    if schemas is not None:
        fp, paths = db.schema_fingerprint(rl)
        first, last, _ = schemas.get(fp, (ts, ts, paths))
        schemas[fp] = (min(first, ts), max(last, ts), paths)
    rows = []
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if not isinstance(w, dict) or w.get("used_percent") is None or not w.get("window_minutes"):
            continue
        seconds = int(w["window_minutes"]) * 60
        rows.append((rl.get("limit_id") or "codex", window_name(seconds), ts, float(w["used_percent"]),
                     w.get("resets_at"), seconds, rl.get("plan_type")))
    return rows


class FileState:
    """单个 rollout 文件读到当前位置时的上下文，存进 file_cursor.state 以便增量续读。"""

    def __init__(self, raw=None):
        raw = raw or {}
        self.model = raw.get("model", "unknown")
        self.session_id = raw.get("session_id")
        self.high = raw.get("high")  # total_token_usage 的高水位，只在缺 last_token_usage 时用
        self.links = raw.get("links") or []  # 最近一次 rate_limits 里的 [scope, window, resets_at]
        self.plan = raw.get("plan")  # 最近一次 rate_limits 里的订阅方案

    def dump(self):
        return json.dumps({"model": self.model, "session_id": self.session_id, "high": self.high,
                           "links": self.links, "plan": self.plan})


def parse_line(raw: bytes, state: FileState, schemas=None):
    """返回 (usage 记录或 None, 快照列表)。"""
    if b'"token_count"' not in raw and b'"turn_context"' not in raw and b'"session_meta"' not in raw:
        return None, []
    try:
        d = json.loads(raw)
    except ValueError:
        return None, []
    kind = d.get("type")
    payload = d.get("payload") or {}
    if kind == "session_meta":
        state.session_id = state.session_id or payload.get("id")
        return None, []
    if kind == "turn_context":
        state.model = payload.get("model") or state.model
        return None, []
    if kind != "event_msg" or payload.get("type") != "token_count":
        return None, []
    ts = parse_ts(d.get("timestamp"))
    if ts is None:
        return None, []
    snapshots = rate_limit_snapshots(payload, ts, schemas)
    links = [[scope, window, resets] for scope, window, _, _, resets, _, _ in snapshots if resets]
    if links:
        state.links = links
    if snapshots and snapshots[0][6]:
        state.plan = snapshots[0][6]
    info = payload.get("info")
    if not isinstance(info, dict):
        return None, snapshots
    state.model = info.get("model") or state.model
    total, last = _counters(info.get("total_token_usage")), _counters(info.get("last_token_usage"))
    if total is None and last is None:
        return None, snapshots

    if last is not None:
        delta = last
    elif state.high is not None:
        delta = [max(0, t - h) for t, h in zip(total, state.high)]
    else:
        delta = total
    if total is not None:
        state.high = total if state.high is None else [max(a, b) for a, b in zip(total, state.high)]

    inp, cached, cache_write, output, reasoning, _ = delta
    cached = min(cached, inp)
    fresh = max(0, inp - cached - cache_write)
    if not (inp or output):
        return None, snapshots
    # 有累计值时 (total, last) 足以区分不同请求；只有单次值时，两次用量相同的请求会撞键，
    # 再带上时间戳（重放的副本时间戳也相同，仍能去重）
    signature = json.dumps([total, last] if total is not None else [d.get("timestamp"), last])
    rec = {
        "id": "codex:" + hashlib.sha1(signature.encode()).hexdigest()[:24],
        "ts": ts,
        "model": state.model,
        "session_id": state.session_id,
        "input": fresh,
        "cache_read": cached,
        "cache_write": cache_write,
        "output": output,
        "reasoning": reasoning,
        # 沿用的旧链接只在窗口还没重置时有效，否则会把新窗口的用量算到已过期的窗口上
        "links": [link for link in state.links if link[2] > ts],
        "plan": state.plan,
    }
    return rec, snapshots


def _deleted(plan, ts, deleted):
    """这条数据属于用户删掉的方案，并且发生在删除之前。"""
    return plan in deleted and ts <= deleted[plan]


def _rollout_files(root):
    for sub in ("sessions", "archived_sessions"):
        d = root / sub
        if d.is_dir():
            yield from d.rglob("rollout-*.jsonl")


def log_files():
    return list(_rollout_files(codex_dir()))


def sync(prices: PriceTable, history_days: int = 90) -> dict:
    root = codex_dir()
    stats = {"files": 0, "changed": 0, "rows": 0, "snapshots": 0}
    cutoff = time.time() - history_days * 86400
    with db.writer() as conn:
        cursors = {r["path"]: r for r in conn.execute("SELECT * FROM file_cursor WHERE path LIKE ?", (str(root) + "%",))}
        deleted = {plan: until for plan, (mode, until) in db.data_rules(conn, TOOL)["plans"].items() if mode == "deleted"}
        for path in _rollout_files(root):
            stats["files"] += 1
            key = str(path)
            cursor = cursors.get(key)
            try:
                if path.stat().st_mtime < cutoff and cursor is None:
                    continue
                change = file_changed(path, cursor)
                if change is None:
                    continue
                st, start, restart = change
                if restart and cursor is not None:
                    db.note_gap(conn, TOOL, cursor["mtime_ns"] / 1e9, st.st_mtime, "truncated", path.name)
                state = FileState(None if restart or cursor is None else json.loads(cursor["state"] or "{}"))
                usage_rows, snap_rows, schemas, end = [], [], {}, start
                for raw, end in iter_complete_lines(path, start):
                    rec, snaps = parse_line(raw, state, schemas)
                    snap_rows.extend(r for r in snaps if not _deleted(r[6], r[2], deleted))
                    if rec and not _deleted(rec["plan"], rec["ts"], deleted):
                        usage_rows.append(rec)
            except OSError as e:
                db.note_gap(conn, TOOL, cursor["mtime_ns"] / 1e9 if cursor else file_birth(path, cutoff), time.time(),
                            "read_error",
                            f"{path.name}: {type(e).__name__}")
                continue
            for r in usage_rows:
                cost = prices.cost(r["model"], input_tokens=r["input"], cache_read=r["cache_read"],
                                   cache_write_5m=r["cache_write"], output_tokens=r["output"])
                conn.execute(INSERT_USAGE, (r["id"], r["ts"], r["model"], int(is_on_plan(TOOL, r["model"])),
                                            r["session_id"], r["input"], r["cache_read"], r["cache_write"],
                                            r["output"], r["reasoning"], cost))
                conn.executemany(INSERT_LINK, [(r["id"], *link) for link in r["links"]])
            conn.executemany(INSERT_SNAPSHOT, snap_rows)
            for fp, (first, last, paths) in schemas.items():
                db.note_schema(conn, TOOL, "log", None, first, last, fingerprint=(fp, paths))
            db.clear_read_gaps(conn, TOOL, path.name)
            stats["rows"] += len(usage_rows)
            stats["snapshots"] += len(snap_rows)
            stats["changed"] += 1
            conn.execute(
                "INSERT OR REPLACE INTO file_cursor (path, size, mtime_ns, offset, state) VALUES (?, ?, ?, ?, ?)",
                (key, st.st_size, st.st_mtime_ns, end, state.dump()),
            )
            conn.commit()
    return stats
