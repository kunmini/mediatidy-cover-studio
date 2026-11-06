# -*- coding: utf-8 -*-
"""媒体库封面渲染模块。

把 N 张海报合成为 1920×1080 的分类封面：
  - 背景：取第一张海报大幅模糊 + 压暗，铺满整幅（跟随海报色调）
  - 右侧：9 张海报按固定坐标斜排（15°、圆角、投影）
  - 左侧：绿色竖条 + 中英文标题
  - 可选：左上角媒体数量角标

所有几何参数取自 LibraryCoverStudio「倾斜圆角海报墙」模板的实测值，
文字基线补偿了 PIL 与 fabric.js 的 47px 差异。
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ── 画布 ──────────────────────────────────────────────────────
CANVAS_W, CANVAS_H = 1920, 1080

# ── 海报墙（左/上为中心点坐标，与模板 originX/Y=center 一致）────
POSTER_W, POSTER_H = 420, 630
POSTER_ANGLE = 15
POSTER_RADIUS = 24
POSTER_POSITIONS = [
    (1201.2, -104.0), (1635.8, 12.5), (2070.5, 129.0),
    (1030.3, 533.5), (1465.0, 650.0), (1899.7, 766.5),
    (859.5, 1171.0), (1294.2, 1287.5), (1728.8, 1404.0),
]

# ── 文字 ──────────────────────────────────────────────────────
# left 补 11px 字形边距；top 差 47px 是 PIL 与 fabric.js 的基线差异
TITLE_LEFT, TITLE_TOP, TITLE_SIZE = 138, 353, 155
SUB_LEFT, SUB_TOP, SUB_SIZE = 138, 553, 66
SUB_CHAR_SPACING = 80          # fabric.js 单位（1/1000 em），非像素
SUB_TRACKING = SUB_SIZE * SUB_CHAR_SPACING / 1000.0
BAR_LEFT, BAR_TOP, BAR_W, BAR_H = 103, 400, 14, 250
BAR_COLOR = "#88e48d"
TEXT_COLOR = "#ffffff"

# ── 背景 ──────────────────────────────────────────────────────
BASE_BG = "#202020"
POSTER_BG_BLUR = 60
POSTER_BG_DIM = 0.35
BG_BLEND = 0.78                # 模糊海报在背景中的权重

# ── 数量角标（参数实测自 MediaTidy 原生海报）──────────────────
BADGE_X, BADGE_Y = 58, 43
BADGE_H = 156
BADGE_MIN_W = 180
BADGE_RADIUS = 45

# ── 缎带款式参数 ──
RIBBON_FONT_SIZE = 52
RIBBON_PAD = 120
RIBBON_OFFSET_X = 0
RIBBON_OFFSET_Y = 0
BADGE_PAD_X = 44
BADGE_FONT_SIZE = 92
BADGE_BG = (32, 37, 41)        # 深灰底色
BADGE_ALPHA = 180              # 默认不透明度（0-255），180≈70%
BADGE_FG = "#ffffff"

# ── 字体优先级 ────────────────────────────────────────────────
_CJK_PRIORITY = ("SourceHanSansCN-Heavy", "SourceHanSansCN", "NotoSansSC",
                 "NotoSansCJKsc", "SourceHanSans", "NotoSansCJK",
                 "wqy", "DroidSansFallback")
_LATIN_PRIORITY = ("DejaVuSans-Bold", "LiberationSans-Bold", "NotoSansMono-Bold",
                   "DejaVuSans", "NotoSansMono")

# 插件自带字体目录（随包分发，开箱即用）
_PLUGIN_FONTS = Path(__file__).resolve().parent.parent / "fonts"


def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def load_fonts(font_dir: str | None = None) -> tuple[str | None, str | None]:
    """返回 (中文字体路径, 拉丁字体路径)。

    优先用插件自带的思源黑体，找不到再退回系统字体。
    """
    import glob

    for name in ("SourceHanSansCN-Heavy.otf", "SourceHanSansCN-Bold.otf"):
        cand = _PLUGIN_FONTS / name
        if cand.is_file():
            latin = _PLUGIN_FONTS / "SourceHanSansCN-Bold.otf"
            return str(cand), str(latin if latin.is_file() else cand)

    roots = [p for p in (font_dir, "/app/data/fonts", "/app/fonts",
                         "/usr/share/fonts", "/app") if p]
    found: list[str] = []
    for root in roots:
        for pat in ("**/*.otf", "**/*.ttf", "**/*.ttc"):
            try:
                found.extend(glob.glob(root + "/" + pat, recursive=True))
            except Exception:
                continue

    def pick(priority):
        for key in priority:
            for f in found:
                if key.lower() in f.lower():
                    return f
        return None

    return pick(_CJK_PRIORITY), pick(_LATIN_PRIORITY)


def _font(path: str | None, size: int):
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default(size)


# ── 背景 ──────────────────────────────────────────────────────
def _poster_atmosphere(posters: list[bytes], w: int, h: int) -> Image.Image | None:
    """把第一张海报放大模糊、轻微压暗后铺满整幅，作为背景底。"""
    if not posters:
        return None
    try:
        im = Image.open(io.BytesIO(posters[0])).convert("RGB")
    except Exception:
        return None
    scale = max(w / im.width, h / im.height)
    im = im.resize((int(im.width * scale) + 1, int(im.height * scale) + 1), Image.LANCZOS)
    left = (im.width - w) // 2
    top = (im.height - h) // 2
    im = im.crop((left, top, left + w, top + h))
    im = im.filter(ImageFilter.GaussianBlur(POSTER_BG_BLUR))
    return Image.blend(im, Image.new("RGB", (w, h), (0, 0, 0)), 0.12)


# ── 海报 ──────────────────────────────────────────────────────
def _round_corners(img: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, img.size[0] - 1, img.size[1] - 1], radius=radius, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def _prepare_poster(data: bytes,
                    size: tuple[int, int] = (POSTER_W, POSTER_H)) -> Image.Image:
    """居中裁切到海报比例并圆角化。"""
    im = Image.open(io.BytesIO(data)).convert("RGB")
    tw, th = size
    target = tw / th
    if im.width / im.height > target:
        nw = int(im.height * target)
        left = (im.width - nw) // 2
        im = im.crop((left, 0, left + nw, im.height))
    else:
        nh = int(im.width / target)
        top = (im.height - nh) // 2
        im = im.crop((0, top, im.width, top + nh))
    return _round_corners(im.resize(size, Image.LANCZOS), POSTER_RADIUS)


def build_poster_wall(posters: Iterable[bytes],
                      w: int = CANVAS_W, h: int = CANVAS_H) -> Image.Image:
    """按模板坐标摆放斜排海报墙，返回带投影的图层。"""
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    placed = []
    for idx, data in enumerate(list(posters)[:len(POSTER_POSITIONS)]):
        try:
            p = _prepare_poster(data)
        except Exception:
            continue
        # fabric.js 正角度为顺时针，PIL rotate 正角度为逆时针 → 取负
        rot = p.rotate(-POSTER_ANGLE, expand=True, resample=Image.BICUBIC)
        cx, cy = POSTER_POSITIONS[idx]
        placed.append((rot, round(cx - rot.width / 2), round(cy - rot.height / 2)))

    for rot, x, y in placed:                      # 先铺投影
        sh = Image.new("RGBA", rot.size, (0, 0, 0, 0))
        sh.paste((0, 0, 0, 170), (0, 0), rot)
        layer.alpha_composite(sh.filter(ImageFilter.GaussianBlur(22)), (x + 12, y + 18))
    for rot, x, y in placed:                      # 再贴海报
        layer.alpha_composite(rot, (x, y))
    return layer


# ── 文字 ──────────────────────────────────────────────────────
def draw_titles(img: Image.Image, title: str, subtitle: str,
                cjk_font: str | None, latin_font: str | None) -> Image.Image:
    """画绿竖条 + 主标题 + 副标题。"""
    draw = ImageDraw.Draw(img)

    draw.rectangle([BAR_LEFT, BAR_TOP, BAR_LEFT + BAR_W, BAR_TOP + BAR_H],
                   fill=_rgb(BAR_COLOR))

    draw.text((TITLE_LEFT, TITLE_TOP), title,
              font=_font(cjk_font, TITLE_SIZE), fill=_rgb(TEXT_COLOR))

    f_sub = _font(latin_font or cjk_font, SUB_SIZE)
    x = SUB_LEFT
    for ch in subtitle:
        draw.text((x, SUB_TOP), ch, font=f_sub, fill=_rgb(TEXT_COLOR))
        box = draw.textbbox((0, 0), ch, font=f_sub)
        x += (box[2] - box[0]) + SUB_TRACKING
    return img


# ── 数量角标 ──────────────────────────────────────────────────
# 彩色底色池（避开纯黑/纯白，保持和封面协调）
BADGE_COLORS = [
    (24, 92, 190),    # 蓝
    (16, 140, 96),    # 绿
    (222, 98, 34),    # 橙
    (150, 58, 150),   # 紫
    (200, 48, 60),    # 红
    (196, 150, 30),   # 金
    (32, 132, 148),   # 青
    (120, 82, 172),   # 靛
]


def _pick_badge_color(seed: str) -> tuple:
    """按库名稳定取色：同一个库每次生成颜色一致，不同库之间颜色分散。"""
    h = 0
    for ch in str(seed or ""):
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return BADGE_COLORS[h % len(BADGE_COLORS)]


def draw_badge(img: Image.Image, count, font_path: str | None,
               label: str = "%s", bg: tuple | None = None,
               fg: str = BADGE_FG, seed: str = "") -> Image.Image:
    """在左上角画媒体数量角标。

    bg 为 None 时按 seed（库名）稳定取一个彩色底。
    """
    if count is None or count == "":
        return img
    text = (label % count) if "%s" in (label or "") else str(label)

    f = _font(font_path, BADGE_FONT_SIZE)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    box = d.textbbox((0, 0), text, font=f)
    tw = box[2] - box[0]
    w = max(BADGE_MIN_W, tw + BADGE_PAD_X * 2)

    base_bg = bg if bg else _pick_badge_color(seed)
    fill_rgba = (*base_bg[:3], base_bg[3] if len(base_bg) > 3 else BADGE_ALPHA)
    d.rounded_rectangle([BADGE_X, BADGE_Y, BADGE_X + w, BADGE_Y + BADGE_H],
                        radius=BADGE_RADIUS, fill=fill_rgba)

    tx = BADGE_X + (w - tw) // 2 - box[0]
    ty = BADGE_Y + (BADGE_H - (box[3] - box[1])) // 2 - box[1]
    d.text((tx, ty), text, font=f, fill=_rgb(fg))
    img.alpha_composite(layer)
    return img


# ── 合成入口 ──────────────────────────────────────────────────
def render_cover(posters: list[bytes], title: str, subtitle: str,
                 fonts: tuple[str | None, str | None] = (None, None),
                 base_color: str = BASE_BG, background: bool = True,
                 badge_count=None, badge_alpha: int | None = None,
                 badge_color: tuple | None = None,
                 random_color: bool = True) -> bytes:
    """生成 1920×1080 封面，返回 PNG 字节。

    Args:
        posters: 海报图片字节列表（最多 9 张，已按作品去重）
        title: 主标题（中文）
        subtitle: 副标题（英文）
        fonts: (中文字体路径, 拉丁字体路径)
        base_color: 背景底色
        background: 是否用海报做模糊背景（False = 纯底色）
        badge_count: 媒体数量；None 则不画角标
        badge_alpha: 角标不透明度 0-255；None 用默认 180
    """
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), _rgb(base_color))
    if background:
        atmos = _poster_atmosphere(posters, CANVAS_W, CANVAS_H)
        if atmos is not None:
            canvas = Image.blend(canvas, atmos, BG_BLEND)
    canvas = canvas.convert("RGBA")

    canvas.alpha_composite(build_poster_wall(posters))
    canvas = draw_titles(canvas, title, subtitle, fonts[0], fonts[1])

    if badge_count is not None and badge_count != "":
        alpha = badge_alpha if badge_alpha is not None else BADGE_ALPHA
        if badge_color:
            bg = (*badge_color[:3], alpha)
        elif random_color:
            # 随机彩色（按库名稳定取色，同库每次一致）
            bg = (*_pick_badge_color(title), alpha)
        else:
            # 关闭随机色 → 用固定深灰（原配色）
            bg = (*BADGE_BG, alpha)
        canvas = draw_badge(canvas, badge_count, fonts[0] or fonts[1], bg=bg)

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
