"""/post_playoff_bracket — paste CFP bracket data (tab-separated: Round, Seed1,
Team1, Record1, Seed2, Team2, Record2 — use TBD for an undetermined
opponent/seed) and post it as a bracket-style image, grouped into columns by
round. Rendering lives in utils/playoff_render.py, following the same
fetch-composite-attach pattern as utils/top25_render.py and
utils/matchup_image.py.

Per-conversation decision: fresh post each time, no edit-in-place/persistence
— the admin just re-runs the command as later rounds get decided."""

import re

import discord
from discord import app_commands
from discord.ext import commands

from utils.data import load_teams, is_admin, resolve_team
from utils.responses import send_ephemeral
from utils.matchup_image import as_send_kwargs
from utils.playoff_render import build_playoff_bracket_file

EMBED_COLOR = 0xFFD700  # gold — matches the accent already used for /post_top25
TBD_VALUES = {"tbd", "-", ""}

EXPECTED_COLUMNS = "Round, Seed1, Team1, Record1, Seed2, Team2, Record2"


def _split_row(raw_line: str) -> list:
    """Tolerant column split: pipe-delimited (markdown tables) first, then
    tab-delimited, then 2+-space-delimited. Unlike _parse_top25_text this
    doesn't attempt a single-space regex fallback — with 7 columns and two
    potentially multi-word team names, that anchor would be too ambiguous to
    parse reliably, so tab or pipe input is recommended for this command."""
    stripped = raw_line.strip()
    if "|" in stripped:
        inner = stripped.strip("|")
        return [p.strip() for p in inner.split("|") if p.strip() != ""]
    parts = [p.strip() for p in raw_line.split("\t") if p.strip() != ""]
    if len(parts) >= 7:
        return parts
    parts = [p.strip() for p in re.split(r"\s{2,}", stripped) if p.strip() != ""]
    return parts


def _parse_team_slot(seed_raw: str, team_raw: str, record_raw: str, teams: dict):
    """Returns (team_dict_or_None, error_or_None). A team_raw of TBD/-/blank
    is a valid 'not decided yet' placeholder (None, None) — not an error."""
    if team_raw.strip().lower() in TBD_VALUES:
        return None, None

    abbr, team_error = resolve_team(team_raw, teams)
    if team_error:
        return None, team_error

    team_info = teams[abbr]
    seed = None
    if seed_raw.strip().lower() not in TBD_VALUES:
        if not seed_raw.strip().isdigit():
            return None, f"`{seed_raw}` isn't a valid seed number."
        seed = int(seed_raw.strip())

    record = record_raw.strip() if record_raw.strip().lower() not in TBD_VALUES else None

    return {
        "seed": seed,
        "name": team_info.get("school") or team_info.get("name"),
        "record": record,
        "color": team_info.get("color"),
        "logo_url": team_info.get("logoDark") or team_info.get("logo"),
    }, None


def _parse_playoff_text(text: str, teams: dict) -> tuple[dict, list]:
    """Parses pasted bracket text into an ordered {round_name: [games]} dict
    (columns render left-to-right in first-seen order) plus a list of error
    strings for lines that couldn't be parsed or matched, reported rather
    than aborting the whole paste."""
    rounds: dict = {}
    errors = []

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return rounds, errors

    # Drop markdown-table separator rows entirely, e.g. "|--|---|------|...|".
    separator_re = re.compile(r"^[\s|:\-]+$")
    lines = [ln for ln in lines if not separator_re.match(ln.strip())]
    if not lines:
        return rounds, errors

    first_parts = _split_row(lines[0])
    if first_parts and first_parts[0].strip().lower() == "round":
        lines = lines[1:]  # drop the header row

    for line_num, raw_line in enumerate(lines, start=1):
        parts = _split_row(raw_line)
        if len(parts) != 7:
            errors.append(f"Line {line_num}: couldn't parse `{raw_line.strip()}` — expected {EXPECTED_COLUMNS}.")
            continue

        round_name, seed1_raw, team1_raw, record1_raw, seed2_raw, team2_raw, record2_raw = parts

        team1, team1_error = _parse_team_slot(seed1_raw, team1_raw, record1_raw, teams)
        if team1_error:
            errors.append(f"Line {line_num}: {team1_error}")
            continue

        team2, team2_error = _parse_team_slot(seed2_raw, team2_raw, record2_raw, teams)
        if team2_error:
            errors.append(f"Line {line_num}: {team2_error}")
            continue

        if team1 is None and team2 is None:
            errors.append(f"Line {line_num}: both teams are TBD — at least one team must be specified.")
            continue

        rounds.setdefault(round_name, []).append({"team1": team1, "team2": team2})

    return rounds, errors


class PlayoffModal(discord.ui.Modal, title="Post Playoff Bracket"):
    def __init__(self, cog: "Playoff"):
        super().__init__(timeout=600)
        self.cog = cog
        self.bracket_input = discord.ui.TextInput(
            label="Paste bracket (tab-separated)",
            style=discord.TextStyle.paragraph,
            placeholder="Round\tSeed1\tTeam1\tRecord1\tSeed2\tTeam2\tRecord2 (TBD if unknown)",
            max_length=4000,
            required=True,
        )
        self.add_item(self.bracket_input)

    async def on_submit(self, interaction: discord.Interaction):
        rounds, errors = _parse_playoff_text(self.bracket_input.value, self.cog.teams)

        if not rounds:
            message = f"Couldn't parse any bracket data from that text. Make sure it's tab-separated: `{EXPECTED_COLUMNS}` (use TBD for an undetermined seed/team/record)."
            if errors:
                message += "\n\n" + "\n".join(f"- {e}" for e in errors[:10])
            await interaction.response.send_message(message, ephemeral=True)
            return

        # Fetching every team's logo and compositing the bracket image takes a
        # moment — defer so Discord doesn't consider the interaction timed out.
        await interaction.response.defer(ephemeral=True)

        file = await build_playoff_bracket_file(rounds)

        embed = discord.Embed(title="2026 College Football Playoff", color=EMBED_COLOR)
        if file is not None:
            embed.set_image(url="attachment://playoff_bracket.png")

        send_kwargs = {"embed": embed, **as_send_kwargs(file)}
        await interaction.channel.send(**send_kwargs)

        game_count = sum(len(games) for games in rounds.values())
        note = ""
        if errors:
            note = f"\n\n{len(errors)} line(s) skipped:\n" + "\n".join(f"- {e}" for e in errors)
        await interaction.followup.send(
            f"Posted playoff bracket with {game_count} game(s) across {len(rounds)} round(s).{note}",
            ephemeral=True,
        )


class Playoff(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.teams = load_teams()

    @app_commands.command(name="post_playoff_bracket", description="Paste CFP bracket data and post it as a bracket image (admin only)")
    async def post_playoff_bracket(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can post the playoff bracket.")
            return
        await interaction.response.send_modal(PlayoffModal(self))


async def setup(bot: commands.Bot):
    await bot.add_cog(Playoff(bot))
