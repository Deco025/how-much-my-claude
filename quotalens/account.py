"""当前登录账号的本地标识，只输出不可逆摘要（不保存、不上传原始 ID 或邮箱）。

用途：覆盖声明绑定到账号，切换账号后旧声明不继承；窗口按查额度时看到的账号分区。
读不到账号标识时返回 None，此时覆盖声明不能生效。

  Claude  Claude Code 的全局配置 ~/.claude.json（设了 CLAUDE_CONFIG_DIR 时在该目录下）里的
          oauthAccount.accountUuid + organizationUuid
  Codex   ~/.codex/auth.json 里的 tokens.account_id
"""
import hashlib
import json
import os
from pathlib import Path

from .paths import codex_dir


def digest(*parts) -> str:
    raw = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(("how-much-my-claude/account\x1f" + raw).encode("utf-8")).hexdigest()[:16]


def _claude_config_path() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) / ".claude.json" if env else Path.home() / ".claude.json"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def claude_account():
    data = _read_json(_claude_config_path())
    oauth = (data or {}).get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accountUuid"):
        return None
    return digest("claude", oauth["accountUuid"], oauth.get("organizationUuid") or "")


def codex_account():
    data = _read_json(codex_dir() / "auth.json")
    tokens = (data or {}).get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict) or not tokens.get("account_id"):
        return None
    return digest("codex", tokens["account_id"])


def current(tool: str):
    return claude_account() if tool == "claude" else codex_account()
