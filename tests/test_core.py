"""核心逻辑测试：python -m unittest discover tests"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quotalens import calibrate, claude_quota, codex_quota, collect_claude, collect_codex, db, events, pricing


class TempDB(unittest.TestCase):
    """每个用例一个独立的临时数据库。CLAIMED = True 时两个工具都有从 0 时刻起生效的覆盖声明。"""
    CLAIMED = False

    def claim(self, tool, start=0.0, account="acct", end=None):
        with db.writer() as conn:
            db.note_account(conn, tool, start, account)
            conn.execute("INSERT INTO coverage_claim (tool, account, start, end) VALUES (?, ?, ?, ?)",
                         (tool, account, start, end))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        patches = [mock.patch.object(db, "DATA_DIR", root), mock.patch.object(db, "DB_PATH", root / "t.db")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        if self.CLAIMED:
            for tool in ("codex", "claude"):
                self.claim(tool)

    def tearDown(self):
        self.tmp.cleanup()


def token_count(ts, last, total, rl=None):
    keys = ("input_tokens", "cached_input_tokens", "output_tokens")
    payload = {"type": "token_count", "info": {
        "total_token_usage": dict(zip(keys, total)), "last_token_usage": dict(zip(keys, last))}}
    if rl:
        payload["rate_limits"] = rl
    return json.dumps({"timestamp": ts, "type": "event_msg", "payload": payload}).encode() + b"\n"


def codex_rl(pct5, reset5, pct7, reset7, plan="plus"):
    return {"limit_id": "codex", "plan_type": plan,
            "primary": {"used_percent": pct5, "window_minutes": 300, "resets_at": reset5},
            "secondary": {"used_percent": pct7, "window_minutes": 10080, "resets_at": reset7}}


class PricingTest(unittest.TestCase):
    def setUp(self):
        self.table = pricing.PriceTable.__new__(pricing.PriceTable)
        self.table.models = {
            "gpt-x": {"input": 2, "output": 10, "cache_read": 0.2, "tiers": [{"size": 1000, "input": 4, "output": 15}]},
            "claude-y": {"input": 4, "output": 20, "cache_read": 0.4, "cache_write": 5,
                         "fast": {"input": 8, "output": 40, "cache_read": 0.8, "cache_write": 10}},
            "claude-haiku-9": {"input": 1, "output": 5},
        }

    def test_normalize(self):
        self.assertEqual(self.table.find("OpenAI/GPT-X-2026-05-14")[0], "gpt-x")
        self.assertEqual(self.table.find("gpt-x@high")[0], "gpt-x")
        self.assertEqual(self.table.find("claude-haiku-9-20251001")[0], "claude-haiku-9")
        self.assertIsNone(self.table.find("deepseek-flash")[0])

    def test_cost_cache_and_tiers(self):
        # 100 万新鲜输入 + 100 万缓存读 + 100 万 1h 缓存写 + 100 万输出
        c = self.table.cost("claude-y", input_tokens=10**6, cache_read=10**6, cache_write_1h=10**6, output_tokens=10**6)
        self.assertAlmostEqual(c, 4 + 0.4 + 8 + 20)  # 1h 写入 = 2 × 输入价
        self.assertAlmostEqual(self.table.cost("claude-y", output_tokens=10**6, speed="fast"), 40)
        self.assertAlmostEqual(self.table.cost("gpt-x", input_tokens=500, output_tokens=0), 500 * 2 / 1e6)
        self.assertAlmostEqual(self.table.cost("gpt-x", input_tokens=2000), 2000 * 4 / 1e6)  # 超过分档按高价

    def test_on_plan(self):
        self.assertTrue(pricing.is_on_plan("claude", "claude-opus-5-5"))
        self.assertFalse(pricing.is_on_plan("claude", "deepseek-flash"))
        self.assertFalse(pricing.is_on_plan("codex", "deepseek/deepseek-v4.1-flash"))
        self.assertTrue(pricing.is_on_plan("codex", "gpt-6-astra"))


class ClaudeParseTest(unittest.TestCase):
    def line(self, msg_id="msg_1", output=10, stop="end_turn", model="claude-opus-5-5", split=True):
        usage = {"input_tokens": 3, "output_tokens": output, "cache_read_input_tokens": 100,
                 "cache_creation_input_tokens": 50, "speed": "standard"}
        if split:
            usage["cache_creation"] = {"ephemeral_5m_input_tokens": 20, "ephemeral_1h_input_tokens": 30}
        return json.dumps({"type": "assistant", "timestamp": "2026-09-30T23:04:37.1Z", "sessionId": "s",
                           "message": {"id": msg_id, "model": model, "usage": usage, "stop_reason": stop}}).encode()

    def test_cache_write_split(self):
        r = collect_claude.parse_line(self.line())
        self.assertEqual((r["cw_5m"], r["cw_1h"]), (20, 30))
        r = collect_claude.parse_line(self.line(split=False))
        self.assertEqual((r["cw_5m"], r["cw_1h"]), (50, 0))

    def test_skip_synthetic_and_dedup(self):
        self.assertIsNone(collect_claude.parse_line(self.line(model="<synthetic>")))
        partial = collect_claude.parse_line(self.line(output=1, stop=None))
        final = collect_claude.parse_line(self.line(output=500))
        self.assertTrue(collect_claude._better(final, partial))
        self.assertFalse(collect_claude._better(partial, final))


class CodexParseTest(unittest.TestCase):
    def test_last_usage_and_snapshots(self):
        state = collect_codex.FileState()
        collect_codex.parse_line(b'{"type":"turn_context","payload":{"model":"gpt-6-astra"}}\n', state)
        rec, snaps = collect_codex.parse_line(
            token_count("2026-09-30T23:04:37.175Z", [1000, 900, 50], [5000, 4000, 300], codex_rl(98.0, 1_790_826_392, 15.0, 1_791_413_192)),
            state)
        self.assertEqual((rec["input"], rec["cache_read"], rec["output"]), (100, 900, 50))
        self.assertEqual(rec["model"], "gpt-6-astra")
        self.assertEqual({(s[1], s[3]) for s in snaps}, {("five_hour", 98.0), ("seven_day", 15.0)})
        self.assertEqual(sorted(rec["links"]), [["codex", "five_hour", 1_790_826_392], ["codex", "seven_day", 1_791_413_192]])

    def test_duplicate_event_same_id(self):
        line = token_count("2026-09-30T23:04:37Z", [10, 0, 5], [10, 0, 5])
        a, _ = collect_codex.parse_line(line, collect_codex.FileState())
        b, _ = collect_codex.parse_line(line, collect_codex.FileState())  # 子代理复制的同一事件
        self.assertEqual(a["id"], b["id"])


class CodexSyncTest(TempDB):
    CLAIMED = True

    def test_sync_links_and_estimate(self):
        home = Path(self.tmp.name) / "codex"
        f = home / "sessions" / "2026" / "09" / "30" / "rollout-a.jsonl"
        f.parent.mkdir(parents=True)
        lines = [b'{"type":"turn_context","payload":{"model":"gpt-x"}}\n']
        total = [0, 0, 0]
        for i in range(1, 21):  # 20 次请求，每次 $0.5（gpt-x 输入 $2/M），每次涨 1%
            last = [250_000, 0, 0]
            total = [t + l for t, l in zip(total, last)]
            lines.append(token_count(f"2026-09-30T10:{i:02d}:00Z", last, total, codex_rl(i * 1.0, 2_000_000_000, 0.0, 2_000_400_000)))
        f.write_bytes(b"".join(lines))
        table = pricing.PriceTable.__new__(pricing.PriceTable)
        table.models = {"gpt-x": {"input": 2, "output": 10}}
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            stats = collect_codex.sync(table, history_days=10_000)
            again = collect_codex.sync(table, history_days=10_000)
        self.assertEqual(stats["rows"], 20)
        self.assertEqual(again["changed"], 0)
        with db.reader() as conn:
            res = {r["window"]: r for r in calibrate.analyze(conn, "codex", now=1_900_000_000)["windows"]}
        self.assertAlmostEqual(res["five_hour"]["cap"], 50.0)  # $10 用了 20% → $50
        self.assertEqual(res["five_hour"]["confidence"], "high")
        self.assertIsNone(res["seven_day"]["cap"])  # 周窗口还是 0%


class CalibrateTest(TempDB):
    CLAIMED = True

    def add(self, conn, uid, tool, ts, model, cost, link=None):
        conn.execute("INSERT INTO usage (id, tool, ts, model, on_plan, cost_usd) VALUES (?, ?, ?, ?, 1, ?)",
                     (uid, tool, ts, model, cost))
        if link:
            conn.execute("INSERT INTO usage_window VALUES (?, ?, ?, ?, ?)", (uid, tool, *link))

    def snap(self, conn, tool, scope, window, ts, pct, resets, seconds, plan="plus"):
        conn.execute("INSERT INTO quota_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test')",
                     (tool, scope, window, ts, pct, resets, seconds, plan))

    def breakdown(self, conn, as_of, window_start=None, **pcts):
        """一组 Claude 周来源分项；默认全部来自 Claude Code。"""
        pcts = pcts or {"claude_code": 100}
        db.save_breakdown(conn, "claude", "seven_day", as_of, window_start,
                          [{"key": k, "display_name": k, "percent": v} for k, v in pcts.items()])

    def codex_window(self, conn, name, start, steps, model="a"):
        """一个 Codex 5 小时窗口；steps 是 [(本地花费, 百分比涨幅)]，花费为 0 表示空闲时的接口快照。"""
        reset, pct = start + 18000, 0.0
        for i, (cost, dpct) in enumerate(steps):
            ts = start + 60 * (i + 1)
            if cost:
                self.add(conn, f"{name}-{i}", "codex", ts, model, cost, ("codex", "five_hour", reset))
            pct += dpct
            self.snap(conn, "codex", "codex", "five_hour", ts, pct, reset, 18000)

    def analyze(self, tool, now):
        with db.reader() as conn:
            return calibrate.analyze(conn, tool, now=now)

    def test_time_mode_ratio_and_early_reset(self):
        # 第一个周窗口用到 $120 / 60% 后提前重置；新窗口的花费只从交界处开始算
        with db.writer() as conn:
            # 第一个窗口从 T 开始，T 晚于声明生效时间（0）
            T = 10_000
            self.add(conn, "a", "claude", T + 1000, "claude-opus-5-5", 120.0)
            self.add(conn, "x", "claude", T + 1500, "deepseek-flash", None)  # 第三方，不计入
            conn.execute("UPDATE usage SET on_plan = 0 WHERE id = 'x'")
            self.snap(conn, "claude", "all", "seven_day", T + 2000, 60.0, T + 604800, 604800)
            self.breakdown(conn, T + 2000, T)
            self.add(conn, "b", "claude", T + 3000, "claude-opus-5-5", 30.0)
            self.snap(conn, "claude", "all", "seven_day", T + 4000, 15.0, T + 2500 + 604800, 604800)
            self.breakdown(conn, T + 4000, T + 2500)
        first, second = self.analyze("claude", T + 5000)["windows"]
        self.assertAlmostEqual(first["cap"], 200.0)   # $120 / 60%
        self.assertEqual(first["confidence"], "high")
        self.assertAlmostEqual(second["cap"], 200.0)  # $30 / 15%，不含第一个窗口的 $120
        self.assertEqual(second["confidence"], "medium")
        with db.reader() as conn:
            kinds = [e["kind"] for e in events.detect(conn, now=T + 5000)]
        self.assertIn("reset", kinds)

    def test_current_windows_drop_other_account(self):
        with db.writer() as conn:
            self.snap(conn, "codex", "codex", "thirty_day", 1000, 85.0, 2_000_000, 2_592_000, "free")
            self.snap(conn, "codex", "codex", "seven_day", 900_000, 15.0, 1_400_000, 604800)
        current = calibrate.current_windows(self.analyze("codex", 900_100)["windows"])
        self.assertEqual([r["window"] for r in current], ["seven_day"])

    def test_model_rates_recover_weights(self):
        # 两个模型：a 每 $1 吃 1%（窗口值 $100），b 每 $1 吃 4%（窗口值 $25）
        with db.writer() as conn:
            for w, (ca, cb) in enumerate([(30, 5), (10, 10), (50, 2), (20, 8)]):
                reset = 10_000_000 + w * 20_000
                start = reset - 18000
                spent_a = spent_b = 0.0
                for step in range(10):
                    ts = start + 100 + step * 60
                    self.add(conn, f"a{w}{step}", "codex", ts, "a", ca / 10, ("codex", "five_hour", reset))
                    self.add(conn, f"b{w}{step}", "codex", ts, "b", cb / 10, ("codex", "five_hour", reset))
                    spent_a, spent_b = spent_a + ca / 10, spent_b + cb / 10
                    self.snap(conn, "codex", "codex", "five_hour", ts, round(spent_a * 1 + spent_b * 4), reset, 18000)
        group = self.analyze("codex", 10_100_000)["groups"][0]
        caps = {r["model"]: r["cap_if_only"] for r in group["rates"]}
        self.assertAlmostEqual(caps["a"], 100, delta=8)
        self.assertAlmostEqual(caps["b"], 25, delta=3)
        self.assertEqual(group["ref_model"], "a")  # 花费最多的模型当主力

    def test_suspect_jump_is_flagged_not_deducted(self):
        # 每 $0.2 涨 2%（窗口值 $10），中间有一次本地只花 $0.01 却涨了 30%，另有一次空闲时涨 4%
        steps = [(0.2, 2)] * 5 + [(0.01, 30)] + [(0.2, 2)] * 5 + [(0, 4)]
        with db.writer() as conn:
            self.codex_window(conn, "w", 1_000_000, steps)
        w = self.analyze("codex", 1_020_000)["windows"][0]
        self.assertAlmostEqual(w["used_percent"], 54)
        self.assertAlmostEqual(w["pct_clean"], 54)               # 不扣
        self.assertAlmostEqual(w["external_pct"], 34, delta=0.5)  # 只标记为疑点
        self.assertTrue(w["contaminated"])
        self.assertAlmostEqual(w["cost_at_snapshot"] / w["used_percent"] * 100, 2.01 / 0.54, delta=0.05)  # 按原始已用%
        self.assertEqual(w["quality"], "unconfirmed")             # 有疑点：不作条件估计
        self.assertIsNone(w["cap"])                               # 也就不给容量数字
        self.assertIn("suspect_unrecorded", [r["code"] for r in w["reasons"]])

    def same_spend_two_readings(self):
        """之前几个窗口都是每 $2 涨 5%（值 $40）；最后一个窗口同一笔 $8，额度先 10%、后 20%（第二次没新花费）。"""
        with db.writer() as conn:
            for k in range(4):
                self.codex_window(conn, f"h{k}", 1_000_000 + k * 20_000, [(2.0, 5)] * 4)
            start = 1_000_000 + 4 * 20_000
            reset = start + 18000
            self.add(conn, "u", "codex", start + 10, "a", 8.0, ("codex", "five_hour", reset))
            self.snap(conn, "codex", "codex", "five_hour", start + 60, 10, reset, 18000)
            self.snap(conn, "codex", "codex", "five_hour", start + 3600, 20, reset, 18000)
        return max(self.analyze("codex", start + 4000)["windows"], key=lambda w: w["start"])

    def test_same_spend_two_readings_not_deducted(self):
        w = self.same_spend_two_readings()
        self.assertEqual(w["pct_clean"], 20)
        self.assertAlmostEqual(w["external_pct"], 0, delta=0.5)   # 合起来符合预期：只是额度更新滞后
        self.assertAlmostEqual(w["cap"], 40.0)
        self.assertEqual(w["quality"], "conditional")
        self.assertTrue(w["claimed"])

    def test_change_detection(self):
        now = 50 * 86400
        with db.writer() as conn:
            # 之前 3 周：每 $0.5 涨 5%，窗口值 $10
            for i in range(8):
                self.codex_window(conn, f"old{i}", now - (20 - 2 * i) * 86400, [(0.5, 5)] * 10)
            # 最近 3 天：每 $0.3 涨 5%，窗口值 $6，收紧 40%
            for i in range(4):
                self.codex_window(conn, f"new{i}", now - (60 - 12 * i) * 3600, [(0.3, 5)] * 10)
        group = self.analyze("codex", now)["groups"][0]
        self.assertEqual(group["status"], "tighter")
        self.assertAlmostEqual(group["ratio"], 0.6, delta=0.05)
        self.assertEqual((group["recent_n"], group["baseline_n"]), (4, 8))

    def test_model_mix_shift_is_not_a_quota_change(self):
        # 额度没变：a 每 $1 涨 10%，b 每 $1 涨 5%。之前以 a 为主、最近以 b 为主，
        # 原始估值会从约 $11 涨到约 $17，折算成主力模型后应该不变
        now = 50 * 86400

        def mixed(conn, name, start, share_a):
            reset, pct = start + 18000, 0.0
            for i in range(10):
                ts = start + 60 * (i + 1)
                ca, cb = 0.5 * share_a, 0.5 * (1 - share_a)
                self.add(conn, f"{name}-{i}a", "codex", ts, "a", ca, ("codex", "five_hour", reset))
                self.add(conn, f"{name}-{i}b", "codex", ts, "b", cb, ("codex", "five_hour", reset))
                pct += 10 * ca + 5 * cb
                self.snap(conn, "codex", "codex", "five_hour", ts, pct, reset, 18000)

        with db.writer() as conn:
            for i in range(8):
                mixed(conn, f"old{i}", now - (20 - 2 * i) * 86400, 0.8 - 0.02 * i)
            for i in range(4):
                mixed(conn, f"new{i}", now - (60 - 12 * i) * 3600, 0.3 + 0.02 * i)
        group = self.analyze("codex", now)["groups"][0]
        self.assertEqual(group["status"], "stable")
        self.assertAlmostEqual(group["ratio"], 1.0, delta=0.05)

    def test_stable_when_unchanged(self):
        now = 50 * 86400
        with db.writer() as conn:
            for i in range(8):
                cost = 0.5 if i % 2 else 0.55  # 正常波动 ±10%
                self.codex_window(conn, f"old{i}", now - (20 - 2 * i) * 86400, [(cost, 5)] * 10)
            for i in range(4):
                cost = 0.55 if i % 2 else 0.5
                self.codex_window(conn, f"new{i}", now - (60 - 12 * i) * 3600, [(cost, 5)] * 10)
        group = self.analyze("codex", now)["groups"][0]
        self.assertEqual(group["status"], "stable")
        self.assertEqual((group["recent_n"], group["baseline_n"]), (4, 8))


    def test_plans_are_grouped_separately(self):
        # team 方案的窗口值 $20，plus 的值 $10：换方案不能被当成暗调；没记录方案的窗口跟随最近的窗口
        with db.writer() as conn:
            for i in range(3):
                self.codex_window(conn, f"t{i}", 1_000_000 + i * 20_000, [(1.0, 5)] * 10)
            for i in range(3):
                self.codex_window(conn, f"p{i}", 2_000_000 + i * 20_000, [(0.5, 5)] * 10)
            conn.execute("UPDATE quota_snapshot SET plan_type = 'team' WHERE ts < 1500000")
            conn.execute("UPDATE quota_snapshot SET plan_type = NULL WHERE ts BETWEEN 1040000 AND 1060000")
        result = self.analyze("codex", 2_100_000)
        caps = {g["plan_type"]: g["ref_cap"] for g in result["groups"]}
        self.assertEqual(set(caps), {"team", "plus"})
        self.assertAlmostEqual(caps["team"], 20.0, delta=0.5)
        self.assertAlmostEqual(caps["plus"], 10.0, delta=0.5)


class SchemaTest(TempDB):
    def test_null_and_object_share_parent(self):
        _, a = db.schema_fingerprint({"credits": None})
        _, b = db.schema_fingerprint({"credits": {"balance": "0"}})
        self.assertIn("credits", a)
        self.assertIn("credits", b)
        self.assertIn("credits.balance", b)

    def test_new_field_event_and_archive_scrubs_pii(self):
        with db.writer() as conn:
            db.note_schema(conn, "codex", "api", {"rate_limit": {"used": 1}}, 0, 10)
            db.note_schema(conn, "codex", "api", {"rate_limit": {"used": 1, "per_model": 2}}, 300_000, 300_010)
            db.archive_response(conn, "codex", 300_000, {"email": "a@b.c", "plan_type": "plus"})
            db.archive_response(conn, "codex", 300_060, {"email": "a@b.c", "plan_type": "plus"})
            ev = events.detect(conn, now=300_100)
            rows = conn.execute("SELECT body, first_ts, last_ts FROM raw_response").fetchall()
        self.assertTrue(any("rate_limit.per_model" in e["detail"] for e in ev))
        self.assertEqual(len(rows), 1)  # 相同内容只延长时间范围
        self.assertNotIn("a@b.c", rows[0]["body"])
        self.assertEqual((rows[0]["first_ts"], rows[0]["last_ts"]), (300_000, 300_060))


class CodexQuotaParseTest(unittest.TestCase):
    def test_skip_unstarted_window(self):
        body = {"plan_type": "plus", "rate_limit": {
            "primary_window": {"used_percent": 0, "limit_window_seconds": 18000, "reset_after_seconds": 18000, "reset_at": 9},
            "secondary_window": {"used_percent": 16, "limit_window_seconds": 604800, "reset_after_seconds": 5000, "reset_at": 8}}}
        rows = codex_quota.parse_usage(body, 1.0)
        self.assertEqual([(r[1], r[3]) for r in rows], [("seven_day", 16.0)])


class ClaudeQuotaParseTest(unittest.TestCase):
    def test_legacy_and_scoped(self):
        body = {
            "five_hour": {"utilization": 33.0, "resets_at": "2026-10-01T08:40:00.123+00:00"},
            "seven_day": {"utilization": 44.0, "resets_at": "2026-10-02T14:00:00Z"},
            "seven_day_opus": {"utilization": 8.0, "resets_at": None},
            "extra_usage": {"is_enabled": False},
            "limits": [{"kind": "weekly_scoped", "group": "weekly", "percent": 37.5,
                        "resets_at": "2026-10-02T14:00:00Z", "scope": {"model": {"display_name": "Fable"}}}],
        }
        rows = {(r[0], r[1]): r for r in claude_quota.parse_usage(body, 1.0, "pro")}
        self.assertEqual(rows[("all", "five_hour")][3], 33.0)
        self.assertEqual(rows[("all", "five_hour")][5], 18000)
        self.assertEqual(rows[("fable", "seven_day")][3], 37.5)
        self.assertIsNone(rows[("opus", "seven_day")][4])


@unittest.skipUnless(sys.platform == "win32", "快捷方式只在 Windows 上创建")
class InstallTest(unittest.TestCase):
    def test_startup_toggle(self):
        from quotalens import install
        suffix = {"win32": ".lnk", "darwin": ".plist"}.get(sys.platform, ".desktop")
        with tempfile.TemporaryDirectory() as d,                 mock.patch.object(install, "startup_path", lambda: Path(d) / f"autostart{suffix}"):
            self.assertFalse(install.startup_enabled())
            install.set_startup(True)
            self.assertTrue(install.startup_enabled())
            install.set_startup(False)
            self.assertFalse(install.startup_enabled())

    def test_launchers_for_other_systems(self):
        import plistlib
        from quotalens import install
        cmd = ["/usr/bin/python3", "/home/me/my apps/desktop.pyw", "--hidden"]
        entry = install.desktop_entry(cmd, autostart=True)
        self.assertIn('Exec=/usr/bin/python3 "/home/me/my apps/desktop.pyw" --hidden', entry)
        self.assertIn("X-GNOME-Autostart-enabled=true", entry)
        self.assertEqual(plistlib.loads(install.launch_agent(cmd))["ProgramArguments"], cmd)
        files = install.app_bundle_files(cmd[:2])
        self.assertIn("exec /usr/bin/python3 '/home/me/my apps/desktop.pyw'", files["Contents/MacOS/launcher"].decode())
        self.assertEqual(plistlib.loads(files["Contents/Info.plist"])["CFBundleExecutable"], "launcher")


class ReviewFixesTest(TempDB):
    """代码审查发现的问题的回归测试。"""

    def test_invalid_override_is_ignored(self):
        override = Path(self.tmp.name) / "override.json"
        override.write_text(json.dumps({"models": {
            "bad": {"input": 1},                      # 缺 output
            "alias": {"same_as": "no-such-model"},    # 指向不存在的模型
            "good": {"input": 1, "output": 2},
        }}), encoding="utf-8")
        with mock.patch.object(pricing, "PRICE_OVERRIDE_PATH", override), \
                mock.patch.object(pricing, "PRICE_CACHE_PATH", Path(self.tmp.name) / "none.json"), \
                self.assertLogs("quotalens", level="WARNING"):
            table = pricing.PriceTable()
        self.assertIsNone(table.find("bad")[0])
        self.assertIsNone(table.find("alias")[0])
        self.assertEqual(table.find("good")[0], "good")
        self.assertIsNone(table.cost("bad", input_tokens=100))  # 不再抛 KeyError

    def test_events_without_totals_do_not_collide(self):
        def event(ts):
            return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 1200, "output_tokens": 50}}}}).encode()
        a, _ = collect_codex.parse_line(event("2026-09-30T10:00:00Z"), collect_codex.FileState())
        b, _ = collect_codex.parse_line(event("2026-09-30T10:01:00Z"), collect_codex.FileState())
        self.assertNotEqual(a["id"], b["id"])

    def test_stale_links_not_reused_after_reset(self):
        from quotalens.util import parse_ts
        state = collect_codex.FileState()
        reset5 = parse_ts("2026-09-30T10:30:00Z")
        week = parse_ts("2026-10-05T00:00:00Z")
        collect_codex.parse_line(token_count("2026-09-30T10:00:00Z", [10, 0, 5], [10, 0, 5],
                                             codex_rl(10.0, reset5, 5.0, week)), state)
        # 5 小时窗口 10:30 已重置，这条事件没带 rate_limits，不能再挂到旧的 5 小时窗口上
        rec, _ = collect_codex.parse_line(token_count("2026-09-30T11:00:00Z", [10, 0, 6], [20, 0, 11]), state)
        self.assertEqual([link[1] for link in rec["links"]], ["seven_day"])

    def test_reset_window_of_same_plan_stays_current(self):
        with db.writer() as conn:
            conn.execute("INSERT INTO quota_snapshot VALUES ('codex', 'codex', 'five_hour', 1000, 40, 5000, 18000, 'plus', 't')")
            conn.execute("INSERT INTO quota_snapshot VALUES ('codex', 'codex', 'seven_day', 90000, 20, 600000, 604800, 'plus', 't')")
        with db.reader() as conn:
            current = calibrate.current_windows(calibrate.analyze(conn, "codex", now=100_000)["windows"])
        self.assertEqual(sorted(w["window"] for w in current), ["five_hour", "seven_day"])

    def test_alerts_recorded_once_per_cooldown(self):
        from quotalens.service import Service
        svc = Service(notify_desktop=False)
        group = {"status": "tighter", "scope": "codex", "window": "five_hour", "plan_type": "plus", "ratio": 0.6,
                 "recent_n": 4, "baseline_n": 8, "ref_model": "a", "recent_median": 6.0, "baseline_median": 10.0,
                 "eligible": True, "declared": True, "analysis_version": calibrate.ANALYSIS_VERSION}
        analysis = {"at": 1000.0, "tools": {"codex": {"groups": [group]}}}
        self.assertEqual(len(svc._record_alerts(analysis)), 1)
        self.assertEqual(svc._record_alerts({**analysis, "at": 2000.0}), [])
        self.assertEqual(len(svc._record_alerts({**analysis, "at": 1000.0 + 25 * 3600})), 1)
        with db.reader() as conn:
            self.assertEqual({r[0] for r in conn.execute("SELECT analysis_version FROM alert")},
                             {calibrate.ANALYSIS_VERSION})

    def test_alert_gate_rejects_ineligible_or_old_results(self):
        # 趋势本应判为收紧，但不满足资格（或来自旧算法）：不入库、不提醒
        from quotalens.service import Service
        svc = Service(notify_desktop=False)
        base = {"status": "tighter", "scope": "codex", "window": "five_hour", "plan_type": "plus", "ratio": 0.6,
                "recent_n": 4, "baseline_n": 8, "ref_model": "a", "recent_median": 6.0, "baseline_median": 10.0}
        for group in ({**base}, {**base, "eligible": False, "analysis_version": calibrate.ANALYSIS_VERSION},
                      {**base, "eligible": True, "declared": True, "analysis_version": 1},
                      # 有资格但含推测成分（不在声明下）：只在页面显示，不写告警
                      {**base, "eligible": True, "declared": False, "analysis_version": calibrate.ANALYSIS_VERSION}):
            self.assertEqual(svc._record_alerts({"at": 1000.0, "tools": {"codex": {"groups": [group]}}}), [])
        with db.reader() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alert").fetchone()[0], 0)

    def test_claude_sync_waits_for_partial_line(self):
        def line(msg_id, output):
            return json.dumps({"type": "assistant", "timestamp": "2026-09-30T10:00:00Z", "sessionId": "s",
                               "message": {"id": msg_id, "model": "claude-x", "stop_reason": "end_turn",
                                           "usage": {"input_tokens": 1, "output_tokens": output}}}).encode() + b"\n"
        home = Path(self.tmp.name) / "claude"
        f = home / "projects" / "p" / "s.jsonl"
        f.parent.mkdir(parents=True)
        first, second = line("m1", 10), line("m2", 20)
        f.write_bytes(first + second[:30])  # 第二行还没写完
        table = pricing.PriceTable.__new__(pricing.PriceTable)
        table.models = {}
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}):
            self.assertEqual(collect_claude.sync(table, history_days=10_000)["rows"], 1)
            f.write_bytes(first + second)
            self.assertEqual(collect_claude.sync(table, history_days=10_000)["rows"], 1)
            self.assertEqual(collect_claude.sync(table, history_days=10_000)["changed"], 0)
        with db.reader() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 2)

    def test_api_rejects_foreign_host_and_cross_origin_post(self):
        from fastapi.testclient import TestClient
        from quotalens.api import create_app
        from quotalens.service import Service
        svc = Service(notify_desktop=False)
        client = TestClient(create_app(svc), base_url="http://127.0.0.1")  # 不进 with，不启动后台线程
        with mock.patch.object(svc, "sync_now"):
            self.assertEqual(client.get("/api/usage").status_code, 200)
            self.assertEqual(client.get("/api/usage", headers={"host": "evil.example"}).status_code, 400)
            self.assertEqual(client.post("/api/sync", headers={"origin": "https://evil.example"}).status_code, 403)
            self.assertEqual(client.post("/api/sync", headers={"origin": "http://127.0.0.1:8787"}).status_code, 200)

    def test_shutdown_stops_service_then_exits(self):
        from fastapi.testclient import TestClient
        from quotalens import api
        from quotalens.service import Service
        svc = Service(notify_desktop=False)
        client = TestClient(api.create_app(svc), base_url="http://127.0.0.1")
        with mock.patch.object(api.threading, "Timer") as timer:  # 别真的退出测试进程
            self.assertEqual(client.post("/api/shutdown", headers={"origin": "https://evil.example"}).status_code, 403)
            timer.assert_not_called()
            self.assertEqual(client.post("/api/shutdown").status_code, 200)
        self.assertTrue(svc._stop.is_set())
        timer.return_value.start.assert_called_once()
        with mock.patch.object(api.os, "_exit") as exit_:  # 浏览器版：到点后结束进程
            timer.call_args.args[1]()
        exit_.assert_called_once_with(0)
        self.assertEqual(client.post("/api/show").status_code, 404)  # 浏览器版没有窗口

    def test_desktop_hooks(self):
        from fastapi.testclient import TestClient
        from quotalens import api
        from quotalens.service import Service
        shown, quit_ = mock.Mock(), mock.Mock()
        client = TestClient(api.create_app(Service(notify_desktop=False), on_show=shown, on_quit=quit_),
                            base_url="http://127.0.0.1")
        self.assertEqual(client.post("/api/show").status_code, 200)
        shown.assert_called_once()
        with mock.patch.object(api.threading, "Timer") as timer:
            client.post("/api/shutdown")
        self.assertIs(timer.call_args.args[1], quit_)  # 桌面版退出走窗口和托盘的正常关闭


class LongTermTest(TempDB):
    CLAIMED = True

    """断档后续上、模型换代、用户对数据的选择（不参与分析 / 删除）。"""

    add, snap, codex_window, analyze = CalibrateTest.add, CalibrateTest.snap, CalibrateTest.codex_window, CalibrateTest.analyze

    def two_periods(self, now, old_model="a", new_model="a"):
        """断档前约 100 天的 8 个窗口（值 $10），停了 3 个多月后续上的 4 个窗口（值 $6）。"""
        with db.writer() as conn:
            for i in range(8):
                self.codex_window(conn, f"old{i}", now - (110 - 2 * i) * 86400, [(0.5, 5)] * 10, model=old_model)
            for i in range(4):
                self.codex_window(conn, f"new{i}", now - (60 - 12 * i) * 3600, [(0.3, 5)] * 10, model=new_model)

    def test_gap_compares_with_windows_before_the_break(self):
        now = 200 * 86400
        self.two_periods(now)
        group = self.analyze("codex", now)["groups"][0]
        # 以前：断档前的窗口超出 60 天就不算了，只能显示「样本不足」，断档期间的收紧会被悄悄吸收
        self.assertEqual(group["status"], "tighter")
        self.assertTrue(group["cross_gap"])
        self.assertAlmostEqual(group["ratio"], 0.6, delta=0.05)
        self.assertGreater(group["gap_days"], 80)
        roles = [p["role"] for p in group["trend"]]
        self.assertEqual((roles.count("baseline"), roles.count("recent")), (8, 4))

    def test_model_change_is_reported_not_alerted(self):
        now = 200 * 86400
        self.two_periods(now, old_model="old-model", new_model="new-model")
        group = self.analyze("codex", now)["groups"][0]
        # 前后没有共同模型：汇率会把收紧当成「新模型本来就贵」，所以不下结论，只给 API 等价金额的粗比
        self.assertEqual(group["status"], "model_changed")
        self.assertEqual((group["model_before"], group["model_after"]), ("old-model", "new-model"))
        self.assertAlmostEqual(group["raw_ratio"], 0.6, delta=0.05)

    def test_ignored_windows_and_plan(self):
        now = 50 * 86400
        with db.writer() as conn:
            for i in range(8):
                self.codex_window(conn, f"old{i}", now - (20 - 2 * i) * 86400, [(0.5, 5)] * 10)
            for i in range(4):
                self.codex_window(conn, f"new{i}", now - (60 - 12 * i) * 3600, [(0.3, 5)] * 10)
        recent = [w for w in self.analyze("codex", now)["windows"] if w["end"] > now - 4 * 86400]
        with db.writer() as conn:
            for w in recent[:2]:
                db.set_rule(conn, "codex", "window", w["key"], "ignore")
        group = self.analyze("codex", now)["groups"][0]
        self.assertEqual(group["status"], "insufficient")  # 最近只剩 2 个窗口，不够 3 个
        self.assertEqual(sum(p["excluded"] for p in group["trend"]), 2)
        with db.writer() as conn:
            db.set_rule(conn, "codex", "window", recent[0]["key"], None)  # 恢复一个
            db.set_rule(conn, "codex", "plan", "plus", "ignore")
        self.assertEqual(self.analyze("codex", now)["groups"][0]["status"], "ignored")

    def test_deleted_plan_does_not_come_back(self):
        home = Path(self.tmp.name) / "codex"
        f = home / "sessions" / "2026" / "09" / "30" / "rollout-a.jsonl"
        f.parent.mkdir(parents=True)
        lines, total = [b'{"type":"turn_context","payload":{"model":"gpt-x"}}\n'], [0, 0, 0]
        base = 1_790_762_400  # 2026-09-30T10:00:00Z；重置时间要在请求之后，请求才会挂到窗口上
        for i in range(1, 21):  # 前 10 次请求用 team 账号，后 10 次换成 plus 账号
            plan, reset5 = ("team", base + 5_000) if i <= 10 else ("plus", base + 20_000)
            last = [250_000, 0, 0]
            total = [t + x for t, x in zip(total, last)]
            lines.append(token_count(f"2026-09-30T10:{i:02d}:00Z", last, total,
                                     codex_rl(i * 1.0, reset5, 1.0, reset5 + 500_000, plan)))
        f.write_bytes(b"".join(lines))
        table = pricing.PriceTable.__new__(pricing.PriceTable)
        table.models = {"gpt-x": {"input": 2, "output": 10}}

        def counts():
            with db.reader() as conn:
                return (conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0],
                        {r[0] for r in conn.execute("SELECT DISTINCT plan_type FROM quota_snapshot")})

        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            collect_codex.sync(table, history_days=10_000)
            self.assertEqual(counts(), (20, {"team", "plus"}))
            with db.reader() as conn:
                team = [w for w in calibrate.analyze(conn, "codex", now=base + 30_000)["windows"] if w["plan_type"] == "team"]
            with db.writer() as conn:
                result = db.delete_plan_data(conn, "codex", "team", team)
            self.assertEqual(result["requests"], 10)
            self.assertEqual(counts(), (10, {"plus"}))
            # 重读全部日志（例如游标被清掉）：删掉的方案不会再导回来，别的方案照常
            with db.writer() as conn:
                conn.execute("DELETE FROM file_cursor")
            collect_codex.sync(table, history_days=10_000)
        self.assertEqual(counts(), (10, {"plus"}))

    def test_data_api(self):
        from fastapi.testclient import TestClient
        from quotalens.api import create_app
        from quotalens.service import Service
        with db.writer() as conn:
            self.codex_window(conn, "w", 1_000_000, [(0.5, 5)] * 4)
        svc = Service(notify_desktop=False)
        client = TestClient(create_app(svc), base_url="http://127.0.0.1")
        with mock.patch.object(collect_codex, "log_files", return_value=[]), \
                mock.patch.object(collect_claude, "log_files", return_value=[]):
            data = client.get("/api/data").json()
        self.assertEqual([(g["tool"], g["plan"], g["mode"]) for g in data["groups"]], [("codex", "plus", "keep")])
        body = {"tool": "codex", "kind": "plan", "target": "plus", "ignore": True}
        self.assertEqual(client.post("/api/data/rule", json=body, headers={"origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(client.post("/api/data/rule", json=body).status_code, 200)
        statuses = [g["status"] for g in svc.analysis()["tools"]["codex"]["groups"]]
        self.assertEqual(statuses, ["ignored"])


class OpenSourceTest(TempDB):
    """开源后别人的环境：数据目录迁移、设置、各系统的登录信息和通知、连接状态接口。"""

    def test_legacy_data_is_copied_not_moved(self):
        from quotalens import paths
        root = Path(self.tmp.name)
        legacy, new = root / "project" / "data", root / "appdata"
        legacy.mkdir(parents=True)
        (root / "project" / "prices_override.json").write_text('{"models": {}}', encoding="utf-8")
        with mock.patch.object(db, "DATA_DIR", legacy), mock.patch.object(db, "DB_PATH", legacy / "quotalens.db"):
            db.init_db()
            with db.writer() as conn:
                conn.execute("INSERT INTO usage (id, tool, ts, model, on_plan) VALUES ('a', 'codex', 1, 'm', 1)")
        self.assertEqual(paths.migrate_legacy_data(legacy, new), legacy)
        with mock.patch.object(db, "DB_PATH", new / "quotalens.db"), db.reader() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 1)
        self.assertTrue((legacy / "quotalens.db").exists())  # 旧的不删
        self.assertTrue((legacy / "MOVED.txt").exists())
        self.assertTrue((new / "prices_override.json").exists())
        self.assertIsNone(paths.migrate_legacy_data(legacy, new))  # 只迁一次

    def test_settings_are_validated_and_applied(self):
        from quotalens import settings
        from quotalens.service import Service
        values = settings.save({"min_change_pct": 500, "language": "fr", "notify": "yes", "unknown": 1})
        self.assertEqual(values["min_change_pct"], 60)       # 夹到上限
        self.assertEqual(values["language"], "auto")          # 不认识的值不收
        self.assertIs(values["notify"], True)
        svc = Service(notify_desktop=False)                   # 命令行覆盖优先
        self.assertFalse(svc.notify_desktop)
        self.assertAlmostEqual(svc.analysis_params["min_change"], 0.6)
        svc.update_settings({"min_change_pct": 25})
        self.assertAlmostEqual(svc.analysis_params["min_change"], 0.25)

    def test_claude_credentials_from_macos_keychain(self):
        home = Path(self.tmp.name) / "claude"
        home.mkdir()
        secret = json.dumps({"claudeAiOauth": {"accessToken": "tok", "subscriptionType": "pro",
                                               "expiresAt": 4_000_000_000_000}})
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout=secret))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}),                 mock.patch.object(claude_quota.sys, "platform", "darwin"),                 mock.patch.object(claude_quota.subprocess, "run", run):
            self.assertEqual(claude_quota.read_credentials(), ("tok", "pro", "ok"))
            self.assertEqual(run.call_args.args[0][:2], ["security", "find-generic-password"])
            run.return_value = mock.Mock(returncode=44, stdout="")
            self.assertEqual(claude_quota.read_credentials()[2], "missing")

    def test_notification_command_per_system(self):
        from quotalens import notify
        with mock.patch.object(notify.sys, "platform", "darwin"):
            args, env = notify.command("标题 \"x\"", "正文")
            self.assertEqual((args[0], args[-2:]), ("osascript", ["标题 \"x\"", "正文"]))  # 文字作为参数，不拼进脚本
        with mock.patch.object(notify.sys, "platform", "linux"),                 mock.patch.object(notify.shutil, "which", lambda name: None):
            self.assertIsNone(notify.command("a", "b"))       # 没有 notify-send 就跳过

    def test_backend_translation(self):
        from quotalens import i18n
        try:
            i18n.set_language("en")
            self.assertEqual(i18n.tr("立即同步"), "Sync now")
            self.assertEqual(i18n.tr("已创建：{path}", path="x"), "Created: x")
            i18n.set_language("zh")
            self.assertEqual(i18n.tr("已创建：{path}", path="x"), "已创建：x")
        finally:
            i18n.set_language("auto")

    def test_settings_and_connections_api(self):
        from fastapi.testclient import TestClient
        from quotalens.api import create_app
        from quotalens.service import Service
        client = TestClient(create_app(Service(notify_desktop=False)), base_url="http://127.0.0.1")
        self.assertEqual(client.post("/api/settings", json={"language": "en"}).json()["values"]["language"], "en")
        self.assertEqual(client.get("/api/settings").json()["values"]["language"], "en")
        with mock.patch.dict(os.environ, {"CODEX_HOME": self.tmp.name, "CLAUDE_CONFIG_DIR": self.tmp.name}),                 mock.patch.object(claude_quota.sys, "platform", "linux"):
            conns = {c["tool"]: c for c in client.get("/api/connections").json()}
        self.assertEqual((conns["claude"]["log_files"], conns["claude"]["credentials"]), (0, "missing"))
        self.assertNotIn("token", json.dumps(conns))

    def test_demo_data_tells_its_story(self):
        from quotalens import demo
        from quotalens.service import Service
        now = 1_790_900_000
        demo.populate(now=now)
        svc = Service(notify_desktop=False, demo=True)
        with mock.patch.object(calibrate.time, "time", return_value=now), db.reader() as conn:
            groups = {(tool, g["window"]): g for tool in ("codex", "claude")
                      for g in calibrate.analyze(conn, tool, now)["groups"]}
            claude_windows = calibrate.analyze(conn, "claude", now)["windows"]
            codex_windows = calibrate.analyze(conn, "codex", now)["windows"]
        self.assertEqual(groups[("codex", "five_hour")]["status"], "tighter")   # 演示的主角：收紧了 30%
        self.assertAlmostEqual(groups[("codex", "five_hour")]["ratio"], 0.7, delta=0.1)
        self.assertEqual(groups[("codex", "seven_day")]["status"], "stable")
        self.assertNotIn(groups[("claude", "five_hour")]["status"], ("tighter", "looser"))
        # 网页聊天：周分项里出现 Chats，那一周和重叠的 5 小时窗口采集不完整；不扣已用%
        chat_weeks = [w for w in claude_windows if w["window"] == "seven_day" and w["quality"] == "incomplete"]
        self.assertEqual(len(chat_weeks), 1)
        self.assertIn("uncollected_sources", [r["code"] for r in chat_weeks[0]["reasons"]])
        self.assertIn("claim_conflict", [r["code"] for r in chat_weeks[0]["reasons"]])
        chat_5h = [w for w in claude_windows if w["window"] == "five_hour" and w["quality"] == "incomplete"]
        self.assertGreaterEqual(len(chat_5h), 1)
        self.assertTrue(all(w["pct_clean"] == w["used_percent"] for w in claude_windows))
        self.assertTrue(any(w["window"] == "five_hour" and w["quality"] == "conditional" for w in claude_windows))
        self.assertLess(max(w["external_pct"] for w in codex_windows), 3)      # Codex 没有疑点，不能误报
        svc.start()  # 演示模式不起后台线程、不联网
        self.assertEqual({c["credentials"] for c in svc.connections()}, {"ok"})

    def test_events_carry_raw_params(self):
        with db.writer() as conn:
            conn.execute("INSERT INTO quota_snapshot VALUES ('codex', 'codex', 'seven_day', 1000, 5, 700000, 604800, 'free', 'log')")
            conn.execute("INSERT INTO quota_snapshot VALUES ('codex', 'codex', 'seven_day', 2000, 6, 700000, 604800, 'plus', 'log')")
            plan = [e for e in events.detect(conn, now=3000) if e["kind"] == "plan"][0]
        self.assertEqual(plan["params"], {"from": "free", "to": "plus"})


class AcceptanceTest(TempDB):
    """方案验收用例：默认未声明、声明边界、来源分项各档、陈旧分项、抖动去重、解析异常、旧库迁移。"""
    add = CalibrateTest.add
    snap = CalibrateTest.snap
    breakdown = CalibrateTest.breakdown
    T = 10_000

    def week(self, as_of_offset=2000, last_snap_offset=2000, **pcts):
        """一个从 T 开始的 Claude 周窗口：花 $120 用到 60%，在 as_of_offset 处给一组来源分项。"""
        T = self.T
        with db.writer() as conn:
            self.add(conn, "a", "claude", T + 1000, "claude-opus-5-5", 120.0)
            self.snap(conn, "claude", "all", "seven_day", T + 2000, 60.0, T + 604800, 604800)
            if last_snap_offset != 2000:
                self.snap(conn, "claude", "all", "seven_day", T + last_snap_offset, 60.0, T + 604800, 604800)
            self.breakdown(conn, T + as_of_offset, T, **pcts)

    def window(self, now=None):
        with db.reader() as conn:
            return calibrate.analyze(conn, "claude", now=now or self.T + 50_000)["windows"][0]

    def codes(self, w):
        return [r["code"] for r in w["reasons"]]

    # 默认状态：没有声明
    def test_default_no_claim_hides_fit_capacity_but_keeps_estimate(self):
        # 没有声明：拟合出的容量（cap、ref_cap）不给；趋势用的是窗口自己的估值（确定 + 推测），照常给
        self.week()
        with db.reader() as conn:
            result = calibrate.analyze(conn, "claude", now=self.T + 50_000)
        w = result["windows"][0]
        self.assertEqual(w["quality"], "unconfirmed")
        self.assertIn("no_claim", self.codes(w))
        self.assertIsNone(w["cap"])
        self.assertIsNotNone(w["estimate"])
        self.assertIsNotNone(w["value"])
        for g in result["groups"]:
            self.assertFalse(g["eligible"])
            self.assertFalse(g["declared"])
            self.assertIsNone(g["ref_cap"])
        pub = calibrate.public(w)
        for k in ("cap", "cap_low", "cap_high"):
            self.assertIsNone(pub.get(k))

    # 声明边界
    def test_claim_today_does_not_cover_earlier_window(self):
        self.claim("claude", start=self.T + 100)
        self.week()
        w = self.window()
        self.assertEqual(w["quality"], "unconfirmed")
        self.assertIn("claim_after_start", self.codes(w))

    def test_claim_of_other_account_does_not_apply(self):
        self.claim("claude", start=0, account="old")
        with db.writer() as conn:
            db.note_account(conn, "claude", self.T - 10, "new")
        self.week()
        w = self.window()
        self.assertEqual(w["quality"], "unconfirmed")
        self.assertIn("claim_other_account", self.codes(w))

    def test_window_spanning_account_switch(self):
        self.claim("claude", start=0, account="a1")
        with db.writer() as conn:
            db.note_account(conn, "claude", self.T + 1500, "a2")
        self.week()
        self.assertIn("account_changed", self.codes(self.window()))

    def test_claim_ended_before_window_end(self):
        self.claim("claude", start=0, end=self.T + 1500)
        self.week()
        self.assertIn("claim_ended", self.codes(self.window()))

    def test_switching_accounts_ends_old_claim(self):
        self.claim("claude", start=0, account="a1")
        with db.writer() as conn:
            db.set_coverage_claim(conn, "claude", "a2", True, now=500)
            claims = db.coverage_claims(conn, "claude")
        old = [c for c in claims if c["account"] == "a1"][0]
        self.assertIsNotNone(old["end"])
        self.assertTrue(any(c["account"] == "a2" and c["end"] is None for c in claims))

    # 方案中的来源分项验收行
    def test_code_84_chats_7_cowork_9_is_incomplete(self):
        self.claim("claude")
        self.week(claude_code=84, chat=7, cowork=9)
        w = self.window()
        self.assertEqual(w["quality"], "incomplete")
        self.assertIsNone(w["cap"])

    def test_code_20_unknown_80_with_claim_is_source_unknown(self):
        self.claim("claude")
        self.week(claude_code=20, brand_new_product=80)
        w = self.window()
        self.assertEqual(w["quality"], "source_unknown")
        self.assertIn("claim_conflict", self.codes(w))
        self.assertIsNone(w["cap"])

    def test_code_97_chats_3_is_conditional_with_bias(self):
        self.claim("claude")
        self.week(claude_code=97, chat=3)
        w = self.window()
        self.assertEqual(w["quality"], "conditional")
        self.assertAlmostEqual(w["bias_pct"], 3)
        self.assertAlmostEqual(w["cap"], 200.0)

    def test_estimate_splits_known_and_inferred_without_claim(self):
        # 未声明、Code 80%：确定 $120，推测 $30，总 $150；已用 60% → 用满约 $250
        self.week(claude_code=80, chat=20)
        w = self.window()
        self.assertIsNone(w["cap"])
        e = w["estimate"]
        self.assertAlmostEqual(e["local"], 120.0)
        self.assertAlmostEqual(e["inferred"], 30.0)
        self.assertAlmostEqual(e["cap"], 250.0)
        self.assertLess(e["cap_low"], 250.0)
        self.assertGreater(e["cap_high"], 250.0)

    def test_estimate_skipped_when_code_share_tiny(self):
        self.week(claude_code=10, chat=90)
        self.assertIsNone(self.window()["estimate"])

    def five_hour(self, start, cost, pct5, weekly, breakdowns):
        """周窗口从 T 开始；5h 窗口 [start, start+5h) 花 cost 用到 pct5。weekly: [(ts, 周%)]；breakdowns: [(ts, {key: %})]。"""
        T = self.T
        with db.writer() as conn:
            self.add(conn, "u5", "claude", start + 600, "claude-opus-5-5", cost)
            for ts, pct in weekly:
                self.snap(conn, "claude", "all", "seven_day", ts, pct, T + 604800, 604800)
            self.snap(conn, "claude", "all", "five_hour", start + 60, 0.0, start + 18000, 18000)
            self.snap(conn, "claude", "all", "five_hour", start + 4 * 3600, pct5, start + 18000, 18000)
            for ts, pcts in breakdowns:
                self.breakdown(conn, ts, T, **pcts)
        with db.reader() as conn:
            return [w for w in calibrate.analyze(conn, "claude", now=start + 20000)["windows"]
                    if w["window"] == "five_hour"][0]

    def test_five_hour_split_from_weekly_breakdown_change(self):
        # 窗口期间周% 20→30；非 Code 百分点 20×10%=2 → 30×20%=6，涨 4，所以窗口里 Code 占 60%
        s = self.T + 3600
        w = self.five_hour(s, 24.0, 50.0, [(s, 20.0), (s + 4 * 3600, 30.0)],
                           [(s, {"claude_code": 90, "chat": 10}), (s + 4 * 3600, {"claude_code": 80, "chat": 20})])
        e = w["estimate"]
        self.assertEqual(e["basis"], "window_delta")
        self.assertAlmostEqual(e["code_pct"], 60.0)
        self.assertAlmostEqual(e["total"], 40.0)    # $24 / 60%
        self.assertAlmostEqual(e["cap"], 80.0)      # $40 / 50%
        self.assertLess(e["cap_low"], 80.0)
        self.assertGreater(e["cap_high"], 80.0)

    def test_five_hour_pure_code_window(self):
        s = self.T + 3600
        w = self.five_hour(s, 24.0, 50.0, [(s, 20.0), (s + 4 * 3600, 30.0)],
                           [(s, {"claude_code": 90, "chat": 10}), (s + 4 * 3600, {"claude_code": 93, "chat": 7})])
        # 非 Code 百分点 20×10%=2 → 30×7%=2.1，只涨 0.1：Code 占 99%，取整误差内和纯 Code 一样
        self.assertAlmostEqual(w["estimate"]["code_pct"], 99.0)
        self.assertGreaterEqual(w["estimate"]["code_high"], 99.9)
        self.assertAlmostEqual(w["estimate"]["cap"], 48.0, delta=0.6)

    def test_five_hour_no_split_without_nearby_breakdown(self):
        # 窗口开始前和窗口内都没有分项（只有窗口结束后很久的一组）：拆不了
        s = self.T + 3600
        w = self.five_hour(s, 24.0, 50.0, [(s, 20.0), (s + 4 * 3600, 30.0), (s + 30000, 31.0)],
                           [(s + 30000, {"claude_code": 80, "chat": 20})])
        self.assertIsNone(w["estimate"])

    def test_five_hour_no_split_when_weekly_barely_moves(self):
        s = self.T + 3600
        w = self.five_hour(s, 2.0, 8.0, [(s, 20.0), (s + 4 * 3600, 21.0)],
                           [(s, {"claude_code": 90, "chat": 10}), (s + 4 * 3600, {"claude_code": 90, "chat": 10})])
        self.assertIsNone(w["estimate"])

    @staticmethod
    def sample(t, week, wpct, noncode):
        return {"t": t, "week": week, "w": wpct, "pp": noncode / 100 * wpct, "err": 0.0}

    def test_split_across_weekly_reset(self):
        # 旧周 50→54（非 Code 0→+2 点），新周从 0 到 6（非 Code 0）：周额度共涨 10，非 Code 2 → Code 80%
        w = {"start": 1000, "last_seen": 1000 + 4 * 3600, "points": [{"ts": 1000, "pct": 0.0}]}
        samples = [self.sample(900, 0, 50, 0), self.sample(5000, 0, 54, 100 * 2 / 54),
                   self.sample(1000 + 4 * 3600, 1, 6, 0)]
        sp = calibrate._window_split(w, samples)
        self.assertEqual(sp["weekly_delta"], 10)
        self.assertAlmostEqual(sp["code"], 80.0)
        self.assertLess(sp["code_low"], 80.0)

    def test_split_starts_inside_window_only_if_little_used(self):
        # 窗口开始前没有分项：用窗口内第一组当起点，那时 5h 才用了 2% 才行
        samples = [self.sample(2000, 0, 20, 10), self.sample(1000 + 4 * 3600, 0, 30, 10)]
        w = {"start": 1000, "last_seen": 1000 + 4 * 3600, "points": [{"ts": 1500, "pct": 2.0}]}
        self.assertAlmostEqual(calibrate._window_split(w, samples)["code"], 90.0)
        w["points"] = [{"ts": 1500, "pct": 30.0}]
        self.assertIsNone(calibrate._window_split(w, samples))

    def test_breakdown_paired_with_same_poll_snapshot(self):
        # 服务器的 as_of 比本机快照早 5 秒：应配上这次查询的周% 30，而不是上一次的 20
        week = {"start": 0, "end": 604800, "points": [{"ts": 1000, "pct": 20.0}, {"ts": 2005, "pct": 30.0}]}
        ctx = {"breakdowns": [{"as_of": 2000, "last_as_of": 2000, "window_start": 0, "invalid": False,
                               "noncode": 10.0, "noncode_err": 0.0}]}
        [smp] = calibrate._weekly_samples([week], ctx)
        self.assertEqual(smp["w"], 30.0)

    def test_claude_estimate_not_blocked_by_external_rise(self):
        # Claude 有来源分项：没有本机日志的涨幅本来就该是网页/App/Cowork，不按「疑似未记录」拦
        base = {"supported": True, "window": "seven_day", "used_percent": 60.0, "cost_at_snapshot": 30.0,
                "external_pct": 20.0, "reasons": [{"code": "suspect_unrecorded", "pct": 20.0}]}
        w = dict(base, sources={"code": 70.0, "known": 30.0, "unknown": 0.0}, reasons=list(base["reasons"]))
        calibrate._set_estimate(w)
        self.assertIsNotNone(w["estimate"])
        # Codex 假设全在本机，涨幅没有日志就说明有漏记，照拦
        w = dict(base, tool="codex", sources=None, reasons=list(base["reasons"]))
        calibrate._set_estimate(w)
        self.assertIsNone(w["estimate"])
        self.assertEqual(w["_est_why"], "suspect_unrecorded")

    def test_estimate_flags_unexplained_external_rise(self):
        # Code 占 99%，但窗口里有 14% 的涨幅没有本机日志：前提可能不成立，标出来
        w = {"supported": True, "window": "five_hour", "sources": None, "reasons": [], "used_percent": 100.0,
             "cost_at_snapshot": 30.0, "external_pct": 14.0,
             "_split": {"code": 99.0, "code_low": 97.0, "code_high": 100.0, "weekly_delta": 13, "noncode_delta": 0.1}}
        calibrate._set_estimate(w)
        self.assertAlmostEqual(w["estimate"]["conflict_pct"], 11.0)
        w["external_pct"] = 2.0
        calibrate._set_estimate(w)
        self.assertEqual(w["estimate"]["conflict_pct"], 0.0)

    def test_service_rebuilds_breakdowns_once(self):
        from quotalens.service import Service
        with mock.patch.object(claude_quota, "backfill_breakdowns", return_value=0) as bf:
            Service(notify_desktop=False)
            Service(notify_desktop=False)
            Service(notify_desktop=False, demo=True)
        bf.assert_called_once()

    def test_backfill_rebuild_recovers_history_before_live_rows(self):
        def body(code, chat):
            return {"seven_day": {"utilization": 30, "resets_at": "2026-10-09T00:00:00Z"},
                    claude_quota.BREAKDOWN_KEY: {"as_of": None, "window_started_at": "2026-10-02T00:00:00Z",
                                                 "rows": [{"key": "claude_code", "display_name": "Code", "percent": code},
                                                          {"key": "chat", "display_name": "Chats", "percent": chat}]}}
        with db.writer() as conn:
            db.archive_response(conn, "claude", 1000.0, body(90, 10))
            db.archive_response(conn, "claude", 2000.0, body(80, 20))
            claude_quota.store_breakdown(conn, claude_quota.parse_breakdown(body(80, 20)), 2000.0)
            self.assertEqual(claude_quota.backfill_breakdowns(conn), 0)   # 只追加：更早的补不进来
            self.assertEqual(claude_quota.backfill_breakdowns(conn, rebuild=True), 2)
            self.assertEqual([b["as_of"] for b in db.breakdowns(conn, "claude")], [1000.0, 2000.0])

    def test_combined_noncode_share_blocks(self):
        # 单看 Chats 3%、未知 3% 都不到 5%，但非 Code 合计 6% 到了
        self.claim("claude")
        self.week(claude_code=94, chat=3, other=3)
        w = self.window()
        self.assertEqual(w["quality"], "incomplete")
        self.assertIn("uncollected_sources", self.codes(w))
        self.assertIsNone(w["cap"])

    def test_stale_breakdown_is_not_conditional(self):
        # 分项停在第 2000 秒，但同一窗口最后一次快照在 5 天后
        self.claim("claude")
        self.week(last_snap_offset=5 * 86400)
        w = self.window(now=self.T + 6 * 86400)
        self.assertEqual(w["quality"], "unconfirmed")
        self.assertIn("breakdown_stale", self.codes(w))

    # 抖动与去重
    def test_breakdown_jitter_does_not_add_rows(self):
        rows = [{"key": "claude_code", "display_name": "Claude Code", "percent": 100}]
        with db.writer() as conn:
            first = db.save_breakdown(conn, "claude", "seven_day", 1000, 5000.0, rows)
            again = db.save_breakdown(conn, "claude", "seven_day", 1300, 5000.4, rows)
            n = conn.execute("SELECT COUNT(*) FROM source_breakdown").fetchone()[0]
            last = db.breakdowns(conn, "claude")
        self.assertTrue(first)
        self.assertFalse(again)
        self.assertEqual(n, 1)
        self.assertEqual(last[-1]["last_as_of"], 1300)

    # parse_breakdown 异常输入
    def test_parse_breakdown_bad_inputs(self):
        P = claude_quota.parse_breakdown
        key = claude_quota.BREAKDOWN_KEY
        self.assertIsNone(P({}))
        self.assertIn("not_object", P({key: []})["issues"])
        self.assertIn("empty", P({key: {"rows": []}})["issues"])
        dup = P({key: {"rows": [{"key": "a", "percent": 50}, {"key": "a", "percent": 50}]}})
        self.assertIn("duplicate_key", dup["issues"])
        self.assertEqual(len(dup["rows"]), 1)
        for bad in (float("nan"), "50", 150, True):
            r = P({key: {"rows": [{"key": "a", "percent": bad}]}})
            self.assertIsNone(r["rows"][0]["percent"])
            self.assertIn("bad_percent", r["issues"])
        self.assertIn("sum_mismatch", P({key: {"rows": [{"key": "a", "percent": 50}]}})["issues"])

    def test_breakdown_issues_are_stored_and_block(self):
        self.claim("claude")
        rows = [{"key": "claude_code", "display_name": None, "percent": 50}]
        with db.writer() as conn:
            self.add(conn, "a", "claude", self.T + 1000, "claude-opus-5-5", 120.0)
            self.snap(conn, "claude", "all", "seven_day", self.T + 2000, 60.0, self.T + 604800, 604800)
            db.save_breakdown(conn, "claude", "seven_day", self.T + 2000, self.T, rows, issues=["sum_mismatch"])
            stored = db.breakdowns(conn, "claude")
        self.assertIn("sum_mismatch", stored[-1]["issues"])
        self.assertNotEqual(self.window()["quality"], "conditional")

    # 旧库迁移
    def test_migration_adds_analysis_version(self):
        with db.writer() as conn:
            conn.execute("DROP TABLE alert")
            conn.execute("CREATE TABLE alert (id INTEGER PRIMARY KEY, ts REAL, tool TEXT, scope TEXT, window TEXT,"
                         " direction TEXT, ratio REAL, title TEXT, detail TEXT)")
            conn.execute("INSERT INTO alert (ts, tool, scope, window, direction, ratio, title, detail)"
                         " VALUES (1, 'codex', 'codex', 'five_hour', 'tighter', 0.5, 't', 'd')")
        db.init_db()
        with db.reader() as conn:
            row = conn.execute("SELECT analysis_version FROM alert").fetchone()
        self.assertEqual(row[0], 1)


class EstimateTrendTest(TempDB):
    """趋势用每个窗口的估值：不要求覆盖声明；分析起点之前的窗口不参与。"""
    add, snap, codex_window, analyze = CalibrateTest.add, CalibrateTest.snap, CalibrateTest.codex_window, None

    def run_analyze(self, now, since=0):
        with db.reader() as conn:
            return calibrate.analyze(conn, "codex", now=now, params={"since": since})

    def seed(self, now):
        with db.writer() as conn:
            for i in range(8):   # 之前 3 周：窗口值 $10
                self.codex_window(conn, f"old{i}", now - (20 - 2 * i) * 86400, [(0.5, 5)] * 10)
            for i in range(4):   # 最近 3 天：窗口值 $6
                self.codex_window(conn, f"new{i}", now - (60 - 12 * i) * 3600, [(0.3, 5)] * 10)

    def test_codex_judged_without_claim_but_not_declared(self):
        now = 50 * 86400
        self.seed(now)
        result = self.run_analyze(now)
        w = result["windows"][-1]
        self.assertEqual(w["estimate"]["basis"], "local_only")
        self.assertAlmostEqual(w["estimate"]["cap"], 6.0, delta=0.01)
        self.assertIsNone(w["cap"])   # 拟合容量仍然只在声明下给
        g = result["groups"][0]
        self.assertEqual(g["status"], "tighter")
        self.assertAlmostEqual(g["ratio"], 0.6, delta=0.05)
        self.assertTrue(g["eligible"])
        self.assertFalse(g["declared"])   # 含推测前提：页面显示，不写告警

    def test_since_drops_older_windows(self):
        now = 50 * 86400
        self.seed(now)
        # 起点放在旧窗口之后：没有基线，不判断
        g = self.run_analyze(now, since=now - 4 * 86400)["groups"][0]
        self.assertEqual(g["status"], "insufficient")
        self.assertEqual(g["baseline_n"], 0)
        self.assertEqual(g["since"], now - 4 * 86400)
        trend = [p for p in g["trend"] if p["in_trend"]]
        self.assertTrue(trend and all(p["start"] >= now - 4 * 86400 for p in trend))
        # 起点之前的窗口照样有估值，只是不参与比较
        windows = self.run_analyze(now, since=now - 4 * 86400)["windows"]
        self.assertTrue(all(w["estimate"] for w in windows))

    def test_blocked_reasons_come_from_missing_estimates(self):
        now = 50 * 86400
        with db.writer() as conn:
            for i in range(8):   # 之前：干净
                self.codex_window(conn, f"old{i}", now - (20 - 2 * i) * 86400, [(0.5, 5)] * 10)
            # 最近：每个窗口都有一大段没有本机日志的涨幅，疑似在别处用过，不给估值
            for i in range(4):
                self.codex_window(conn, f"new{i}", now - (60 - 12 * i) * 3600, [(0.5, 5)] * 4 + [(0, 30)])
        result = self.run_analyze(now)
        recent = [w for w in result["windows"] if w["start"] >= now - 4 * 86400]
        self.assertTrue(recent and all(w["estimate"] is None for w in recent))
        self.assertTrue(all(w["_est_why"] == "suspect_unrecorded" for w in recent))
        g = result["groups"][0]
        self.assertEqual(g["status"], "not_eligible")
        self.assertIn("suspect_unrecorded", g["blocked"])
        self.assertNotIn("no_claim", g["blocked"])


if __name__ == "__main__":
    unittest.main()
