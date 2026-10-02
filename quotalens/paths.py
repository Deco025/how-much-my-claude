"""各 CLI 的本地目录，以及本程序自己的数据目录。

数据目录（数据库、备份、价格缓存、日志、手填的价格）放在系统的用户数据目录，
更新、重装或重新下载项目都不会碰到它：
  Windows  %LOCALAPPDATA%\\how-much-my-claude
  macOS    ~/Library/Application Support/how-much-my-claude
  Linux    $XDG_DATA_HOME/how-much-my-claude（默认 ~/.local/share/how-much-my-claude）
设置环境变量 QUOTALENS_DATA_DIR 可以改到别处。
"""
import logging
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

APP_NAME = "how-much-my-claude"
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent  # 从源码运行时是项目目录；pip 安装时是 site-packages，不往里写东西
WEB_DIR = PACKAGE_DIR / "web"
PRICE_SEED_PATH = PACKAGE_DIR / "prices_seed.json"

log = logging.getLogger("quotalens")


def user_data_dir() -> Path:
    env = os.environ.get("QUOTALENS_DATA_DIR")
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / APP_NAME


DATA_DIR = user_data_dir()
DB_PATH = DATA_DIR / "quotalens.db"
PRICE_CACHE_PATH = DATA_DIR / "prices_cache.json"
PRICE_OVERRIDE_PATH = DATA_DIR / "prices_override.json"
LOG_PATH = DATA_DIR / "quota-lens.log"

# 0.2 之前数据放在源码目录的 data/ 下，手填价格放在源码目录
LEGACY_DATA_DIR = PROJECT_ROOT / "data"
LEGACY_OVERRIDE_PATH = PROJECT_ROOT / "prices_override.json"


def claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def codex_dir() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"


def _row_counts(path: Path):
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("usage", "quota_snapshot", "usage_window") if t in tables}
    finally:
        conn.close()


def migrate_legacy_data(legacy_dir: Path = None, data_dir: Path = None):
    """把旧版放在源码目录 data/ 下的数据复制到新的数据目录。只复制、不删旧的。

    启动时、打开数据库之前调用。新目录里已经有数据库就什么都不做。
    返回迁移了的旧目录；没有要迁移的返回 None。复制出来的数据库和原来的行数对不上就报错，不启动。
    """
    legacy_dir = legacy_dir or LEGACY_DATA_DIR
    data_dir = data_dir or DATA_DIR
    db_path = data_dir / "quotalens.db"
    data_dir.mkdir(parents=True, exist_ok=True)
    override = data_dir / "prices_override.json"
    legacy_override = legacy_dir.parent / "prices_override.json"
    if legacy_override.exists() and not override.exists():
        shutil.copy2(legacy_override, override)
    legacy_db = legacy_dir / "quotalens.db"
    if db_path.exists() or not legacy_db.exists() or legacy_dir.resolve() == data_dir.resolve():
        return None

    tmp = db_path.with_name("quotalens.db.migrating")
    src, dst = sqlite3.connect(legacy_db), sqlite3.connect(tmp)
    try:
        with dst:
            src.backup(dst)  # 在线备份：旧库的 WAL 里还没合并的内容也会带上
    finally:
        src.close()
        dst.close()
    if _row_counts(tmp) != _row_counts(legacy_db):
        tmp.unlink()
        raise RuntimeError(f"迁移数据库后行数对不上，已放弃迁移：{legacy_db}")
    tmp.replace(db_path)
    if (legacy_dir / "prices_cache.json").exists():
        shutil.copy2(legacy_dir / "prices_cache.json", data_dir / "prices_cache.json")
    if (legacy_dir / "backups").is_dir():
        shutil.copytree(legacy_dir / "backups", data_dir / "backups", dirs_exist_ok=True)
    (legacy_dir / "MOVED.txt").write_text(
        f"{time.strftime('%Y-%m-%d %H:%M')} 起数据改存到 {data_dir}\n"
        f"Data moved to {data_dir}\n\n"
        "这个文件夹里的旧数据没有删除。确认新位置一切正常后，可以自己删掉这个 data 文件夹。\n"
        "The old copy here was left untouched; delete this folder yourself once everything looks fine.\n",
        encoding="utf-8")
    log.info("已把旧数据从 %s 复制到 %s", legacy_dir, data_dir)
    return legacy_dir
