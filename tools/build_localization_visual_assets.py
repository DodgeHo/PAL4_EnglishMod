from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SHOWCASE_ASSETS = ROOT / "showcase" / "assets"
METRICS_JSON = ASSETS / "localization-metrics.json"

INFOGRAPHIC = ASSETS / "localization-infographic.png"
EXAMPLES = ASSETS / "localization-examples.png"
WORKFLOW_GIF = ASSETS / "localization-workflow.gif"

NAVY = "#0F172A"
BLUE = "#2563EB"
GREEN = "#059669"
ORANGE = "#EA580C"
GRAY = "#475569"
LIGHT = "#F8FAFC"
LINE = "#CBD5E1"
WHITE = "#FFFFFF"
INK = "#111827"


def font(size: int, bold: bool = False, chinese: bool = False) -> ImageFont.FreeTypeFont:
    candidates = []
    if chinese:
        candidates.extend(
            [
                r"C:\Windows\Fonts\msyh.ttc",
                r"C:\Windows\Fonts\simhei.ttf",
                r"C:\Windows\Fonts\simsun.ttc",
            ]
        )
    candidates.extend(
        [
            r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
            r"C:\Windows\Fonts\calibrib.ttf" if bold else r"C:\Windows\Fonts\calibri.ttf",
        ]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


FONTS = {
    "title": font(46, bold=True),
    "subtitle": font(24),
    "section": font(28, bold=True),
    "metric": font(46, bold=True),
    "label": font(21),
    "body": font(23),
    "small": font(18),
    "small_bold": font(18, bold=True),
    "cn": font(24, chinese=True),
    "cn_small": font(20, chinese=True),
    "en": font(23),
}


def load_metrics() -> dict:
    if not METRICS_JSON.exists():
        import build_localization_showcase

        build_localization_showcase.main()
    return json.loads(METRICS_JSON.read_text(encoding="utf-8"))


def rounded_rect(draw: ImageDraw.ImageDraw, box, radius=22, fill=WHITE, outline=LINE, width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def text(draw: ImageDraw.ImageDraw, xy, value, fill=INK, font_key="body", anchor=None):
    draw.text(xy, value, fill=fill, font=FONTS[font_key], anchor=anchor)


def wrap_by_pixels(draw: ImageDraw.ImageDraw, value: str, max_width: int, font_key: str) -> list[str]:
    words = value.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textbbox((0, 0), candidate, font=FONTS[font_key])[2] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def draw_wrapped(draw: ImageDraw.ImageDraw, xy, value: str, max_width: int, font_key: str, fill=INK, line_gap=8):
    x, y = xy
    lines = wrap_by_pixels(draw, value, max_width, font_key)
    line_h = FONTS[font_key].size + line_gap
    for index, line in enumerate(lines):
        text(draw, (x, y + index * line_h), line, fill=fill, font_key=font_key)
    return y + len(lines) * line_h


def short_metric(value: int, suffix: str = "") -> str:
    if value >= 1000:
        return f"{value:,}{suffix}"
    return f"{value}{suffix}"


def save_infographic(metrics: dict) -> None:
    img = Image.new("RGB", (1600, 1060), WHITE)
    draw = ImageDraw.Draw(img)

    text(draw, (70, 58), "PAL4 English Mod", fill=NAVY, font_key="title")
    text(
        draw,
        (72, 116),
        "Chinese-to-English localization, terminology management, and release-ready patch tooling.",
        fill=GRAY,
        font_key="subtitle",
    )

    cards = [
        ("9,532", "translated text entries", BLUE),
        ("178k+", "approx. English words", GREEN),
        ("34", "script/source files", ORANGE),
        ("13", "terminology sheets", BLUE),
    ]
    x = 70
    for value, label, color in cards:
        rounded_rect(draw, (x, 180, x + 340, 330), radius=28, fill=LIGHT, outline=LINE)
        text(draw, (x + 28, 222), value, fill=color, font_key="metric")
        text(draw, (x + 30, 286), label, fill=GRAY, font_key="label")
        x += 370

    text(draw, (70, 405), "What the translation covers", fill=NAVY, font_key="section")
    categories = metrics["content_categories"]
    max_count = max(item["count"] for item in categories)
    y = 470
    for item in categories:
        label = item["label"]
        count = item["count"]
        bar_w = max(16, int(count / max_count * 760))
        text(draw, (85, y + 4), label, fill=INK, font_key="label")
        draw.rounded_rectangle((420, y, 1180, y + 30), radius=15, fill="#E2E8F0")
        draw.rounded_rectangle((420, y, 420 + bar_w, y + 30), radius=15, fill=BLUE)
        text(draw, (1215, y + 2), f"{count:,}", fill=NAVY, font_key="label")
        y += 58

    rounded_rect(draw, (70, 910, 1530, 1000), radius=26, fill="#EFF6FF", outline="#BFDBFE")
    text(draw, (105, 939), "Readable proof for non-technical reviewers:", fill=NAVY, font_key="small_bold")
    text(
        draw,
        (105, 970),
        "large-scale English writing + consistent terminology + in-game QA + player-facing release documentation",
        fill=GRAY,
        font_key="small",
    )

    img.save(INFOGRAPHIC, quality=95)


EXAMPLE_ROWS = [
    (
        "Dialogue",
        "云天河：（这地方好暗，以前都没进来过——）",
        "Yun Tianhe: (This place is so dark, never been in here before-)",
    ),
    (
        "Item description",
        "仙人炼制的药膏，具有白骨生肌的神奇功效。",
        "An ointment refined by immortals, possessing the miraculous effect of regenerating flesh on bone.",
    ),
    (
        "Quest objective",
        "现在可以去西北面的石沉溪洞追山猪啦！",
        "Now you can go to the Stone Sink Creek Cave northwest to pursue Wild Boar!",
    ),
    ("Skill name", "恸天贯日式", "MournSky St."),
    ("Scene name", "青鸾峰", "Qingluan Peak"),
]


def save_examples() -> None:
    img = Image.new("RGB", (1600, 1180), WHITE)
    draw = ImageDraw.Draw(img)

    text(draw, (70, 58), "Before / After Translation Samples", fill=NAVY, font_key="title")
    text(
        draw,
        (72, 116),
        "Examples from dialogue, item descriptions, quest text, skills, and location names.",
        fill=GRAY,
        font_key="subtitle",
    )

    y = 190
    for idx, (kind, cn, en) in enumerate(EXAMPLE_ROWS):
        fill = "#F8FAFC" if idx % 2 == 0 else "#FFFFFF"
        rounded_rect(draw, (70, y, 1530, y + 160), radius=24, fill=fill, outline=LINE)
        text(draw, (105, y + 30), kind, fill=BLUE, font_key="small_bold")
        text(draw, (105, y + 70), cn, fill=INK, font_key="cn")
        draw_wrapped(draw, (780, y + 66), en, 680, "en", fill=INK, line_gap=8)
        draw.line((735, y + 30, 735, y + 130), fill=LINE, width=2)
        y += 178

    rounded_rect(draw, (70, 1090, 1530, 1145), radius=20, fill="#ECFDF5", outline="#BBF7D0")
    text(
        draw,
        (105, 1107),
        "The point is not isolated vocabulary. The work is consistency across thousands of lines and game contexts.",
        fill="#065F46",
        font_key="small_bold",
    )

    img.save(EXAMPLES, quality=95)


def workflow_frame(active_index: int) -> Image.Image:
    img = Image.new("RGB", (1200, 420), WHITE)
    draw = ImageDraw.Draw(img)
    text(draw, (55, 44), "Localization Workflow", fill=NAVY, font_key="title")

    steps = [
        "Extract Chinese text",
        "Translate text",
        "Normalize terminology",
        "QA in game context",
        "Package release",
    ]
    start_x = 60
    y = 165
    box_w = 190
    gap = 38
    for index, label in enumerate(steps):
        x = start_x + index * (box_w + gap)
        active = index <= active_index
        fill = "#DBEAFE" if active else "#F8FAFC"
        outline = BLUE if active else LINE
        rounded_rect(draw, (x, y, x + box_w, y + 92), radius=20, fill=fill, outline=outline, width=3)
        for n, line in enumerate(textwrap.wrap(label, width=15)):
            text(draw, (x + box_w / 2, y + 34 + n * 25), line, fill=NAVY, font_key="small_bold", anchor="mm")
        if index < len(steps) - 1:
            ax = x + box_w + 10
            draw.line((ax, y + 46, ax + gap - 20, y + 46), fill=BLUE if active else LINE, width=4)
            draw.polygon(
                [(ax + gap - 20, y + 46), (ax + gap - 32, y + 38), (ax + gap - 32, y + 54)],
                fill=BLUE if active else LINE,
            )

    text(
        draw,
        (60, 330),
        "From raw Chinese game strings to a tested, installable English patch.",
        fill=GRAY,
        font_key="subtitle",
    )
    return img


def save_workflow_gif() -> None:
    frames = [workflow_frame(i) for i in range(5)]
    frames += [frames[-1]] * 2
    frames[0].save(
        WORKFLOW_GIF,
        save_all=True,
        append_images=frames[1:],
        duration=650,
        loop=0,
        optimize=True,
    )


def main() -> None:
    SHOWCASE_ASSETS.mkdir(parents=True, exist_ok=True)
    metrics = load_metrics()
    save_infographic(metrics)
    save_examples()
    save_workflow_gif()
    for path in [INFOGRAPHIC, EXAMPLES, WORKFLOW_GIF, METRICS_JSON]:
        if path.exists():
            shutil.copy2(path, SHOWCASE_ASSETS / path.name)
    print(f"Wrote {INFOGRAPHIC.relative_to(ROOT)}")
    print(f"Wrote {EXAMPLES.relative_to(ROOT)}")
    print(f"Wrote {WORKFLOW_GIF.relative_to(ROOT)}")
    print(f"Mirrored assets to {SHOWCASE_ASSETS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
