"""Builds a composite "Top 25 rankings" image — rank, team logo, team name,
movement indicator, and record per row — for the /post_top25 command.

Mirrors the fetch/composite/attach pattern established in utils/matchup_image.py:
this module returns a discord.File the caller attaches to a message, with the
embed referencing it via attachment://top25.png (see as_send_kwargs/as_edit_kwargs
in matchup_image.py, which work with any discord.File and are reused here too).

CHANGES vs previous version:
  1. Renders at SCALE x the final size (supersampling), so Discord's embed
     scaling doesn't blur the image the way a native-resolution render does.
  2. Movement arrows are now drawn as actual triangle polygons instead of
     relying on the font having glyphs for U+25B2 / U+25BC — the fallback
     fonts (Liberation Sans) and PIL's built-in bitmap font don't reliably
     include those characters, which is why arrows were posting as blank
     boxes / missing glyphs.
"""

import asyncio
import io

import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont

CANVAS_BG_COLOR = (49, 51, 56, 255)  # matches Discord's dark embed background, same as matchup_image.py
ROW_BG_ALT = (43, 45, 49, 255)
DIVIDER_COLOR = (58, 60, 65, 255)

# Supersampling factor: everything below is defined at "logical" size, then
# multiplied by SCALE when actually drawing. Discord will display the image
# scaled to whatever width the embed uses, so rendering 2-3x sharper than the
# logical size means it stays crisp instead of looking upscaled/blurry.
SCALE = 3

CARD_WIDTH_LOGICAL = 620
ROW_HEIGHT_LOGICAL = 46
LOGO_SIZE_LOGICAL = 36
PADDING_X_LOGICAL = 20
TOP_PADDING_LOGICAL = 14
BOTTOM_PADDING_LOGICAL = 14

RANK_FONT_SIZE_LOGICAL = 19
TEAM_FONT_SIZE_LOGICAL = 19
RECORD_FONT_SIZE_LOGICAL = 17
MOVEMENT_FONT_SIZE_LOGICAL = 17

RANK_COL_WIDTH_LOGICAL = 36
LOGO_TO_TEAM_GAP_LOGICAL = 14
MOVEMENT_COL_FROM_RIGHT_LOGICAL = 190
RECORD_COL_FROM_RIGHT_LOGICAL = 95

CARD_WIDTH = CARD_WIDTH_LOGICAL * SCALE
ROW_HEIGHT = ROW_HEIGHT_LOGICAL * SCALE
LOGO_SIZE = LOGO_SIZE_LOGICAL * SCALE
PADDING_X = PADDING_X_LOGICAL * SCALE
TOP_PADDING = TOP_PADDING_LOGICAL * SCALE
BOTTOM_PADDING = BOTTOM_PADDING_LOGICAL * SCALE

RANK_FONT_SIZE = RANK_FONT_SIZE_LOGICAL * SCALE
TEAM_FONT_SIZE = TEAM_FONT_SIZE_LOGICAL * SCALE
RECORD_FONT_SIZE = RECORD_FONT_SIZE_LOGICAL * SCALE
MOVEMENT_FONT_SIZE = MOVEMENT_FONT_SIZE_LOGICAL * SCALE

# Primary DejaVu paths (Debian/Ubuntu with fonts-dejavu-core installed), plus a
# couple of common fallback locations. If none of these exist on the host —
# e.g. a slim container image without system fonts — we fall back to PIL's
# built-in bitmap font rather than crashing the whole render.
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
    """Tries each candidate TTF path in order; falls back to PIL's built-in
    default font (fixed size, no anti-aliasing scaling) if none are available
    on this host, so a missing font package degrades the image rather than
    crashing the /post_top25 command."""
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        # Older Pillow versions' load_default() doesn't accept a size arg.
        return ImageFont.load_default()

UP_COLOR = (87, 242, 135, 255)
DOWN_COLOR = (237, 66, 69, 255)
NEW_COLOR = (88, 101, 242, 255)
FLAT_COLOR = (148, 150, 155, 255)


def _parse_movement(movement: str):
    """Splits a movement string into (direction, magnitude_text, color).
    direction is 'up', 'down', or None (for NEW / flat, which are drawn as
    plain text, not triangles).

    Accepts either arrow-prefixed strings ('▲4' / '▼2') or sign-prefixed
    strings ('+4' / '-2'), since the data feeding this render has used both
    formats at different points in the pipeline."""
    if movement.startswith("▲"):
        return "up", movement[1:], UP_COLOR
    if movement.startswith("▼"):
        return "down", movement[1:], DOWN_COLOR
    if movement.startswith("+") and movement[1:].isdigit():
        return "up", movement[1:], UP_COLOR
    if movement.startswith("-") and movement[1:].isdigit():
        return "down", movement[1:], DOWN_COLOR
    if movement.upper() == "NEW":
        return None, "NEW", NEW_COLOR
    return None, movement, FLAT_COLOR


def _draw_movement(draw, x: int, y: int, row_height: int, movement: str, font) -> None:
    """Draws the movement indicator. Arrows are drawn as filled triangle
    polygons (not font glyphs) since font glyph coverage for U+25B2/U+25BC is
    unreliable across fallback fonts and PIL's built-in bitmap font."""
    direction, label, color = _parse_movement(movement)
    cy = y + row_height // 2

    if direction is None:
        draw.text((x, y + (row_height - MOVEMENT_FONT_SIZE) // 2), label, font=font, fill=color)
        return

    tri_size = MOVEMENT_FONT_SIZE
    half = tri_size // 2
    if direction == "up":
        points = [(x, cy + half), (x + tri_size, cy + half), (x + tri_size // 2, cy - half)]
    else:
        points = [(x, cy - half), (x + tri_size, cy - half), (x + tri_size // 2, cy + half)]
    draw.polygon(points, fill=color)

    text_x = x + tri_size + (6 * SCALE)
    draw.text((text_x, y + (row_height - MOVEMENT_FONT_SIZE) // 2), label, font=font, fill=color)


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
    # Center the (possibly non-square) resized logo on a transparent square canvas
    # so every row's logo occupies the same footprint regardless of source aspect ratio.
    square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    square.paste(resized, ((size - new_w) // 2, (size - new_h) // 2), resized)
    return square


async def build_top25_file(rows: list[dict]) -> discord.File | None:
    """rows: list of dicts with keys rank (int), movement (str, e.g. '▲4' / '▼2' /
    'NEW' / '—'), team_name (str, display name to print), record (str), and
    logo_url (str or None). Returns a discord.File ready to attach, or None if
    rows is empty."""
    if not rows:
        return None

    logo_urls = [r.get("logo_url") for r in rows]
    async with aiohttp.ClientSession() as session:
        logo_imgs = await asyncio.gather(*[
            _fetch_logo(session, url) if url else asyncio.sleep(0, result=None)
            for url in logo_urls
        ])

    canvas_height = TOP_PADDING + ROW_HEIGHT * len(rows) + BOTTOM_PADDING
    canvas = Image.new("RGB", (CARD_WIDTH, canvas_height), CANVAS_BG_COLOR[:3])
    draw = ImageDraw.Draw(canvas)

    rank_font = _load_font(FONT_BOLD_CANDIDATES, RANK_FONT_SIZE)
    team_font = _load_font(FONT_REGULAR_CANDIDATES, TEAM_FONT_SIZE)
    record_font = _load_font(FONT_REGULAR_CANDIDATES, RECORD_FONT_SIZE)
    movement_font = _load_font(FONT_BOLD_CANDIDATES, MOVEMENT_FONT_SIZE)

    y = TOP_PADDING
    for i, row in enumerate(rows):
        x = PADDING_X
        draw.text((x, y + (ROW_HEIGHT - RANK_FONT_SIZE) // 2), f"{row['rank']:>2}", font=rank_font, fill=(200, 202, 205, 255))
        x += RANK_COL_WIDTH_LOGICAL * SCALE

        logo = logo_imgs[i]
        if logo is not None:
            square_logo = _resize_to_square(logo, LOGO_SIZE)
            canvas.paste(square_logo, (x, y + (ROW_HEIGHT - LOGO_SIZE) // 2), square_logo)
        x += LOGO_SIZE + LOGO_TO_TEAM_GAP_LOGICAL * SCALE

        draw.text((x, y + (ROW_HEIGHT - TEAM_FONT_SIZE) // 2), row["team_name"], font=team_font, fill=(255, 255, 255, 255))

        movement = row.get("movement", "—")
        _draw_movement(draw, CARD_WIDTH - MOVEMENT_COL_FROM_RIGHT_LOGICAL * SCALE, y, ROW_HEIGHT, movement, movement_font)

        record = row.get("record", "")
        draw.text((CARD_WIDTH - RECORD_COL_FROM_RIGHT_LOGICAL * SCALE, y + (ROW_HEIGHT - RECORD_FONT_SIZE) // 2), record,
                   font=record_font, fill=(180, 182, 185, 255))

        y += ROW_HEIGHT
        if i < len(rows) - 1:
            draw.line((PADDING_X, y, CARD_WIDTH - PADDING_X, y), fill=DIVIDER_COLOR, width=1 * SCALE)

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    buffer.seek(0)

    return discord.File(buffer, filename="top25.png")
