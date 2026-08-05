"""Builds a composite "Top 25 rankings" image — rank, team logo, team name,
movement indicator, and record per row — for the /post_top25 command.

Mirrors the fetch/composite/attach pattern established in utils/matchup_image.py:
this module returns a discord.File the caller attaches to a message, with the
embed referencing it via attachment://top25.png (see as_send_kwargs/as_edit_kwargs
in matchup_image.py, which work with any discord.File and are reused here too).
"""

import asyncio
import io

import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont

CANVAS_BG_COLOR = (49, 51, 56, 255)  # matches Discord's dark embed background, same as matchup_image.py
ROW_BG_ALT = (43, 45, 49, 255)
DIVIDER_COLOR = (58, 60, 65, 255)

CARD_WIDTH = 460
ROW_HEIGHT = 36
LOGO_SIZE = 28
PADDING_X = 16
TOP_PADDING = 10
BOTTOM_PADDING = 10

RANK_FONT_SIZE = 15
TEAM_FONT_SIZE = 15
RECORD_FONT_SIZE = 13
MOVEMENT_FONT_SIZE = 13

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

USER_TAG_COLOR = (255, 215, 0, 255)  # gold — matches EMBED_COLOR in cogs/top25.py
USER_TAG_TEXT = "(U)"
USER_TAG_GAP = 6  # space between end of team name and the tag


def _movement_color(movement: str) -> tuple:
    if movement.startswith("▲"):
        return UP_COLOR
    if movement.startswith("▼"):
        return DOWN_COLOR
    if movement.upper() == "NEW":
        return NEW_COLOR
    return FLAT_COLOR


MOVEMENT_TRIANGLE_SIZE = 9
MOVEMENT_TRIANGLE_GAP = 4  # space between triangle and the rank-change number


def _draw_triangle(draw: ImageDraw.ImageDraw, x: int, y_center: float, size: int, direction: str, color: tuple):
    """Draws a small filled up- or down-pointing triangle centered vertically
    at y_center, left edge at x. Used instead of the Unicode ▲/▼ characters
    because those glyphs are missing from some fonts (e.g. Liberation Sans),
    which renders as a 'tofu' box on hosts that don't have DejaVu — a shape
    drawn with polygon() always renders identically regardless of font."""
    half = size / 2
    if direction == "up":
        points = [(x, y_center + half), (x + size, y_center + half), (x + half, y_center - half)]
    else:
        points = [(x, y_center - half), (x + size, y_center - half), (x + half, y_center + half)]
    draw.polygon(points, fill=color)


def _draw_movement(draw: ImageDraw.ImageDraw, x: int, y: int, row_height: int, movement: str, font, color: tuple):
    """Draws the movement column. '▲N' / '▼N' render as a drawn triangle plus
    the number (numbers are plain ASCII/Latin, safe in any font); '-' and
    'NEW' are plain text already, so they render fine as-is."""
    if movement.startswith("▲") or movement.startswith("▼"):
        direction = "up" if movement.startswith("▲") else "down"
        number = movement[1:]
        y_center = y + row_height / 2
        _draw_triangle(draw, x, y_center, MOVEMENT_TRIANGLE_SIZE, direction, color)
        num_x = x + MOVEMENT_TRIANGLE_SIZE + MOVEMENT_TRIANGLE_GAP
        num_y = y + (row_height - MOVEMENT_FONT_SIZE) // 2
        draw.text((num_x, num_y), number, font=font, fill=color)
    else:
        text_y = y + (row_height - MOVEMENT_FONT_SIZE) // 2
        draw.text((x, text_y), movement, font=font, fill=color)


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
    resized = img.resize((new_w, new_h))
    # Center the (possibly non-square) resized logo on a transparent square canvas
    # so every row's logo occupies the same footprint regardless of source aspect ratio.
    square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    square.paste(resized, ((size - new_w) // 2, (size - new_h) // 2), resized)
    return square


async def build_top25_file(rows: list[dict]) -> discord.File | None:
    """rows: list of dicts with keys rank (int), movement (str, e.g. '▲4' / '▼2' /
    'NEW' / '—'), team_name (str, display name to print), record (str),
    logo_url (str or None), and user_controlled (bool, optional — draws
    '(U)' in bold gold after the team name when True). Returns a discord.File
    ready to attach, or None if rows is empty."""
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
        x += 28

        logo = logo_imgs[i]
        if logo is not None:
            square_logo = _resize_to_square(logo, LOGO_SIZE)
            canvas.paste(square_logo, (x, y + (ROW_HEIGHT - LOGO_SIZE) // 2), square_logo)
        x += LOGO_SIZE + 10

        team_name_y = y + (ROW_HEIGHT - TEAM_FONT_SIZE) // 2
        draw.text((x, team_name_y), row["team_name"], font=team_font, fill=(255, 255, 255, 255))

        if row.get("user_controlled"):
            name_width = draw.textlength(row["team_name"], font=team_font)
            tag_x = x + name_width + USER_TAG_GAP
            tag_y = y + (ROW_HEIGHT - MOVEMENT_FONT_SIZE) // 2
            draw.text((tag_x, tag_y), USER_TAG_TEXT, font=movement_font, fill=USER_TAG_COLOR)

        movement = row.get("movement", "-")
        _draw_movement(draw, CARD_WIDTH - 140, y, ROW_HEIGHT, movement, movement_font, _movement_color(movement))

        record = row.get("record", "")
        draw.text((CARD_WIDTH - 70, y + (ROW_HEIGHT - RECORD_FONT_SIZE) // 2), record,
                   font=record_font, fill=(180, 182, 185, 255))

        y += ROW_HEIGHT
        if i < len(rows) - 1:
            draw.line((PADDING_X, y, CARD_WIDTH - PADDING_X, y), fill=DIVIDER_COLOR, width=1)

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    buffer.seek(0)

    return discord.File(buffer, filename="top25.png")
