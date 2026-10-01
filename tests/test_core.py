"""核心逻辑测试：python -m unittest discover tests"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quotalens import calibrate, claude_quota, codex_quota, collect_claude, collect_codex, db, events, pricing


class TempDB(unittest.TestCase):
    """每个用例一个独立的临时数据库。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        patches = [mock.patch.object(db, "DATA_DIR", root), mock.patch.object(db, "DB_PATH", root / "t.db")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        db.init_db()

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
    def add(self, conn, uid, tool, ts, model, cost, link=None):
        conn.execute("INSERT INTO usage (id, tool, ts, model, on_plan, cost_usd) VALUES (?, ?, ?, ?, 1, ?)",
                     (uid, tool, ts, model, cost))
        if link:
            conn.execute("INSERT INTO usage_window VALUES (?, ?, ?, ?, ?)", (uid, tool, *link))

    def snap(self, conn, tool, scope, window, ts, pct, resets, seconds, plan="plus"):
        conn.execute("INSERT INTO quota_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test')",
                     (tool, scope, window, ts, pct, resets, seconds, plan))

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
            self.add(conn, "a", "claude", 1000, "claude-opus-5-5", 120.0)
            self.add(conn, "x", "claude", 1500, "deepseek-flash", None)  # 第三方，不计入
            conn.execute("UPDATE usage SET on_plan = 0 WHERE id = 'x'")
            self.snap(conn, "claude", "all", "seven_day", 2000, 60.0, 600_000, 604800)
            self.add(conn, "b", "claude", 3000, "claude-opus-5-5", 30.0)
            self.snap(conn, "claude", "all", "seven_day", 4000, 15.0, 2500 + 604800, 604800)
        first, second = self.analyze("claude", 5000)["windows"]
        self.assertAlmostEqual(first["cap"], 200.0)   # $120 / 60%
        self.assertEqual(first["confidence"], "high")
        self.assertAlmostEqual(second["cap"], 200.0)  # $30 / 15%，不含第一个窗口的 $120
        self.assertEqual(second["confidence"], "medium")
        with db.reader() as conn:
            kinds = [e["kind"] for e in events.detect(conn, now=5000)]
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

    def test_external_jump_is_removed(self):
        # 每 $0.2 涨 2%（窗口值 $10），中间有一次本地只花 $0.01 却涨了 30%，另有一次空闲时涨 4%
        steps = [(0.2, 2)] * 5 + [(0.01, 30)] + [(0.2, 2)] * 5 + [(0, 4)]
        with db.writer() as conn:
            self.codex_window(conn, "w", 1_000_000, steps)
        w = self.analyze("codex", 1_020_000)["windows"][0]
        self.assertAlmostEqual(w["used_percent"], 54)
        self.assertAlmostEqual(w["external_pct"], 34, delta=0.5)
        self.assertTrue(w["contaminated"])
        self.assertAlmostEqual(w["cap"], 10.0, delta=0.5)  # $2.01 / 约 20%

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
                 "recent_n": 4, "baseline_n": 8, "ref_model": "a", "recent_median": 6.0, "baseline_median": 10.0}
        analysis = {"at": 1000.0, "tools": {"codex": {"groups": [group]}}}
        self.assertEqual(len(svc._record_alerts(analysis)), 1)
        self.assertEqual(svc._record_alerts({**analysis, "at": 2000.0}), [])
        self.assertEqual(len(svc._record_alerts({**analysis, "at": 1000.0 + 25 * 3600})), 1)

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


if __name__ == "__main__":
    unittest.main()
