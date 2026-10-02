"""把宣传动画（页面地址带 ?story，见 quotalens/web/story.js）录成 GIF，给 README 用。

  python tools/record_story.py                       # 中文、浅色 → docs/images/story-zh-light.gif
  python tools/record_story.py --lang en --scheme dark

需要 Playwright（pip install playwright；有 Edge 或 Chrome 就不用另装浏览器，否则 playwright install chromium）
和 ffmpeg。剧情模式不请求任何接口，所以只起一个静态文件服务。
逐帧调用 __story.seek(t) 再截图，动图的节奏和截图快慢无关。
"""
import argparse
import functools
import http.server
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "quotalens" / "web"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve():
    handler = functools.partial(QuietHandler, directory=str(WEB))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def launch(p):
    for channel in ("msedge", "chrome", None):
        try:
            return p.chromium.launch(channel=channel) if channel else p.chromium.launch()
        except Exception:
            continue
    raise SystemExit("找不到可用的浏览器：装 Edge / Chrome，或运行 playwright install chromium")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lang", default="zh", choices=["zh", "en"])
    parser.add_argument("--scheme", default="light", choices=["light", "dark"])
    parser.add_argument("--fps", type=int, default=20, help="GIF 每秒帧数（帧间隔只能是 1/100 秒的整数倍：20、25、50）")
    parser.add_argument("--scale", type=float, default=2, help="截图倍率，2 在高分屏上也清楚")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    out = args.out or ROOT / "docs" / "images" / f"story-{args.lang}-{args.scheme}.gif"
    if not shutil.which("ffmpeg"):
        raise SystemExit("需要 ffmpeg")

    from playwright.sync_api import sync_playwright

    server = serve()
    url = f"http://127.0.0.1:{server.server_address[1]}/index.html?story&paused&lang={args.lang}"
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        tmp = Path(tmp)
        browser = launch(p)
        page = browser.new_page(viewport={"width": 640, "height": 720}, device_scale_factor=args.scale,
                                color_scheme=args.scheme)
        page.goto(url)
        page.wait_for_function("window.__story")
        page.wait_for_timeout(300)  # 等字体
        duration = page.evaluate("__story.duration")
        # 截图范围：横幅 + 卡片，四周留一点空
        clip = page.evaluate("""() => {
          const rs = ['#alert-banner', '.story-card'].map(s => document.querySelector(s).getBoundingClientRect());
          const pad = 16, top = Math.min(...rs.map(r => r.top)) - pad, left = Math.min(...rs.map(r => r.left)) - pad;
          return { x: left, y: top, width: Math.max(...rs.map(r => r.right)) + pad - left,
                   height: Math.max(...rs.map(r => r.bottom)) + pad - top };
        }""")
        frames = round(duration * args.fps)
        for i in range(frames):
            page.evaluate(f"__story.seek({i / args.fps})")
            page.screenshot(path=str(tmp / f"f{i:04d}.png"), clip=clip)
        browser.close()
        server.shutdown()

        pattern, palette = str(tmp / "f%04d.png"), str(tmp / "palette.png")
        quiet = ["-hide_banner", "-loglevel", "error", "-y"]
        subprocess.run(["ffmpeg", *quiet, "-framerate", str(args.fps), "-i", pattern,
                        "-vf", "palettegen=max_colors=256:stats_mode=full", palette], check=True)
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", *quiet, "-framerate", str(args.fps), "-i", pattern, "-i", palette,
                        "-lavfi", "paletteuse=dither=none:diff_mode=rectangle", "-loop", "0", str(out)], check=True)
    print(f"{out}  {frames} 帧，{out.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
