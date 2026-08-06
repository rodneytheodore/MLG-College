"""Builds a composite "Game Table" image — away team, home team, and status
per row — for the CPU/user games channel displays, and (passed a single-row
list) for an individual game's thread card. Using the same function for both
means the thread card is guaranteed to look identical to a row in the
channel table, rather than being a separately-maintained design.

Mirrors utils/scheme_cards_render.py and utils/roster_render.py: columns are
measured against their own widest actual value before the canvas is
created, so team names never truncate under normal data. Away is listed
before home throughout, matching the existing "{away} vs {home}" convention
used in embed titles and utils/matchup_image.py elsewhere in this codebase.
"""

import asyncio
import io

import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont

CANVAS_BG_COLOR = (49, 51, 56, 255)  # matches Discord's dark embed background
DIVIDER_COLOR = (58, 60, 65, 255)

SCALE = 3

ROW_HEIGHT_LOGICAL = 56
LOGO_SIZE_LOGICAL = 40
PADDING_X_LOGICAL = 26
TOP_PADDING_LOGICAL = 18
BOTTOM_PADDING_LOGICAL = 18

TEAM_FONT_SIZE_LOGICAL = 20
VS_FONT_SIZE_LOGICAL = 15
STATUS_FONT_SIZE_LOGICAL = 15

LOGO_TO_TEAM_GAP_LOGICAL = 14
GAP_LOGICAL = 24
VS_COL_WIDTH_LOGICAL = 36

MIN_TEAM_COL_WIDTH_LOGICAL = 140
MIN_STATUS_COL_WIDTH_LOGICAL = 120
MAX_TEAM_COL_WIDTH_LOGICAL = 260
MAX_STATUS_COL_WIDTH_LOGICAL = 200

STATUS_PILL_PAD_X_LOGICAL = 12
STATUS_PILL_PAD_Y_LOGICAL = 6

HEADER_HEIGHT_LOGICAL = 42
HEADER_FONT_SIZE_LOGICAL = 17
HEADER_BG_COLOR = (36, 38, 42, 255)
HEADER_TEXT_COLOR = (140, 143, 148, 255)

# Status pill colors: (background, foreground) per status "kind".
STATUS_COLORS = {
    "done": ((45, 74, 58), (87, 242, 135)),
    "sched": ((45, 54, 84), (142, 161, 255)),
    "pending": ((58, 60, 65), (200, 202, 205)),
}

ROW_HEIGHT = ROW_HEIGHT_LOGICAL * SCALE
LOGO_SIZE = LOGO_SIZE_LOGICAL * SCALE
PADDING_X = PADDING_X_LOGICAL * SCALE
TOP_PADDING = TOP_PADDING_LOGICAL * SCALE
BOTTOM_PADDING = BOTTOM_PADDING_LOGICAL * SCALE

TEAM_FONT_SIZE = TEAM_FONT_SIZE_LOGICAL * SCALE
VS_FONT_SIZE = VS_FONT_SIZE_LOGICAL * SCALE
STATUS_FONT_SIZE = STATUS_FONT_SIZE_LOGICAL * SCALE

LOGO_TO_TEAM_GAP = LOGO_TO_TEAM_GAP_LOGICAL * SCALE
GAP = GAP_LOGICAL * SCALE
VS_COL_WIDTH = VS_COL_WIDTH_LOGICAL * SCALE

MIN_TEAM_COL_WIDTH = MIN_TEAM_COL_WIDTH_LOGICAL * SCALE
MIN_STATUS_COL_WIDTH = MIN_STATUS_COL_WIDTH_LOGICAL * SCALE
MAX_TEAM_COL_WIDTH = MAX_TEAM_COL_WIDTH_LOGICAL * SCALE
MAX_STATUS_COL_WIDTH = MAX_STATUS_COL_WIDTH_LOGICAL * SCALE

STATUS_PILL_PAD_X = STATUS_PILL_PAD_X_LOGICAL * SCALE
STATUS_PILL_PAD_Y = STATUS_PILL_PAD_Y_LOGICAL * SCALE

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
    """Safety-net truncation -- only engages past a column's MAX_* ceiling,
    since columns are otherwise sized to fit their widest actual value."""
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


def _draw_status_pill(draw, x: int, y: int, row_height: int, text: str, kind: str, font, pill_width: int) -> None:
    bg, fg = STATUS_COLORS.get(kind, STATUS_COLORS["pending"])
    text_w = draw.textlength(text, font=font)
    pill_height = STATUS_FONT_SIZE + STATUS_PILL_PAD_Y * 2
    pill_y = y + (row_height - pill_height) // 2
    try:
        draw.rounded_rectangle((x, pill_y, x + pill_width, pill_y + pill_height), radius=pill_height // 2, fill=bg)
    except AttributeError:
        # Older Pillow without rounded_rectangle -- a plain rect is still legible.
        draw.rectangle((x, pill_y, x + pill_width, pill_y + pill_height), fill=bg)
    text_x = x + (pill_width - text_w) // 2
    draw.text((text_x, pill_y + STATUS_PILL_PAD_Y), text, font=font, fill=fg)


def _draw_header(draw, font, card_width: int, away_x: int, home_x: int, status_x: int) -> None:
    draw.rectangle((0, 0, card_width, HEADER_HEIGHT), fill=HEADER_BG_COLOR[:3])
    label_y = (HEADER_HEIGHT - HEADER_FONT_SIZE) // 2

    draw.text((away_x, label_y), "AWAY", font=font, fill=HEADER_TEXT_COLOR)
    draw.text((home_x, label_y), "HOME", font=font, fill=HEADER_TEXT_COLOR)
    draw.text((status_x, label_y), "STATUS", font=font, fill=HEADER_TEXT_COLOR)

    draw.line((0, HEADER_HEIGHT, card_width, HEADER_HEIGHT), fill=DIVIDER_COLOR, width=1 * SCALE)


async def build_game_table_file(rows: list[dict]) -> discord.File | None:
    """rows: list of dicts with keys away_name, away_logo_url, home_name,
    home_logo_url, status_label (display text), status_kind ('done' /
    'sched' / 'pending' -- controls pill color). Returns a discord.File
    ready to attach, or None if rows is empty.

    Pass a single-item list to get one game's "card" -- used for per-thread
    posts so they share the exact same look as a channel table row."""
    if not rows:
        return None

    away_logo_urls = [r.get("away_logo_url") for r in rows]
    home_logo_urls = [r.get("home_logo_url") for r in rows]
    async with aiohttp.ClientSession() as session:
        away_logo_imgs, home_logo_imgs = await asyncio.gather(
            asyncio.gather(*[_fetch_logo(session, u) if u else asyncio.sleep(0, result=None) for u in away_logo_urls]),
            asyncio.gather(*[_fetch_logo(session, u) if u else asyncio.sleep(0, result=None) for u in home_logo_urls]),
        )

    team_font = _load_font(FONT_BOLD_CANDIDATES, TEAM_FONT_SIZE)
    vs_font = _load_font(FONT_REGULAR_CANDIDATES, VS_FONT_SIZE)
    status_font = _load_font(FONT_BOLD_CANDIDATES, STATUS_FONT_SIZE)
    header_font = _load_font(FONT_BOLD_CANDIDATES, HEADER_FONT_SIZE)

    away_texts = [row["away_name"] for row in rows]
    home_texts = [row["home_name"] for row in rows]
    status_texts = [row.get("status_label") or "Pending" for row in rows]

    measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    away_col_width = _col_width(measure_draw, away_texts, team_font, "AWAY", header_font, MIN_TEAM_COL_WIDTH, MAX_TEAM_COL_WIDTH)
    home_col_width = _col_width(measure_draw, home_texts, team_font, "HOME", header_font, MIN_TEAM_COL_WIDTH, MAX_TEAM_COL_WIDTH)
    status_text_width = max([measure_draw.textlength(t, font=status_font) for t in status_texts] + [0])
    status_col_width = int(max(MIN_STATUS_COL_WIDTH, min(status_text_width + STATUS_PILL_PAD_X * 2, MAX_STATUS_COL_WIDTH)))

    away_col_x = PADDING_X + LOGO_SIZE + LOGO_TO_TEAM_GAP
    vs_col_x = away_col_x + away_col_width + GAP
    home_logo_x = vs_col_x + VS_COL_WIDTH + GAP
    home_col_x = home_logo_x + LOGO_SIZE + LOGO_TO_TEAM_GAP
    status_col_x = home_col_x + home_col_width + GAP
    card_width = status_col_x + status_col_width + PADDING_X

    canvas_height = HEADER_HEIGHT + TOP_PADDING + ROW_HEIGHT * len(rows) + BOTTOM_PADDING
    canvas = Image.new("RGB", (card_width, canvas_height), CANVAS_BG_COLOR[:3])
    draw = ImageDraw.Draw(canvas)

    _draw_header(draw, header_font, card_width, away_col_x, home_col_x, status_col_x)

    y = HEADER_HEIGHT + TOP_PADDING
    for i, row in enumerate(rows):
        away_logo = away_logo_imgs[i]
        if away_logo is not None:
            square = _resize_to_square(away_logo, LOGO_SIZE)
            canvas.paste(square, (PADDING_X, y + (ROW_HEIGHT - LOGO_SIZE) // 2), square)

        away_text = _fit_text(draw, away_texts[i], team_font, away_col_width)
        draw.text((away_col_x, y + (ROW_HEIGHT - TEAM_FONT_SIZE) // 2), away_text, font=team_font, fill=(255, 255, 255, 255))

        vs_w = draw.textlength("vs", font=vs_font)
        draw.text((vs_col_x + (VS_COL_WIDTH - vs_w) // 2, y + (ROW_HEIGHT - VS_FONT_SIZE) // 2), "vs", font=vs_font, fill=(109, 111, 120, 255))

        home_logo = home_logo_imgs[i]
        if home_logo is not None:
            square = _resize_to_square(home_logo, LOGO_SIZE)
            canvas.paste(square, (home_logo_x, y + (ROW_HEIGHT - LOGO_SIZE) // 2), square)

        home_text = _fit_text(draw, home_texts[i], team_font, home_col_width)
        draw.text((home_col_x, y + (ROW_HEIGHT - TEAM_FONT_SIZE) // 2), home_text, font=team_font, fill=(255, 255, 255, 255))

        status_label = _fit_text(draw, status_texts[i], status_font, status_col_width - STATUS_PILL_PAD_X * 2)
        _draw_status_pill(draw, status_col_x, y, ROW_HEIGHT, status_label, row.get("status_kind", "pending"), status_font, status_col_width)

        y += ROW_HEIGHT
        if i < len(rows) - 1:
            draw.line((PADDING_X, y, card_width - PADDING_X, y), fill=DIVIDER_COLOR, width=1 * SCALE)

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    buffer.seek(0)

    return discord.File(buffer, filename="game_table.png")
