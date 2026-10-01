import os
import re
from datetime import datetime, timezone

_FRACTION = re.compile(r"\.(\d+)")

WINDOW_NAMES = {18000: "five_hour", 604800: "seven_day", 2592000: "thirty_day"}
WINDOW_SECONDS = {v: k for k, v in WINDOW_NAMES.items()}


def parse_ts(value):
    """ISO 8601 字符串 → epoch 秒；解析失败返回 None。

    Python 3.10 的 fromisoformat 不认 'Z'，小数位也只认 3 或 6 位，这里先规整。
    """
    if not isinstance(value, str) or not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    s = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def window_name(seconds) -> str:
    if seconds in WINDOW_NAMES:
        return WINDOW_NAMES[seconds]
    hours = int(seconds) // 3600
    return f"{hours // 24}_day" if hours >= 24 else f"{hours}_hour"


def iter_complete_lines(path, start: int):
    """从 start 字节处逐行读，只产出以换行结尾的完整行。

    产出 (line_bytes, offset_after_line)；写了一半的末行留给下一轮。
    """
    with open(path, "rb") as f:
        f.seek(start)
        offset = start
        for line in f:
            if not line.endswith(b"\n"):
                return
            offset += len(line)
            yield line, offset


def file_changed(path, cursor):
    """返回 (stat, 起始偏移, 是否从头重读)；文件没变返回 None。"""
    st = os.stat(path)
    if cursor and cursor["size"] == st.st_size and cursor["mtime_ns"] == st.st_mtime_ns:
        return None
    if cursor and st.st_size >= cursor["offset"]:
        return st, cursor["offset"], False
    return st, 0, True
