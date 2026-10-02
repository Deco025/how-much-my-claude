"""浏览器版：只起后台服务，页面在浏览器里看。

  how-much-my-claude-server [--open]    （从源码运行：python run.py）
  然后访问 http://127.0.0.1:8787

  how-much-my-claude-server --demo --open
    用虚构的演示数据看看界面长什么样：数据放在临时目录，不读真实日志、不查额度、不联网。

日常用桌面版就行（how-much-my-claude，或 install 创建的快捷方式）。
"""
import argparse
import logging
import os
import sys
import tempfile
import webbrowser

LOG_MAX_BYTES = 5 * 1024 * 1024


def log_to_file_without_console():
    """用 pythonw / 图形入口启动时没有控制台，stdout/stderr 是 None，uvicorn 和 logging 写日志会出错，改写到文件。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from . import paths
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = paths.LOG_PATH
    mode = "w" if path.exists() and path.stat().st_size > LOG_MAX_BYTES else "a"
    sys.stdout = sys.stderr = open(path, mode, encoding="utf-8", buffering=1)


def setup():
    """所有入口启动时先做：日志、把旧版数据搬到新的数据目录（在打开数据库之前）。"""
    from . import paths
    log_to_file_without_console()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    paths.migrate_legacy_data()


def _use_demo_dirs():
    """演示模式：数据目录和两个 CLI 的目录都指向空的临时目录。必须在导入 quotalens 的其他模块之前调用。"""
    root = tempfile.mkdtemp(prefix="how-much-my-claude-demo-")
    for name, sub in (("QUOTALENS_DATA_DIR", "data"), ("CODEX_HOME", "codex"), ("CLAUDE_CONFIG_DIR", "claude")):
        os.environ[name] = os.path.join(root, sub)
        os.makedirs(os.environ[name])
    return root


def main():
    parser = argparse.ArgumentParser(description="How much my Claude：把 Codex / Claude 订阅用量折算成等价 API 金额")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--demo", action="store_true", help="用虚构的演示数据运行（不碰真实数据）")
    parser.add_argument("--poll-minutes", type=int, help="临时覆盖「设置」里 Claude 空闲时查额度的间隔")
    parser.add_argument("--history-days", type=int, help="临时覆盖「设置」里首次导入多久以内的日志")
    parser.add_argument("--no-notify", action="store_true", help="这次运行不弹系统通知")
    args = parser.parse_args()
    if args.demo:
        _use_demo_dirs()
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    else:
        setup()

    import uvicorn

    from . import paths
    from .api import create_app
    from .service import Service

    if args.demo:
        from . import db, demo
        db.init_db()
        counts = demo.populate()
        service = Service(notify_desktop=False, demo=True)
        logging.getLogger("quotalens").info("演示模式：已生成 %d 次请求、%d 条额度快照", counts["usage"], counts["snapshots"])
    else:
        service = Service(claude_poll_minutes=args.poll_minutes, history_days=args.history_days,
                          notify_desktop=False if args.no_notify else None)
    logging.getLogger("quotalens").info("How much my Claude 已启动：http://127.0.0.1:%d（数据在 %s）",
                                        args.port, paths.DATA_DIR)
    if args.open:
        webbrowser.open(f"http://127.0.0.1:{args.port}")
    uvicorn.run(create_app(service), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
