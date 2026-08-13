"""Builds a composite "CFP Bracket" image — one column per round, each column
stacking its matchups top to bottom — for the /post_playoff_bracket command.

Deliberately does NOT attempt to draw literal bracket connector lines between
rounds (unlike a traditional tournament bracket graphic). The admin pastes
whatever rounds/games are currently decided, in the order they should appear,
and this renders them as parallel round columns — a "results so far" view
rather than a full advancing-bracket diagram. Mirrors the fetch-composite-
attach pattern established in utils/matchup_image.py and utils/top25_render.py.
"""

import asyncio
import io

import aiohttp
import discord
from PIL import Image, ImageDraw

from utils.render_common import load_font, fetch_logo, resize_to_square, hex_to_rgb, FONT_BOLD_CANDIDATES, FONT_REGULAR_CANDIDATES

CANVAS_BG_COLOR = (49, 51, 56, 255)  # matches Discord's dark embed background, same as top25_render.py
DIVIDER_COLOR = (58, 60, 65, 255)
TBD_COLOR = (90, 92, 97, 255)

COLUMN_WIDTH = 240
COLUMN_GAP = 14
COLUMN_HEADER_HEIGHT = 30
GAME_BOX_HEIGHT = 108
GAME_GAP = 14
LOGO_SIZE = 26
TOP_PADDING = 14
BOTTOM_PADDING = 14
SIDE_PADDING = 16

ROUND_HEADER_FONT_SIZE = 15
SEED_FONT_SIZE = 11
TEAM_FONT_SIZE = 13
RECORD_FONT_SIZE = 11

TEAM_COLOR_BAR_WIDTH = 4


def _draw_team_slot(draw: ImageDraw.ImageDraw, canvas: Image.Image, x: int, y: int, width: int, height: int,
                     team: dict | None, logo_img, seed_font, team_font, record_font):
    """Draws one team's half of a matchup box: a colored left bar, seed,
    logo (if available), name, and record. team=None renders a muted 'TBD'
    placeholder for a slot that hasn't been decided yet."""
    if team is None:
        draw.rectangle((x, y, x + width, y + height), fill=(38, 39, 43, 255))
        draw.text((x + 12, y + height // 2 - RECORD_FONT_SIZE // 2), "TBD", font=record_font, fill=TBD_COLOR)
        return

    bar_color = hex_to_rgb(team.get("color"))
    draw.rectangle((x, y, x + TEAM_COLOR_BAR_WIDTH, y + height), fill=bar_color)

    inner_x = x + TEAM_COLOR_BAR_WIDTH + 8
    if logo_img is not None:
        square_logo = resize_to_square(logo_img, LOGO_SIZE)
        canvas.paste(square_logo, (inner_x, y + height // 2 - LOGO_SIZE // 2), square_logo)
    text_x = inner_x + LOGO_SIZE + 8

    # Three stacked lines (seed / name / record) inset from the top of this
    # half-box, each on its own row with a small gap — sized to fit within
    # half_height (see GAME_BOX_HEIGHT) without touching the other team's slot.
    line_gap = 2
    top_inset = 6
    seed_text = f"#{team['seed']}" if team.get("seed") else ""
    seed_y = y + top_inset
    draw.text((text_x, seed_y), seed_text, font=seed_font, fill=(180, 182, 185, 255))

    name_y = seed_y + SEED_FONT_SIZE + line_gap
    draw.text((text_x, name_y), team["name"], font=team_font, fill=(255, 255, 255, 255))

    if team.get("record"):
        record_y = name_y + TEAM_FONT_SIZE + line_gap
        draw.text((text_x, record_y), team["record"], font=record_font, fill=(180, 182, 185, 255))


async def build_playoff_bracket_file(rounds: "dict[str, list[dict]]") -> discord.File | None:
    """rounds: ordered dict of round_name -> list of games, where each game is
    {"team1": {seed, name, record, color, logo_url} or None,
     "team2": {...} or None}. A None team renders as a 'TBD' placeholder.
    Returns a discord.File ready to attach, or None if there are no games."""
    if not rounds or not any(rounds.values()):
        return None

    # Collect every logo URL across every round/game up front so they can all
    # be fetched concurrently in one aiohttp session, same pattern as top25_render.
    logo_urls = []
    for games in rounds.values():
        for game in games:
            for key in ("team1", "team2"):
                team = game.get(key)
                logo_urls.append(team.get("logo_url") if team else None)

    async with aiohttp.ClientSession() as session:
        logo_imgs = await asyncio.gather(*[
            fetch_logo(session, url) if url else asyncio.sleep(0, result=None)
            for url in logo_urls
        ])

    num_columns = len(rounds)
    max_games = max(len(games) for games in rounds.values())
    canvas_width = SIDE_PADDING * 2 + num_columns * COLUMN_WIDTH + (num_columns - 1) * COLUMN_GAP
    canvas_height = TOP_PADDING + COLUMN_HEADER_HEIGHT + max_games * (GAME_BOX_HEIGHT + GAME_GAP) + BOTTOM_PADDING

    canvas = Image.new("RGB", (canvas_width, canvas_height), CANVAS_BG_COLOR[:3])
    draw = ImageDraw.Draw(canvas)

    header_font = load_font(FONT_BOLD_CANDIDATES, ROUND_HEADER_FONT_SIZE)
    seed_font = load_font(FONT_BOLD_CANDIDATES, SEED_FONT_SIZE)
    team_font = load_font(FONT_REGULAR_CANDIDATES, TEAM_FONT_SIZE)
    record_font = load_font(FONT_REGULAR_CANDIDATES, RECORD_FONT_SIZE)

    logo_idx = 0
    col_x = SIDE_PADDING
    for round_name, games in rounds.items():
        draw.text((col_x, TOP_PADDING), round_name.upper(), font=header_font, fill=(255, 215, 0, 255))

        game_y = TOP_PADDING + COLUMN_HEADER_HEIGHT
        for game in games:
            half_height = GAME_BOX_HEIGHT // 2

            team1 = game.get("team1")
            logo1 = logo_imgs[logo_idx]
            logo_idx += 1
            _draw_team_slot(draw, canvas, col_x, game_y, COLUMN_WIDTH, half_height,
                             team1, logo1, seed_font, team_font, record_font)

            team2 = game.get("team2")
            logo2 = logo_imgs[logo_idx]
            logo_idx += 1
            _draw_team_slot(draw, canvas, col_x, game_y + half_height, COLUMN_WIDTH, half_height,
                             team2, logo2, seed_font, team_font, record_font)

            draw.rectangle((col_x, game_y, col_x + COLUMN_WIDTH, game_y + GAME_BOX_HEIGHT),
                            outline=DIVIDER_COLOR, width=1)

            game_y += GAME_BOX_HEIGHT + GAME_GAP

        col_x += COLUMN_WIDTH + COLUMN_GAP

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    buffer.seek(0)

    return discord.File(buffer, filename="playoff_bracket.png")
