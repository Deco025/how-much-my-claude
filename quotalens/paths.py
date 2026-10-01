"""各 CLI 的本地目录与本项目数据目录。"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "quotalens.db"
PRICE_CACHE_PATH = DATA_DIR / "prices_cache.json"
PRICE_OVERRIDE_PATH = PROJECT_ROOT / "prices_override.json"
PRICE_SEED_PATH = Path(__file__).resolve().parent / "prices_seed.json"


def claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def codex_dir() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else Path.home() / ".codex"
