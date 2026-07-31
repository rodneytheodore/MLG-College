"""/post_top25 — paste a Top 25 rankings table (tab-separated: Rank, Movement,
Team, Record[, Last Week]) and post it as an embed with each team's logo.
Rendering lives in utils/top25_render.py, following the same
fetch-composite-attach pattern as utils/matchup_image.py."""

import re

import discord
from discord import app_commands
from discord.ext import commands

from utils.data import load_teams, is_admin, resolve_team
from utils.responses import send_ephemeral
from utils.matchup_image import as_send_kwargs
from utils.top25_render import build_top25_file

EMBED_COLOR = 0xFFD700  # gold — matches the accent already used for user-game embeds in scheduling.py


def _parse_top25_text(text: str, teams: dict) -> tuple[list[dict], list[str]]:
    """Parses a pasted rankings table into row dicts. Tolerant of a header row
    ('Rk  Mv  Team  Record  LW'), tab- or multi-space-separated columns, and
    an optional trailing 'Last Week' column (ignored). Returns (rows, errors) —
    lines that don't parse or whose team can't be matched are skipped and
    reported rather than aborting the whole paste."""
    rows = []
    errors = []

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return rows, errors

    first_token = re.split(r"\t+|\s{2,}", lines[0].strip())[0]
    if not first_token.isdigit():
        lines = lines[1:]  # drop the header row, e.g. "Rk  Mv  Team  Record  LW"

    for line_num, raw_line in enumerate(lines, start=1):
        parts = [p.strip() for p in raw_line.split("\t") if p.strip() != ""]
        if len(parts) < 4:
            parts = [p.strip() for p in re.split(r"\s{2,}", raw_line.strip()) if p.strip() != ""]
        if len(parts) < 4:
            errors.append(f"Line {line_num}: couldn't parse `{raw_line.strip()}` — expected Rank, Movement, Team, Record.")
            continue

        rank_raw, movement, team_raw, record = parts[0], parts[1], parts[2], parts[3]
        if not rank_raw.isdigit():
            errors.append(f"Line {line_num}: `{rank_raw}` isn't a valid rank number.")
            continue

        team_abbr, team_error = resolve_team(team_raw, teams)
        if team_error:
            errors.append(f"Line {line_num}: {team_error}")
            continue

        team_info = teams[team_abbr]
        rows.append({
            "rank": int(rank_raw),
            "movement": movement,
            "team_name": team_info.get("school") or team_info.get("name"),
            "record": record,
            "logo_url": team_info.get("logoDark") or team_info.get("logo"),
        })

    rows.sort(key=lambda r: r["rank"])
    return rows, errors


class Top25Modal(discord.ui.Modal, title="Post Top 25 Rankings"):
    def __init__(self, cog: "Top25", week: int | None):
        super().__init__(timeout=600)
        self.cog = cog
        self.week = week
        self.rankings_input = discord.ui.TextInput(
            label="Paste rankings (tab-separated)",
            style=discord.TextStyle.paragraph,
            placeholder="Rk\tMv\tTeam\tRecord\tLW\n1\t—\tMiami\t4-0\t1\n2\t—\tOhio State\t3-0\t2\n...",
            max_length=4000,
            required=True,
        )
        self.add_item(self.rankings_input)

    async def on_submit(self, interaction: discord.Interaction):
        rows, errors = _parse_top25_text(self.rankings_input.value, self.cog.teams)

        if not rows:
            message = "Couldn't parse any rankings from that text. Make sure it's tab-separated: `Rank  Movement  Team  Record`."
            if errors:
                message += "\n\n" + "\n".join(f"- {e}" for e in errors[:10])
            await interaction.response.send_message(message, ephemeral=True)
            return

        # Fetching every team's logo and compositing the image takes a moment —
        # defer so Discord doesn't consider the interaction timed out.
        await interaction.response.defer(ephemeral=True)

        title = f"Top 25 — Week {self.week}" if self.week else "Top 25 Rankings"
        file = await build_top25_file(rows)

        embed = discord.Embed(title=title, color=EMBED_COLOR)
        if file is not None:
            embed.set_image(url="attachment://top25.png")

        send_kwargs = {"embed": embed, **as_send_kwargs(file)}
        await interaction.channel.send(**send_kwargs)

        note = ""
        if errors:
            note = f"\n\n{len(errors)} line(s) skipped:\n" + "\n".join(f"- {e}" for e in errors)
        await interaction.followup.send(f"Posted Top 25 with {len(rows)} team(s).{note}", ephemeral=True)


class Top25(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.teams = load_teams()

    @app_commands.command(name="post_top25", description="Paste a Top 25 rankings table and post it as an embed with logos (admin only)")
    @app_commands.describe(week="Optional week number to show in the title")
    async def post_top25(self, interaction: discord.Interaction, week: int = None):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can post Top 25 rankings.")
            return
        await interaction.response.send_modal(Top25Modal(self, week))


async def setup(bot: commands.Bot):
    await bot.add_cog(Top25(bot))
