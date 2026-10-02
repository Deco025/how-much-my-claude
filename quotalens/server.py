"""浏览器版：只起后台服务，页面在浏览器里看。

  how-much-my-claude-server [--open]    （从源码运行：python run.py）
  然后访问 http://127.0.0.1:8787

日常用桌面版就行（how-much-my-claude，或 install 创建的快捷方式）。
"""
import argparse
import logging
import sys
import webbrowser

from . import paths

LOG_MAX_BYTES = 5 * 1024 * 1024


def log_to_file_without_console():
    """用 pythonw / 图形入口启动时没有控制台，stdout/stderr 是 None，uvicorn 和 logging 写日志会出错，改写到文件。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = paths.LOG_PATH
    mode = "w" if path.exists() and path.stat().st_size > LOG_MAX_BYTES else "a"
    sys.stdout = sys.stderr = open(path, mode, encoding="utf-8", buffering=1)


def setup():
    """所有入口启动时先做：日志、把旧版数据搬到新的数据目录（在打开数据库之前）。"""
    log_to_file_without_console()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    paths.migrate_legacy_data()


def main():
    parser = argparse.ArgumentParser(description="How much my Claude：把 Codex / Claude 订阅用量折算成等价 API 金额")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--poll-minutes", type=int, help="临时覆盖「设置」里 Claude 空闲时查额度的间隔")
    parser.add_argument("--history-days", type=int, help="临时覆盖「设置」里首次导入多久以内的日志")
    parser.add_argument("--no-notify", action="store_true", help="这次运行不弹系统通知")
    args = parser.parse_args()
    setup()

    import uvicorn

    from .api import create_app
    from .service import Service

    service = Service(claude_poll_minutes=args.poll_minutes, history_days=args.history_days,
                      notify_desktop=False if args.no_notify else None)
    logging.getLogger("quotalens").info("How much my Claude 已启动：http://127.0.0.1:%d（数据在 %s）",
                                        args.port, paths.DATA_DIR)
    if args.open:
        webbrowser.open(f"http://127.0.0.1:{args.port}")
    uvicorn.run(create_app(service), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
