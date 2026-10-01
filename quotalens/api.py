"""HTTP 接口 + 静态页面。只监听 127.0.0.1。"""
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import calibrate, claude_quota, db, stats
from .paths import PROJECT_ROOT
from .service import Service

WEB_DIR = PROJECT_ROOT / "web"
TOOLS = ("codex", "claude")
RELIABLE_RATE_WINDOWS = 4  # 模型至少出现在这么多窗口里，汇率才算可靠
LOCAL_HOSTS = ["127.0.0.1", "localhost"]


def create_app(service: Service) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        service.start()
        yield
        service.stop()

    app = FastAPI(title="quota-lens", lifespan=lifespan)
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
        for key in ("ref_model", "ref_cap", "baseline_median", "recent_median", "status", "ratio", "threshold"):
            out[key] = group.get(key) if group else None
        if not group or not w["active"]:
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
        _, claude_plan, cred = claude_quota.read_credentials()
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
        rows = []
        with db.reader() as conn:
            for r in conn.execute("SELECT tool, model, on_plan, COUNT(*) AS requests, MAX(ts) AS last_used "
                                  "FROM usage GROUP BY tool, model, on_plan ORDER BY last_used DESC"):
                key, price = service.prices.find(r["model"])
                rows.append({**dict(r), "price_key": key, "price": price})
        return {"fetched_at": service.prices.fetched_at, "models": rows}

    @app.post("/api/sync")
    def sync():
        service.sync_now()
        return service.status

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
    return app
