"""演示数据：一个虚构的 Codex Plus + Claude Pro 用户最近两个月的用量，用来试用界面和截图。

  how-much-my-claude-server --demo --open     （从源码运行：python run.py --demo --open）

数据放在临时目录、按「现在」生成，不读任何真实日志，不查额度，不联网。剧情：
  - 两个工具都在数据开始前打开了覆盖声明（虚构账号），所以窗口可作条件估计；
  - Codex 的 5 小时窗口最近 3 天被悄悄收紧了 30%（会触发告警），每周窗口没变；
  - Claude 两个窗口都没变。11 天前在网页上聊了一会儿：周来源分项里出现了 Chats 占比，
    那一周和那段时间的 5 小时窗口进入「采集不完整」，不估值、不参与趋势（网页用量不会被扣掉）。
"""
import math
import random
import time

from . import db
from .pricing import PriceTable

FIVE_HOURS, WEEK = 5 * 3600, 7 * 86400
DAYS = 63

# 「真实」额度：一个窗口全用这个模型，能花掉多少美元的等价 API
CODEX_5H = {"gpt-6-astra": 12.0, "gpt-6-sol": 8.5}
CODEX_WEEK = {"gpt-6-astra": 66.0, "gpt-6-sol": 47.0}
CLAUDE_5H = {"claude-opus-5-5": 26.0, "claude-sonnet-5-5": 36.0}
CLAUDE_WEEK = {"claude-opus-5-5": 240.0, "claude-sonnet-5-5": 330.0}
# (主力模型, 另一个模型)：主力模型的占比随时间慢慢变（大约 3 周多一个来回），和真人一样，
# 不同窗口的模型组合不同，汇率回归才分得开两个模型
CODEX_MODELS = ("gpt-6-astra", "gpt-6-sol")
CLAUDE_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5")
TIGHTENED_SINCE_DAYS = 3
TIGHTEN_TO = 0.7

INSERT_USAGE = """
INSERT INTO usage (id, tool, ts, model, on_plan, session_id, input_tokens, cache_read_tokens, cache_write_5m_tokens,
                   cache_write_1h_tokens, output_tokens, reasoning_tokens, speed, cost_usd)
VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 'standard', ?)
"""
INSERT_SNAPSHOT = "INSERT OR IGNORE INTO quota_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
INSERT_LINK = "INSERT OR IGNORE INTO usage_window VALUES (?, 'codex', 'codex', ?, ?)"


class _Window:
    """一个额度窗口：第一次使用时开始，到点重置。worth 是这个窗口里各模型「值多少钱」。"""

    def __init__(self, length, worth):
        self.length, self.worth, self.reset, self.pct = length, worth, 0.0, 0.0

    def touch(self, ts, rng, scale=1.0):
        if ts >= self.reset:
            noise = rng.lognormvariate(0, 0.07)  # 每个窗口有 ±7% 左右的自然波动
            self.current = {m: v * noise * scale for m, v in self.worth.items()}
            self.reset, self.pct = ts + self.length, 0.0

    def spend(self, model, cost):
        self.pct = min(100.0, self.pct + 100 * cost / self.current[model])


def _sessions(rng, now, per_day, last_start):
    """每天 0～2 段使用，最后一段一直用到「现在」前后，「现在」卡片上才有进行中的窗口。"""
    out = []
    for day in range(DAYS, 0, -1):
        base = now - day * 86400
        for _ in range(rng.choices((0, 1, 2), weights=(1 - per_day, per_day * 0.7, per_day * 0.3))[0]):
            start = base + rng.uniform(9, 21) * 3600
            out.append((start, start + rng.uniform(1.2, 3.5) * 3600))
    out.append((now - last_start, now - 300))
    out.sort()
    # 最近 3 天每天至少用一次：5 小时窗口的「最近」样本要够
    for day in (2.6, 1.6):
        start = now - day * 86400
        if not any(start - 43200 < s < start + 43200 for s, _ in out):
            out.append((start, start + 2.5 * 3600))
    # 重叠的合并成一段：同一个人不会同时开两段，时间交错会被分析当成本地日志之外的跳涨
    merged = []
    for start, end in sorted(out):
        if merged and start < merged[-1][1] + 600:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _tokens(rng, tool):
    if tool == "codex":
        output = rng.randint(400, 3200)
        return {"input_tokens": rng.randint(1500, 8000), "cache_read": rng.randint(15000, 80000),
                "cache_write_5m": 0, "cache_write_1h": 0, "output_tokens": output, "reasoning": int(output * 0.35)}
    return {"input_tokens": rng.randint(200, 3000), "cache_read": rng.randint(50000, 180000),
            "cache_write_5m": rng.randint(0, 4000), "cache_write_1h": rng.randint(0, 6000),
            "output_tokens": rng.randint(800, 6000), "reasoning": 0}


def _pick(rng, models, ts):
    share = 0.55 + 0.4 * math.sin(2 * math.pi * ts / (24 * 86400))
    return models[0] if rng.random() < share else models[1]


def populate(now=None, seed=7) -> dict:
    """往当前数据库写一套演示数据，返回各表写入的行数。"""
    now = now or time.time()
    rng = random.Random(seed)
    prices = PriceTable()
    counts = {"usage": 0, "snapshots": 0}
    with db.writer() as conn:
        # ── Codex：日志里每次请求都带着两个窗口的百分比和重置时间 ──
        five, week = _Window(FIVE_HOURS, CODEX_5H), _Window(WEEK, CODEX_WEEK)
        n = 0
        for start, end in _sessions(rng, now, per_day=0.75, last_start=2.4 * 3600):
            ts = start
            while ts < end:
                five.touch(ts, rng, TIGHTEN_TO if ts > now - TIGHTENED_SINCE_DAYS * 86400 else 1.0)
                week.touch(ts, rng)
                if five.pct >= 100 or week.pct >= 100:
                    break  # 用满了，等重置
                model = _pick(rng, CODEX_MODELS, ts)
                tok = _tokens(rng, "codex")
                cost = prices.cost(model, input_tokens=tok["input_tokens"], cache_read=tok["cache_read"],
                                   output_tokens=tok["output_tokens"])
                uid = f"demo:codex:{n}"
                conn.execute(INSERT_USAGE, (uid, "codex", ts, model, f"s{int(start)}", tok["input_tokens"],
                                            tok["cache_read"], 0, 0, tok["output_tokens"], tok["reasoning"], cost))
                five.spend(model, cost)
                week.spend(model, cost)
                for w, name in ((five, "five_hour"), (week, "seven_day")):
                    conn.execute(INSERT_LINK, (uid, name, w.reset))
                    conn.execute(INSERT_SNAPSHOT, ("codex", "codex", name, ts, float(int(w.pct)), w.reset, w.length,
                                                   "plus", "log"))
                    counts["snapshots"] += 1
                n += 1
                ts += rng.uniform(180, 420)
        counts["usage"] += n

        # ── Claude：日志只有用量，百分比靠每 3 分钟查一次接口 ──
        five, week = _Window(FIVE_HOURS, CLAUDE_5H), _Window(WEEK, CLAUDE_WEEK)
        n, last_poll, web_chat_day = 0, 0.0, now - 11 * 86400
        chat_pp, chat_week = 0.0, None   # 本周网页聊天用掉的百分点
        for start, end in _sessions(rng, now, per_day=0.85, last_start=1.8 * 3600):
            ts, chatted = start, False
            while ts < end:
                five.touch(ts, rng)
                week.touch(ts, rng)
                if five.pct >= 100 or week.pct >= 100:
                    break
                model = _pick(rng, CLAUDE_MODELS, ts)
                tok = _tokens(rng, "claude")
                cost = prices.cost(model, input_tokens=tok["input_tokens"], cache_read=tok["cache_read"],
                                   cache_write_5m=tok["cache_write_5m"], cache_write_1h=tok["cache_write_1h"],
                                   output_tokens=tok["output_tokens"])
                conn.execute(INSERT_USAGE, (f"demo:claude:{n}", "claude", ts, model, f"s{int(start)}",
                                            tok["input_tokens"], tok["cache_read"], tok["cache_write_5m"],
                                            tok["cache_write_1h"], tok["output_tokens"], 0, cost))
                five.spend(model, cost)
                week.spend(model, cost)
                n += 1
                # 11 天前那段使用中间，在网页上聊了一会儿：百分比涨了，本地日志里没有
                if chat_week != week.reset:
                    chat_pp, chat_week = 0.0, week.reset
                if not chatted and abs(start - web_chat_day) < 86400 and ts > start + 3600:
                    five.pct, week.pct, chatted = min(100.0, five.pct + 14), min(100.0, week.pct + 4), True
                    chat_pp += 4
                    last_poll = 0.0
                if ts - last_poll >= 180:
                    for w, name in ((five, "five_hour"), (week, "seven_day")):
                        conn.execute(INSERT_SNAPSHOT, ("claude", "all", name, ts, float(int(w.pct)), w.reset,
                                                       w.length, "pro", "api"))
                        counts["snapshots"] += 1
                    db.save_breakdown(conn, "claude", "seven_day", ts, week.reset - WEEK,
                                      _breakdown_rows(week.pct, chat_pp), now=ts)
                    last_poll = ts
                ts += rng.uniform(100, 220)
        counts["usage"] += n
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('price_version', ?)", (prices.version,))
        # 覆盖声明：数据开始前一天打开，绑定虚构账号
        since = now - (DAYS + 1) * 86400
        for tool in ("codex", "claude"):
            db.note_account(conn, tool, since, f"demo-{tool}")
            conn.execute("INSERT INTO coverage_claim (tool, account, start, end) VALUES (?, ?, ?, NULL)",
                         (tool, f"demo-{tool}", since))
    return counts


def _breakdown_rows(week_pct, chat_pp):
    """周来源分项：占已用部分的整数百分比。"""
    chat = round(100 * chat_pp / week_pct) if week_pct > 0 else 0
    return [{"key": "claude_code", "display_name": "Claude Code", "percent": float(100 - chat)},
            {"key": "chat", "display_name": "Chats", "percent": float(chat)},
            {"key": "cowork", "display_name": "Cowork", "percent": 0.0},
            {"key": "other", "display_name": "Other", "percent": 0.0}]
