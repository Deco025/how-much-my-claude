"""生成应用图标 quotalens/web/icon.ico 和 icon.png（Linux / macOS 用）：Claude Code 吉祥物（像素小章鱼）做 🤔 思考状。

在 32×32 的像素格上画：方块身体、竖条眼睛、两侧小短手、四条小腿是吉祥物本来的样子；
🤔 的部分是眼睛往上看、一边眉毛挑起，头顶右上方一个问号。
身体是主体：放大到几乎占满画布并横向居中，任务栏里才和其他图标一样大、一样正。
放大到 256 正好是 8 倍，像素边缘保持锐利。

运行：python tools/make_icon.py（顺带输出 tools/icon_preview.png 方便查看）
"""
from pathlib import Path

from PIL import Image

SIZE = 32
CORAL = (217, 119, 87, 255)      # Claude 珊瑚色
INK = (26, 26, 26, 255)
GRAY = (110, 110, 110, 255)    # 浮在身体外的眉毛和气泡小点：深浅两种任务栏上都看得见


class Canvas:
    def __init__(self):
        self.img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    def rect(self, x0, y0, x1, y1, color):
        """填充 [x0, x1] × [y0, y1]（含端点）。"""
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                self.img.putpixel((x, y), color)

    def dots(self, points, color):
        for x, y in points:
            self.img.putpixel((x, y), color)


def draw() -> Image.Image:
    c = Canvas()
    # 身体占满画布宽度（连手 30 格，左右各留 1 格），横向居中；纵向让身体本身靠近中线，
    # 问号顶到上边，不按「身体 + 问号」的外框居中，否则身体会显得偏下。
    # 比例照吉祥物原图：身体 6×4 个单位、手和腿 1 个单位高，这里 1 单位 = 4 像素
    c.rect(4, 9, 27, 24, CORAL)
    c.rect(1, 17, 3, 20, CORAL)
    c.rect(28, 17, 30, 20, CORAL)
    for x in (6, 10, 20, 24):
        c.rect(x, 25, x + 1, 28, CORAL)

    # 眼睛往上看（贴近头顶）；右眉挑成一道小弧，浮在头顶上
    c.rect(8, 11, 9, 14, INK)
    c.rect(22, 11, 23, 14, INK)
    c.dots([(21, 8), (22, 7), (23, 7), (24, 8)], GRAY)

    # 问号（不加框，2 像素粗的笔画）贴在头顶右上方，和身体只隔 1 格
    question = [
        ".XXXX.",
        "XX..XX",
        "....XX",
        "..XXX.",
        "..XX..",
        "......",
        "..XX..",
        "..XX..",
    ]
    for dy, row in enumerate(question):
        c.dots([(25 + dx, dy) for dx, ch in enumerate(row) if ch == "X"], CORAL)
    return c.img


def main() -> Image.Image:
    base = draw()
    # 32 及其整数倍直接按像素放大，保持锐利（交给 Pillow 从 256 缩回 32 会发虚）；
    # 16、24、48 这类非整数倍才交给 Pillow 缩放
    exact = [base.resize((s, s), Image.NEAREST) for s in (64, 128, 256)]
    web = Path(__file__).resolve().parent.parent / "quotalens" / "web"
    exact[-1].save(web / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
                   append_images=[base, *exact[:-1]])
    exact[-1].save(web / "icon.png")
    return exact[-1]


if __name__ == "__main__":
    main().save(Path(__file__).with_name("icon_preview.png"))
