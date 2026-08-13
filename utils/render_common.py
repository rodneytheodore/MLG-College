"""Shared image-rendering helpers used by utils/top25_render.py and
utils/playoff_render.py: font loading with a fallback chain (so a missing
font package on the host degrades gracefully instead of crashing — see the
/post_top25 font bug this was extracted from), team logo fetching, logo
resizing, and hex color parsing for team-colored elements."""

import io

import aiohttp
from PIL import Image, ImageFont

# Primary DejaVu paths (Debian/Ubuntu with fonts-dejavu-core installed), plus a
# couple of common fallback locations. If none of these exist on the host —
# e.g. a slim container image without system fonts — callers fall back to
# PIL's built-in bitmap font rather than crashing the render.
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


def load_font(candidates: list, size: int):
    """Tries each candidate TTF path in order; falls back to PIL's built-in
    default font if none are available on this host, so a missing font
    package degrades the image rather than crashing the command."""
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


async def fetch_logo(session: aiohttp.ClientSession, url: str) -> Image.Image | None:
    """Returns None on any failure so one bad/missing logo URL doesn't take
    down the whole render — callers fall back to a blank square for that slot."""
    if not url:
        return None
    try:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None


def resize_to_square(img: Image.Image, size: int) -> Image.Image:
    ratio = size / max(img.width, img.height)
    new_w, new_h = max(1, int(img.width * ratio)), max(1, int(img.height * ratio))
    resized = img.resize((new_w, new_h))
    # Center the (possibly non-square) resized logo on a transparent square canvas
    # so every slot's logo occupies the same footprint regardless of source aspect ratio.
    square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    square.paste(resized, ((size - new_w) // 2, (size - new_h) // 2), resized)
    return square


def hex_to_rgb(hex_color: str, default: tuple = (90, 92, 97)) -> tuple:
    """Parses a hex color string (with or without leading '#') into an RGB
    tuple; falls back to a neutral gray if missing or unparseable, since not
    every entry in fbs_teams_full.json is guaranteed to have a usable color."""
    if not hex_color:
        return default
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return default
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default
