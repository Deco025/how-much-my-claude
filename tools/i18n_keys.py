"""列出页面上所有要翻译的中文，检查 web/i18n.js 的英文表有没有漏。

  python tools/i18n_keys.py         列出缺英文的中文原文（没有输出就是齐了）
  python tools/i18n_keys.py --all   列出全部中文原文

扫的是 app.js 里所有带中文的字符串字面量（含模板字符串 ${} 里的），和 index.html 的 data-i18n* 属性。
"""
import re
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "quotalens" / "web"
CJK = re.compile(r"[一-鿿「」（），。：；、？！…～◀▶]")
REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^")


def js_strings(src: str):
    """JS 源码里的字符串字面量（跳过注释和正则字面量）。"""
    out, i, n = [], 0, len(src)

    def read_quoted(i, quote):
        j, buf = i + 1, []
        while j < n and src[j] != quote:
            if src[j] == "\\":
                buf.append(src[j:j + 2])
                j += 2
                continue
            buf.append(src[j])
            j += 1
        return "".join(buf), j + 1

    def scan(i, stop=None):
        depth = 0
        while i < n:
            c = src[i]
            if stop and c == "{":
                depth += 1
            elif stop and c == "}":
                if depth == 0:
                    return i + 1
                depth -= 1
            if src.startswith("//", i):
                i = src.find("\n", i)
                i = n if i < 0 else i
            elif src.startswith("/*", i):
                i = src.find("*/", i) + 2
            elif c in "'\"":
                text, i = read_quoted(i, c)
                out.append(text)
            elif c == "`":
                i += 1
                buf = []
                while i < n and src[i] != "`":
                    if src[i] == "\\":
                        buf.append(src[i:i + 2])
                        i += 2
                    elif src.startswith("${", i):
                        buf.append("${}")
                        i = scan(i + 2, stop="}")
                    else:
                        buf.append(src[i])
                        i += 1
                out.append("".join(buf))
                i += 1
            elif c == "/" and _regex_allowed(src, i):
                j, in_class = i + 1, False
                while j < n and (src[j] != "/" or in_class):
                    if src[j] == "\\":
                        j += 1
                    elif src[j] == "[":
                        in_class = True
                    elif src[j] == "]":
                        in_class = False
                    j += 1
                i = j + 1
            else:
                i += 1
        return i

    scan(0)
    return out


def _regex_allowed(src, i):
    j = i - 1
    while j >= 0 and src[j] in " \t\n":
        j -= 1
    return j < 0 or src[j] in REGEX_BEFORE


def keys():
    found = []
    for s in js_strings((WEB / "app.js").read_text(encoding="utf-8")):
        s = s.replace("\\n", "\n")
        if CJK.search(s) and "${}" not in s and s not in found:
            found.append(s)
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for m in re.finditer(r'data-i18n(?:-tip|-aria|-title|-placeholder)?="([^"]*)"', html):
        if m.group(1) not in found:
            found.append(m.group(1))
    return found


def english_table():
    src = (WEB / "i18n.js").read_text(encoding="utf-8")
    start = src.index("const EN = {")
    body = src[start:src.index("\n};", start)]  # 只认行首的 }; ——值里可能有 "{when}; "
    strings = js_strings(body)
    return set(strings[0::2])  # 键、值交替出现


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    all_keys = keys()
    if "--all" in sys.argv:
        print("\n".join(all_keys))
    else:
        have = english_table()
        missing = [k for k in all_keys if k not in have]
        print("\n".join(missing))
