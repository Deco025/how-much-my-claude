"""今天 / 近 7 天 / 近 30 天的用量汇总、时间序列和按模型拆分。"""
import time
from datetime import datetime

RANGES = {"today": None, "7d": 7, "30d": 30}

TOKEN_SUMS = """
    COUNT(*)                                        AS requests,
    SUM(input_tokens)                               AS input_tokens,
    SUM(cache_read_tokens)                          AS cache_read_tokens,
    SUM(cache_write_5m_tokens + cache_write_1h_tokens) AS cache_write_tokens,
    SUM(output_tokens)                              AS output_tokens,
    SUM(reasoning_tokens)                           AS reasoning_tokens,
    COALESCE(SUM(cost_usd), 0)                      AS cost_usd,
    SUM(cost_usd IS NULL)                           AS unpriced_requests
"""


def range_start(name: str, now=None) -> float:
    now = now or time.time()
    days = RANGES.get(name, 7)
    if days is None:
        return datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return now - days * 86400


def _where(tool, since):
    sql, args = " WHERE ts >= ?", [since]
    if tool in ("claude", "codex"):
        sql += " AND tool = ?"
        args.append(tool)
    return sql, args


def _with_rate(row):
    d = {k: (row[k] or 0) for k in row.keys()}
    prompt = d["input_tokens"] + d["cache_read_tokens"] + d["cache_write_tokens"]
    d["cache_hit_rate"] = d["cache_read_tokens"] / prompt if prompt else None
    return d


def usage_report(conn, range_name="7d", tool="all"):
    since = range_start(range_name)
    where, args = _where(tool, since)
    summary = _with_rate(conn.execute(f"SELECT {TOKEN_SUMS} FROM usage{where}", args).fetchone())

    bucket = "strftime('%Y-%m-%d %H:00', ts, 'unixepoch', 'localtime')" if range_name == "today" \
        else "date(ts, 'unixepoch', 'localtime')"
    series = [dict(r) for r in conn.execute(
        f"SELECT {bucket} AS bucket, tool, COUNT(*) AS requests, COALESCE(SUM(cost_usd), 0) AS cost_usd, "
        f"SUM(input_tokens) AS input_tokens, SUM(cache_read_tokens) AS cache_read_tokens, "
        f"SUM(cache_write_5m_tokens + cache_write_1h_tokens) AS cache_write_tokens, SUM(output_tokens) AS output_tokens "
        f"FROM usage{where} GROUP BY bucket, tool ORDER BY bucket", args)]

    models = [_with_rate(r) for r in conn.execute(
        f"SELECT tool, model, on_plan, {TOKEN_SUMS} FROM usage{where} "
        f"GROUP BY tool, model, on_plan ORDER BY cost_usd DESC, requests DESC", args)]
    return {"range": range_name, "since": since, "summary": summary, "series": series, "models": models}
