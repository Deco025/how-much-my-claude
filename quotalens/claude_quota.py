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

from . import db
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


def _classify(key: str):
    """接口顶层键名 → (scope, window)。"""
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
        "User-Agent": "quota-lens",
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
    with db.writer() as conn:
        conn.executemany(INSERT_SNAPSHOT, rows)
        db.archive_response(conn, "claude", result["at"], body)
    result["windows"] = len(rows)
    result["percents"] = {f"{scope}:{window}": pct for scope, window, _, pct, *_ in rows}
    extra = body.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        result["extra_usage"] = {k: extra.get(k) for k in ("used_credits", "monthly_limit", "currency")}
    return result
