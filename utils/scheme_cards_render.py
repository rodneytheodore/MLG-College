"""Builds a composite "Scheme Cards" table image — team logo, name, offense
scheme, and defense scheme per row — for the scheme cards master channel.

Mirrors the fetch/composite/attach + supersampled-render pattern established
in utils/top25_render.py: one discord.File per conference, attached via
attachment://scheme_cards.png, built fresh on every refresh_scheme_cards_channel()
call so it always reflects current data. Font loading and logo fetching are
deliberately duplicated (not imported) from top25_render.py so this module
stays self-contained, matching the existing convention where each render
module (matchup_image.py, top25_render.py) owns its own font-loading logic
rather than sharing a common helper.

COLUMN SIZING: each column (team, coach, offense, defense) is measured
against its own widest actual value before the canvas is created, so no
column ever truncates real data with an ellipsis — the image widens instead.
A generous MAX_*_COL_WIDTH ceiling still applies per column purely as a
safety net against pathological input (e.g. a stray 200-character string),
not as normal behavior.
"""

import asyncio
import io

import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont

CANVAS_BG_COLOR = (49, 51, 56, 255)  # matches Discord's dark embed background
DIVIDER_COLOR = (58, 60, 65, 255)

# Supersampling factor — see top25_render.py for rationale (renders sharper
# than Discord's display width so the embed image scale-down stays crisp).
SCALE = 3

ROW_HEIGHT_LOGICAL = 56
LOGO_SIZE_LOGICAL = 40
PADDING_X_LOGICAL = 26
TOP_PADDING_LOGICAL = 18
BOTTOM_PADDING_LOGICAL = 18

TEAM_FONT_SIZE_LOGICAL = 22
SCHEME_FONT_SIZE_LOGICAL = 20

LOGO_TO_TEAM_GAP_LOGICAL = 14
GAP_LOGICAL = 10

# Floors so a column never looks unnaturally squeezed even when every value
# in it happens to be short (e.g. every defense scheme this week is "4-3").
MIN_TEAM_COL_WIDTH_LOGICAL = 150
MIN_COACH_COL_WIDTH_LOGICAL = 120
MIN_OFFENSE_COL_WIDTH_LOGICAL = 120
MIN_DEFENSE_COL_WIDTH_LOGICAL = 120

# Ceilings — a safety net against pathological input, not normal behavior.
# Real team names / scheme labels are far shorter than this.
MAX_TEAM_COL_WIDTH_LOGICAL = 320
MAX_COACH_COL_WIDTH_LOGICAL = 260
MAX_OFFENSE_COL_WIDTH_LOGICAL = 260
MAX_DEFENSE_COL_WIDTH_LOGICAL = 260

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
SCHEME_FONT_SIZE = SCHEME_FONT_SIZE_LOGICAL * SCALE

LOGO_TO_TEAM_GAP = LOGO_TO_TEAM_GAP_LOGICAL * SCALE
GAP = GAP_LOGICAL * SCALE

MIN_TEAM_COL_WIDTH = MIN_TEAM_COL_WIDTH_LOGICAL * SCALE
MIN_COACH_COL_WIDTH = MIN_COACH_COL_WIDTH_LOGICAL * SCALE
MIN_OFFENSE_COL_WIDTH = MIN_OFFENSE_COL_WIDTH_LOGICAL * SCALE
MIN_DEFENSE_COL_WIDTH = MIN_DEFENSE_COL_WIDTH_LOGICAL * SCALE

MAX_TEAM_COL_WIDTH = MAX_TEAM_COL_WIDTH_LOGICAL * SCALE
MAX_COACH_COL_WIDTH = MAX_COACH_COL_WIDTH_LOGICAL * SCALE
MAX_OFFENSE_COL_WIDTH = MAX_OFFENSE_COL_WIDTH_LOGICAL * SCALE
MAX_DEFENSE_COL_WIDTH = MAX_DEFENSE_COL_WIDTH_LOGICAL * SCALE

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
    """Same fallback chain as top25_render._load_font: try each TTF path,
    then fall back to PIL's built-in font rather than crashing the render."""
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
    """Truncates text with an ellipsis if it would overflow max_width. Only
    ever engaged as a safety net when a value exceeds its column's MAX_*
    ceiling -- normal-length data is never truncated, since columns are
    sized to fit their widest actual value."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "\u2026", font=font) > max_width:
        text = text[:-1]
    return (text.rstrip() + "\u2026") if text else "\u2026"


def _col_width(draw, texts: list, font, header_label: str, header_font, min_width: int, max_width: int) -> int:
    """Measures the widest actual value in a column (plus its header label)
    and returns a width that fits all of it, clamped to [min_width, max_width]."""
    widest = max([draw.textlength(t, font=font) for t in texts] + [draw.textlength(header_label, font=header_font)])
    return int(max(min_width, min(widest, max_width)))


async def _fetch_logo(session: aiohttp.ClientSession, url: str) -> Image.Image | None:
    """Returns None on any failure so one bad/missing logo URL doesn't take
    down the whole render — the caller falls back to a blank square for that row."""
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


def _draw_header(draw, font, card_width: int, team_x: int, coach_x: int, offense_x: int, defense_x: int) -> None:
    """TEAM / COACH / OFFENSE / DEFENSE column labels above the rows."""
    draw.rectangle((0, 0, card_width, HEADER_HEIGHT), fill=HEADER_BG_COLOR[:3])
    label_y = (HEADER_HEIGHT - HEADER_FONT_SIZE) // 2

    draw.text((team_x, label_y), "TEAM", font=font, fill=HEADER_TEXT_COLOR)
    draw.text((coach_x, label_y), "COACH", font=font, fill=HEADER_TEXT_COLOR)
    draw.text((offense_x, label_y), "OFFENSE", font=font, fill=HEADER_TEXT_COLOR)
    draw.text((defense_x, label_y), "DEFENSE", font=font, fill=HEADER_TEXT_COLOR)

    draw.line((0, HEADER_HEIGHT, card_width, HEADER_HEIGHT), fill=DIVIDER_COLOR, width=1 * SCALE)


async def build_scheme_cards_file(rows: list[dict]) -> discord.File | None:
    """rows: list of dicts with keys team_name (str), coach_name (str),
    offense_scheme (str), defense_scheme (str), and logo_url (str or None).
    Returns a discord.File ready to attach, or None if rows is empty."""
    if not rows:
        return None

    logo_urls = [r.get("logo_url") for r in rows]
    async with aiohttp.ClientSession() as session:
        logo_imgs = await asyncio.gather(*[
            _fetch_logo(session, url) if url else asyncio.sleep(0, result=None)
            for url in logo_urls
        ])

    team_font = _load_font(FONT_BOLD_CANDIDATES, TEAM_FONT_SIZE)
    scheme_font = _load_font(FONT_REGULAR_CANDIDATES, SCHEME_FONT_SIZE)
    header_font = _load_font(FONT_BOLD_CANDIDATES, HEADER_FONT_SIZE)

    team_texts = [row["team_name"] for row in rows]
    coach_texts = [row.get("coach_name") or "Unknown" for row in rows]
    offense_texts = [row.get("offense_scheme") or "Not set" for row in rows]
    defense_texts = [row.get("defense_scheme") or "Not set" for row in rows]

    # Measuring text width doesn't need the final canvas -- a throwaway 1x1
    # image gives us a draw context to call textlength() against before we
    # know the real canvas dimensions.
    measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    team_col_width = _col_width(measure_draw, team_texts, team_font, "TEAM", header_font, MIN_TEAM_COL_WIDTH, MAX_TEAM_COL_WIDTH)
    coach_col_width = _col_width(measure_draw, coach_texts, scheme_font, "COACH", header_font, MIN_COACH_COL_WIDTH, MAX_COACH_COL_WIDTH)
    offense_col_width = _col_width(measure_draw, offense_texts, scheme_font, "OFFENSE", header_font, MIN_OFFENSE_COL_WIDTH, MAX_OFFENSE_COL_WIDTH)
    defense_col_width = _col_width(measure_draw, defense_texts, scheme_font, "DEFENSE", header_font, MIN_DEFENSE_COL_WIDTH, MAX_DEFENSE_COL_WIDTH)

    team_col_x = PADDING_X + LOGO_SIZE + LOGO_TO_TEAM_GAP
    coach_col_x = team_col_x + team_col_width + GAP
    offense_col_x = coach_col_x + coach_col_width + GAP
    defense_col_x = offense_col_x + offense_col_width + GAP
    card_width = defense_col_x + defense_col_width + PADDING_X

    canvas_height = HEADER_HEIGHT + TOP_PADDING + ROW_HEIGHT * len(rows) + BOTTOM_PADDING
    canvas = Image.new("RGB", (card_width, canvas_height), CANVAS_BG_COLOR[:3])
    draw = ImageDraw.Draw(canvas)

    _draw_header(draw, header_font, card_width, team_col_x, coach_col_x, offense_col_x, defense_col_x)

    y = HEADER_HEIGHT + TOP_PADDING
    for i, row in enumerate(rows):
        logo = logo_imgs[i]
        if logo is not None:
            square_logo = _resize_to_square(logo, LOGO_SIZE)
            canvas.paste(square_logo, (PADDING_X, y + (ROW_HEIGHT - LOGO_SIZE) // 2), square_logo)

        team_text = _fit_text(draw, team_texts[i], team_font, team_col_width)
        draw.text((team_col_x, y + (ROW_HEIGHT - TEAM_FONT_SIZE) // 2), team_text, font=team_font, fill=(255, 255, 255, 255))

        coach_text = _fit_text(draw, coach_texts[i], scheme_font, coach_col_width)
        draw.text((coach_col_x, y + (ROW_HEIGHT - SCHEME_FONT_SIZE) // 2), coach_text, font=scheme_font, fill=(200, 202, 205, 255))

        offense_text = _fit_text(draw, offense_texts[i], scheme_font, offense_col_width)
        draw.text((offense_col_x, y + (ROW_HEIGHT - SCHEME_FONT_SIZE) // 2), offense_text, font=scheme_font, fill=(200, 202, 205, 255))

        defense_text = _fit_text(draw, defense_texts[i], scheme_font, defense_col_width)
        draw.text((defense_col_x, y + (ROW_HEIGHT - SCHEME_FONT_SIZE) // 2), defense_text, font=scheme_font, fill=(200, 202, 205, 255))

        y += ROW_HEIGHT
        if i < len(rows) - 1:
            draw.line((PADDING_X, y, card_width - PADDING_X, y), fill=DIVIDER_COLOR, width=1 * SCALE)

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    buffer.seek(0)

    return discord.File(buffer, filename="scheme_cards.png")
