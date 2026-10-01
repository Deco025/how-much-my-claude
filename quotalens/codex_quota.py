"""在本地空闲时查询 Codex 额度百分比（chatgpt.com 的非公开接口，CC Switch 用的同一个）。

日志只在本地使用 Codex 时才写额度信息。空闲时如果额度还在涨，说明有本地日志之外的
消耗（云任务、别的设备），这正是要和「厂商暗调」区分开的情况。

凭据只读 ~/.codex/auth.json，绝不刷新 token（由 Codex CLI 自己负责）。
"""
import json
import time
import urllib.error
import urllib.request

from . import db
from .paths import codex_dir
from .util import parse_ts, window_name

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
STALE_SECONDS = 8 * 86400  # Codex CLI 超过 8 天会刷新 token，再旧的多半已失效

INSERT_SNAPSHOT = """
INSERT OR IGNORE INTO quota_snapshot (tool, scope, window, ts, used_percent, resets_at, window_seconds, plan_type, source)
VALUES ('codex', ?, ?, ?, ?, ?, ?, ?, 'api')
"""


def read_credentials():
    """返回 (access_token, account_id, 状态)。状态：ok / missing / expired / error。"""
    try:
        data = json.loads((codex_dir() / "auth.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None, "missing"
    except (OSError, ValueError):
        return None, None, "error"
    if data.get("auth_mode") != "chatgpt":
        return None, None, "missing"
    tokens = data.get("tokens") or {}
    if not tokens.get("access_token"):
        return None, None, "missing"
    refreshed = parse_ts(data.get("last_refresh"))
    if refreshed and time.time() - refreshed > STALE_SECONDS:
        return None, None, "expired"
    return tokens["access_token"], tokens.get("account_id"), "ok"


def parse_usage(body: dict, ts: float):
    """接口响应 → 快照行参数列表。"""
    rl = body.get("rate_limit") or {}
    plan = body.get("plan_type")
    rows = []
    for key in ("primary_window", "secondary_window"):
        w = rl.get(key)
        if not isinstance(w, dict) or w.get("used_percent") is None or not w.get("limit_window_seconds"):
            continue
        seconds = int(w["limit_window_seconds"])
        # 窗口还没开始：0%，重置时间是「现在 + 窗口长度」并随时间后移，不是真实窗口
        if not w["used_percent"] and (w.get("reset_after_seconds") or 0) >= seconds - 60:
            continue
        rows.append(("codex", window_name(seconds), ts, float(w["used_percent"]), w.get("reset_at"), seconds, plan))
    return rows


def poll(timeout=15) -> dict:
    token, account_id, status = read_credentials()
    result = {"status": status, "at": time.time()}
    if status != "ok":
        return result
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "codex-cli", "Accept": "application/json"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    try:
        with urllib.request.urlopen(urllib.request.Request(USAGE_URL, headers=headers), timeout=timeout) as resp:
            raw = resp.read()
        body = json.loads(raw)
    except urllib.error.HTTPError as e:
        result["status"] = "expired" if e.code in (401, 403) else ("rate_limited" if e.code == 429 else "error")
        result["error"] = f"HTTP {e.code}"
        return result
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        result["status"], result["error"] = "error", str(e)
        return result
    rows = parse_usage(body, result["at"])
    with db.writer() as conn:
        conn.executemany(INSERT_SNAPSHOT, rows)
        db.archive_response(conn, "codex", result["at"], body)
    result["windows"] = len(rows)
    result["percents"] = {f"{scope}:{window}": pct for scope, window, _, pct, *_ in rows}
    result["plan"] = body.get("plan_type")
    return result
