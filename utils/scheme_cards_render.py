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

CARD_WIDTH_LOGICAL = 700
ROW_HEIGHT_LOGICAL = 44
LOGO_SIZE_LOGICAL = 30
PADDING_X_LOGICAL = 20
TOP_PADDING_LOGICAL = 14
BOTTOM_PADDING_LOGICAL = 14

TEAM_FONT_SIZE_LOGICAL = 17
SCHEME_FONT_SIZE_LOGICAL = 16

LOGO_TO_TEAM_GAP_LOGICAL = 10
TEAM_COL_WIDTH_LOGICAL = 150
GAP_LOGICAL = 8
COACH_COL_WIDTH_LOGICAL = 130
OFFENSE_COL_WIDTH_LOGICAL = 140
# Defense column takes whatever's left: CARD_WIDTH - everything before it.
COACH_COL_X_LOGICAL = (
    PADDING_X_LOGICAL + LOGO_SIZE_LOGICAL + LOGO_TO_TEAM_GAP_LOGICAL
    + TEAM_COL_WIDTH_LOGICAL + GAP_LOGICAL
)
OFFENSE_COL_X_LOGICAL = COACH_COL_X_LOGICAL + COACH_COL_WIDTH_LOGICAL + GAP_LOGICAL
DEFENSE_COL_X_LOGICAL = OFFENSE_COL_X_LOGICAL + OFFENSE_COL_WIDTH_LOGICAL + GAP_LOGICAL
DEFENSE_COL_WIDTH_LOGICAL = CARD_WIDTH_LOGICAL - DEFENSE_COL_X_LOGICAL - PADDING_X_LOGICAL

HEADER_HEIGHT_LOGICAL = 32
HEADER_FONT_SIZE_LOGICAL = 13
HEADER_BG_COLOR = (36, 38, 42, 255)
HEADER_TEXT_COLOR = (140, 143, 148, 255)

CARD_WIDTH = CARD_WIDTH_LOGICAL * SCALE
ROW_HEIGHT = ROW_HEIGHT_LOGICAL * SCALE
LOGO_SIZE = LOGO_SIZE_LOGICAL * SCALE
PADDING_X = PADDING_X_LOGICAL * SCALE
TOP_PADDING = TOP_PADDING_LOGICAL * SCALE
BOTTOM_PADDING = BOTTOM_PADDING_LOGICAL * SCALE

TEAM_FONT_SIZE = TEAM_FONT_SIZE_LOGICAL * SCALE
SCHEME_FONT_SIZE = SCHEME_FONT_SIZE_LOGICAL * SCALE

TEAM_COL_X = (PADDING_X_LOGICAL + LOGO_SIZE_LOGICAL + LOGO_TO_TEAM_GAP_LOGICAL) * SCALE
TEAM_COL_WIDTH = TEAM_COL_WIDTH_LOGICAL * SCALE
COACH_COL_X = COACH_COL_X_LOGICAL * SCALE
COACH_COL_WIDTH = COACH_COL_WIDTH_LOGICAL * SCALE
OFFENSE_COL_X = OFFENSE_COL_X_LOGICAL * SCALE
OFFENSE_COL_WIDTH = OFFENSE_COL_WIDTH_LOGICAL * SCALE
DEFENSE_COL_X = DEFENSE_COL_X_LOGICAL * SCALE
DEFENSE_COL_WIDTH = DEFENSE_COL_WIDTH_LOGICAL * SCALE

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
    """Truncates text with an ellipsis if it would overflow max_width, so a
    long team name or scheme label can't bleed into the next column."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "\u2026", font=font) > max_width:
        text = text[:-1]
    return (text.rstrip() + "\u2026") if text else "\u2026"


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


def _draw_header(draw, font) -> None:
    """TEAM / OFFENSE / DEFENSE column labels above the rows."""
    draw.rectangle((0, 0, CARD_WIDTH, HEADER_HEIGHT), fill=HEADER_BG_COLOR[:3])
    label_y = (HEADER_HEIGHT - HEADER_FONT_SIZE) // 2

    draw.text((TEAM_COL_X, label_y), "TEAM", font=font, fill=HEADER_TEXT_COLOR)
    draw.text((COACH_COL_X, label_y), "COACH", font=font, fill=HEADER_TEXT_COLOR)
    draw.text((OFFENSE_COL_X, label_y), "OFFENSE", font=font, fill=HEADER_TEXT_COLOR)
    draw.text((DEFENSE_COL_X, label_y), "DEFENSE", font=font, fill=HEADER_TEXT_COLOR)

    draw.line((0, HEADER_HEIGHT, CARD_WIDTH, HEADER_HEIGHT), fill=DIVIDER_COLOR, width=1 * SCALE)


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

    canvas_height = HEADER_HEIGHT + TOP_PADDING + ROW_HEIGHT * len(rows) + BOTTOM_PADDING
    canvas = Image.new("RGB", (CARD_WIDTH, canvas_height), CANVAS_BG_COLOR[:3])
    draw = ImageDraw.Draw(canvas)

    team_font = _load_font(FONT_BOLD_CANDIDATES, TEAM_FONT_SIZE)
    scheme_font = _load_font(FONT_REGULAR_CANDIDATES, SCHEME_FONT_SIZE)
    header_font = _load_font(FONT_BOLD_CANDIDATES, HEADER_FONT_SIZE)

    _draw_header(draw, header_font)

    y = HEADER_HEIGHT + TOP_PADDING
    for i, row in enumerate(rows):
        logo = logo_imgs[i]
        if logo is not None:
            square_logo = _resize_to_square(logo, LOGO_SIZE)
            canvas.paste(square_logo, (PADDING_X, y + (ROW_HEIGHT - LOGO_SIZE) // 2), square_logo)

        team_text = _fit_text(draw, row["team_name"], team_font, TEAM_COL_WIDTH)
        draw.text((TEAM_COL_X, y + (ROW_HEIGHT - TEAM_FONT_SIZE) // 2), team_text, font=team_font, fill=(255, 255, 255, 255))

        coach_text = _fit_text(draw, row.get("coach_name") or "Unknown", scheme_font, COACH_COL_WIDTH)
        draw.text((COACH_COL_X, y + (ROW_HEIGHT - SCHEME_FONT_SIZE) // 2), coach_text, font=scheme_font, fill=(200, 202, 205, 255))

        offense_text = _fit_text(draw, row.get("offense_scheme") or "Not set", scheme_font, OFFENSE_COL_WIDTH)
        draw.text((OFFENSE_COL_X, y + (ROW_HEIGHT - SCHEME_FONT_SIZE) // 2), offense_text, font=scheme_font, fill=(200, 202, 205, 255))

        defense_text = _fit_text(draw, row.get("defense_scheme") or "Not set", scheme_font, DEFENSE_COL_WIDTH)
        draw.text((DEFENSE_COL_X, y + (ROW_HEIGHT - SCHEME_FONT_SIZE) // 2), defense_text, font=scheme_font, fill=(200, 202, 205, 255))

        y += ROW_HEIGHT
        if i < len(rows) - 1:
            draw.line((PADDING_X, y, CARD_WIDTH - PADDING_X, y), fill=DIVIDER_COLOR, width=1 * SCALE)

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    buffer.seek(0)

    return discord.File(buffer, filename="scheme_cards.png")
