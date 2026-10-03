"""HTTP 接口 + 静态页面。只监听 127.0.0.1。"""
import os
import threading
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import calibrate, collect_claude, collect_codex, db, pricing, settings, stats
from .paths import WEB_DIR
from .service import Service

TOOLS = ("codex", "claude")
RELIABLE_RATE_WINDOWS = 4  # 模型至少出现在这么多窗口里，汇率才算可靠
LOCAL_HOSTS = ["127.0.0.1", "localhost"]


def _exit_process():
    os._exit(0)


def create_app(service: Service, on_show=None, on_quit=None) -> FastAPI:
    """on_show / on_quit 由桌面版传入（把窗口调到前面 / 退出整个程序）；浏览器版不传。"""
    quit_app = on_quit or _exit_process

    @asynccontextmanager
    async def lifespan(_app):
        service.start()
        yield
        service.stop()

    app = FastAPI(title="How much my Claude", lifespan=lifespan)
    # 只认本机域名：挡住 DNS 重绑定（外部网页把自己的域名解析到 127.0.0.1 来读数据）
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        """拒绝别的网站发来的写请求（跨站表单 POST /api/sync 会让工具拿你的 token 去查额度）。"""
        origin = request.headers.get("origin")
        if request.method != "GET" and origin and urlsplit(origin).hostname not in LOCAL_HOSTS:
            return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)
        return await call_next(request)

    def _group_of(result, w):
        return next((g for g in result["groups"] if (g["scope"], g["window"], g.get("plan_type"))
                     == (w["scope"], w["window"], w["plan_type"])), None)

    def _current(w, group):
        out = calibrate.public(w)
        out["remaining_if_only"] = []
        for key in ("ref_model", "ref_cap", "baseline_median", "recent_median", "status", "ratio", "threshold",
                    "eligible", "blocked", "fit_basis"):
            out[key] = group.get(key) if group else None
        # 「按某模型还能用多少钱」也是容量估值：只在窗口可作条件估计时给
        if not group or not w["active"] or w.get("quality") != "conditional" or group.get("fit_basis") != "conditional":
            return out
        left = max(0.0, 100 - w["used_percent"])
        for r in group.get("rates", []):
            if r["unit"] == "usd" and r["cap_if_only"]:
                out["remaining_if_only"].append({"model": r["model"], "usd": left / r["pct_per_unit"],
                                                 "reliable": r["windows"] >= RELIABLE_RATE_WINDOWS})
        return out

    @app.get("/api/overview")
    def overview():
        analysis = service.analysis()
        tools = []
        for tool in TOOLS:
            result = analysis["tools"][tool]
            current = calibrate.current_windows(result["windows"])
            current.sort(key=lambda w: (not w["supported"], w["window"] != "five_hour", w["scope"]))
            tools.append({
                "tool": tool,
                "plan": next((w["plan_type"] for w in current if w["plan_type"]), None),
                "windows": [_current(w, _group_of(result, w)) for w in current],
            })
        claude_plan, cred = service.claude_plan_and_credentials()
        for t in tools:
            if t["tool"] == "claude":
                t["plan"] = t["plan"] or claude_plan
                t["credential_status"] = cred
        return {"now": time.time(), "analysis_at": analysis["at"], "tools": tools, "status": service.status}

    @app.get("/api/monitor")
    def monitor():
        analysis = service.analysis()
        groups = [g for tool in TOOLS for g in analysis["tools"][tool]["groups"] if g.get("status") != "unsupported"]
        with db.reader() as conn:
            alerts = [dict(r) for r in conn.execute("SELECT * FROM alert ORDER BY ts DESC LIMIT 20")]
        return {"analysis_at": analysis["at"], "groups": groups, "events": analysis["events"], "alerts": alerts}

    @app.get("/api/usage")
    def usage(range: str = "7d", tool: str = "all"):  # noqa: A002 — 查询参数名
        if range not in stats.RANGES:
            raise HTTPException(400, "range 只能是 today / 7d / 30d")
        with db.reader() as conn:
            return stats.usage_report(conn, range, tool)

    @app.get("/api/windows")
    def windows(tool: str = "codex", limit: int = 150):
        if tool not in TOOLS:
            raise HTTPException(400, "未知工具")
        result = service.analysis()["tools"][tool]
        rows = sorted(result["windows"], key=lambda w: w["end"], reverse=True)[:limit]
        return [calibrate.public(w) for w in rows]

    @app.get("/api/window")
    def window_detail(tool: str, scope: str, window: str, end: float):
        if tool not in TOOLS:
            raise HTTPException(400, "未知工具")
        for w in service.analysis()["tools"][tool]["windows"]:
            if w["scope"] == scope and w["window"] == window and abs(w["end"] - end) < 1:
                return calibrate.public(w, with_points=True)
        raise HTTPException(404, "找不到这个窗口")

    @app.get("/api/prices")
    def prices():
        """models：用过的模型和它们查到的价格；catalog：价格表里的全部模型（美元 / 百万 token）。"""
        rows = []
        with db.reader() as conn:
            for r in conn.execute("SELECT tool, model, on_plan, COUNT(*) AS requests, MAX(ts) AS last_used "
                                  "FROM usage GROUP BY tool, model, on_plan ORDER BY last_used DESC"):
                key, price = service.prices.find(r["model"])
                rows.append({**dict(r), "price_key": key, "price": price})
        table = service.prices
        catalog = [{"model": name, "source": table.sources.get(name), "alias_of": p.get("alias_of"),
                    **{k: p.get(k) for k in pricing.PRICE_KEYS},
                    "tiers": p.get("tiers") or [], "fast": p.get("fast")}
                   for name, p in sorted(table.models.items())]
        return {"fetched_at": table.fetched_at, "models": rows, "catalog": catalog,
                "override_path": str(pricing.PRICE_OVERRIDE_PATH)}

    # ── 设置、连接状态 ──────────────────────────────────

    @app.get("/api/settings")
    def get_settings():
        return {"values": service.settings, "defaults": settings.DEFAULTS, "limits": settings.LIMITS,
                "data_dir": str(db.DATA_DIR)}

    @app.post("/api/settings")
    def post_settings(changes: dict = Body(...)):
        return {"values": service.update_settings(changes)}

    @app.get("/api/coverage")
    def get_coverage():
        return service.coverage()

    @app.post("/api/coverage")
    def post_coverage(tool: str = Body(...), on: bool = Body(...)):
        """覆盖声明：这个账号此后所有消耗额度的使用都会被本项目采集到。绑定当前账号，从现在起生效。"""
        _check_tool(tool)
        try:
            return service.set_coverage(tool, on)
        except ValueError:
            raise HTTPException(409, "读不到当前登录的账号，声明不能生效") from None

    @app.get("/api/connections")
    def connections():
        """两个工具各自：本机有没有它的日志、登录信息找不找得到、最近一次查额度的结果。不含 token。"""
        return service.connections()

    # ── 数据管理 ────────────────────────────────────────

    @app.get("/api/data")
    def data_overview():
        """存了什么、按订阅方案分组，以及本机还有多少日志没导入。"""
        analysis = service.analysis()
        groups, logs = [], {}
        with db.reader() as conn:
            imported = {r[0] for r in conn.execute("SELECT path FROM file_cursor")}
            for tool, mod in (("codex", collect_codex), ("claude", collect_claude)):
                rules = db.data_rules(conn, tool)
                by_plan = {}
                for w in analysis["tools"][tool]["windows"]:
                    by_plan.setdefault(w["plan_type"], []).append(w)
                for plan, ws in by_plan.items():
                    groups.append({"tool": tool, "plan": plan, "first": min(w["start"] for w in ws),
                                   "last": max(w["last_seen"] for w in ws), "windows": len(ws),
                                   "ignored_windows": sum(w["excluded"] for w in ws),
                                   "mode": rules["plans"].get(plan, ("keep",))[0]})
                for plan, (mode, until) in rules["plans"].items():
                    if mode == "deleted" and plan not in by_plan:
                        groups.append({"tool": tool, "plan": plan, "mode": "deleted", "deleted_at": until})
                usage = conn.execute("SELECT COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last FROM usage WHERE tool = ?",
                                     (tool,)).fetchone()
                files = mod.log_files()
                pending = [f for f in files if str(f) not in imported]
                logs[tool] = {"requests": usage["n"], "first": usage["first"], "last": usage["last"],
                              "files": len(files), "pending": len(pending),
                              "pending_oldest": min((f.stat().st_mtime for f in pending), default=None)}
        backups = sorted((db.DATA_DIR / "backups").glob("*.db")) if (db.DATA_DIR / "backups").is_dir() else []
        return {"db_bytes": db.DB_PATH.stat().st_size if db.DB_PATH.exists() else 0, "data_dir": str(db.DATA_DIR),
                "backups": {"daily": sum(b.name.startswith("quotalens-") for b in backups),
                            "before_delete": sum(b.name.startswith("before-delete-") for b in backups)},
                "groups": groups, "logs": logs}

    def _check_tool(tool):
        if tool not in TOOLS:
            raise HTTPException(400, "未知工具")

    @app.post("/api/data/rule")
    def data_rule(tool: str = Body(...), kind: str = Body(...), target: str = Body(...), ignore: bool = Body(...)):
        """某个订阅方案 / 窗口：不参与分析（ignore=true）或恢复。数据本身不动。"""
        _check_tool(tool)
        if kind not in ("plan", "window"):
            raise HTTPException(400, "kind 只能是 plan / window")
        service.set_rule(tool, kind, target, "ignore" if ignore else None)
        return {"ok": True}

    @app.post("/api/data/delete")
    def data_delete(tool: str = Body(...), plan: str = Body(...)):
        _check_tool(tool)
        return service.delete_plan(tool, plan)

    @app.post("/api/data/import-history")
    def data_import_history():
        return service.import_history()

    @app.post("/api/sync")
    def sync():
        service.sync_now()
        return service.status

    @app.post("/api/shutdown")
    def shutdown():
        """退出程序（页面上的「停止服务」用）。

        数据库写入都在事务里，直接退出进程不会损坏数据；先回复再退出，调用方能拿到结果。
        """
        service.stop()
        threading.Timer(0.5, quit_app).start()
        return {"ok": True}

    @app.post("/api/show")
    def show():
        """桌面版已在运行时，再次双击图标会调这个接口，把现有窗口调到前面。"""
        if on_show is None:
            raise HTTPException(404, "不是桌面版，没有窗口")
        on_show()
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
    return app
