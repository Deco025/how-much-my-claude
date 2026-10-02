"""额度规则的「明调」痕迹：接口字段变化、订阅方案变化、新增限额窗口、窗口提前重置。

全部从已入库的数据推出来，和日志导入顺序无关；这些事件也是解读额度趋势跳变的背景。
"""
import json
import time

from . import calibrate

NEW_AFTER = 86400          # 比最早记录晚这么久才出现的字段 / 窗口，才算「新增」
GONE_BEFORE = 2 * 86400    # 比最新记录早这么久就不再出现的字段，才算「消失」
SOURCE_NAMES = {"log": "日志", "api": "额度接口"}
TOOL_NAMES = {"codex": "Codex", "claude": "Claude"}


def _event(ts, tool, kind, title, detail="", window=None, params=None):
    """window 为空表示影响整个工具（方案、字段变化），否则只关乎该窗口类型。

    title / detail 是中文说明；params 是同一件事的原始数据，页面切到英文时用它重新组句。
    """
    return {"ts": ts, "tool": tool, "kind": kind, "title": title, "detail": detail, "window": window,
            "params": params or {}}


def _schema_events(conn):
    out = []
    groups = {}
    for r in conn.execute("SELECT tool, source, first_ts, last_ts, shape FROM schema_seen"):
        groups.setdefault((r["tool"], r["source"]), []).append(r)
    for (tool, source), rows in groups.items():
        first_all, last_all = min(r["first_ts"] for r in rows), max(r["last_ts"] for r in rows)
        span = {}
        for r in rows:
            for path in json.loads(r["shape"]):
                a, b = span.get(path, (r["first_ts"], r["last_ts"]))
                span[path] = (min(a, r["first_ts"]), max(b, r["last_ts"]))
        # 同一小时内出现 / 消失的字段合成一个事件
        added, removed = {}, {}
        for path, (a, b) in span.items():
            if a > first_all + NEW_AFTER:
                added.setdefault(round(a / 3600), []).append((a, path))
            if b < last_all - GONE_BEFORE:
                removed.setdefault(round(b / 3600), []).append((b, path))
        where = f"{TOOL_NAMES[tool]}{SOURCE_NAMES.get(source, source)}"
        for items in added.values():
            fields = sorted(p for _, p in items)
            out.append(_event(min(a for a, _ in items), tool, "schema", f"{where}出现新字段", "、".join(fields),
                              params={"change": "added", "source": source, "fields": fields}))
        for items in removed.values():
            fields = sorted(p for _, p in items)
            out.append(_event(max(b for b, _ in items), tool, "schema", f"{where}的字段不再出现", "、".join(fields),
                              params={"change": "removed", "source": source, "fields": fields}))
    return out


def _plan_and_window_events(conn, tool):
    out = []
    rows = conn.execute("SELECT ts, scope, window, plan_type FROM quota_snapshot WHERE tool = ? ORDER BY ts",
                        (tool,)).fetchall()
    if not rows:
        return out
    first_all = rows[0]["ts"]
    plan, seen = None, {}
    for r in rows:
        if r["plan_type"] and r["plan_type"] != plan:
            if plan is not None:
                out.append(_event(r["ts"], tool, "plan", "订阅方案变化", f"{plan} → {r['plan_type']}",
                                  params={"from": plan, "to": r["plan_type"]}))
            plan = r["plan_type"]
        key = (r["scope"], r["window"])
        if key not in seen:
            seen[key] = r["ts"]
            if r["ts"] > first_all + NEW_AFTER:
                out.append(_event(r["ts"], tool, "window", "出现新的限额窗口", f"{r['scope']} · {r['window']}",
                                  window=r["window"], params={"scope": r["scope"], "window": r["window"]}))
    return out


def _early_resets(conn, tool):
    out = []
    by_key = {}
    for inst in calibrate.instances_for(conn, tool):
        by_key.setdefault((inst["scope"], inst["window"]), []).append(inst)
    for (scope, window), insts in by_key.items():
        insts.sort(key=lambda i: i["first_seen"])
        for prev, nxt in zip(insts, insts[1:]):
            # 上一个窗口还没到点就有了新窗口，且之后再没出现过（排除多个账号交替使用的情况）
            if nxt["first_seen"] < prev["reset_max"] - 600 and prev["last_seen"] <= nxt["first_seen"] + 60:
                pct = max(s["used_percent"] for s in prev["snapshots"])
                note = "，可能用了重置券或切换了账号" if tool == "codex" else ""
                out.append(_event(nxt["first_seen"], tool, "reset", "窗口提前重置",
                                  f"{scope} · {window}：上个窗口用到 {pct:.0f}%，原定 "
                                  f"{time.strftime('%m-%d %H:%M', time.localtime(prev['reset_max']))} 重置{note}",
                                  window=window, params={"scope": scope, "window": window, "pct": pct,
                                                         "due": prev["reset_max"], "maybe_switch": tool == "codex"}))
    return out


def detect(conn, days=90, now=None):
    now = now or time.time()
    out = _schema_events(conn)
    for tool in ("codex", "claude"):
        out.extend(_plan_and_window_events(conn, tool))
        out.extend(_early_resets(conn, tool))
    out = [e for e in out if e["ts"] >= now - days * 86400]
    out.sort(key=lambda e: e["ts"], reverse=True)
    return out
