import json
import os

import discord
from discord import app_commands
from discord.ext import commands

from utils.data import load_roster, load_teams, is_admin
from utils.responses import send_ephemeral

DATA_DIR = os.environ.get("DATA_DIR", "data").strip()


def load_offense_installs() -> dict:
    path = os.path.join(DATA_DIR, "offense_installs.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save_offense_installs(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "offense_installs.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---- Modals ----

class InstallOffenseModal1(discord.ui.Modal, title="Base Formations (1 of 3)"):
    """Slots 01–05, all required."""

    f01 = discord.ui.TextInput(label="01", placeholder="e.g. Gun Bunch", required=True, max_length=60)
    f02 = discord.ui.TextInput(label="02", placeholder="Formation name", required=True, max_length=60)
    f03 = discord.ui.TextInput(label="03", placeholder="Formation name", required=True, max_length=60)
    f04 = discord.ui.TextInput(label="04", placeholder="Formation name", required=True, max_length=60)
    f05 = discord.ui.TextInput(label="05", placeholder="Formation name", required=True, max_length=60)

    def __init__(self, abbr: str):
        super().__init__()
        self.abbr = abbr

    async def on_submit(self, interaction: discord.Interaction):
        batch = [
            self.f01.value.strip(),
            self.f02.value.strip(),
            self.f03.value.strip(),
            self.f04.value.strip(),
            self.f05.value.strip(),
        ]
        await interaction.response.send_modal(InstallOffenseModal2(self.abbr, batch))


class InstallOffenseModal2(discord.ui.Modal, title="Base Formations (2 of 3)"):
    """Slots 06–08 required, 09–10 optional."""

    f06 = discord.ui.TextInput(label="06", placeholder="Formation name", required=True, max_length=60)
    f07 = discord.ui.TextInput(label="07", placeholder="Formation name", required=True, max_length=60)
    f08 = discord.ui.TextInput(label="08", placeholder="Formation name", required=True, max_length=60)
    f09 = discord.ui.TextInput(label="09 (optional)", placeholder="Optional", required=False, max_length=60)
    f10 = discord.ui.TextInput(label="10 (optional)", placeholder="Optional", required=False, max_length=60)

    def __init__(self, abbr: str, prev: list[str]):
        super().__init__()
        self.abbr = abbr
        self.prev = prev

    async def on_submit(self, interaction: discord.Interaction):
        batch = [
            self.f06.value.strip(),
            self.f07.value.strip(),
            self.f08.value.strip(),
            self.f09.value.strip(),
            self.f10.value.strip(),
        ]
        await interaction.response.send_modal(InstallOffenseModal3(self.abbr, self.prev + batch))


class InstallOffenseModal3(discord.ui.Modal, title="Base Formations (3 of 3)"):
    """Slots 11–14, all optional."""

    f11 = discord.ui.TextInput(label="11 (optional)", placeholder="Optional", required=False, max_length=60)
    f12 = discord.ui.TextInput(label="12 (optional)", placeholder="Optional", required=False, max_length=60)
    f13 = discord.ui.TextInput(label="13 (optional)", placeholder="Optional", required=False, max_length=60)
    f14 = discord.ui.TextInput(label="14 (optional)", placeholder="Optional", required=False, max_length=60)

    def __init__(self, abbr: str, prev: list[str]):
        super().__init__()
        self.abbr = abbr
        self.prev = prev

    async def on_submit(self, interaction: discord.Interaction):
        final_batch = [
            self.f11.value.strip(),
            self.f12.value.strip(),
            self.f13.value.strip(),
            self.f14.value.strip(),
        ]
        # Filter empty slots across all three modals
        all_formations = [f for f in (self.prev + final_batch) if f]

        if len(all_formations) < 8:
            await interaction.response.send_message(
                f"❌ At least 8 formations are required. Got {len(all_formations)}. "
                f"Run `/install_offense` again to resubmit.",
                ephemeral=True,
            )
            return

        installs = load_offense_installs()
        installs[self.abbr] = {"formations": all_formations}
        save_offense_installs(installs)

        teams = load_teams()
        team = teams.get(self.abbr, {})
        team_color = int(team.get("color", "C9A227"), 16)
        team_name = team.get("name", self.abbr)

        left_val = "\n".join(f"`{i + 1:02d}` {f}" for i, f in enumerate(all_formations[:7]))
        right_val = "\n".join(f"`{i + 8:02d}` {f}" for i, f in enumerate(all_formations[7:])) or "—"

        embed = discord.Embed(title=f"{team_name} — Base Formations", color=team_color)
        logo = team.get("logoDark") or team.get("logo")
        if logo:
            embed.set_thumbnail(url=logo)
        embed.add_field(name="01–07", value=left_val, inline=True)
        embed.add_field(name="08–14", value=right_val, inline=True)
        embed.set_footer(text=f"{len(all_formations)} formations · {interaction.user.display_name}")

        await interaction.response.send_message(embed=embed, ephemeral=True)


# ---- Cog ----

class InstallOffense(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="install_offense",
        description="Submit your team's base offensive formations (8–14 required)",
    )
    async def install_offense(self, interaction: discord.Interaction):
        roster = load_roster()
        abbr = next(
            (a for a, v in roster.items() if v["user_id"] == interaction.user.id),
            None,
        )
        if abbr is None:
            await send_ephemeral(interaction, "You don't own a team in this league.")
            return

        await interaction.response.send_modal(InstallOffenseModal1(abbr=abbr))


async def setup(bot: commands.Bot):
    await bot.add_cog(InstallOffense(bot))
