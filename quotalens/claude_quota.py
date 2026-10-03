"""轮询 Claude 订阅额度百分比（Claude Code 的 /usage 用的同一个接口，非公开）。

凭据只读 Claude Code 自己存的登录信息，绝不刷新 token：刷新会轮换 refresh token，
导致 Claude Code 本身掉登录。token 过期时提示用户随便跑一次 claude 让它自己刷新。
token 只发往 api.anthropic.com，不落日志。

Claude Code 把登录信息存在：
  Windows / Linux  ~/.claude/.credentials.json
  macOS            系统钥匙串里名为「Claude Code-credentials」的项（第一次读取时系统会问要不要允许）
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import account, db
from .paths import claude_dir
from .util import WINDOW_SECONDS, parse_ts

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

INSERT_SNAPSHOT = """
INSERT OR IGNORE INTO quota_snapshot (tool, scope, window, ts, used_percent, resets_at, window_seconds, plan_type, source)
VALUES ('claude', ?, ?, ?, ?, ?, ?, ?, 'api')
"""


KEYCHAIN_SERVICE = "Claude Code-credentials"


def _keychain_credentials():
    """macOS 钥匙串里的 Claude Code 登录信息（JSON 文本）；没有就返回 None。只读不写。"""
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def credentials_location() -> str:
    return "macOS Keychain: " + KEYCHAIN_SERVICE if sys.platform == "darwin" else str(claude_dir() / ".credentials.json")


def read_credentials():
    """返回 (access_token, plan, 状态说明)。状态：ok / missing / expired / error。"""
    path = claude_dir() / ".credentials.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = _keychain_credentials() if sys.platform == "darwin" else None
        if raw is None:
            return None, None, "missing"
        try:
            data = json.loads(raw)
        except ValueError:
            return None, None, "error"
    except (OSError, ValueError):
        return None, None, "error"
    entry = data.get("claudeAiOauth") or data.get("claude.ai_oauth") or {}
    token, plan = entry.get("accessToken"), entry.get("subscriptionType")
    if not token:
        return None, plan, "missing"
    expires = entry.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000 < time.time():
        return None, plan, "expired"
    return token, plan, "ok"


BREAKDOWN_KEY = "seven_day_breakdown"
COLLECTED_SOURCES = {"claude_code"}           # 本项目能采集到的来源（但分不出是哪台设备）
KNOWN_SOURCES = {"claude_code", "chat", "cowork", "other"}


def parse_breakdown(body: dict):
    """周额度来源分项 → {"as_of", "window_start", "rows", "issues"}；接口没给分项时返回 None。

    只校验、不修正：非有限数值、越界百分比记为无法解析（percent=None），重复 key 只保留第一个并记下问题，
    合计偏离 100 超过取整误差也记下问题。未知新产品原样保留。空列表保留为一个无法解析的占位行。
    """
    raw = body.get(BREAKDOWN_KEY)
    if raw is None:
        return None
    issues = []
    if not isinstance(raw, dict):
        return {"as_of": None, "window_start": None, "rows": [{"key": "_invalid", "display_name": None, "percent": None}],
                "issues": ["not_object"]}
    rows, keys = [], set()
    items = raw.get("rows")
    if not isinstance(items, list):
        issues.append("rows_missing")
        items = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not item["key"]:
            issues.append("bad_row")
            continue
        key = item["key"]
        if key in keys:
            issues.append("duplicate_key")
            continue
        keys.add(key)
        pct = item.get("percent")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or pct != pct or pct in (float("inf"), float("-inf")) \
                or not 0 <= pct <= 100:
            issues.append("bad_percent")
            pct = None
        name = item.get("display_name")
        rows.append({"key": key, "display_name": name if isinstance(name, str) else None,
                     "percent": float(pct) if pct is not None else None})
    if not rows:
        issues.append("empty")
        rows.append({"key": "_empty", "display_name": None, "percent": None})
    valid = [r["percent"] for r in rows if r["percent"] is not None]
    if valid and abs(sum(valid) - 100) > 0.5 * len(rows) + 1e-9:
        issues.append("sum_mismatch")
    return {"as_of": parse_ts(raw.get("as_of")), "window_start": parse_ts(raw.get("window_started_at")),
            "rows": rows, "issues": issues}


def store_breakdown(conn, breakdown, fetched_at) -> bool:
    """存一组分项（as_of 缺失时用抓取时刻）。返回是否是新的一组。"""
    return db.save_breakdown(conn, "claude", "seven_day", breakdown["as_of"] or fetched_at,
                             breakdown["window_start"], breakdown["rows"], now=fetched_at,
                             issues=breakdown.get("issues") or ())


def backfill_breakdowns(conn) -> int:
    """从归档的原始响应里补出历史分项（不重新请求接口）。返回新补的组数。"""
    n = 0
    for r in conn.execute("SELECT first_ts, body FROM raw_response WHERE tool = 'claude' ORDER BY first_ts").fetchall():
        try:
            body = json.loads(r["body"])
        except ValueError:
            continue
        breakdown = parse_breakdown(body) if isinstance(body, dict) else None
        if breakdown and store_breakdown(conn, breakdown, r["first_ts"]):
            n += 1
    return n


def _classify(key: str):
    """接口顶层键名 → (scope, window)。来源分项不是额度窗口，显式排除。"""
    if key == BREAKDOWN_KEY:
        return None
    if key in WINDOW_SECONDS:
        return "all", key
    for window in WINDOW_SECONDS:
        if key.startswith(window + "_"):
            return key[len(window) + 1:], window
    return None


def parse_usage(body: dict, ts: float, plan):
    """接口响应 → 快照行参数列表。兼容旧版顶层窗口和新版 limits[] 模型专属周限额。"""
    rows = {}
    for key, w in body.items():
        if not isinstance(w, dict) or w.get("utilization") is None:
            continue
        cls = _classify(key)
        if cls:
            rows[cls] = (float(w["utilization"]), parse_ts(w.get("resets_at")))
    for limit in body.get("limits") or []:
        if not isinstance(limit, dict) or limit.get("kind") != "weekly_scoped" or limit.get("group") != "weekly":
            continue
        scope = limit.get("scope") or {}
        name = ((scope.get("model") or {}).get("display_name") or "").strip().lower()
        if not name or scope.get("surface") or not isinstance(limit.get("percent"), (int, float)):
            continue
        rows[(name, "seven_day")] = (float(limit["percent"]), parse_ts(limit.get("resets_at")))
    return [(scope, window, ts, pct, resets, WINDOW_SECONDS[window], plan)
            for (scope, window), (pct, resets) in rows.items()]


def poll(timeout=15) -> dict:
    """查询一次并写快照。返回给状态页看的结果，不含 token。"""
    token, plan, status = read_credentials()
    result = {"status": status, "plan": plan, "at": time.time()}
    if status != "ok":
        return result
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "how-much-my-claude",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        result["status"] = "expired" if e.code in (401, 403) else ("rate_limited" if e.code == 429 else "error")
        result["error"] = f"HTTP {e.code}"
        return result
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        result["status"], result["error"] = "error", str(e)
        return result
    rows = parse_usage(body, result["at"], plan)
    breakdown = parse_breakdown(body)
    with db.writer() as conn:
        conn.executemany(INSERT_SNAPSHOT, rows)
        db.archive_response(conn, "claude", result["at"], body)
        db.note_account(conn, "claude", result["at"], account.claude_account())
        if breakdown:
            result["breakdown_changed"] = store_breakdown(conn, breakdown, result["at"])
    result["windows"] = len(rows)
    # 百分比和重置时间：任何一个变了，分析缓存都要更新（提前重置时百分比可能不变）
    # 重置时间每次查询都有亚秒级漂移：取整到分钟再比较，否则每次轮询都算「有变化」
    result["percents"] = {f"{scope}:{window}": [pct, db.round_reset(resets)] for scope, window, _, pct, resets, *_ in rows}
    extra = body.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        result["extra_usage"] = {k: extra.get(k) for k in ("used_credits", "monthly_limit", "currency")}
    return result
