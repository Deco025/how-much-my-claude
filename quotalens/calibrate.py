"""额度分析：推算每个窗口值多少钱，并监控厂商是否暗调额度。

流程（按 工具 × scope × 窗口类型 分组）：
  1. 窗口实例：同一组里 resets_at 相同（允许漂移）的快照。花费归属：
       Codex   按 usage_window 链接精确归属（日志里每次请求带着它所消耗窗口的 resets_at）。
       Claude  按时间 [resets_at - 窗口长度, resets_at]；前提是只登录一个账号、不用 API key 跑 claude-*。
  2. 拆步：相邻两次百分比上涨之间，本地各模型花了多少。
  3. 疑似未记录消耗：用汇率算每步的预期涨幅，涨幅远超预期（或本地没花钱却涨了）的步只标记为疑点，
     不从已用% 里扣（可能是别处的使用，也可能只是日志和额度没对齐）。疑点多的窗口不参与校准和趋势。
     首个快照之前的累计是「未分段」样本，不参与疑点检测。
  3b. 估值条件：每个窗口给一个质量状态（见 QUALITY）。只有「可作条件估计」的窗口显示容量估值、
     参与汇率拟合和趋势。它需要用户的覆盖声明（同一账号、窗口开始前已生效）、采集无缺口、
     Claude 周来源分项里没有显著的其他来源。证据和声明冲突时以证据为准。
  4. 汇率：在干净窗口上做非负最小二乘  已用% ≈ Σ 汇率[模型] × 花费[模型]。
     实测额度消耗和 API 价格不成正比，不同模型同样花 $1 吃掉的额度可以差几倍。
  5. 趋势：每个窗口折算成「全用主力模型时，一个窗口值多少钱」，消除模型组合的影响。
     最近的窗口和之前几周对比，偏离超过噪声就判为收紧 / 放宽。
     断档：之前几周没有可比的窗口（停用、退订过一段时间）时，拿断档前最后几个窗口当基线，
     不让断档期间发生的调整被悄悄吸收成「新常态」。
  6. 换代：最近窗口和基线窗口的主力模型不同（几乎没有共同模型）时，额度变化和模型差异分不开，
     只给按 API 等价金额的粗略对比，不报警。

百分比的两个毛病：只有整数精度（推算给区间），并行会话会报旧值（取单调最大值）。
"""
import statistics
import time

from . import db

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

ANALYSIS_DAYS = 60       # 汇率拟合和趋势图的时间范围（断档前用作基线的窗口另外补进来）
MIN_FIT_PCT = 5          # 已用% 太小的窗口分辨率不够，不参与拟合
MIN_TREND_PCT = 10       # 参与趋势判断的最低已用%
MIN_MODEL_WINDOWS = 2    # 模型至少出现在几个窗口里才单独估计汇率，否则并入「其他」
OTHER = "(其他)"

EXTERNAL_MIN_STEP = 3    # 单步至少涨这么多 % 才可能判为外部消耗
EXTERNAL_FACTOR = 3      # 且超过预期涨幅的 3 倍再加 2 个点
EXTERNAL_SLACK = 2
IDLE_MIN_STEP = 2        # 本地完全没花钱时涨这么多 % 就算外部消耗
CONTAMINATED_SHARE = 0.15  # 疑似未记录消耗占已用% 达到这个比例，窗口不参与校准和趋势
ALIGN_SECONDS = 600        # 疑点出现在最近这么久内：可能只是日志和额度还没对齐

# 来源分项（Claude 周窗口）：占已用部分的百分比，整数取整
SOURCE_SHARE_LIMIT = 5.0   # 显著影响阈值（待回放验证的产品假设），按非 Code 合计的上限算
BREAKDOWN_STALE = 2 * 3600  # 周分项比窗口最后一次快照早这么久：期间的来源说不清
COLLECTED_SOURCES = {"claude_code"}   # 本项目能采集的来源（分不出设备）
KNOWN_UNCOLLECTED = {"chat", "cowork"}  # 已知、但本项目采集不到的来源；其余（other、未知新产品）算来源不明

# 窗口质量状态，严重的在前
QUALITY = ["incomplete", "source_unknown", "aligning", "waiting", "unconfirmed", "conditional"]
ANALYSIS_VERSION = 4       # 1 = 扣除推测的外部消耗；2 = 不扣除，加估值条件；3 = 周分项历史从原始响应补全；
                           # 4 = 趋势改用每个窗口的估值（确定 + 推测），不再要求覆盖声明；可设分析起点

MIN_CHANGE = 0.20        # 低于这个幅度的变化不报（单窗口噪声约 ±20%）
CHANGE_Z = 2.5
DEFAULT_SPREAD = 0.20    # 基线窗口太少、算不出离散度时的假设值

# 最近 vs 基线的取法。5 小时窗口：最近 72 小时 vs 之前 3 周；长窗口：最新 1 个 vs 6 周内的前几个。
# 基线不够时往前找，取断档前最后 fallback 个窗口
SHORT_RULE = {"recent": 72 * 3600, "baseline": 21 * 86400, "min_recent": 3, "min_baseline": 5, "fallback": 10}
LONG_RULE = {"baseline": 42 * 86400, "min_recent": 1, "min_baseline": 2, "fallback": 4}
MODEL_OVERLAP = 0.5      # 最近窗口的花费里，至少一半来自基线期也在用的模型，前后才可比
SHARED_MODEL_SHARE = 0.10  # 模型在基线期花费里占到这么多，才算「基线期也在用」


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
            # 第一个快照就已经有用量：窗口起点到这里的累计没被分步观察过（未分段）
            steps.append({"ts": s["ts"], "dpct": pct - running, "vec": pending, "unsegmented": not points[:-1]})
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
        "unsegmented_pct": steps[0]["dpct"] if steps and steps[0]["unsegmented"] else 0.0,
        "quality": None, "reasons": [], "claimed": False, "bias_pct": None, "sources": None, "estimate": None,
        "cap": None, "cap_low": None, "cap_high": None, "confidence": None,
        "value": None, "index": None, "in_trend": False,
        # 窗口键：首次看到的重置时间不随后续快照漂移，用来记住用户「不参与分析」的选择
        "key": f"{inst['scope']}:{inst['window']}:{int(inst['reset_min'])}",
        "_reset_min": inst["reset_min"], "_reset_max": inst["reset_max"],
        "excluded": False, "role": None,
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
        if s.get("unsegmented"):
            ext = 0.0
        elif local <= 0:
            ext = s["dpct"] if s["dpct"] >= IDLE_MIN_STEP else 0.0
        elif s["dpct"] >= EXTERNAL_MIN_STEP and s["dpct"] >= EXTERNAL_FACTOR * expected + EXTERNAL_SLACK:
            ext = s["dpct"] - expected
        else:
            ext = 0.0
        s["expected"], s["external"] = expected, ext
        external += ext
    # 对齐：额度更新可能滞后于日志，一步涨得多、下一步没花钱也涨，合起来却符合预期。
    # 所以疑点总量不超过整个窗口累计的超出部分（有汇率时）；各步按比例缩小
    pct = w["used_percent"]
    if fit and external > 0:
        excess = max(0.0, pct - _expected(w["_vec"], fit)[0])
        if excess < external:
            for s in w["_steps"]:
                s["external"] *= excess / external
            external = excess
    # 只标记，不扣除：external_pct 是「疑似未记录消耗」，pct_clean 始终等于已用%
    w["external_pct"] = min(external, pct)
    w["pct_clean"] = pct
    w["contaminated"] = pct > 0 and w["external_pct"] / pct >= CONTAMINATED_SHARE


def _set_cap(w):
    """只有可作条件估计的窗口才有容量数字；其余窗口一律为空，API 和页面都拿不到。"""
    for k in ("cap", "cap_low", "cap_high", "confidence"):
        w[k] = None
    pct, cost = w["pct_clean"], w["cost_at_snapshot"]
    if w["supported"] and w.get("quality") == "conditional" and pct >= 1 and cost > 0:
        w["cap"] = cost / (pct / 100)
        w["cap_low"] = cost / ((pct + 0.5) / 100)
        w["cap_high"] = cost / ((pct - 0.5) / 100)
        w["confidence"] = confidence(pct)


ESTIMATE_MIN_CODE = 20     # Code 占比低于这个 %，推测部分是确定部分的 4 倍以上，不再给总量
ESTIMATE_MIN_CODE_LOW = 10  # 考虑取整误差后 Code 占比的下限低于这个 %，区间上沿没有意义，也不给
ESTIMATE_BLOCKERS = {"breakdown_invalid", "breakdown_stale", "collect_gap", "suspect_unrecorded", "aligning"}
SPLIT_MAX_GAP = 1800       # 5h 拆分：窗口起止离最近一组周分项最多这么久（秒）
SPLIT_MIN_WEEKLY = 3       # 窗口期间周% 至少涨这么多才拆（周% 和分项都是整数，涨得太少分不清）
SPLIT_START_PCT = 5        # 窗口开始前没有分项时，用窗口内第一组分项当起点，要求那时 5h 已用不超过这个 %
POLL_PAIR = 10             # 分项的 as_of 是服务器时间，和同一次查询的本机快照时刻差几秒（实测 -6.5～+4.5）
ESTIMATE_CONFLICT = 3      # 无本机日志的涨幅比非 Code 能解释的还多这么多 %：Code 可能也在别的设备上用过


def _weekly_samples(weeks, ctx):
    """周分项 × 周已用% → 时间线：每个时刻的周%、非 Code 百分点（非 Code 占比 × 周%）及其取整误差。"""
    out = []
    for w in weeks:
        for b in ctx["breakdowns"]:
            if b["invalid"] or not _same_week(b, w):
                continue
            # [as_of, last_as_of] 期间每次查询的占比都没变：这段里的每个周% 快照都能算出非 Code 百分点
            ts = {b["as_of"], b["last_as_of"]} | {p["ts"] for p in w["points"] if b["as_of"] <= p["ts"] <= b["last_as_of"]}
            for t in ts:
                wpct = _pct_at(w["points"], t + POLL_PAIR)   # 配同一次查询的周%，而不是上一次的
                if wpct is None:
                    continue
                out.append({"t": t, "week": w["start"], "w": wpct, "pp": b["noncode"] / 100 * wpct,
                            "err": b["noncode_err"] / 100 * wpct + 0.5 * b["noncode"] / 100})
    out.sort(key=lambda x: x["t"])
    return out


def _window_split(w, samples):
    """5 小时窗口里 Code 占多少：看同一段时间周额度涨了多少、其中多少是非 Code 涨的。

    5h% 和周% 由同一批用量推动，所以窗口内非 Code 的份额 ≈ 非 Code 百分点增量 / 周% 增量
    （真实数据回放：只用 Code 的窗口每 1% 约 $0.39，按此拆出的混用窗口约 $0.42）。
    返回 {"code", "code_low", "code_high", "weekly_delta", "noncode_delta", "from", "to"}，拆不了返回 None。
    """
    end = [x for x in samples if w["start"] < x["t"] <= w["last_seen"] + 60]
    if not end or w["last_seen"] - end[-1]["t"] > SPLIT_MAX_GAP:
        return None
    s1 = end[-1]
    before = [x for x in samples if x["t"] <= w["start"]]
    if before and w["start"] - before[-1]["t"] <= SPLIT_MAX_GAP:
        s0, h0 = before[-1], 0.0
    else:
        s0 = end[0] if end[0] is not s1 else None
        h0 = _pct_at(w["points"], s0["t"]) if s0 else None
        if h0 is None or h0 > SPLIT_START_PCT:
            return None
    if s0["week"] == s1["week"]:
        dw, dn = s1["w"] - s0["w"], s1["pp"] - s0["pp"]
        errs = [s0["err"], s1["err"]]
    else:   # 周额度在这段里重置：旧周涨的 + 新周从 0 涨的
        e = [x for x in samples if x["week"] == s0["week"] and s0["t"] <= x["t"] <= s1["t"]][-1]
        dw, dn = e["w"] - s0["w"] + s1["w"], e["pp"] - s0["pp"] + s1["pp"]
        errs = [s0["err"], e["err"], s1["err"]]
    if dw < SPLIT_MIN_WEEKLY:
        return None
    # 最坏情况：每个周% 都是取整值（±0.5），每个非 Code 百分点各自带取整误差，全部同向
    ew, en = 0.5 * len(errs), sum(errs)
    clamp = lambda x: min(1.0, max(0.0, x))
    f, f_lo, f_hi = clamp(dn / dw), clamp((dn - en) / (dw + ew)), clamp((dn + en) / (dw - ew))
    return {"code": 100 * (1 - f), "code_low": 100 * (1 - f_hi), "code_high": 100 * (1 - f_lo),
            "weekly_delta": dw, "noncode_delta": dn, "from": s0["t"], "to": s1["t"]}


def _set_estimate(w):
    """Claude 窗口：总花费 = 确定（本机 Code 日志 × 官方价）+ 推测（其他来源按同样的额度/美元比例折算）。
    周窗口的 Code 占比直接来自官方分项；5 小时窗口用窗口期间周分项的变化拆（见 _window_split）。
    Codex 拿不到分项：假设这个账号的用量都在本机（Code 占 100%），无本机日志的涨幅由 suspect_unrecorded 拦下。
    前提：Code 只在这台电脑上用；其他来源每 1% 额度和 Code 等价。和 cap 不同，不要求覆盖声明。
    没有估值时把原因写进 w["_est_why"]，趋势卡片据此说明为什么不判断。"""
    w["estimate"], w["_est_why"] = None, None
    if not w["supported"]:
        w["_est_why"] = "unsupported"
        return
    if w.get("tool") == "codex":
        code = code_low = code_high = 100.0
        basis, extra = "local_only", {}
    elif w["window"] == "seven_day" and w.get("sources"):
        code = w["sources"]["code"]
        code_low, code_high, basis, extra = max(0.0, code - 0.5), min(100.0, code + 0.5), "breakdown", {}
    elif w["window"] == "five_hour" and w.get("_split"):
        sp = w["_split"]
        code, code_low, code_high, basis = sp["code"], sp["code_low"], sp["code_high"], "window_delta"
        extra = {"weekly_delta": sp["weekly_delta"], "noncode_delta": sp["noncode_delta"]}
    else:
        w["_est_why"] = "no_split" if w["window"] == "five_hour" else "no_breakdown"
        return
    # 有来源分项（Claude）时，无本机日志的涨幅本来就应该来自网页、App、Cowork，不按「疑似未记录」拦，
    # 超出非 Code 能解释的部分另由 conflict_pct 判断；只有 Codex（假设全在本机）才按它拦
    skip = set() if basis == "local_only" else {"suspect_unrecorded"}
    blocker = next((r["code"] for r in w["reasons"] if r.get("code") in ESTIMATE_BLOCKERS - skip), None)
    if blocker:
        w["_est_why"] = blocker
        return
    pct, cost = w["used_percent"], w["cost_at_snapshot"]
    if pct < MIN_FIT_PCT or cost <= 0:
        w["_est_why"] = "too_little"
        return
    if code < ESTIMATE_MIN_CODE or code_low < ESTIMATE_MIN_CODE_LOW:
        w["_est_why"] = "code_share_low"
        return
    total = cost * 100 / code
    # 无本机日志的涨幅（external_pct）本该都是非 Code；比非 Code 最多能解释的还多，说明前提可能不成立，估值偏低
    conflict = w.get("external_pct", 0.0) - (100 - code_low) / 100 * pct
    w["estimate"] = {
        "basis": basis, **extra, "conflict_pct": conflict if conflict >= ESTIMATE_CONFLICT else 0.0,
        "local": cost, "inferred": total - cost, "total": total,
        "total_low": cost * 100 / code_high, "total_high": cost * 100 / code_low,
        "code_pct": code, "noncode_pct": 100 - code, "code_low": code_low, "code_high": code_high,
        "cap": total / (pct / 100),
        "cap_low": cost * 1e4 / ((pct + 0.5) * code_high),
        "cap_high": cost * 1e4 / ((pct - 0.5) * code_low),
    }


# ── 估值条件：覆盖声明、采集缺口、来源分项 ─────────────────

def _coverage_context(conn, tool):
    ctx = {"claims": db.coverage_claims(conn, tool), "accounts": db.accounts_seen(conn, tool),
           "gaps": db.collect_gaps(conn, tool), "breakdowns": []}
    if tool == "claude":
        ctx["breakdowns"] = [dict(b, **_source_shares(b["rows"], b.get("issues"))) for b in db.breakdowns(conn, tool)]
    return ctx


BREAKDOWN_BAD = {"not_object", "rows_missing", "bad_row"}   # 有行被丢掉或结构不对：整组说不清


def _source_shares(rows, issues=None):
    """一组分项 → Code / 已知未采集 / 来源不明的占比，以及算上取整误差后的上限。
    known_names / unknown_names：占比大于 0 的来源显示名，给界面说明原因用。"""
    code = known = unknown = 0.0
    k_known = k_unknown = 0
    invalid, known_names, unknown_names = bool(BREAKDOWN_BAD & set(issues or ())), [], []
    for r in rows:
        key, pct = r["key"], r["percent"]
        if pct is None or key.startswith("_"):
            invalid = True
            continue
        name = r.get("display_name") or key
        if key in COLLECTED_SOURCES:
            code += pct
        elif key in KNOWN_UNCOLLECTED:
            known, k_known = known + pct, k_known + 1
            if pct > 0:
                known_names.append(name)
        else:
            unknown, k_unknown = unknown + pct, k_unknown + 1
            if pct > 0:
                unknown_names.append(name)
    # 合计明显超过 100：分项本身有问题；明显不足 100：缺的部分来源不明（不自动补齐）
    if code + known + unknown - 0.5 * len(rows) > 100:
        invalid = True
    missing = 100 - (code + known + unknown) - 0.5 * len(rows)
    if missing > 0 and not invalid:
        unknown += missing
    return {"code": code, "known": known, "unknown": unknown, "invalid": invalid,
            "known_hi": known + 0.5 * k_known, "unknown_hi": unknown + 0.5 * k_unknown,
            "noncode": known + unknown, "noncode_err": 0.5 * (k_known + k_unknown),
            "known_names": known_names, "unknown_names": unknown_names}


def _window_account(w, accounts):
    """窗口开始前最后一次看到的账号，加上窗口期间看到的账号。不止一个说明期间换过账号。"""
    seen = {a for ts, a in accounts if w["start"] <= ts <= w["last_seen"]}
    before = [a for ts, a in accounts if ts < w["start"]]
    if before:
        seen.add(before[-1])
    return seen


def _claim_check(w, ctx):
    """返回 (适用的声明或 None, 不适用的原因)。

    声明绑定账号，并且只对开始时间不早于生效时刻、且声明持续到窗口最后一次快照的窗口适用。
    """
    claims = ctx["claims"]
    if not claims:
        return None, {"code": "no_claim"}
    if all(c["start"] > w["start"] for c in claims):
        return None, {"code": "claim_after_start"}
    seen = _window_account(w, ctx["accounts"])
    if len(seen) > 1:
        return None, {"code": "account_changed"}
    if not seen:
        return None, {"code": "account_unknown"}
    acct = next(iter(seen))
    mine = [c for c in claims if c["account"] == acct]
    if not mine:
        return None, {"code": "claim_other_account"}
    until = min(w["end"], w["last_seen"])
    for c in mine:
        if c["start"] <= w["start"] and (c["end"] is None or c["end"] >= until):
            return c, None
    if any(c["start"] > w["start"] and (c["end"] is None or c["end"] >= until) for c in mine):
        return None, {"code": "claim_after_start"}
    return None, {"code": "claim_ended"}


def _same_week(b, w):
    return (w["start"] - RESET_JITTER <= b["as_of"] <= w["end"] + RESET_JITTER
            and (b["window_start"] is None or abs(b["window_start"] - w["start"]) <= RESET_JITTER))


def _weekly_breakdown(w, ctx):
    """周窗口内、不晚于最后一次额度快照的最新一组分项。"""
    hits = [b for b in ctx["breakdowns"] if _same_week(b, w) and b["as_of"] <= w["last_seen"] + RESET_JITTER]
    return hits[-1] if hits else None


def _pct_at(points, t):
    """窗口在 t 时刻的已用%；t 早于第一个快照返回 None。"""
    pct = None
    for p in points:
        if p["ts"] > t:
            break
        pct = p["pct"]
    return pct


def _noncode_intervals(weeks, ctx):
    """用相邻分项之间「非 Code 百分点」（非 Code 占比 × 周已用%）的增量定位非 Code 使用的时段。

    weeks：Claude 总额度的周窗口。返回 (located, flagged, ambiguous)，都是 [(start, end)]：
    located = 分项能说明情况的时段；flagged = 非 Code 百分点明显上涨；ambiguous = 只有取整级别的变化。
    """
    located, flagged, ambiguous = [], [], []
    for w in weeks:
        # 窗口起点：非 Code 百分点为 0
        prev_t, prev_pp, prev_err = w["start"], 0.0, 0.0
        for b in (b for b in ctx["breakdowns"] if _same_week(b, w)):
            for t in (b["as_of"], b["last_as_of"]):
                if t <= prev_t:
                    continue
                wpct = _pct_at(w["points"], t)
                if wpct is None or b["invalid"]:
                    prev_t, prev_pp, prev_err = t, None, None   # 这一段说不清
                    continue
                pp = b["noncode"] / 100 * wpct
                err = b["noncode_err"] / 100 * wpct + 0.5 * b["noncode"] / 100
                if prev_pp is not None:
                    delta = pp - prev_pp
                    located.append((prev_t, t))
                    if delta > prev_err + err:
                        flagged.append((prev_t, t))
                    elif delta > 1e-9:
                        ambiguous.append((prev_t, t))
                prev_t, prev_pp, prev_err = t, pp, err
    return located, flagged, ambiguous


def _overlaps(a0, a1, spans):
    return any(s < a1 and e > a0 for s, e in spans)


def _covered(a0, a1, spans):
    """[a0, a1] 是否被 spans 的并集完全覆盖。"""
    t = a0
    for s, e in sorted(spans):
        if s > t:
            break
        t = max(t, e)
    return t >= a1


def _assess(w, ctx, noncode):
    """给窗口定质量状态（QUALITY）和原因。证据优先：阻断条件出现时，有没有覆盖声明都不估值。"""
    if not w["supported"]:
        return
    reasons, level = [], "conditional"

    def cap(lv, reason):
        nonlocal level
        reasons.append(reason)
        if QUALITY.index(lv) < QUALITY.index(level):
            level = lv

    until = min(w["end"], w["last_seen"])
    gaps = [g for g in ctx["gaps"] if g["start"] < until and g["end"] > w["start"]]
    if gaps:
        cap("incomplete", {"code": "collect_gap", "kinds": sorted({g["kind"] for g in gaps})})

    if w["tool"] == "claude":
        if w["window"] == "seven_day":
            b = _weekly_breakdown(w, ctx)
            if b is None:
                cap("unconfirmed", {"code": "no_breakdown"})
            else:
                if w["last_seen"] - b["last_as_of"] > BREAKDOWN_STALE:
                    # 分项停在较早的时刻：之后的使用来自哪里说不清
                    cap("unconfirmed", {"code": "breakdown_stale", "as_of": b["last_as_of"]})
                w["sources"] = {"as_of": b["as_of"], "last_as_of": b["last_as_of"], "rows": b["rows"],
                                "code": b["code"], "known": b["known"], "unknown": b["unknown"]}
                if b["invalid"] and w["used_percent"] >= 1:
                    cap("source_unknown", {"code": "breakdown_invalid"})
                if b["unknown_hi"] >= SOURCE_SHARE_LIMIT:
                    cap("source_unknown", {"code": "unknown_sources", "share": b["unknown"], "names": b["unknown_names"]})
                if b["known_hi"] >= SOURCE_SHARE_LIMIT:
                    cap("incomplete", {"code": "uncollected_sources", "share": b["known"], "names": b["known_names"]})
                elif b["unknown_hi"] < SOURCE_SHARE_LIMIT and b["known_hi"] + b["unknown_hi"] >= SOURCE_SHARE_LIMIT:
                    # 单看都不到阈值，但非 Code 合计到了
                    cap("incomplete", {"code": "uncollected_sources", "share": b["noncode"],
                                       "names": b["known_names"] + b["unknown_names"]})
                if b["noncode"] > 0:
                    w["bias_pct"] = b["noncode"]   # 估值至少偏低这么多（Code 分项里别的设备还算不进来）
        elif w["window"] == "five_hour" and noncode is not None:
            located, flagged, ambiguous = noncode
            if _overlaps(w["start"], until, flagged):
                cap("incomplete", {"code": "noncode_in_window"})
            elif _overlaps(w["start"], until, ambiguous) or not _covered(w["start"], until, located):
                cap("unconfirmed", {"code": "noncode_unlocated"})

    if w["contaminated"]:
        recent = [st for st in w["_steps"]
                  if st.get("external", 0) > 0 and w["last_seen"] - st["ts"] <= ALIGN_SECONDS]
        if w["active"] and recent:
            cap("aligning", {"code": "aligning"})
        else:
            cap("unconfirmed", {"code": "suspect_unrecorded", "pct": w["external_pct"]})

    if w["used_percent"] < MIN_FIT_PCT or w["cost_at_snapshot"] <= 0:
        cap("waiting", {"code": "waiting"})

    claim, why = _claim_check(w, ctx)
    w["claimed"] = claim is not None
    if claim is None:
        cap("unconfirmed", why)
    elif level in ("incomplete", "source_unknown"):
        reasons.append({"code": "claim_conflict"})   # 声明了覆盖完整，但证据显示有别的来源
    w["quality"], w["reasons"] = level, reasons


# ── 趋势与变化判断 ───────────────────────────────────────

def _robust_spread(values):
    if len(values) < 3:
        return DEFAULT_SPREAD
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    return max(1.4826 * mad / med, 0.05) if med else DEFAULT_SPREAD


def _split(windows, window_seconds, now):
    """按对比规则分成 (最近, 近期基线, 最近之前的全部窗口按时间排序, 规则)。"""
    if window_seconds <= 6 * 3600:
        rule = SHORT_RULE
        cut = now - rule["recent"]
        recent = [w for w in windows if w["last_seen"] >= cut]
        older = sorted((w for w in windows if w["last_seen"] < cut), key=lambda w: w["last_seen"])
        baseline = [w for w in older if w["last_seen"] >= now - rule["baseline"]]
    else:
        rule = LONG_RULE
        ordered = sorted(windows, key=lambda w: w["end"])
        recent, older = ordered[-1:], ordered[:-1]
        baseline = [w for w in older if w["end"] >= now - rule["baseline"]][-4:]
    return recent, baseline, older, rule


def _gap_anchors(windows, window_seconds, now):
    """近期基线不够时（中间停用过），断档前最后几个可用窗口，补进汇率拟合和趋势。"""
    candidates = [w for w in windows if w["used_percent"] >= MIN_TREND_PCT]
    _, baseline, older, rule = _split(candidates, window_seconds, now)
    if len(baseline) >= rule["min_baseline"]:
        return []
    # 多留几个：拟合后可能有窗口被判为外部消耗污染
    return older[-(rule["fallback"] + 4):]


def _spend_shares(windows):
    total = {}
    for w in windows:
        for (model, unit), v in w["_vec"].items():
            if unit == "usd":
                total[model] = total.get(model, 0.0) + v
    s = sum(total.values())
    return {m: v / s for m, v in total.items()} if s > 0 else {}


def _model_overlap(recent, baseline):
    """(最近窗口的花费里来自基线期也在用的模型的比例, 基线主力模型, 最近主力模型)。"""
    r, b = _spend_shares(recent), _spend_shares(baseline)
    if not r or not b:
        return 1.0, None, None
    shared = sum(v for m, v in r.items() if b.get(m, 0.0) >= SHARED_MODEL_SHARE)
    return shared, max(b, key=b.get), max(r, key=r.get)


DEFAULT_PARAMS = {"min_change": MIN_CHANGE, "model_overlap": MODEL_OVERLAP}


def _change_status(trend, window_seconds, now, params=DEFAULT_PARAMS):
    """最近的窗口 vs 之前的基线：stable / tighter / looser / model_changed / insufficient。

    params 来自页面「设置」：min_change 判为被调的最小幅度，model_overlap 共同模型低于多少算换了模型。
    """
    recent, baseline, older, rule = _split(trend, window_seconds, now)
    cross_gap = False
    if len(baseline) < rule["min_baseline"] and len(older) >= rule["min_baseline"]:
        baseline, cross_gap = older[-rule["fallback"]:], True
    out = {"recent_n": len(recent), "baseline_n": len(baseline), "status": "insufficient",
           "ratio": None, "threshold": None, "recent_median": None, "baseline_median": None,
           "cross_gap": cross_gap, "baseline_from": None, "baseline_to": None, "gap_days": None,
           "model_before": None, "model_after": None, "model_overlap": None,
           "raw_ratio": None, "raw_recent": None, "raw_baseline": None}
    if baseline:
        out["baseline_from"] = min(w["start"] for w in baseline)
        out["baseline_to"] = max(w["end"] for w in baseline)
    if len(recent) < rule["min_recent"] or len(baseline) < rule["min_baseline"]:
        return out
    for w in recent:
        w["role"] = "recent"
    for w in baseline:
        w["role"] = "baseline"
    if cross_gap:
        out["gap_days"] = max(0.0, (min(w["start"] for w in recent) - out["baseline_to"]) / 86400)

    r_vals, b_vals = [w["value"] for w in recent], [w["value"] for w in baseline]
    r_med, b_med = statistics.median(r_vals), statistics.median(b_vals)
    ratio = r_med / b_med
    threshold = max(params["min_change"],
                    CHANGE_Z * _robust_spread(b_vals) * (1 / len(r_vals) + 1 / len(b_vals)) ** 0.5)
    out.update(ratio=ratio, threshold=threshold, recent_median=r_med, baseline_median=b_med)

    # 换代：前后几乎没有共同模型时，汇率回归会把额度变化当成「新模型本来就贵」吸收掉，分不开
    overlap, before, after = _model_overlap(recent, baseline)
    out.update(model_overlap=overlap, model_before=before, model_after=after)
    if overlap < params["model_overlap"]:
        raw_r = [w["estimate"]["cap"] for w in recent if w["estimate"]]
        raw_b = [w["estimate"]["cap"] for w in baseline if w["estimate"]]
        if raw_r and raw_b:
            out.update(raw_recent=statistics.median(raw_r), raw_baseline=statistics.median(raw_b))
            out["raw_ratio"] = out["raw_recent"] / out["raw_baseline"]
        out["status"] = "model_changed"
        return out
    out["status"] = "tighter" if ratio < 1 - threshold else "looser" if ratio > 1 + threshold else "stable"
    return out


def _no_assess(w):
    """没有覆盖信息时（直接调用 _analyze_group 的测试）：只按数据本身定状态，不当作有声明。"""
    _assess(w, {"claims": [], "accounts": [], "gaps": [], "breakdowns": []}, None)


def _analyze_group(windows, now, params=DEFAULT_PARAMS, assess=_no_assess):
    """同一 (scope, 窗口类型, 方案) 的全部窗口：标记疑点、定估值条件、拟合汇率、折算价值、判断变化。"""
    usable = [w for w in windows if not w["excluded"]]
    recent = [w for w in usable if w["end"] >= now - ANALYSIS_DAYS * 86400]
    anchors = [w for w in _gap_anchors(usable, windows[0]["window_seconds"], now) if w not in recent]
    fit_set = recent + anchors
    # 第一轮：所有窗口粗拟合 → 标记疑点、定估值条件；
    # 第二轮：只用「可作条件估计」的窗口拟合。没有这样的窗口时，用无疑点窗口做观察拟合，只给详情页看
    fit = _fit_rates([w for w in fit_set if w["used_percent"] >= MIN_FIT_PCT], "used_percent")
    for w in windows:
        _detect_external(w, fit)
        assess(w)
    basis = "conditional"
    clean_fit = _fit_rates([w for w in fit_set if w["quality"] == "conditional"], "used_percent")
    if not clean_fit:
        basis = "observed"
        clean_fit = _fit_rates([w for w in fit_set if not w["contaminated"] and w["used_percent"] >= MIN_FIT_PCT],
                               "used_percent")
    if clean_fit:
        fit = clean_fit
        for w in windows:
            _detect_external(w, fit)
            assess(w)
    for w in windows:
        _set_cap(w)
        _set_estimate(w)

    group = {"ref_model": None, "ref_cap": None, "rates": [], "fit_windows": 0, "median_error_pct": None,
             "fit_basis": basis if fit else None, "analysis_version": ANALYSIS_VERSION}
    if fit:
        group["fit_windows"] = fit["windows"]
        if fit["median_error"] is not None:
            group["median_error_pct"] = fit["median_error"] * 100
        for (model, unit), rate in fit["rates"].items():
            group["rates"].append({"model": model, "unit": unit, "windows": fit["presence"][(model, unit)],
                                   "pct_per_unit": rate, "cap_if_only": 100 / rate if rate > 1e-9 else None})
        group["rates"].sort(key=lambda r: -r["windows"])
        # 主力模型：最近这段时间（不含断档前补进来的窗口）干净窗口里花费最多、且有汇率的有价模型
        spend = {}
        for w in (recent or fit_set):
            if not w["contaminated"]:
                for k, v in w["_vec"].items():
                    if k[1] == "usd" and fit["rates"].get(k, 0) > 0:
                        spend[k] = spend.get(k, 0.0) + v
        if spend:
            ref = max(spend, key=spend.get)
            group["ref_model"], group["ref_cap"] = ref[0], 100 / fit["rates"][ref]
    # 趋势：每个窗口用自己的估值（用满约值多少美元，API 等价，确定 + 推测）。
    # 不要求覆盖声明；分析起点之前的窗口（采集方式不同的旧数据）不参与
    # 有主力模型时按拟合汇率把每个窗口的估值折算成「全用主力模型」的金额，模型组合的变化（例如便宜模型的
    # 占比变大）不会被当成额度变化；某个窗口里没有汇率的花费太多，折算不可靠，这个窗口不进趋势。
    # 估值和本机记录冲突（conflict_pct > 0，估值偏低）的窗口也不进趋势，免得把结论推向「收紧」
    since = params.get("since") or 0
    in_range = {id(w) for w in fit_set}
    ref = (group["ref_model"], "usd") if group["ref_model"] else None
    group["value_model"] = group["ref_model"]
    for w in windows:
        est = w["estimate"]
        if est is None:
            continue
        w["value"] = est["cap"]
        if ref:
            usd = {k: v for k, v in w["_vec"].items() if k[1] == "usd"}
            expected, covered = _expected(usd, fit)
            local = sum(usd.values())
            if local > 0 and expected > 0 and covered / expected >= 0.7:
                w["value"] = est["cap"] * (expected / fit["rates"][ref]) / local
            else:
                w["value"], w["_est_why"] = None, "model_unpriced"
                continue
        if est["conflict_pct"] > 0:
            w["_est_why"] = "estimate_conflict"
        w["in_trend"] = (not w["excluded"] and w["start"] >= since and not est["conflict_pct"]
                         and w["used_percent"] >= MIN_TREND_PCT and id(w) in in_range)
    group["since"] = since
    group["estimated_n"] = sum(1 for w in windows if w["in_trend"])   # 能参与比较的窗口数
    trend = [w for w in windows if w["in_trend"]]
    group.update(_change_status(trend, windows[0]["window_seconds"], now, params))
    _eligibility(group, usable, windows[0]["window_seconds"], now, since)
    if basis != "conditional":
        # 观察拟合只说明大致比例，不能当成账号容量：金额类字段一律不输出
        group["ref_cap"] = None
        for r in group["rates"]:
            r["pct_per_unit"] = r["cap_if_only"] = None
    return group


def _eligibility(group, usable, window_seconds, now, since=0):
    """趋势资格：收紧/放宽只能来自基线和待检测窗口都有估值的比较。
    样本不够时区分「不可判断」（窗口有，但没有估值）和「历史基线不足」（窗口本身就不够）。
    declared：参与比较的窗口全部在覆盖声明下（估值里没有推测成分的前提）。只有这样的结论才写告警、弹通知；
    其余结论只在页面上显示，标为「推测」。"""
    # 独立复核：参与比较的窗口必须全部有估值、在分析起点之后、各自数量够，才算有资格
    compared = [w for w in usable if w["role"] in ("recent", "baseline")]
    rule = SHORT_RULE if window_seconds <= 6 * 3600 else LONG_RULE
    group["eligible"] = (group["status"] in ("stable", "tighter", "looser")
                         and all(w["estimate"] is not None and w["in_trend"] for w in compared)
                         and sum(w["role"] == "recent" for w in compared) >= rule["min_recent"]
                         and sum(w["role"] == "baseline" for w in compared) >= rule["min_baseline"])
    group["declared"] = group["eligible"] and all(w["quality"] == "conditional" for w in compared)
    group["blocked"] = {}
    if group["status"] != "insufficient":
        return
    cands = [w for w in usable if w["supported"] and w["used_percent"] >= MIN_TREND_PCT
             and w["end"] >= now - ANALYSIS_DAYS * 86400 and w["start"] >= since]
    recent, baseline, older, rule = _split(cands, window_seconds, now)
    blocked = [w for w in recent + baseline if w["estimate"] is None or w.get("_est_why")]
    for w in blocked:
        code = w.get("_est_why") or "no_estimate"
        group["blocked"][code] = group["blocked"].get(code, 0) + 1
    if blocked and (len(recent) >= rule["min_recent"]
                    and max(len(baseline), len(older)) >= rule["min_baseline"]):
        group["status"] = "not_eligible"


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


def analyze(conn, tool, now=None, params=None):
    """返回 {"windows": [...], "groups": [...]}；窗口按开始时间排序。

    不同订阅方案的额度本来就不同，所以分组键里带上方案，换方案不会被当成暗调。
    用户在页面上标了「不参与分析」的窗口不进拟合和趋势；整个方案标了的，分组状态记为 ignored、不报警。
    """
    now = now or time.time()
    params = {**DEFAULT_PARAMS, **(params or {})}
    rules = db.data_rules(conn, tool)
    windows = [_build_window(conn, tool, inst, now) for inst in instances_for(conn, tool)]
    _fill_plans(windows)
    for w in windows:
        w["excluded"] = w["key"] in rules["windows"]
    ctx = _coverage_context(conn, tool)
    noncode = None
    if tool == "claude":
        noncode = _noncode_intervals([w for w in windows if w["window"] == "seven_day" and w["scope"] == "all"], ctx)
        weeks = [w for w in windows if w["window"] == "seven_day" and w["scope"] == "all"]
        samples = _weekly_samples(weeks, ctx)
        for w in windows:
            if w["window"] == "five_hour" and w["scope"] == "all":
                w["_split"] = _window_split(w, samples)

    def assess(w):
        _assess(w, ctx, noncode)
    by_key = {}
    for w in windows:
        by_key.setdefault((w["scope"], w["window"], w["plan_type"]), []).append(w)
    groups = []
    for (scope, window, plan), ws in by_key.items():
        group = _analyze_group(ws, now, params, assess) if ws[0]["supported"] else {"status": "unsupported", "rates": []}
        if rules["plans"].get(plan, (None,))[0] == "ignore":
            group["status"], group["eligible"], group["declared"] = "ignored", False, False
        group.update(tool=tool, scope=scope, window=window, plan_type=plan, window_seconds=ws[0]["window_seconds"],
                     last_seen=max(w["last_seen"] for w in ws), latest_end=max(w["end"] for w in ws),
                     windows_n=len(ws))
        # 趋势图：近 60 天，加上断档前被拿来当基线的窗口
        group["trend"] = [{"start": w["start"], "end": w["end"], "last_seen": w["last_seen"], "value": w["value"],
                           "pct": w["used_percent"], "pct_clean": w["pct_clean"], "external_pct": w["external_pct"],
                           "contaminated": w["contaminated"], "in_trend": w["in_trend"], "cost": w["cost_at_snapshot"],
                           "excluded": w["excluded"], "role": w["role"], "key": w["key"],
                           "quality": w["quality"], "claimed": w["claimed"],
                           "cap_low": w["estimate"]["cap_low"] if w["estimate"] else None,
                           "cap_high": w["estimate"]["cap_high"] if w["estimate"] else None}
                          for w in ws if w["value"] is not None
                          and (w["end"] >= now - ANALYSIS_DAYS * 86400 or w["role"] == "baseline")]
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
                         "external": s.get("external", 0.0), "unsegmented": s.get("unsegmented", False),
                         "local_usd": sum(v for k, v in s["vec"].items() if k[1] == "usd")} for s in w["_steps"]]
    return out
