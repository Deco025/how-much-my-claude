"""How much my Claude 桌面版：原生窗口 + 系统托盘。

  how-much-my-claude             打开窗口；已经在运行就把现有窗口调到前面（从源码运行：desktop.pyw）
  how-much-my-claude --hidden    启动时不弹窗口，只在后台采集（开机自启用）；再打开一次就显示窗口

Windows / Linux：关掉窗口只是缩到托盘，后台继续采集；托盘右键「退出」才真正退出。
macOS：托盘和窗口抢主线程，不放托盘；关掉窗口就退出，想一直采集请开开机自启。
窗口里显示的页面和浏览器版完全相同。
"""
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from . import paths

PORT = 8787
URL = f"http://127.0.0.1:{PORT}"

log = logging.getLogger("quotalens")


def _port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _post(path: str):
    """向已在运行的实例发请求，返回 HTTP 状态码；连不上返回 None。"""
    try:
        with urllib.request.urlopen(urllib.request.Request(URL + path, method="POST"), timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return None


def _hand_over_to_running_instance() -> bool:
    """端口已被占用时：桌面版就让它把窗口调到前面，本进程退出（返回 True）。

    占着端口的若是没有窗口的浏览器版，先让它退出，由本进程接管。
    """
    if not _port_open():
        return False
    if _post("/api/show") == 200:
        return True
    _post("/api/shutdown")
    for _ in range(40):
        if not _port_open():
            return False
        time.sleep(0.25)
    return True  # 关不掉别人的服务，就别再起一个抢端口


def _start_tray(show, sync, quit_app):
    """系统托盘；macOS 不放（pystray 和 pywebview 都要占主线程），其他系统起不来也不影响使用。"""
    if sys.platform == "darwin":
        return None
    try:
        import pystray
        from PIL import Image

        from . import install
        from .i18n import tr
        icon = paths.WEB_DIR / ("icon.ico" if sys.platform == "win32" else "icon.png")
        tray = pystray.Icon("how-much-my-claude", Image.open(icon), "How much my Claude", menu=pystray.Menu(
            pystray.MenuItem(tr("打开 How much my Claude"), show, default=True),  # 单击托盘图标也是这一项
            pystray.MenuItem(tr("立即同步"), sync),
            pystray.MenuItem(tr("开机自启"), lambda: install.set_startup(not install.startup_enabled()),
                             checked=lambda item: install.startup_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(tr("退出"), quit_app),
        ))
        tray.run_detached()
        return tray
    except Exception as e:  # noqa: BLE001 — 例如 Linux 桌面没有托盘
        log.warning("系统托盘不可用，关掉窗口就会退出：%s", e)
        return None


def main():
    from .server import setup
    setup()
    if _hand_over_to_running_instance():
        return

    import uvicorn
    import webview

    from .api import create_app
    from .service import Service

    if sys.platform == "win32":
        import ctypes
        # 任务栏用本程序自己的图标，不和其他 Python 程序归成一组
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(paths.APP_NAME)

    window = webview.create_window("How much my Claude", URL, width=1240, height=880, min_size=(420, 560),
                                   hidden="--hidden" in sys.argv, text_select=True, background_color="#F9F9F7")
    state = {"quitting": False, "minimized": False, "tray": None}
    window.events.minimized += lambda: state.update(minimized=True)
    window.events.restored += lambda: state.update(minimized=False)
    window.events.maximized += lambda: state.update(minimized=False)

    def show():
        window.show()
        if state["minimized"]:
            window.restore()

    def quit_app():
        state["quitting"] = True
        if state["tray"]:
            state["tray"].stop()
        window.destroy()

    def on_closing():
        if state["quitting"] or not state["tray"]:
            return True  # 没有托盘时关窗口就是退出，不然就找不回来了
        window.hide()  # 点关闭按钮只是缩到托盘，继续采集
        return False

    window.events.closing += on_closing

    service = Service()
    server = uvicorn.Server(uvicorn.Config(create_app(service, on_show=show, on_quit=quit_app),
                                           host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, name="quotalens-http", daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    if not server.started:
        # 几乎同时打开了两次：另一个实例抢先占了端口，交给它
        log.warning("端口 %d 被占用，交给已在运行的实例", PORT)
        _post("/api/show")
        return
    log.info("How much my Claude 桌面版已启动：%s（数据在 %s）", URL, paths.DATA_DIR)

    def sync_in_background():
        threading.Thread(target=service.sync_now, name="quotalens-sync", daemon=True).start()

    state["tray"] = _start_tray(show, sync_in_background, quit_app)

    # 阻塞到窗口被销毁（退出）为止；页面的本地存储放在数据目录，左上角标志等偏好能记住
    icon = paths.WEB_DIR / ("icon.ico" if sys.platform == "win32" else "icon.png")
    webview.start(private_mode=False, storage_path=str(paths.DATA_DIR / "webview"), icon=str(icon))

    server.should_exit = True
    service.stop()
    if state["tray"]:
        state["tray"].stop()
    log.info("How much my Claude 桌面版已退出")
    # 托盘和 WebView 运行时留下的后台线程会让进程多拖好几秒，收尾完直接结束
    os._exit(0)


if __name__ == "__main__":
    main()
