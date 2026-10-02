"""创建快捷方式和开机自启，以后双击就能打开，不用开命令行。

  how-much-my-claude-install            创建快捷方式（从源码运行：python install.py）
  how-much-my-claude-install --startup  另外设为开机自启：登录后在后台运行，不弹窗口
  how-much-my-claude-install --remove   删除以上创建的快捷方式

各系统放在哪里：
  Windows  桌面上的「How much my Claude」；开机自启放在「启动」文件夹
  macOS    ~/Applications/How much my Claude.app；开机自启是 ~/Library/LaunchAgents 里的 plist
  Linux    应用菜单（~/.local/share/applications）；开机自启在 ~/.config/autostart
开机自启也可以在托盘菜单里开关。从源码运行时快捷方式指向项目里的 desktop.pyw，移动项目目录后要重新运行一次。
"""
import functools
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .i18n import tr
from .paths import APP_NAME, PROJECT_ROOT, WEB_DIR

NAME = "How much my Claude"
LEGACY_NAMES = ("额度透视.lnk",)  # 改名前的 Windows 快捷方式，安装 / 卸载时一并清掉
SOURCE_ENTRY = PROJECT_ROOT / "desktop.pyw"

# 路径、参数都走环境变量传给 PowerShell，避免中文和空格的转义问题
SHORTCUT_SCRIPT = """
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:QL_LNK)
$s.TargetPath = $env:QL_TARGET
$s.Arguments = $env:QL_ARGS
$s.WorkingDirectory = $env:QL_DIR
$s.IconLocation = $env:QL_ICON
$s.Description = $env:QL_DESC
$s.Save()
"""


def _python() -> Path:
    """Windows 上用 pythonw，启动时不弹命令行窗口。"""
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.with_name("pythonw.exe").exists():
        return exe.with_name("pythonw.exe")
    return exe


def launch_command(hidden=False) -> list:
    """打开桌面版的命令。从源码运行时用 desktop.pyw，pip 安装的用 python -m quotalens.desktop。"""
    cmd = [str(_python()), str(SOURCE_ENTRY)] if SOURCE_ENTRY.exists() else [str(_python()), "-m", "quotalens.desktop"]
    return cmd + (["--hidden"] if hidden else [])


# ── 各系统的位置 ─────────────────────────────────────────

@functools.lru_cache(maxsize=None)
def _windows_folder(name: str) -> Path:
    """Desktop / Startup 的实际位置（桌面可能被 OneDrive 重定向）。查一次就缓存，托盘菜单会反复问。"""
    out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                          f"[Environment]::GetFolderPath('{name}')"],
                         capture_output=True, text=True, check=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return Path(out.stdout.strip())


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def shortcut_path() -> Path:
    if sys.platform == "win32":
        return _windows_folder("Desktop") / f"{NAME}.lnk"
    if sys.platform == "darwin":
        return Path.home() / "Applications" / f"{NAME}.app"
    return _xdg("XDG_DATA_HOME", ".local/share") / "applications" / f"{APP_NAME}.desktop"


def startup_path() -> Path:
    if sys.platform == "win32":
        return _windows_folder("Startup") / f"{NAME}.lnk"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{APP_NAME}.plist"
    return _xdg("XDG_CONFIG_HOME", ".config") / "autostart" / f"{APP_NAME}.desktop"


# ── 文件内容 ─────────────────────────────────────────────

def _desktop_quote(arg: str) -> str:
    """freedesktop .desktop 文件 Exec 行的转义。"""
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    return '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'


def desktop_entry(cmd: list, autostart: bool) -> str:
    lines = ["[Desktop Entry]", "Type=Application", f"Name={NAME}",
             "Comment=Codex / Claude subscription usage in API dollars",
             "Exec=" + " ".join(_desktop_quote(a) for a in cmd),
             f"Icon={WEB_DIR / 'icon.png'}", "Terminal=false", "Categories=Utility;"]
    if autostart:
        lines.append("X-GNOME-Autostart-enabled=true")
    return "\n".join(lines) + "\n"


def launch_agent(cmd: list) -> bytes:
    """macOS 登录时自动运行的 LaunchAgent。"""
    return plistlib.dumps({"Label": APP_NAME, "ProgramArguments": cmd, "RunAtLoad": True,
                           "ProcessType": "Interactive"})


def app_bundle_files(cmd: list) -> dict:
    """macOS 的 .app：只有一个启动脚本，双击时运行桌面版。返回 {相对路径: 内容}。"""
    info = plistlib.dumps({"CFBundleName": NAME, "CFBundleDisplayName": NAME, "CFBundleIdentifier": APP_NAME,
                           "CFBundleExecutable": "launcher", "CFBundlePackageType": "APPL"})
    script = "#!/bin/sh\nexec " + shlex.join(cmd) + ' "$@"\n'
    return {"Contents/Info.plist": info, "Contents/MacOS/launcher": script.encode()}


def _make_windows_shortcut(path: Path, cmd: list, description: str) -> None:
    env = {**os.environ, "QL_LNK": str(path), "QL_TARGET": cmd[0], "QL_ARGS": subprocess.list2cmdline(cmd[1:]),
           "QL_DIR": str(PROJECT_ROOT if SOURCE_ENTRY.exists() else Path.home()),
           "QL_ICON": str(WEB_DIR / "icon.ico"), "QL_DESC": description}
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SHORTCUT_SCRIPT],
                   env=env, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _write(path: Path, autostart: bool) -> None:
    cmd = launch_command(hidden=autostart)
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        _make_windows_shortcut(path, cmd, tr("登录后在后台运行 How much my Claude") if autostart
                               else tr("打开 How much my Claude 桌面版"))
    elif sys.platform == "darwin" and autostart:
        path.write_bytes(launch_agent(cmd))
    elif sys.platform == "darwin":
        for rel, content in app_bundle_files(cmd).items():
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (path / "Contents/MacOS/launcher").chmod(0o755)
    else:
        path.write_text(desktop_entry(cmd, autostart), encoding="utf-8")


def _remove(path: Path) -> bool:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
    else:
        return False
    return True


# ── 对外 ─────────────────────────────────────────────────

def startup_enabled() -> bool:
    return startup_path().exists()


def set_startup(enabled: bool) -> None:
    if enabled:
        _write(startup_path(), autostart=True)
    else:
        _remove(startup_path())


def _legacy_shortcuts():
    if sys.platform != "win32":
        return []
    return [_windows_folder(folder) / name for folder in ("Desktop", "Startup") for name in LEGACY_NAMES]


def main():
    if "--remove" in sys.argv:
        for path in [shortcut_path(), startup_path(), *_legacy_shortcuts()]:
            if _remove(path):
                print(tr("已删除：{path}", path=path))
        return
    for path in _legacy_shortcuts():
        _remove(path)
    _write(shortcut_path(), autostart=False)
    print(tr("已创建：{path}", path=shortcut_path()))
    if "--startup" in sys.argv:
        set_startup(True)
        print(tr("已创建：{path}", path=startup_path()))


if __name__ == "__main__":
    main()
