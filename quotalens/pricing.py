"""模型价格：models.dev 为主，本地覆盖为辅；按官方 API 价把 token 折算成美元。

价格表合并顺序（后者覆盖前者）：
  prices_seed.json（随代码附带的快照） < data/prices_cache.json（最近一次 models.dev 拉取）
  < prices_override.json（用户手填，可写 {"same_as": "其他模型"} 做别名）
单位：美元 / 百万 token。
"""
import hashlib
import json
import logging
import re
import time
import urllib.request

from .paths import PRICE_CACHE_PATH, PRICE_OVERRIDE_PATH, PRICE_SEED_PATH

log = logging.getLogger("quotalens")

MODELS_DEV_URL = "https://models.dev/api.json"
PRICE_KEYS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")

_DATE_SUFFIX = re.compile(r"-(\d{8}|\d{4}-\d{2}-\d{2})$")
_EFFORT_SUFFIX = re.compile(r"-(minimal|low|medium|high|xhigh|max)$")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def extract_models_dev(raw: dict) -> dict:
    """从 models.dev 的 api.json 里取 Anthropic / OpenAI 文本模型的价格。"""
    keys = ("input", "output", "cache_read", "cache_write")
    out = {}
    for provider in ("anthropic", "openai"):
        for model_id, m in ((raw.get(provider) or {}).get("models") or {}).items():
            cost = m.get("cost") or {}
            if not isinstance(cost.get("input"), (int, float)) or not isinstance(cost.get("output"), (int, float)):
                continue
            entry = {k: cost[k] for k in keys if isinstance(cost.get(k), (int, float))}
            tiers = [
                {"size": t["tier"]["size"], **{k: t[k] for k in keys if k in t}}
                for t in cost.get("tiers") or []
                if (t.get("tier") or {}).get("type") == "context"
            ]
            if tiers:
                entry["tiers"] = tiers
            fast = (((m.get("experimental") or {}).get("modes") or {}).get("fast") or {}).get("cost")
            if fast:
                entry["fast"] = {k: fast[k] for k in keys if k in fast}
            out[model_id.lower()] = entry
    return out


def _valid_price(entry) -> bool:
    return isinstance(entry, dict) and all(isinstance(entry.get(k), (int, float)) for k in ("input", "output"))


def lookup_candidates(model: str):
    """模型名规范化后的查价候选，从精确到宽松。"""
    m = model.strip().lower().replace("[1m]", "")
    m = m.rsplit("/", 1)[-1].split(":", 1)[0].replace("@", "-")
    yield m
    base = _DATE_SUFFIX.sub("", m)
    yield base
    yield _EFFORT_SUFFIX.sub("", base)


def is_on_plan(tool: str, model: str) -> bool:
    """这次调用是否计入订阅额度。

    带 provider 前缀（deepseek/xxx）或非官方模型名的，是经 CC Switch 等工具
    转发到第三方的请求，不消耗 Claude / ChatGPT 订阅额度。
    """
    m = model.strip().lower()
    if "/" in m:
        return False
    if tool == "claude":
        return m.startswith("claude-")
    if tool == "codex":
        return m.startswith(("gpt-", "codex-", "o1", "o3", "o4"))
    return False


class PriceTable:
    def __init__(self):
        self.models: dict = {}
        self.sources: dict = {}  # 模型 → seed / models.dev / override（页面上的价格表显示来源）
        self.fetched_at = None
        self.version = ""
        self.reload()

    def reload(self) -> None:
        """重新合并三层价格表。"""
        seed = _read_json(PRICE_SEED_PATH) or {}
        models = dict(seed.get("models", {}))
        sources = dict.fromkeys(models, "seed")
        cache = _read_json(PRICE_CACHE_PATH) or {}
        models.update(cache.get("models", {}))
        sources.update(dict.fromkeys(cache.get("models", {}), "models.dev"))
        override = (_read_json(PRICE_OVERRIDE_PATH) or {}).get("models", {})
        for name, entry in override.items():
            if isinstance(entry, dict) and "same_as" in entry:
                _, target = self._find_in(models, str(entry["same_as"]))
                if target:
                    models[name.lower()] = dict(target, alias_of=entry["same_as"])
                    sources[name.lower()] = "override"
                else:
                    log.warning("prices_override.json：%s 的 same_as 指向的 %s 没有价格，已忽略", name, entry["same_as"])
            elif _valid_price(entry):
                models[name.lower()] = entry
                sources[name.lower()] = "override"
            else:
                # 缺 input / output 的条目会让计价报错、导致整个采集停下，宁可忽略
                log.warning("prices_override.json：%s 缺少数值型的 input / output，已忽略", name)
        self.models, self.sources = models, sources
        self.version = hashlib.sha1(json.dumps(models, sort_keys=True).encode()).hexdigest()
        self.fetched_at = cache.get("fetched_at") or seed.get("fetched_at")

    @staticmethod
    def _find_in(models, model):
        for key in lookup_candidates(model):
            if key in models:
                return key, models[key]
        return None, None

    def find(self, model: str):
        return self._find_in(self.models, model)

    def cost(self, model, *, input_tokens=0, cache_read=0, cache_write_5m=0, cache_write_1h=0,
             output_tokens=0, speed=None):
        """返回等价 API 美元；没有价格时返回 None。"""
        _, price = self.find(model)
        if price is None:
            return None
        p = dict(price)
        if speed == "fast" and price.get("fast"):
            p.update(price["fast"])
        else:
            prompt = input_tokens + cache_read + cache_write_5m + cache_write_1h
            for tier in sorted(price.get("tiers") or [], key=lambda t: t["size"]):
                if prompt > tier["size"]:
                    p.update({k: v for k, v in tier.items() if k != "size"})
        inp = p["input"]
        total = (
            input_tokens * inp
            + cache_read * p.get("cache_read", inp)
            + cache_write_5m * p.get("cache_write", inp * 1.25)
            # Anthropic 1 小时缓存写入是基础输入价的 2 倍，models.dev 只给 5 分钟档
            + cache_write_1h * p.get("cache_write_1h", inp * 2)
            + output_tokens * p["output"]
        )
        return total / 1_000_000


def refresh_from_models_dev(timeout=30) -> int:
    """拉取 models.dev 写入缓存，返回模型数。网络失败抛异常，由调用方记录。"""
    req = urllib.request.Request(MODELS_DEV_URL, headers={"User-Agent": "how-much-my-claude"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = json.loads(resp.read())
    models = extract_models_dev(raw)
    if not models:
        raise ValueError("models.dev 返回里没有可用的 Anthropic / OpenAI 价格")
    PRICE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PRICE_CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"source": "models.dev", "fetched_at": time.strftime("%Y-%m-%d %H:%M"),
                               "models": models}, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(PRICE_CACHE_PATH)
    return len(models)
