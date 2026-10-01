"""Claude Code 会话日志 → usage 表。

日志位置：~/.claude/projects/<项目>/<会话>.jsonl（含 subagents 子目录）。
每次模型调用落在 type=assistant 的行里，message.usage 是该次调用的 token。
一次调用的多个内容块会写成多行、共用同一个 message.id，所以按 message.id 去重，
保留 stop_reason 非空或 output 最大的那行。
"""
import json
import time

from . import db
from .paths import claude_dir
from .pricing import PriceTable, is_on_plan
from .util import file_changed, iter_complete_lines, parse_ts

TOOL = "claude"

UPSERT = """
INSERT INTO usage (id, tool, ts, model, on_plan, session_id, input_tokens, cache_read_tokens,
                   cache_write_5m_tokens, cache_write_1h_tokens, output_tokens, reasoning_tokens,
                   speed, cost_usd)
VALUES (?, 'claude', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    output_tokens = excluded.output_tokens,
    reasoning_tokens = excluded.reasoning_tokens,
    cost_usd = excluded.cost_usd
WHERE excluded.output_tokens > usage.output_tokens
"""


def parse_line(raw: bytes):
    """解析一行；不是带用量的 assistant 消息就返回 None。"""
    if b'"assistant"' not in raw or b'"usage"' not in raw:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if d.get("type") != "assistant":
        return None
    msg = d.get("message") or {}
    usage = msg.get("usage")
    msg_id = msg.get("id")
    model = msg.get("model") or "unknown"
    if not msg_id or not isinstance(usage, dict) or model == "<synthetic>":
        return None

    def n(obj, key):
        v = (obj or {}).get(key)
        return int(v) if isinstance(v, (int, float)) else 0

    cache_write = n(usage, "cache_creation_input_tokens")
    split = usage.get("cache_creation")
    if isinstance(split, dict) and (n(split, "ephemeral_5m_input_tokens") or n(split, "ephemeral_1h_input_tokens")):
        cw_5m, cw_1h = n(split, "ephemeral_5m_input_tokens"), n(split, "ephemeral_1h_input_tokens")
    else:
        cw_5m, cw_1h = cache_write, 0
    ts = parse_ts(d.get("timestamp"))
    if ts is None:
        return None
    return {
        "id": "claude:" + msg_id,
        "ts": ts,
        "model": model,
        "session_id": d.get("sessionId"),
        "input": n(usage, "input_tokens"),
        "cache_read": n(usage, "cache_read_input_tokens"),
        "cw_5m": cw_5m,
        "cw_1h": cw_1h,
        "output": n(usage, "output_tokens"),
        "reasoning": n(usage.get("output_tokens_details"), "thinking_tokens"),
        "speed": usage.get("speed"),
        "final": msg.get("stop_reason") is not None,
    }


def _better(new, old):
    if new["final"] != old["final"]:
        return new["final"]
    return new["output"] > old["output"]


def sync(prices: PriceTable, history_days: int = 90) -> dict:
    root = claude_dir() / "projects"
    stats = {"files": 0, "changed": 0, "rows": 0}
    if not root.is_dir():
        return stats
    cutoff = time.time() - history_days * 86400
    with db.writer() as conn:
        cursors = {r["path"]: r for r in conn.execute("SELECT * FROM file_cursor WHERE path LIKE ?", (str(root) + "%",))}
        for path in root.rglob("*.jsonl"):
            stats["files"] += 1
            key = str(path)
            try:
                if path.stat().st_mtime < cutoff and key not in cursors:
                    continue
                change = file_changed(path, cursors.get(key))
                if change is None:
                    continue
                st, start, _ = change
                messages, end = {}, start
                for raw, end in iter_complete_lines(path, start):
                    rec = parse_line(raw)
                    if rec and (rec["id"] not in messages or _better(rec, messages[rec["id"]])):
                        messages[rec["id"]] = rec
            except OSError:
                continue
            for r in messages.values():
                if not (r["input"] or r["output"] or r["cache_read"] or r["cw_5m"] or r["cw_1h"]):
                    continue
                cost = prices.cost(r["model"], input_tokens=r["input"], cache_read=r["cache_read"],
                                   cache_write_5m=r["cw_5m"], cache_write_1h=r["cw_1h"],
                                   output_tokens=r["output"], speed=r["speed"])
                conn.execute(UPSERT, (r["id"], r["ts"], r["model"], int(is_on_plan(TOOL, r["model"])),
                                      r["session_id"], r["input"], r["cache_read"], r["cw_5m"], r["cw_1h"],
                                      r["output"], r["reasoning"], r["speed"], cost))
            stats["rows"] += len(messages)
            stats["changed"] += 1
            conn.execute(
                "INSERT OR REPLACE INTO file_cursor (path, size, mtime_ns, offset, state) VALUES (?, ?, ?, ?, NULL)",
                (key, st.st_size, st.st_mtime_ns, end),
            )
            conn.commit()
    return stats
