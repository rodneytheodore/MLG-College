import json
import os

import discord
from discord import app_commands
from discord.ext import commands

from utils.data import load_roster, load_teams, is_admin
from utils.responses import send_ephemeral

DATA_DIR = os.environ.get("DATA_DIR", "data").strip()


def load_defense_installs() -> dict:
    path = os.path.join(DATA_DIR, "defense_installs.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save_defense_installs(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "defense_installs.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---- Modals ----

class InstallDefenseModal1(discord.ui.Modal, title="Base Formations"):
    """Up to 5 base defensive formations."""

    f01 = discord.ui.TextInput(label="01", placeholder="e.g. 4-3 Under", required=True, max_length=60)
    f02 = discord.ui.TextInput(label="02", placeholder="e.g. 3-4 Odd", required=False, max_length=60)
    f03 = discord.ui.TextInput(label="03 (optional)", placeholder="Optional", required=False, max_length=60)
    f04 = discord.ui.TextInput(label="04 (optional)", placeholder="Optional", required=False, max_length=60)
    f05 = discord.ui.TextInput(label="05 (optional)", placeholder="Optional", required=False, max_length=60)

    def __init__(self, abbr: str):
        super().__init__()
        self.abbr = abbr

    async def on_submit(self, interaction: discord.Interaction):
        formations = [
            self.f01.value.strip(),
            self.f02.value.strip(),
            self.f03.value.strip(),
            self.f04.value.strip(),
            self.f05.value.strip(),
        ]
        await interaction.response.send_modal(InstallDefenseModal2(self.abbr, formations))


class InstallDefenseModal2(discord.ui.Modal, title="Sub Packages (1 of 2)"):
    """Sub packages slots 01-05."""

    s01 = discord.ui.TextInput(label="01", placeholder="e.g. Nickel Over", required=True, max_length=60)
    s02 = discord.ui.TextInput(label="02", placeholder="e.g. Dime Rush", required=False, max_length=60)
    s03 = discord.ui.TextInput(label="03 (optional)", placeholder="Optional", required=False, max_length=60)
    s04 = discord.ui.TextInput(label="04 (optional)", placeholder="Optional", required=False, max_length=60)
    s05 = discord.ui.TextInput(label="05 (optional)", placeholder="Optional", required=False, max_length=60)

    def __init__(self, abbr: str, formations: list[str]):
        super().__init__()
        self.abbr = abbr
        self.formations = formations

    async def on_submit(self, interaction: discord.Interaction):
        subs = [
            self.s01.value.strip(),
            self.s02.value.strip(),
            self.s03.value.strip(),
            self.s04.value.strip(),
            self.s05.value.strip(),
        ]
        await interaction.response.send_modal(
            InstallDefenseModal3(self.abbr, self.formations, subs)
        )


class InstallDefenseModal3(discord.ui.Modal, title="Sub Packages (2 of 2)"):
    """Sub packages slots 06-08, all optional."""

    s06 = discord.ui.TextInput(label="06 (optional)", placeholder="Optional", required=False, max_length=60)
    s07 = discord.ui.TextInput(label="07 (optional)", placeholder="Optional", required=False, max_length=60)
    s08 = discord.ui.TextInput(label="08 (optional)", placeholder="Optional", required=False, max_length=60)

    def __init__(self, abbr: str, formations: list[str], prev_subs: list[str]):
        super().__init__()
        self.abbr = abbr
        self.formations = formations
        self.prev_subs = prev_subs

    async def on_submit(self, interaction: discord.Interaction):
        final_subs = [
            self.s06.value.strip(),
            self.s07.value.strip(),
            self.s08.value.strip(),
        ]

        all_formations = [f for f in self.formations if f]
        all_subs = [s for s in (self.prev_subs + final_subs) if s]

        if not all_formations:
            await interaction.response.send_message(
                "At least one base formation is required. Run `/install_defense` again.",
                ephemeral=True,
            )
            return

        if not all_subs:
            await interaction.response.send_message(
                "At least one sub package is required. Run `/install_defense` again.",
                ephemeral=True,
            )
            return

        installs = load_defense_installs()
        installs[self.abbr] = {"formations": all_formations, "sub_packages": all_subs}
        save_defense_installs(installs)

        teams = load_teams()
        team = teams.get(self.abbr, {})
        team_color = int(team.get("color", "C9A227"), 16)
        team_name = team.get("name", self.abbr)

        formation_val = "\n".join(f"`{i + 1:02d}` {f}" for i, f in enumerate(all_formations))
        subs_val = "\n".join(f"`{i + 1:02d}` {s}" for i, s in enumerate(all_subs))

        embed = discord.Embed(title=f"{team_name} — Defensive Install", color=team_color)
        logo = team.get("logoDark") or team.get("logo")
        if logo:
            embed.set_thumbnail(url=logo)
        embed.add_field(name="Base Formations", value=formation_val, inline=True)
        embed.add_field(name="Sub Packages", value=subs_val, inline=True)
        embed.set_footer(
            text=f"{len(all_formations)} formation(s) · {len(all_subs)} sub package(s) · {interaction.user.display_name}"
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)


# ---- Cog ----

class InstallDefense(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="install_defense",
        description="Submit your team's base defensive formations and sub packages",
    )
    async def install_defense(self, interaction: discord.Interaction):
        roster = load_roster()
        abbr = next(
            (a for a, v in roster.items() if v["user_id"] == interaction.user.id),
            None,
        )
        if abbr is None:
            await send_ephemeral(interaction, "You don't own a team in this league.")
            return

        await interaction.response.send_modal(InstallDefenseModal1(abbr=abbr))


async def setup(bot: commands.Bot):
    await bot.add_cog(InstallDefense(bot))
