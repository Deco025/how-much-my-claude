"""后端直接给人看的少量文字（托盘菜单、系统通知、安装脚本）的英文版。

写法：tr("中文原文 {变量}", 变量=...)。中文原文就是键，界面语言是英文时查 EN，查不到就用中文。
页面上的翻译在 web/i18n.js，同样的写法。
"""
import locale
import os
import sys

EN = {
    # 托盘
    "打开 How much my Claude": "Open How much my Claude",
    "立即同步": "Sync now",
    "开机自启": "Start at login",
    "退出": "Quit",
    # 系统通知
    "{tool} {window}可能被收紧了 {change}%": "{tool} {window} may have been tightened by {change}%",
    "{tool} {window}可能被放宽了 {change}%": "{tool} {window} may have been loosened by {change}%",
    "5 小时窗口": "5-hour window",
    "每周窗口": "weekly window",
    "30 天窗口": "30-day window",
    "最近 {recent_n} 个窗口折合 {model} 约 ${recent:.1f}，之前 {baseline_n} 个窗口约 ${baseline:.1f}":
        "Recent {recent_n} windows ≈ ${recent:.1f} of {model}, previous {baseline_n} windows ≈ ${baseline:.1f}",
    "（跨断档：和 {day} 之前的窗口比）": " (across a break: compared with windows before {day})",
    # 安装脚本
    "已创建：{path}": "Created: {path}",
    "已删除：{path}": "Removed: {path}",
    "打开 How much my Claude 桌面版": "Open the How much my Claude desktop app",
    "登录后在后台运行 How much my Claude": "Run How much my Claude in the background after login",
}

_language = "auto"


def set_language(language: str) -> None:
    global _language
    _language = language


def _system_is_chinese() -> bool:
    if sys.platform == "win32":
        try:
            import ctypes
            return ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x04  # LANG_CHINESE
        except (AttributeError, OSError):
            pass
    for value in (os.environ.get("LC_ALL"), os.environ.get("LANG"), locale.getlocale()[0]):
        if value:
            return value.lower().startswith(("zh", "chinese"))
    return False


def current() -> str:
    if _language in ("zh", "en"):
        return _language
    return "zh" if _system_is_chinese() else "en"


def tr(text: str, **kwargs) -> str:
    template = EN.get(text, text) if current() == "en" else text
    return template.format(**kwargs) if kwargs else template
