"""生成应用图标 quotalens/web/icon.ico 和 icon.png（Linux / macOS 用）：Claude Code 吉祥物举着放大镜。

在 32×32 的像素格上画：方块身体、竖条眼睛、两侧小短手、四条小腿，头顶右上方一个问号。
没有眉毛。放大镜压在右眼前，右眼隔着镜片放大；木柄从镜片斜到右手，近处粗、远处细。
宣传动画里的那只不从这里出，仍由 promo/mascot.js 画。
身体是主体：放大到几乎占满画布并横向居中，任务栏里才和其他图标一样大、一样正。
放大到 256 正好是 8 倍，像素边缘保持锐利。

运行：python tools/make_icon.py（顺带输出 tools/icon_preview.png 方便查看）
"""
from pathlib import Path

from PIL import Image

SIZE = 32
CORAL = (217, 119, 87, 255)      # Claude 珊瑚色
INK = (26, 26, 26, 255)
RIM = (195, 194, 183, 255)       # 细镜框
BLUE = (120, 180, 240)           # 镜片，半透明叠在眼睛上
GLINT = (255, 255, 255, 255)
WOOD = (122, 82, 48, 255)
LENS_ALPHA = 0.42

# 镜片：K 框，l 镜片，W 高光。和宣传动画的 MASCOT_LENS 同一张
LENS = [
    "..KKKKK..",
    ".KlllllK.",
    "KllWllllK",
    "KlWlllllK",
    "KlllllllK",
    "KlllllllK",
    "KlllllllK",
    ".KlllllK.",
    "..KKKKK..",
]
LENS_X, LENS_Y = 18, 8


class Canvas:
    def __init__(self):
        self.img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    def put(self, x, y, color):
        if 0 <= x < SIZE and 0 <= y < SIZE:
            self.img.putpixel((x, y), color)

    def rect(self, x0, y0, x1, y1, color):
        """填充 [x0, x1] × [y0, y1]（含端点）。"""
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                self.put(x, y, color)

    def dots(self, points, color):
        for x, y in points:
            self.put(x, y, color)

    def blend(self, x, y, rgb, alpha):
        r, g, b, a = self.img.getpixel((x, y))
        if a == 0:
            r, g, b = 0, 0, 0
        sr, sg, sb = rgb
        self.put(x, y, (
            int(sr * alpha + r * (1 - alpha)),
            int(sg * alpha + g * (1 - alpha)),
            int(sb * alpha + b * (1 - alpha)),
            255,
        ))


def draw() -> Image.Image:
    c = Canvas()
    # 身体占满画布宽度（连手 30 格，左右各留 1 格），横向居中。
    # 比例照吉祥物原图：身体 6×4 个单位、手和腿 1 个单位高，这里 1 单位 = 4 像素
    c.rect(4, 9, 27, 24, CORAL)
    c.rect(1, 17, 3, 20, CORAL)
    c.rect(28, 17, 30, 20, CORAL)
    for x in (6, 10, 20, 24):
        c.rect(x, 25, x + 1, 28, CORAL)

    c.rect(8, 11, 9, 14, INK)     # 左眼
    c.rect(21, 10, 23, 15, INK)   # 右眼，隔着镜片放大

    # 问号（不加框，2 像素粗的笔画）贴在头顶右上方
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

    # 柄从镜片斜向右下，收到右手里。近处粗，远处细。
    c.dots([
        (25, 16), (26, 16), (27, 16),
        (26, 17), (27, 17), (28, 17),
        (27, 18), (28, 18), (29, 18),
        (28, 19), (29, 19),
        (29, 20), (30, 20),
        (30, 21),
        (30, 22), (31, 22),
    ], WOOD)
    c.dots([
        (29, 17), (30, 17), (31, 17),
        (30, 18), (31, 18),
        (30, 19), (31, 19),
        (31, 20), (31, 21),
        (29, 21), (29, 22), (31, 23),
    ], CORAL)

    for dy, row in enumerate(LENS):
        for dx, ch in enumerate(row):
            x, y = LENS_X + dx, LENS_Y + dy
            if ch == "l":
                c.blend(x, y, BLUE, LENS_ALPHA)
            elif ch == "K":
                c.put(x, y, RIM)
            elif ch == "W":
                c.put(x, y, GLINT)
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
