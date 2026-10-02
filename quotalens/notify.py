"""系统桌面通知，不装额外依赖：
  Windows  系统自带的 PowerShell 弹 Toast
  macOS    osascript 的 display notification
  Linux    notify-send（大多数桌面环境自带；没有就跳过）
"""
import base64
import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger("quotalens")

# 借用 PowerShell 的 AppUserModelID，未注册自己的应用也能弹出通知
APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
APP_NAME = "How much my Claude"

WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual></toast>')
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).AppendChild($xml.CreateTextNode($env:QL_TITLE)) | Out-Null
$texts.Item(1).AppendChild($xml.CreateTextNode($env:QL_BODY)) | Out-Null
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:QL_APP).Show($toast)
"""
# 标题、正文作为参数传进去，不拼进脚本，避免引号转义问题
MAC_SCRIPT = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
              "-e", "end run"]


def command(title: str, body: str):
    """(要运行的命令, 额外的环境变量)；当前系统弹不了通知时返回 None。"""
    if sys.platform == "win32":
        encoded = base64.b64encode(WINDOWS_SCRIPT.encode("utf-16-le")).decode()
        return (["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                {"QL_TITLE": title, "QL_BODY": body, "QL_APP": APP_ID})
    if sys.platform == "darwin":
        return ["osascript", *MAC_SCRIPT, title, body], {}
    if shutil.which("notify-send"):
        return ["notify-send", f"--app-name={APP_NAME}", title, body], {}
    return None


def toast(title: str, body: str) -> bool:
    cmd = command(title, body)
    if cmd is None:
        return False
    args, extra_env = cmd
    try:
        subprocess.run(args, env={**os.environ, **extra_env}, capture_output=True, timeout=20, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("桌面通知失败: %s", e)
        return False
