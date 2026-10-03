"""后台任务：扫日志、自适应轮询额度、分析与告警、刷新价格、每日备份。

轮询节奏：
  Claude  日志里有新请求时每 3 分钟查一次额度（每个窗口拿到更多数据点），空闲时按「设置」里的间隔（默认 30 分钟）。
  Codex   使用中不用查（日志自带额度）；空闲 10 分钟后每 20 分钟查一次，
          空闲时额度还在涨，就是本地日志之外的消耗。
"""
import logging
import threading
import time
import traceback

from . import (account, calibrate, claude_quota, codex_quota, collect_claude, collect_codex, db, events, i18n,
               notify, settings)
from .i18n import tr
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
    def __init__(self, claude_poll_minutes=None, history_days=None, notify_desktop=None, demo=False):
        """参数是命令行给的临时覆盖（不保存）；没给的用页面「设置」里存的值。

        demo=True 是演示模式：数据库里已经是生成好的演示数据，不扫日志、不查额度、不刷新价格、不联网。
        """
        self.demo = demo
        db.init_db()
        self.prices = PriceTable()
        self._overrides = {k: v for k, v in (("claude_poll_minutes", claude_poll_minutes),
                                              ("history_days", history_days), ("notify", notify_desktop))
                           if v is not None}
        self.apply_settings(settings.load())
        self._sync_lock = threading.Lock()
        self._analysis_lock = threading.Lock()
        self._stop = threading.Event()
        self.status = {"last_scan": None, "scan": {}, "claude_quota": None, "codex_quota": None,
                       "prices": None, "backup": None, "errors": []}
        self._last = {"claude_poll": 0.0, "codex_poll": 0.0, "prices": 0.0, "backup": 0.0, "analysis": 0.0}
        self._activity = {"claude": 0.0, "codex": 0.0}
        self._analysis = None
        self.reprice_if_needed()

    # ── 设置 ────────────────────────────────────────────

    def apply_settings(self, values):
        self.settings = values
        effective = {**values, **self._overrides}
        i18n.set_language(effective["language"])
        self.claude_idle_poll = max(MIN_POLL_INTERVAL, effective["claude_poll_minutes"] * 60)
        self.history_days = effective["history_days"]
        self.notify_desktop = effective["notify"]
        self.analysis_params = {"min_change": effective["min_change_pct"] / 100,
                                "model_overlap": effective["model_overlap_pct"] / 100}

    def update_settings(self, changes):
        self.apply_settings(settings.save(changes))
        self.analysis(refresh=True)  # 判断阈值可能变了
        return self.settings

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
            changed |= self._note_accounts()
            self.status["last_scan"] = time.time()
        return changed

    def _note_accounts(self):
        """记下本机当前登录的账号（只在变化时写一条）。换了账号要重算：旧声明不再适用。"""
        if self.demo:
            return False
        changed = False
        with db.writer() as conn:
            for tool in TOOLS:
                acct = account.current(tool)
                if acct and acct != db.latest_account(conn, tool):
                    db.note_account(conn, tool, time.time(), acct)
                    changed = True
        return changed

    # ── 覆盖声明 ────────────────────────────────────────

    def coverage(self):
        """每个工具：当前账号能否识别、声明是否对当前账号生效、历史声明。账号只给出是否为当前账号，不给摘要。"""
        out = {}
        with db.reader() as conn:
            for tool in TOOLS:
                current = db.latest_account(conn, tool) if self.demo else account.current(tool)
                claims = db.coverage_claims(conn, tool)
                active = next((c for c in claims if c["end"] is None), None)
                out[tool] = {
                    "account_known": current is not None,
                    "on": bool(active and active["account"] == current),
                    # 有未结束的声明，但属于别的账号：切换过账号，需要重新确认
                    "other_account": bool(active and active["account"] != current),
                    "since": active["start"] if active else None,
                    "history": [{"start": c["start"], "end": c["end"], "current_account": c["account"] == current}
                                for c in claims],
                }
        return out

    def set_coverage(self, tool, on):
        """打开或关闭覆盖声明。打开时绑定当前账号、从现在起生效；读不到账号时拒绝。"""
        with db.writer() as conn:
            current = db.latest_account(conn, tool) if self.demo else account.current(tool)
            if on and not current:
                raise ValueError("account unknown")
            now = time.time()
            if current:
                db.note_account(conn, tool, now, current)
            db.set_coverage_claim(conn, tool, current, on, now=now)
        self.analysis(refresh=True)
        return self.coverage()

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
        """记下轮询结果；百分比、重置时间或来源分项真的变了才算「有变化」，避免每次轮询都重算整套分析。"""
        previous = (self.status.get(key) or {}).get("percents")
        self.status[key] = result
        return result["status"] == "ok" and (result.get("percents") != previous or bool(result.get("breakdown_changed")))

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
                        "tools": {tool: calibrate.analyze(conn, tool, now, self.analysis_params) for tool in TOOLS},
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
                # 入库前再查一次资格：必须是当前算法版本、基线和待检测窗口都满足估值条件的结论。
                # 旧分析结果或以后的改动不能绕过这里写出「收紧」记录
                if not g.get("eligible") or g.get("analysis_version") != calibrate.ANALYSIS_VERSION:
                    continue
                key = f"{g['scope']}:{g['window']}:{g['plan_type']}"
                with db.writer() as conn:
                    recent = conn.execute(
                        "SELECT 1 FROM alert WHERE tool = ? AND window = ? AND direction = ? AND ts > ?",
                        (tool, key, g["status"], now - ALERT_COOLDOWN)).fetchone()
                    if recent:
                        continue
                    template = ("{tool} {window}可能被收紧了 {change}%" if g["status"] == "tighter"
                                else "{tool} {window}可能被放宽了 {change}%")
                    title = tr(template, tool="Codex" if tool == "codex" else "Claude",
                               window=tr(WINDOW_NAMES.get(g["window"], g["window"])),
                               change=f"{abs(g['ratio'] - 1) * 100:.0f}")
                    detail = tr("最近 {recent_n} 个窗口折合 {model} 约 ${recent:.1f}，之前 {baseline_n} 个窗口约 ${baseline:.1f}",
                                recent_n=g["recent_n"], model=g["ref_model"], recent=g["recent_median"],
                                baseline_n=g["baseline_n"], baseline=g["baseline_median"])
                    if g.get("cross_gap"):
                        detail += tr("（跨断档：和 {day} 之前的窗口比）",
                                     day=time.strftime("%m-%d", time.localtime(g["baseline_to"])))
                    conn.execute("INSERT INTO alert (ts, tool, window, direction, ratio, detail, analysis_version)"
                                 " VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (now, tool, key, g["status"], g["ratio"], f"{title}。{detail}",
                                  calibrate.ANALYSIS_VERSION))
                out.append((title, detail))
        return out

    # ── 数据管理（页面上的「数据管理」） ──────────────────

    def set_rule(self, tool, kind, target, mode):
        """标记某个方案 / 窗口不参与分析（mode="ignore"），或恢复（mode=None）。"""
        with db.writer() as conn:
            db.set_rule(conn, tool, kind, target, mode)
        return self.analysis(refresh=True)

    def delete_plan(self, tool, plan):
        """删掉一个订阅方案的全部窗口数据。先整库备份一份（不参与轮换），删错了还能找回来。"""
        with self._sync_lock:
            backup_path = db.backup(label="before-delete")
            with db.reader() as conn:
                windows = [w for w in calibrate.analyze(conn, tool)["windows"] if w["plan_type"] == plan]
            with db.writer() as conn:
                result = db.delete_plan_data(conn, tool, plan, windows)
        log.info("已删除 %s %s 的数据：%s，删除前的备份在 %s", tool, plan, result, backup_path)
        self.analysis(refresh=True)
        return {**result, "backup": backup_path}

    def import_history(self):
        """把本机上还留着的全部历史日志导进来（首次启动只导近 history_days 天）。重复导入不会重复计数。"""
        with self._sync_lock:
            out = {}
            for name, mod in (("codex", collect_codex), ("claude", collect_claude)):
                out[name] = mod.sync(self.prices, history_days=36500)
        self.analysis(refresh=True)
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
        if self.demo:
            return self.analysis(refresh=True)
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
        if self.demo:
            # 演示模式不跑后台任务；状态填成「刚同步过、两边都正常」，页面才不会一直等第一次同步
            now = time.time()
            self.status.update(last_scan=now, claude_quota={"status": "ok", "at": now, "plan": "pro"},
                               codex_quota={"status": "ok", "at": now, "plan": "plus"})
            return
        threading.Thread(target=self.run_forever, name="quotalens-worker", daemon=True).start()

    def connections(self):
        """两个工具的连接状态（页面顶部的提示、「运行状态」用）。不含 token。"""
        from . import collect_claude, collect_codex, paths
        if self.demo:
            return [{"tool": tool, "log_dir": "demo", "log_files": 1, "credentials": "ok", "credentials_where": "demo",
                     "last_poll": self.status.get(f"{tool}_quota")} for tool in TOOLS]
        out = []
        for tool, mod, quota in (("codex", collect_codex, codex_quota), ("claude", collect_claude, claude_quota)):
            out.append({"tool": tool, "log_dir": str(paths.codex_dir() if tool == "codex" else paths.claude_dir()),
                        "log_files": len(mod.log_files()), "credentials": quota.read_credentials()[-1],
                        "credentials_where": quota.credentials_location(),
                        "last_poll": self.status.get(f"{tool}_quota")})
        return out

    def claude_plan_and_credentials(self):
        """(订阅方案, 登录状态)；演示模式不读真实的登录信息。"""
        if self.demo:
            return "pro", "ok"
        _, plan, status = claude_quota.read_credentials()
        return plan, status

    def stop(self):
        self._stop.set()
