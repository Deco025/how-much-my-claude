"""启动：python run.py  →  http://127.0.0.1:8787"""
import argparse
import logging
import webbrowser

import uvicorn

from quotalens.api import create_app
from quotalens.service import Service


def main():
    parser = argparse.ArgumentParser(description="额度透视：Codex / Claude 订阅用量折算等价 API 金额")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--poll-minutes", type=int, default=30,
                        help="Claude Code 空闲时轮询额度的间隔（使用中固定每 3 分钟一次）")
    parser.add_argument("--history-days", type=int, default=90, help="首次导入多久以内的日志")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--no-notify", action="store_true", help="发现额度变化时不弹 Windows 通知")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    service = Service(claude_poll_minutes=args.poll_minutes, history_days=args.history_days,
                      notify_desktop=not args.no_notify)
    if args.open:
        webbrowser.open(f"http://127.0.0.1:{args.port}")
    uvicorn.run(create_app(service), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
