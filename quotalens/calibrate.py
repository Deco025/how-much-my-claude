"""额度分析：推算每个窗口值多少钱，并监控厂商是否暗调额度。

流程（按 工具 × scope × 窗口类型 分组）：
  1. 窗口实例：同一组里 resets_at 相同（允许漂移）的快照。花费归属：
       Codex   按 usage_window 链接精确归属（日志里每次请求带着它所消耗窗口的 resets_at）。
       Claude  按时间 [resets_at - 窗口长度, resets_at]；前提是只登录一个账号、不用 API key 跑 claude-*。
  2. 拆步：相邻两次百分比上涨之间，本地各模型花了多少。
  3. 外部消耗：用基线汇率算每步的预期涨幅，涨幅远超预期（或本地没花钱却涨了）的部分，
     判为本地日志之外的消耗（别的设备、云任务、网页聊天），从已用% 里扣掉。
     外部消耗占比高的窗口不参与汇率拟合和趋势。
  4. 汇率：在干净窗口上做非负最小二乘  已用% ≈ Σ 汇率[模型] × 花费[模型]。
     实测额度消耗和 API 价格不成正比，不同模型同样花 $1 吃掉的额度可以差几倍。
  5. 趋势：每个窗口折算成「全用主力模型时，一个窗口值多少钱」，消除模型组合的影响。
     最近的窗口和之前几周对比，偏离超过噪声就判为收紧 / 放宽。

百分比的两个毛病：只有整数精度（推算给区间），并行会话会报旧值（取单调最大值）。
"""
import statistics
import time

# (工具, scope) → 计入该限额的模型名过滤（SQL LIKE）；None 表示该工具全部官方模型
SCOPE_FILTERS = {
    ("claude", "all"): None,
    ("claude", "opus"): "%opus%",
    ("claude", "sonnet"): "%sonnet%",
    ("claude", "fable"): "%fable%",
    ("codex", "codex"): None,
}
LINKED_TOOLS = {"codex"}
RESET_JITTER = 900       # 同一窗口的 resets_at 在相邻快照间的最大漂移（秒）

ANALYSIS_DAYS = 60       # 汇率拟合和趋势图的时间范围
MIN_FIT_PCT = 5          # 已用% 太小的窗口分辨率不够，不参与拟合
MIN_TREND_PCT = 10       # 参与趋势判断的最低已用%
MIN_MODEL_WINDOWS = 2    # 模型至少出现在几个窗口里才单独估计汇率，否则并入「其他」
OTHER = "(其他)"

EXTERNAL_MIN_STEP = 3    # 单步至少涨这么多 % 才可能判为外部消耗
EXTERNAL_FACTOR = 3      # 且超过预期涨幅的 3 倍再加 2 个点
EXTERNAL_SLACK = 2
IDLE_MIN_STEP = 2        # 本地完全没花钱时涨这么多 % 就算外部消耗
CONTAMINATED_SHARE = 0.15

MIN_CHANGE = 0.20        # 低于这个幅度的变化不报（单窗口噪声约 ±20%）
CHANGE_Z = 2.5
DEFAULT_SPREAD = 0.20    # 基线窗口太少、算不出离散度时的假设值


def confidence(pct: float) -> str:
    if pct >= 20:
        return "high"
    if pct >= 5:
        return "medium"
    return "low"


# ── 窗口实例 ─────────────────────────────────────────────

def _group_instances(snapshots, linked: bool):
    groups = {}
    for s in snapshots:
        groups.setdefault((s["scope"], s["window"]), []).append(s)
    instances = []
    for (scope, window), rows in groups.items():
        rows.sort(key=lambda r: r["resets_at"])
        current, prev_reset, key_instances = None, None, []
        for r in rows:
            if current is None or r["resets_at"] - prev_reset > RESET_JITTER:
                current = {"scope": scope, "window": window, "window_seconds": r["window_seconds"],
                           "reset_min": r["resets_at"], "plan_type": None, "snapshots": []}
                key_instances.append(current)
            prev_reset = current["reset_max"] = r["resets_at"]
            current["snapshots"].append(r)
        for inst in key_instances:
            inst["snapshots"].sort(key=lambda r: r["ts"])
            inst["plan_type"] = next((s["plan_type"] for s in reversed(inst["snapshots"]) if s["plan_type"]), None)
            inst["start"] = inst["reset_min"] - inst["window_seconds"]
            inst["end"] = inst["reset_max"]
            inst["first_seen"] = inst["snapshots"][0]["ts"]
            inst["last_seen"] = inst["snapshots"][-1]["ts"]
        if not linked:
            # 时间归属：下一个窗口提前开始（提前重置）时，在交界处截断上一个
            key_instances.sort(key=lambda i: i["first_seen"])
            for prev, nxt in zip(key_instances, key_instances[1:]):
                if nxt["start"] < prev["end"]:
                    boundary = max(nxt["start"], prev["last_seen"])
                    prev["end"], nxt["start"] = boundary, boundary
        instances.extend(key_instances)
    instances.sort(key=lambda i: i["start"])
    return instances


def instances_for(conn, tool):
    snaps = [dict(r) for r in conn.execute(
        "SELECT scope, window, ts, used_percent, resets_at, window_seconds, plan_type, source FROM quota_snapshot "
        "WHERE tool = ? AND resets_at IS NOT NULL AND window_seconds IS NOT NULL", (tool,))]
    return _group_instances(snaps, linked=tool in LINKED_TOOLS)


def _window_rows(conn, tool, inst, linked):
    """窗口内计入该限额的请求，按时间排序：(ts, model, cost_usd 或 None, 百万 token 数)。"""
    model_filter = SCOPE_FILTERS.get((tool, inst["scope"]))
    cols = "u.ts, u.model, u.cost_usd, (u.input_tokens + u.cache_read_tokens + u.output_tokens) / 1e6 AS mtok"
    if linked:
        sql = (f"SELECT {cols} FROM usage_window w JOIN usage u ON u.id = w.usage_id "
               "WHERE w.tool = ? AND w.scope = ? AND w.window = ? AND w.resets_at BETWEEN ? AND ? AND u.on_plan = 1")
        args = [tool, inst["scope"], inst["window"], inst["reset_min"] - 1, inst["reset_max"] + 1]
    else:
        sql = f"SELECT {cols} FROM usage u WHERE u.tool = ? AND u.on_plan = 1 AND u.ts >= ? AND u.ts < ?"
        args = [tool, inst["start"], inst["end"]]
    if model_filter:
        sql += " AND u.model LIKE ?"
        args.append(model_filter)
    return conn.execute(sql + " ORDER BY u.ts", args).fetchall()


# ── 单个窗口：拆步 ───────────────────────────────────────

def _key_value(row):
    """拟合变量：有价格的模型用美元，没价格的（如 codex-auto-review）用百万 token。"""
    if row["cost_usd"] is not None:
        return (row["model"], "usd"), row["cost_usd"]
    return (row["model"], "mtok"), row["mtok"]


def _build_window(conn, tool, inst, now):
    linked = tool in LINKED_TOOLS
    supported = (tool, inst["scope"]) in SCOPE_FILTERS
    rows = _window_rows(conn, tool, inst, linked) if supported else []
    cum, pending, steps, points = {}, {}, [], []
    i, running, cost = 0, 0.0, 0.0
    for s in inst["snapshots"]:
        while i < len(rows) and rows[i]["ts"] <= s["ts"]:
            key, value = _key_value(rows[i])
            cum[key] = cum.get(key, 0.0) + value
            pending[key] = pending.get(key, 0.0) + value
            cost += rows[i]["cost_usd"] or 0.0
            i += 1
        pct = max(running, s["used_percent"])
        points.append({"ts": s["ts"], "pct": pct, "cost": cost})
        if pct > running:
            steps.append({"ts": s["ts"], "dpct": pct - running, "vec": pending})
            pending, running = {}, pct
    cost_so_far = cost + sum(r["cost_usd"] or 0.0 for r in rows[i:])
    return {
        "tool": tool, "scope": inst["scope"], "window": inst["window"], "window_seconds": inst["window_seconds"],
        "plan_type": inst["plan_type"], "start": inst["start"], "end": inst["end"], "active": inst["end"] > now,
        "first_seen": inst["first_seen"], "last_seen": inst["last_seen"], "supported": supported,
        "attribution": "linked" if linked else "time",
        "used_percent": running, "snapshot_at": inst["last_seen"],
        "cost_at_snapshot": cost, "cost_so_far": cost_so_far,
        "unpriced_requests": sum(1 for r in rows if r["cost_usd"] is None),
        "points": points, "_steps": steps, "_vec": cum,
        "external_pct": 0.0, "pct_clean": running, "contaminated": False,
        "cap": None, "cap_low": None, "cap_high": None, "confidence": None,
        "value": None, "index": None, "in_trend": False,
    }


# ── 汇率拟合与外部消耗识别 ───────────────────────────────

def _nnls(a, b):
    """非负最小二乘：反复去掉最负的系数再解，变量很少时足够用。"""
    import numpy as np
    active = list(range(a.shape[1]))
    x = np.zeros(a.shape[1])
    while active:
        sol, *_ = np.linalg.lstsq(a[:, active], b, rcond=None)
        if (sol >= 0).all():
            x[active] = sol
            break
        active.pop(int(np.argmin(sol)))
    return x


def _fit_rates(windows, target):
    """各窗口 (花费向量 → 已用%) 回归汇率。返回 None 表示数据不够。"""
    import numpy as np
    obs = [(w["_vec"], w[target]) for w in windows if w["_vec"]]
    if len(obs) < 2:
        return None
    presence = {}
    for vec, _ in obs:
        for k in vec:
            presence[k] = presence.get(k, 0) + 1
    keys = sorted(k for k, n in presence.items() if n >= MIN_MODEL_WINDOWS)
    has_other = any(n < MIN_MODEL_WINDOWS for n in presence.values())
    a = np.array([[vec.get(k, 0.0) for k in keys] + ([sum(v for k, v in vec.items() if k not in keys)] if has_other else [])
                  for vec, _ in obs])
    b = np.array([pct for _, pct in obs])
    x = _nnls(a, b)
    errors = [abs(p - y) / y for p, y in zip(a @ x, b) if y > 0]
    return {
        "rates": {k: float(r) for k, r in zip(keys, x)},
        "other": float(x[-1]) if has_other else None,
        "presence": presence,
        "windows": len(obs),
        "median_error": statistics.median(errors) if errors else None,
    }


def _expected(vec, fit):
    """按汇率算一段花费预期会涨多少 %，以及其中有多少来自单独估计过的模型。"""
    other, covered = 0.0, 0.0
    for k, v in vec.items():
        if k in fit["rates"]:
            covered += fit["rates"][k] * v
        elif fit["other"] is not None:
            other += fit["other"] * v
    return other + covered, covered


def _local_rate(w):
    """没有汇率可用时，用窗口自己各步 %/花费 的加权中位数当预期。"""
    pairs = sorted((s["dpct"] / sum(s["vec"].values()), sum(s["vec"].values()))
                   for s in w["_steps"] if sum(s["vec"].values()) > 0)
    if not pairs:
        return 0.0
    half, acc = sum(p[1] for p in pairs) / 2, 0.0
    for ratio, weight in pairs:
        acc += weight
        if acc >= half:
            return ratio
    return pairs[-1][0]


def _detect_external(w, fit):
    external = 0.0
    local_rate = None if fit else _local_rate(w)
    for s in w["_steps"]:
        local = sum(s["vec"].values())
        expected = _expected(s["vec"], fit)[0] if fit else local_rate * local
        if local <= 0:
            ext = s["dpct"] if s["dpct"] >= IDLE_MIN_STEP else 0.0
        elif s["dpct"] >= EXTERNAL_MIN_STEP and s["dpct"] >= EXTERNAL_FACTOR * expected + EXTERNAL_SLACK:
            ext = s["dpct"] - expected
        else:
            ext = 0.0
        s["expected"], s["external"] = expected, ext
        external += ext
    pct = w["used_percent"]
    w["external_pct"] = min(external, pct)
    w["pct_clean"] = pct - w["external_pct"]
    w["contaminated"] = pct > 0 and w["external_pct"] / pct >= CONTAMINATED_SHARE


def _set_cap(w):
    pct, cost = w["pct_clean"], w["cost_at_snapshot"]
    if w["supported"] and pct >= 1 and cost > 0:
        w["cap"] = cost / (pct / 100)
        w["cap_low"] = cost / ((pct + 0.5) / 100)
        w["cap_high"] = cost / ((pct - 0.5) / 100)
        w["confidence"] = "low" if w["contaminated"] else confidence(pct)


# ── 趋势与变化判断 ───────────────────────────────────────

def _robust_spread(values):
    if len(values) < 3:
        return DEFAULT_SPREAD
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    return max(1.4826 * mad / med, 0.05) if med else DEFAULT_SPREAD


def _change_status(trend, window_seconds, now):
    """最近的窗口 vs 之前的基线：stable / tighter / looser / insufficient。"""
    if window_seconds <= 6 * 3600:
        recent = [w for w in trend if w["last_seen"] >= now - 72 * 3600]
        baseline = [w for w in trend if now - 21 * 86400 <= w["last_seen"] < now - 72 * 3600]
        min_recent, min_baseline = 3, 5
    else:
        ordered = sorted(trend, key=lambda w: w["end"])
        recent = ordered[-1:]
        baseline = [w for w in ordered[:-1] if w["end"] >= now - 42 * 86400][-4:]
        min_recent, min_baseline = 1, 2
    out = {"recent_n": len(recent), "baseline_n": len(baseline), "status": "insufficient",
           "ratio": None, "threshold": None, "recent_median": None, "baseline_median": None}
    if len(recent) < min_recent or len(baseline) < min_baseline:
        return out
    r_vals, b_vals = [w["value"] for w in recent], [w["value"] for w in baseline]
    r_med, b_med = statistics.median(r_vals), statistics.median(b_vals)
    ratio = r_med / b_med
    threshold = max(MIN_CHANGE, CHANGE_Z * _robust_spread(b_vals) * (1 / len(r_vals) + 1 / len(b_vals)) ** 0.5)
    status = "tighter" if ratio < 1 - threshold else "looser" if ratio > 1 + threshold else "stable"
    out.update(status=status, ratio=ratio, threshold=threshold, recent_median=r_med, baseline_median=b_med)
    return out


def _analyze_group(windows, now):
    """同一 (scope, 窗口类型) 的全部窗口：识别外部消耗、拟合汇率、折算价值、判断变化。"""
    recent = [w for w in windows if w["end"] >= now - ANALYSIS_DAYS * 86400]
    # 第一轮：所有窗口粗拟合 → 识别外部消耗；第二轮：只用干净窗口、扣掉外部消耗后重新拟合
    fit = _fit_rates([w for w in recent if w["used_percent"] >= MIN_FIT_PCT], "used_percent")
    for w in windows:
        _detect_external(w, fit)
    clean_fit = _fit_rates([w for w in recent if not w["contaminated"] and w["pct_clean"] >= MIN_FIT_PCT], "pct_clean")
    if clean_fit:
        fit = clean_fit
        for w in windows:
            _detect_external(w, fit)
    for w in windows:
        _set_cap(w)

    group = {"ref_model": None, "ref_cap": None, "rates": [], "fit_windows": 0, "median_error_pct": None}
    if fit:
        group["fit_windows"] = fit["windows"]
        if fit["median_error"] is not None:
            group["median_error_pct"] = fit["median_error"] * 100
        for (model, unit), rate in fit["rates"].items():
            group["rates"].append({"model": model, "unit": unit, "windows": fit["presence"][(model, unit)],
                                   "pct_per_unit": rate, "cap_if_only": 100 / rate if rate > 1e-9 else None})
        group["rates"].sort(key=lambda r: -r["windows"])
        # 主力模型：干净窗口里花费最多、且有汇率的有价模型
        spend = {}
        for w in recent:
            if not w["contaminated"]:
                for k, v in w["_vec"].items():
                    if k[1] == "usd" and fit["rates"].get(k, 0) > 0:
                        spend[k] = spend.get(k, 0.0) + v
        if spend:
            ref = max(spend, key=spend.get)
            group["ref_model"], group["ref_cap"] = ref[0], 100 / fit["rates"][ref]
            for w in windows:
                expected, covered = _expected(w["_vec"], fit)
                if expected > 0 and w["pct_clean"] > 0 and covered / expected >= 0.7:
                    w["index"] = w["pct_clean"] / expected
                    w["value"] = group["ref_cap"] / w["index"]
                    w["in_trend"] = (not w["contaminated"] and w["pct_clean"] >= MIN_TREND_PCT
                                     and w["end"] >= now - ANALYSIS_DAYS * 86400)
    trend = [w for w in windows if w["in_trend"]]
    group.update(_change_status(trend, windows[0]["window_seconds"], now))
    return group


def _fill_plans(windows):
    """没记录方案的窗口（旧版 CLI 日志）按时间上最近的同类窗口推断方案。"""
    by_key = {}
    for w in windows:
        by_key.setdefault((w["scope"], w["window"]), []).append(w)
    for ws in by_key.values():
        known = [w for w in ws if w["plan_type"]]
        for w in ws:
            if not w["plan_type"] and known:
                w["plan_type"] = min(known, key=lambda k: abs(k["start"] - w["start"]))["plan_type"]


def analyze(conn, tool, now=None):
    """返回 {"windows": [...], "groups": [...]}；窗口按开始时间排序。

    不同订阅方案的额度本来就不同，所以分组键里带上方案，换方案不会被当成暗调。
    """
    now = now or time.time()
    windows = [_build_window(conn, tool, inst, now) for inst in instances_for(conn, tool)]
    _fill_plans(windows)
    by_key = {}
    for w in windows:
        by_key.setdefault((w["scope"], w["window"], w["plan_type"]), []).append(w)
    groups = []
    for (scope, window, plan), ws in by_key.items():
        group = _analyze_group(ws, now) if ws[0]["supported"] else {"status": "unsupported", "rates": []}
        group.update(tool=tool, scope=scope, window=window, plan_type=plan, window_seconds=ws[0]["window_seconds"],
                     last_seen=max(w["last_seen"] for w in ws), latest_end=max(w["end"] for w in ws),
                     windows_n=len(ws))
        group["trend"] = [{"start": w["start"], "end": w["end"], "last_seen": w["last_seen"], "value": w["value"],
                           "pct": w["used_percent"], "pct_clean": w["pct_clean"], "external_pct": w["external_pct"],
                           "contaminated": w["contaminated"], "in_trend": w["in_trend"], "cost": w["cost_at_snapshot"]}
                          for w in ws if w["value"] is not None and w["end"] >= now - ANALYSIS_DAYS * 86400]
        groups.append(group)
    return {"windows": windows, "groups": groups}


def current_windows(windows):
    """当前账号正在用的窗口：每个 (scope, window) 最近有快照的实例，且方案和最新快照相同。

    切换过账号时，旧账号的窗口（例如免费账号的 30 天窗口）方案不同，据此滤掉。不按快照新旧
    过滤：窗口还没开始时接口不报这个窗口，已重置的 5 小时窗口会长时间没有新快照，但它仍属于当前账号。
    """
    latest = {}
    for w in windows:
        key = (w["scope"], w["window"])
        if w["snapshot_at"] and (key not in latest or w["snapshot_at"] > latest[key]["snapshot_at"]):
            latest[key] = w
    if not latest:
        return []
    plan = max(latest.values(), key=lambda w: w["snapshot_at"])["plan_type"]
    return [w for w in latest.values() if plan is None or w["plan_type"] == plan]


def public(w, with_points=False):
    """去掉内部字段，供 API 输出。"""
    out = {k: v for k, v in w.items() if not k.startswith("_") and k != "points"}
    if with_points:
        out["points"] = w["points"]
        out["steps"] = [{"ts": s["ts"], "dpct": s["dpct"], "expected": s.get("expected"),
                         "external": s.get("external", 0.0),
                         "local_usd": sum(v for k, v in s["vec"].items() if k[1] == "usd")} for s in w["_steps"]]
    return out
