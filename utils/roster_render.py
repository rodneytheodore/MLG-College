"""Builds a composite "Roster" table image — team logo, team name, and
current owner per row — for the roster channel.

Mirrors utils/scheme_cards_render.py: one discord.File per conference,
attached via attachment://roster.png, built fresh on every
refresh_roster_channel() call. Columns are measured against their own
widest actual value before the canvas is created, so no team name or
owner username ever gets truncated with an ellipsis under normal data --
the image widens instead. Font loading and logo fetching are deliberately
duplicated (not imported) from the other render modules so this one stays
self-contained, matching the existing convention.
"""

import asyncio
import io

import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont

CANVAS_BG_COLOR = (49, 51, 56, 255)  # matches Discord's dark embed background
DIVIDER_COLOR = (58, 60, 65, 255)

# Supersampling factor — see top25_render.py for rationale.
SCALE = 3

ROW_HEIGHT_LOGICAL = 56
LOGO_SIZE_LOGICAL = 40
PADDING_X_LOGICAL = 26
TOP_PADDING_LOGICAL = 18
BOTTOM_PADDING_LOGICAL = 18

TEAM_FONT_SIZE_LOGICAL = 22
OWNER_FONT_SIZE_LOGICAL = 20

LOGO_TO_TEAM_GAP_LOGICAL = 18
GAP_LOGICAL = 30

MIN_TEAM_COL_WIDTH_LOGICAL = 200
MIN_OWNER_COL_WIDTH_LOGICAL = 160

# Ceiling — a safety net against pathological input (e.g. a freak
# 200-character username), not normal behavior.
MAX_TEAM_COL_WIDTH_LOGICAL = 340
MAX_OWNER_COL_WIDTH_LOGICAL = 320

HEADER_HEIGHT_LOGICAL = 42
HEADER_FONT_SIZE_LOGICAL = 17
HEADER_BG_COLOR = (36, 38, 42, 255)
HEADER_TEXT_COLOR = (140, 143, 148, 255)

ROW_HEIGHT = ROW_HEIGHT_LOGICAL * SCALE
LOGO_SIZE = LOGO_SIZE_LOGICAL * SCALE
PADDING_X = PADDING_X_LOGICAL * SCALE
TOP_PADDING = TOP_PADDING_LOGICAL * SCALE
BOTTOM_PADDING = BOTTOM_PADDING_LOGICAL * SCALE

TEAM_FONT_SIZE = TEAM_FONT_SIZE_LOGICAL * SCALE
OWNER_FONT_SIZE = OWNER_FONT_SIZE_LOGICAL * SCALE

LOGO_TO_TEAM_GAP = LOGO_TO_TEAM_GAP_LOGICAL * SCALE
GAP = GAP_LOGICAL * SCALE

MIN_TEAM_COL_WIDTH = MIN_TEAM_COL_WIDTH_LOGICAL * SCALE
MIN_OWNER_COL_WIDTH = MIN_OWNER_COL_WIDTH_LOGICAL * SCALE
MAX_TEAM_COL_WIDTH = MAX_TEAM_COL_WIDTH_LOGICAL * SCALE
MAX_OWNER_COL_WIDTH = MAX_OWNER_COL_WIDTH_LOGICAL * SCALE

HEADER_HEIGHT = HEADER_HEIGHT_LOGICAL * SCALE
HEADER_FONT_SIZE = HEADER_FONT_SIZE_LOGICAL * SCALE

FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
]
FONT_REGULAR_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
]


def _load_font(candidates: list, size: int):
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    """Safety-net truncation -- only engages if a value exceeds its column's
    MAX_* ceiling, since columns are otherwise sized to fit their widest
    actual value."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "\u2026", font=font) > max_width:
        text = text[:-1]
    return (text.rstrip() + "\u2026") if text else "\u2026"


def _col_width(draw, texts: list, font, header_label: str, header_font, min_width: int, max_width: int) -> int:
    widest = max([draw.textlength(t, font=font) for t in texts] + [draw.textlength(header_label, font=header_font)])
    return int(max(min_width, min(widest, max_width)))


async def _fetch_logo(session: aiohttp.ClientSession, url: str) -> Image.Image | None:
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None


def _resize_to_square(img: Image.Image, size: int) -> Image.Image:
    ratio = size / max(img.width, img.height)
    new_w, new_h = max(1, int(img.width * ratio)), max(1, int(img.height * ratio))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    square.paste(resized, ((size - new_w) // 2, (size - new_h) // 2), resized)
    return square


def _draw_header(draw, font, card_width: int, team_x: int, owner_x: int) -> None:
    draw.rectangle((0, 0, card_width, HEADER_HEIGHT), fill=HEADER_BG_COLOR[:3])
    label_y = (HEADER_HEIGHT - HEADER_FONT_SIZE) // 2

    draw.text((team_x, label_y), "TEAM", font=font, fill=HEADER_TEXT_COLOR)
    draw.text((owner_x, label_y), "OWNER", font=font, fill=HEADER_TEXT_COLOR)

    draw.line((0, HEADER_HEIGHT, card_width, HEADER_HEIGHT), fill=DIVIDER_COLOR, width=1 * SCALE)


async def build_roster_file(rows: list[dict]) -> discord.File | None:
    """rows: list of dicts with keys team_name (str), owner_name (str), and
    logo_url (str or None). Returns a discord.File ready to attach, or None
    if rows is empty."""
    if not rows:
        return None

    logo_urls = [r.get("logo_url") for r in rows]
    async with aiohttp.ClientSession() as session:
        logo_imgs = await asyncio.gather(*[
            _fetch_logo(session, url) if url else asyncio.sleep(0, result=None)
            for url in logo_urls
        ])

    team_font = _load_font(FONT_BOLD_CANDIDATES, TEAM_FONT_SIZE)
    owner_font = _load_font(FONT_REGULAR_CANDIDATES, OWNER_FONT_SIZE)
    header_font = _load_font(FONT_BOLD_CANDIDATES, HEADER_FONT_SIZE)

    team_texts = [row["team_name"] for row in rows]
    owner_texts = [row.get("owner_name") or "Unknown" for row in rows]

    measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    team_col_width = _col_width(measure_draw, team_texts, team_font, "TEAM", header_font, MIN_TEAM_COL_WIDTH, MAX_TEAM_COL_WIDTH)
    owner_col_width = _col_width(measure_draw, owner_texts, owner_font, "OWNER", header_font, MIN_OWNER_COL_WIDTH, MAX_OWNER_COL_WIDTH)

    team_col_x = PADDING_X + LOGO_SIZE + LOGO_TO_TEAM_GAP
    owner_col_x = team_col_x + team_col_width + GAP
    card_width = owner_col_x + owner_col_width + PADDING_X

    canvas_height = HEADER_HEIGHT + TOP_PADDING + ROW_HEIGHT * len(rows) + BOTTOM_PADDING
    canvas = Image.new("RGB", (card_width, canvas_height), CANVAS_BG_COLOR[:3])
    draw = ImageDraw.Draw(canvas)

    _draw_header(draw, header_font, card_width, team_col_x, owner_col_x)

    y = HEADER_HEIGHT + TOP_PADDING
    for i, row in enumerate(rows):
        logo = logo_imgs[i]
        if logo is not None:
            square_logo = _resize_to_square(logo, LOGO_SIZE)
            canvas.paste(square_logo, (PADDING_X, y + (ROW_HEIGHT - LOGO_SIZE) // 2), square_logo)

        team_text = _fit_text(draw, team_texts[i], team_font, team_col_width)
        draw.text((team_col_x, y + (ROW_HEIGHT - TEAM_FONT_SIZE) // 2), team_text, font=team_font, fill=(255, 255, 255, 255))

        owner_text = _fit_text(draw, owner_texts[i], owner_font, owner_col_width)
        draw.text((owner_col_x, y + (ROW_HEIGHT - OWNER_FONT_SIZE) // 2), owner_text, font=owner_font, fill=(200, 202, 205, 255))

        y += ROW_HEIGHT
        if i < len(rows) - 1:
            draw.line((PADDING_X, y, card_width - PADDING_X, y), fill=DIVIDER_COLOR, width=1 * SCALE)

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    buffer.seek(0)

    return discord.File(buffer, filename="roster.png")
