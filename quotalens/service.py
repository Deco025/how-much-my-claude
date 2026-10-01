"""后台任务：扫日志、自适应轮询额度、分析与告警、刷新价格、每日备份。

轮询节奏：
  Claude  日志里有新请求时每 3 分钟查一次额度（每个窗口拿到更多数据点），空闲时按 --poll-minutes。
  Codex   使用中不用查（日志自带额度）；空闲 10 分钟后每 20 分钟查一次，
          空闲时额度还在涨，就是本地日志之外的消耗。
"""
import logging
import threading
import time
import traceback

from . import calibrate, claude_quota, codex_quota, collect_claude, collect_codex, db, events, notify
from .pricing import PriceTable, refresh_from_models_dev

log = logging.getLogger("quotalens")

SCAN_INTERVAL = 60
PRICE_REFRESH_INTERVAL = 24 * 3600
BACKUP_INTERVAL = 24 * 3600
ANALYSIS_MAX_AGE = 600
ACTIVE_SECONDS = 600            # 最近这么久内有新请求算「使用中」
CLAUDE_ACTIVE_POLL = 180
MIN_POLL_INTERVAL = 120
CODEX_IDLE_POLL = 20 * 60
ALERT_COOLDOWN = 24 * 3600
TOOLS = ("codex", "claude")
WINDOW_NAMES = {"five_hour": "5 小时窗口", "seven_day": "每周窗口", "thirty_day": "30 天窗口"}


class Service:
    def __init__(self, claude_poll_minutes=30, history_days=90, notify_desktop=True):
        db.init_db()
        self.prices = PriceTable()
        self.claude_idle_poll = max(MIN_POLL_INTERVAL, claude_poll_minutes * 60)
        self.history_days = history_days
        self.notify_desktop = notify_desktop
        self._sync_lock = threading.Lock()
        self._analysis_lock = threading.Lock()
        self._stop = threading.Event()
        self.status = {"last_scan": None, "scan": {}, "claude_quota": None, "codex_quota": None,
                       "prices": None, "backup": None, "errors": []}
        self._last = {"claude_poll": 0.0, "codex_poll": 0.0, "prices": 0.0, "backup": 0.0, "analysis": 0.0}
        self._activity = {"claude": 0.0, "codex": 0.0}
        self._analysis = None
        self.reprice_if_needed()

    # ── 状态 ────────────────────────────────────────────

    def _error(self, where, exc):
        log.warning("%s 失败: %s", where, exc)
        log.debug(traceback.format_exc())
        self.status["errors"] = ([{"at": time.time(), "where": where, "error": str(exc)}] + self.status["errors"])[:20]

    # ── 采集 ────────────────────────────────────────────

    def scan_logs(self):
        changed = False
        with self._sync_lock:
            for name, mod in (("codex", collect_codex), ("claude", collect_claude)):
                try:
                    started = time.time()
                    stats = mod.sync(self.prices, self.history_days)
                    stats["seconds"] = round(time.time() - started, 2)
                    self.status["scan"][name] = stats
                    if stats["rows"]:
                        self._activity[name] = time.time()
                        changed = True
                except Exception as e:  # noqa: BLE001 — 单个来源失败不影响另一个
                    self._error(f"扫描 {name} 日志", e)
            self.status["last_scan"] = time.time()
        return changed

    def _due(self, key, interval, now):
        return now - self._last[key] >= interval

    def poll_claude(self, force=False):
        now = time.time()
        active = now - self._activity["claude"] < ACTIVE_SECONDS
        interval = MIN_POLL_INTERVAL if force else (CLAUDE_ACTIVE_POLL if active else self.claude_idle_poll)
        if not self._due("claude_poll", interval, now):
            return False
        self._last["claude_poll"] = now
        result = claude_quota.poll()
        if result["status"] == "rate_limited":
            self._last["claude_poll"] = now + self.claude_idle_poll  # 被限流就多等一轮
        return self._record_poll("claude_quota", result)

    def _record_poll(self, key, result):
        """记下轮询结果；只有百分比真的变了才算「有变化」，避免每次轮询都重算整套分析。"""
        previous = (self.status.get(key) or {}).get("percents")
        self.status[key] = result
        return result["status"] == "ok" and result.get("percents") != previous

    def poll_codex(self, force=False):
        """只在本地空闲时查：使用中日志已经带着额度信息。"""
        now = time.time()
        idle = now - self._activity["codex"] >= ACTIVE_SECONDS
        if not (force or idle) or not self._due("codex_poll", MIN_POLL_INTERVAL if force else CODEX_IDLE_POLL, now):
            return False
        self._last["codex_poll"] = now
        result = codex_quota.poll()
        if result["status"] == "rate_limited":
            self._last["codex_poll"] = now + CODEX_IDLE_POLL
        return self._record_poll("codex_quota", result)

    # ── 分析与告警 ──────────────────────────────────────

    def analysis(self, refresh=False):
        """缓存的分析结果；数据有变化或太旧时重算。"""
        new_alerts = []
        with self._analysis_lock:
            if refresh or self._analysis is None or time.time() - self._last["analysis"] > ANALYSIS_MAX_AGE:
                now = time.time()
                with db.reader() as conn:
                    self._analysis = {
                        "at": now,
                        "tools": {tool: calibrate.analyze(conn, tool, now) for tool in TOOLS},
                        "events": events.detect(conn, now=now),
                    }
                self._last["analysis"] = now
                new_alerts = self._record_alerts(self._analysis)
            analysis = self._analysis
        # 弹通知要起 PowerShell，可能要好几秒，放在锁外，别让页面请求跟着卡住
        for title, detail in new_alerts:
            log.warning("%s。%s", title, detail)
            if self.notify_desktop:
                notify.toast(title, detail)
        return analysis

    def _record_alerts(self, analysis):
        """把新出现的变化写进 alert 表（同一方向 24 小时内只记一次），返回要提醒的 (标题, 详情)。"""
        now, out = analysis["at"], []
        for tool, result in analysis["tools"].items():
            for g in result["groups"]:
                if g.get("status") not in ("tighter", "looser"):
                    continue
                key = f"{g['scope']}:{g['window']}:{g['plan_type']}"
                with db.writer() as conn:
                    recent = conn.execute(
                        "SELECT 1 FROM alert WHERE tool = ? AND window = ? AND direction = ? AND ts > ?",
                        (tool, key, g["status"], now - ALERT_COOLDOWN)).fetchone()
                    if recent:
                        continue
                    change = (g["ratio"] - 1) * 100
                    title = f"{'Codex' if tool == 'codex' else 'Claude'} {WINDOW_NAMES.get(g['window'], g['window'])}" \
                            f"可能被{'收紧' if g['status'] == 'tighter' else '放宽'}了 {abs(change):.0f}%"
                    detail = (f"最近 {g['recent_n']} 个窗口折合 {g['ref_model']} 约 ${g['recent_median']:.1f}，"
                              f"之前 {g['baseline_n']} 个窗口约 ${g['baseline_median']:.1f}")
                    conn.execute("INSERT INTO alert (ts, tool, window, direction, ratio, detail) VALUES (?, ?, ?, ?, ?, ?)",
                                 (now, tool, key, g["status"], g["ratio"], f"{title}。{detail}"))
                out.append((title, detail))
        return out

    # ── 价格 ────────────────────────────────────────────

    def refresh_prices(self):
        try:
            count = refresh_from_models_dev()
            self.status["prices"] = {"at": time.time(), "models": count}
        except Exception as e:  # noqa: BLE001
            self._error("刷新 models.dev 价格", e)
        self._last["prices"] = time.time()
        self.prices.reload()
        self.reprice_if_needed()

    def reprice_if_needed(self):
        with db.reader() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'price_version'").fetchone()
        if row and row["value"] == self.prices.version:
            return
        with self._sync_lock, db.writer() as conn:
            rows = conn.execute("SELECT id, model, input_tokens, cache_read_tokens, cache_write_5m_tokens, "
                                "cache_write_1h_tokens, output_tokens, speed FROM usage").fetchall()
            updates = [(self.prices.cost(r["model"], input_tokens=r["input_tokens"], cache_read=r["cache_read_tokens"],
                                         cache_write_5m=r["cache_write_5m_tokens"],
                                         cache_write_1h=r["cache_write_1h_tokens"],
                                         output_tokens=r["output_tokens"], speed=r["speed"]), r["id"])
                       for r in rows]
            conn.executemany("UPDATE usage SET cost_usd = ? WHERE id = ?", updates)
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('price_version', ?)", (self.prices.version,))
        log.info("价格表变化，已重新计价 %d 条", len(updates))
        self._analysis = None

    # ── 主循环 ──────────────────────────────────────────

    def sync_now(self):
        """页面上的「立即同步」：重载价格覆盖、扫日志、查两边额度、重算分析。"""
        self.prices.reload()
        self.reprice_if_needed()
        self.scan_logs()
        self.poll_claude(force=True)
        self.poll_codex(force=True)
        return self.analysis(refresh=True)

    def tick(self):
        now = time.time()
        if self._due("prices", PRICE_REFRESH_INTERVAL, now):
            self.refresh_prices()
        changed = self.scan_logs()
        changed |= self.poll_claude()
        changed |= self.poll_codex()
        if changed or self._analysis is None:
            self.analysis(refresh=True)
        if self._due("backup", BACKUP_INTERVAL, now):
            self._last["backup"] = now
            try:
                self.status["backup"] = {"at": now, "path": db.backup()}
            except Exception as e:  # noqa: BLE001
                self._error("备份数据库", e)

    def run_forever(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 — 后台线程不能死
                self._error("后台循环", e)
            self._stop.wait(SCAN_INTERVAL)

    def start(self):
        threading.Thread(target=self.run_forever, name="quotalens-worker", daemon=True).start()

    def stop(self):
        self._stop.set()
