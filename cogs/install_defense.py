import json
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils.data import load_roster, load_teams
from utils.responses import send_ephemeral

DATA_DIR = os.environ.get("DATA_DIR", "data").strip()

# ---- Constants (also imported by scheme_cards) ----

PRESSURE_TYPES = [
    ("Stunts & Games (TEX, ET, Pirate, etc)", "Stunts & Games"),
    ("Interior Blitzes (A-Gap, Cross Dog, Mug)", "Interior Blitzes"),
    ("Edge Blitzes (Sam, Will, Nickel, Corner)", "Edge Blitzes"),
    ("Zone Pressures (Fire Zones)", "Zone Pressures"),
    ("Sim Pressures (Creepers, Sims)", "Sim Pressures"),
    ("Man Pressures (Cover 0, Cover 1)", "Man Pressures"),
]
PRESSURE_TYPES_MAX_SELECT = 3

BASE_COVERAGES = [(c, c) for c in [
    "Cover 0", "Cover 1", "Cover 2", "Cover 2 Man",
    "Cover 3 Sky/Cloud", "Cover 3 Match/Seam",
    "Cover 4 Quarters/Palms", "Cover 6/Cover 9",
]]
BASE_COVERAGES_MAX_SELECT = 4


# ---- Storage ----

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


# ---- Shared multi-select view ----

class DefenseMultiSelectView(discord.ui.View):
    """Reusable multi-select step with a Confirm button."""

    def __init__(self, options: list[tuple[str, str]], max_values: int, placeholder: str, on_confirm):
        super().__init__(timeout=180)
        self._on_confirm = on_confirm
        self.selected: list[str] = []

        select = discord.ui.Select(
            placeholder=placeholder,
            min_values=1,
            max_values=max_values,
            options=[
                discord.SelectOption(label=label[:100], value=value[:100])
                for label, value in options
            ],
        )
        select.callback = self._on_select(select)
        self.add_item(select)

        btn = discord.ui.Button(label="Confirm →", style=discord.ButtonStyle.primary)
        btn.callback = self._on_confirm_click
        self.add_item(btn)

    def _on_select(self, select: discord.ui.Select):
        async def callback(interaction: discord.Interaction):
            self.selected = select.values
            await interaction.response.defer()
        return callback

    async def _on_confirm_click(self, interaction: discord.Interaction):
        if not self.selected:
            await interaction.response.send_message(
                "Select at least one option before confirming.", ephemeral=True
            )
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await self._on_confirm(interaction, self.selected)


# ---- Final confirm view ----

class DefenseInstallConfirmView(discord.ui.View):
    def __init__(self, abbr: str, formations: list[str], sub_packages: list[str],
                 coverages: list[str], pressures: list[str]):
        super().__init__(timeout=60)
        self.abbr = abbr
        self.formations = formations
        self.sub_packages = sub_packages
        self.coverages = coverages
        self.pressures = pressures

    @discord.ui.button(label="✅ Save Install", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        installs = load_defense_installs()
        installs[self.abbr] = {
            "formations": self.formations,
            "sub_packages": self.sub_packages,
            "coverages": self.coverages,
            "pressures": self.pressures,
        }
        save_defense_installs(installs)

        teams = load_teams()
        team = teams.get(self.abbr, {})
        team_color = int(team.get("color", "BA0C2F"), 16)
        team_name = team.get("name", self.abbr)
        submitted = datetime.now(timezone.utc).strftime("%B %d, %Y")

        formation_val = "\n".join(f"`{i+1:02d}` {f}" for i, f in enumerate(self.formations))
        subs_val = "\n".join(f"`{i+1:02d}` {s}" for i, s in enumerate(self.sub_packages))
        coverage_val = "\n".join(f"`{i+1:02d}` {c}" for i, c in enumerate(self.coverages))
        pressure_val = "\n".join(f"`{i+1:02d}` {p}" for i, p in enumerate(self.pressures))

        embed = discord.Embed(title=f"{team_name} — Defensive Install", color=team_color)
        logo = team.get("logoDark") or team.get("logo")
        if logo:
            embed.set_thumbnail(url=logo)
        embed.add_field(name="Base Formations", value=formation_val, inline=True)
        embed.add_field(name="Sub Packages", value=subs_val, inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=False)
        embed.add_field(name="Base Coverages", value=coverage_val, inline=True)
        embed.add_field(name="Pressure Packages", value=pressure_val, inline=True)
        embed.set_footer(text=f"Last updated: {submitted}")

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=None, embed=embed, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled. Nothing was saved.", view=self)


# ---- Continue button (bridges modal → modal since Discord forbids modal → modal) ----

class _ContinueView(discord.ui.View):
    """Ephemeral 'Continue' button used between chained modals."""

    def __init__(self, label: str, on_click):
        super().__init__(timeout=300)
        self._on_click = on_click
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
        btn.callback = self._handle
        self.add_item(btn)

    async def _handle(self, interaction: discord.Interaction):
        await self._on_click(interaction)


# ---- Modals ----

class InstallDefenseModal1(discord.ui.Modal, title="Base Formations"):
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
            self.f01.value.strip(), self.f02.value.strip(),
            self.f03.value.strip(), self.f04.value.strip(), self.f05.value.strip(),
        ]
        abbr = self.abbr

        async def on_continue(interaction: discord.Interaction):
            await interaction.response.send_modal(InstallDefenseModal2(abbr, formations))

        view = _ContinueView("Continue → Sub Packages 01–05", on_continue)
        await interaction.response.send_message(
            "**Base Formations saved.** Click to continue:", view=view, ephemeral=True
        )


class InstallDefenseModal2(discord.ui.Modal, title="Sub Packages (1 of 2)"):
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
            self.s01.value.strip(), self.s02.value.strip(), self.s03.value.strip(),
            self.s04.value.strip(), self.s05.value.strip(),
        ]
        abbr = self.abbr
        formations = self.formations

        async def on_continue(interaction: discord.Interaction):
            await interaction.response.send_modal(InstallDefenseModal3(abbr, formations, subs))

        view = _ContinueView("Continue → Sub Packages 06–08", on_continue)
        await interaction.response.send_message(
            "**Sub Packages 01–05 saved.** Click to continue:", view=view, ephemeral=True
        )


class InstallDefenseModal3(discord.ui.Modal, title="Sub Packages (2 of 2)"):
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
            self.s06.value.strip(), self.s07.value.strip(), self.s08.value.strip(),
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

        abbr = self.abbr

        async def on_pressures(interaction: discord.Interaction, pressures: list[str]):
            view = DefenseInstallConfirmView(abbr, all_formations, all_subs, coverages_holder[0], pressures)
            await interaction.response.edit_message(
                content="**Defensive Install — confirm?**",
                view=view,
            )

        coverages_holder: list[list[str]] = [[]]

        async def on_coverages(interaction: discord.Interaction, coverages: list[str]):
            coverages_holder[0] = coverages
            pressure_view = DefenseMultiSelectView(
                PRESSURE_TYPES, PRESSURE_TYPES_MAX_SELECT,
                f"Select pressure packages (up to {PRESSURE_TYPES_MAX_SELECT})",
                on_pressures,
            )
            await interaction.response.edit_message(
                content="**Defensive Install (4 of 4)** — Select your pressure packages:",
                view=pressure_view,
            )

        coverage_view = DefenseMultiSelectView(
            BASE_COVERAGES, BASE_COVERAGES_MAX_SELECT,
            f"Select base coverages (up to {BASE_COVERAGES_MAX_SELECT})",
            on_coverages,
        )
        await interaction.response.send_message(
            "**Defensive Install (3 of 4)** — Select your base coverages:",
            view=coverage_view,
            ephemeral=True,
        )


# ---- Cog ----

class InstallDefense(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="install_defense",
        description="Submit your team's defensive formations, packages, coverages, and pressures",
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
