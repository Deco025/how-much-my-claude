"""Windows 桌面通知（用系统自带的 PowerShell，不装额外依赖）。其他系统静默跳过。"""
import base64
import logging
import os
import subprocess
import sys

log = logging.getLogger("quotalens")

# 借用 PowerShell 的 AppUserModelID，未注册自己的应用也能弹出通知
APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

SCRIPT = r"""
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


def toast(title: str, body: str) -> bool:
    if sys.platform != "win32":
        return False
    encoded = base64.b64encode(SCRIPT.encode("utf-16-le")).decode()
    # 文本走环境变量传入，不拼进脚本，避免引号转义问题
    env = {**os.environ, "QL_TITLE": title, "QL_BODY": body, "QL_APP": APP_ID}
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                       env=env, capture_output=True, timeout=20, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("桌面通知失败: %s", e)
        return False
