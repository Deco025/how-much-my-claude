"""页面「设置」里能改的选项。存在数据库的 meta 表里，跟着数据库一起备份、迁移。"""
import json

from . import db

DEFAULTS = {
    "language": "auto",         # auto（跟随系统）/ zh / en
    "notify": True,             # 发现额度可能被调时弹系统通知
    "claude_poll_minutes": 30,  # Claude Code 空闲时查额度的间隔（使用中固定 3 分钟一次）
    "history_days": 90,         # 首次启动导入多久以内的日志（之后可以在「数据管理」里导入全部）
    "min_change_pct": 20,       # 偏离超过这么多才判为被调；这是下限，数据本身波动大时自动放宽
    "model_overlap_pct": 50,    # 最近窗口的花费里，前后共同在用的模型低于这个比例，就判为「换了模型」
}
LIMITS = {
    "claude_poll_minutes": (2, 240),
    "history_days": (7, 3650),
    "min_change_pct": (5, 60),
    "model_overlap_pct": (10, 90),
}
CHOICES = {"language": ("auto", "zh", "en")}


def clean(values: dict) -> dict:
    """按默认值的类型校验，超出范围的数值夹到边界，不认识的键和不合法的值丢掉。"""
    out = {}
    for key, default in DEFAULTS.items():
        if key not in values:
            continue
        value = values[key]
        if key in CHOICES:
            if value in CHOICES[key]:
                out[key] = value
        elif isinstance(default, bool):
            if isinstance(value, bool):
                out[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            lo, hi = LIMITS[key]
            out[key] = int(min(hi, max(lo, value)))
    return out


def load() -> dict:
    with db.reader() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'settings'").fetchone()
    try:
        saved = json.loads(row["value"]) if row else {}
    except ValueError:
        saved = {}
    return {**DEFAULTS, **clean(saved if isinstance(saved, dict) else {})}


def save(changes: dict) -> dict:
    values = {**load(), **clean(changes)}
    with db.writer() as conn:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('settings', ?)", (json.dumps(values),))
    return values
