"""Builds a composite 'Team A logo vs Team B logo' image for game embeds."""

import io

import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont

TARGET_LOGO_HEIGHT = 75
CANVAS_BG_COLOR = (49, 51, 56, 255)  # matches Discord's dark embed background
VS_GAP_WIDTH = 30
PADDING = 9


async def _fetch_logo(session: aiohttp.ClientSession, url: str) -> Image.Image:
    async with session.get(url) as resp:
        resp.raise_for_status()
        data = await resp.read()
    return Image.open(io.BytesIO(data)).convert("RGBA")


def _resize_to_height(img: Image.Image, height: int) -> Image.Image:
    ratio = height / img.height
    return img.resize((max(1, int(img.width * ratio)), height))


def _composite(home_logo: Image.Image, away_logo: Image.Image) -> Image.Image:
    home_resized = _resize_to_height(home_logo, TARGET_LOGO_HEIGHT)
    away_resized = _resize_to_height(away_logo, TARGET_LOGO_HEIGHT)

    canvas_width = home_resized.width + VS_GAP_WIDTH + away_resized.width + (PADDING * 2)
    canvas_height = TARGET_LOGO_HEIGHT + PADDING
    canvas = Image.new("RGBA", (canvas_width, canvas_height), CANVAS_BG_COLOR)

    canvas.paste(home_resized, (PADDING // 2, PADDING // 2), home_resized)
    canvas.paste(
        away_resized,
        (canvas_width - away_resized.width - PADDING // 2, PADDING // 2),
        away_resized,
    )

    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 15
        )
    except OSError:
        font = ImageFont.load_default()

    vs_text = "VS"
    bbox = draw.textbbox((0, 0), vs_text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    text_x = (canvas_width - text_w) / 2
    text_y = (canvas_height - text_h) / 2 - bbox[1]
    draw.text((text_x, text_y), vs_text, font=font, fill=(255, 255, 255, 255))

    return canvas


async def build_matchup_file(home_logo_url: str, away_logo_url: str) -> discord.File | None:
    """Fetches both logos, composites them side-by-side with a VS divider,
    and returns a discord.File ready to attach to a message. Returns None
    if either logo fails to download, so callers can fall back gracefully
    (e.g. to the single-thumbnail embed) rather than erroring the whole post."""
    try:
        async with aiohttp.ClientSession() as session:
            home_img = await _fetch_logo(session, home_logo_url)
            away_img = await _fetch_logo(session, away_logo_url)
    except Exception:
        # Deliberately broad: any failure (network, bad URL, corrupt image,
        # decoding error) should fall back gracefully, not crash the post.
        return None

    composite = _composite(home_img, away_img)

    buffer = io.BytesIO()
    composite.convert("RGB").save(buffer, format="PNG")
    buffer.seek(0)

    return discord.File(buffer, filename="matchup.png")


# ---- Attachment helpers ----
#
# build_matchup_file() (and build_game_embed() in cogs/scheduling.py, which
# calls it) can return None if the logo fetch/composite fails, so every
# caller that sends or edits a message with the resulting embed needs the
# same "only attach if there's actually a file" boilerplate. Centralizing
# it here (rather than repeating `if file is not None: kwargs[...] = ...`
# at every call site in scheduling.py, both for CPU and user games) means
# there's one place to change if that attachment logic ever needs to.

def as_send_kwargs(file: discord.File | None) -> dict:
    """Spread into channel.send(**kwargs, **as_send_kwargs(file)) to attach a
    freshly built matchup image, or attach nothing if the build failed."""
    return {"file": file} if file is not None else {}


def as_edit_kwargs(file: discord.File | None) -> dict:
    """Spread into message.edit(**kwargs, **as_edit_kwargs(file)) to replace
    a message's image with a freshly built matchup image, or leave the
    existing attachment untouched if the build failed."""
    return {"attachments": [file]} if file is not None else {}
