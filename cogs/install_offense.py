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

CORE_RUN_CONCEPTS = [(c, c) for c in [
    "Inside Zone", "Outside Zone/Wide Zone", "Split Zone", "Zone Read",
    "Duo", "Iso (Lead)", "Power", "Counter", "Trap",
    "Sweep", "Toss",
    "Power Read", "Speed Option", "Triple Option",
]]
CORE_RUN_MAX_SELECT = 6

PASS_QUICK_GAME = [
    ("Spacing - Three shallow routes to stretch zone horizontally", "Spacing"),
    ("Slant - Quick angled route to the middle", "Slant"),
    ("Stick - Sit route plus flat for a horizontal stretch", "Stick"),
    ("Hitch - Push vertical, snap back for a rhythm throw", "Hitch"),
    ("Snag - Corner, snag, flat triangle read", "Snag"),
    ("Ohio - Go route outside, out route inside", "Ohio"),
    ("Omaha - Quick out, throw on the break", "Omaha"),
]
PASS_QUICK_MIN, PASS_QUICK_MAX = 1, 3

PASS_INTERMEDIATE = [
    ("Choice - Receiver reads leverage and breaks in, out, or sits", "Choice"),
    ("Curls - Push vertical then turn back for a comeback catch", "Curls"),
    ("Drive - Shallow drag underneath a deeper crosser", "Drive"),
    ("Follow - Two receivers stack the same route, one trailing", "Follow"),
    ("Levels - Stacked in-breaking routes at different depths", "Levels"),
    ("Mesh - Two receivers cross shallow to rub off man coverage", "Mesh"),
    ("Salem/Pivot - Shallow option paired with a deeper in route", "Salem/Pivot"),
    ("Shallow Cross - Flat shallow crosser underneath the defense", "Shallow Cross"),
    ("Smash - Corner and hitch high-low read vs Cover 2", "Smash"),
    ("Spot - Corner, spot, flat triangle read vs zone", "Spot"),
    ("Texas - Deep post plus RB angle route for a high-low read", "Texas"),
]
PASS_INTERMEDIATE_MIN, PASS_INTERMEDIATE_MAX = 4, 6

PASS_DEEP = [
    ("Dagger - Seam clear-out with a deep dig settling behind it", "Dagger"),
    ("Divide - Two deep routes split the field to stress one safety", "Divide"),
    ("Double Post - Two posts at different depths flood one half", "Double Post"),
    ("Flood - Three routes stacked at different depths on one side", "Flood"),
    ("Double Moves - Quick route faked, then broken deep", "Double Moves"),
    ("Switch - Receivers cross release to confuse coverage", "Switch"),
    ("Verticals - Four receivers push vertical to stretch the zone", "Verticals"),
    ("Deep Cross - Long crossing route over the middle", "Deep Cross"),
]
PASS_DEEP_MIN, PASS_DEEP_MAX = 2, 4


# ---- Storage ----

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


# ---- Multi-select view (same pattern as scheme_cards) ----

class MultiSelectStepView(discord.ui.View):
    def __init__(self, options: list[tuple[str, str]], max_select: int, label: str,
                 on_confirm, min_select: int = 1):
        super().__init__(timeout=120)
        self.values_order = [value for _, value in options]
        self.on_confirm = on_confirm
        self.min_select = min_select
        self.selected: list[str] = []

        self.select = discord.ui.Select(
            placeholder=f"Select {min_select}-{max_select} {label.lower()}...",
            min_values=min_select,
            max_values=min(max_select, len(options)),
            options=[
                discord.SelectOption(label=lbl[:100], value=val[:100])
                for lbl, val in options
            ],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        confirm_btn = discord.ui.Button(label="Confirm Selection", style=discord.ButtonStyle.success)
        confirm_btn.callback = self._on_confirm_click
        self.add_item(confirm_btn)

    async def _on_select(self, interaction: discord.Interaction):
        self.selected = self.select.values
        self.select.placeholder = f"{len(self.selected)} selected"
        await interaction.response.edit_message(view=self)

    async def _on_confirm_click(self, interaction: discord.Interaction):
        if len(self.selected) < self.min_select:
            await interaction.response.send_message(
                f"Select at least {self.min_select} option(s) first.", ephemeral=True
            )
            return
        ordered = sorted(self.selected, key=self.values_order.index)
        for child in self.children:
            child.disabled = True
        await self.on_confirm(interaction, ordered)


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
        # Disable the button via webhook edit (separate from interaction response)
        # so it can't be clicked again while the modal is open.
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        # Use the interaction response to open the next modal.
        await self._on_click(interaction)


# ---- Modals ----

class InstallOffenseModal1(discord.ui.Modal, title="Base Formations (1 of 3)"):
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
            self.f01.value.strip(), self.f02.value.strip(), self.f03.value.strip(),
            self.f04.value.strip(), self.f05.value.strip(),
        ]
        abbr = self.abbr

        async def on_continue(interaction: discord.Interaction):
            await interaction.response.send_modal(InstallOffenseModal2(abbr, batch))

        view = _ContinueView("Continue → Formations 06–10", on_continue)
        await interaction.response.send_message(
            "**Formations 01–05 saved.** Click to continue:", view=view, ephemeral=True
        )


class InstallOffenseModal2(discord.ui.Modal, title="Base Formations (2 of 3)"):
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
            self.f06.value.strip(), self.f07.value.strip(), self.f08.value.strip(),
            self.f09.value.strip(), self.f10.value.strip(),
        ]
        all_so_far = self.prev + batch
        abbr = self.abbr

        async def on_continue(interaction: discord.Interaction):
            await interaction.response.send_modal(InstallOffenseModal3(abbr, all_so_far))

        view = _ContinueView("Continue → Formations 11–14 (optional)", on_continue)
        await interaction.response.send_message(
            "**Formations 06–10 saved.** Click to continue:", view=view, ephemeral=True
        )


class InstallOffenseModal3(discord.ui.Modal, title="Base Formations (3 of 3)"):
    f11 = discord.ui.TextInput(label="11 (optional)", placeholder="Optional", required=False, max_length=60)
    f12 = discord.ui.TextInput(label="12 (optional)", placeholder="Optional", required=False, max_length=60)
    f13 = discord.ui.TextInput(label="13 (optional)", placeholder="Optional", required=False, max_length=60)
    f14 = discord.ui.TextInput(label="14 (optional)", placeholder="Optional", required=False, max_length=60)
    f15 = discord.ui.TextInput(label="15 (optional)", placeholder="Optional", required=False, max_length=60)

    def __init__(self, abbr: str, prev: list[str]):
        super().__init__()
        self.abbr = abbr
        self.prev = prev

    async def on_submit(self, interaction: discord.Interaction):
        final_batch = [
            self.f11.value.strip(), self.f12.value.strip(), self.f13.value.strip(),
            self.f14.value.strip(), self.f15.value.strip(),
        ]
        all_formations = [f for f in (self.prev + final_batch) if f]

        if len(all_formations) < 8:
            await interaction.response.send_message(
                f"At least 8 formations are required. Got {len(all_formations)}. "
                f"Run `/install_offense` again to resubmit.",
                ephemeral=True,
            )
            return

        abbr = self.abbr
        data: dict = {"formations": all_formations}

        async def after_deep(interaction: discord.Interaction, selected: list[str]):
            data["deep_pass"] = selected
            await _save_and_show(interaction, abbr, data)

        async def after_intermediate(interaction: discord.Interaction, selected: list[str]):
            data["intermediate_pass"] = selected
            view = MultiSelectStepView(
                PASS_DEEP, PASS_DEEP_MAX, "deep pass concepts", after_deep,
                min_select=PASS_DEEP_MIN,
            )
            await interaction.response.edit_message(
                content=f"**Offensive Install — Deep Pass concepts** "
                        f"(pick {PASS_DEEP_MIN}–{PASS_DEEP_MAX}, then confirm):",
                view=view,
            )

        async def after_quick(interaction: discord.Interaction, selected: list[str]):
            data["quick_pass"] = selected
            view = MultiSelectStepView(
                PASS_INTERMEDIATE, PASS_INTERMEDIATE_MAX, "intermediate pass concepts",
                after_intermediate, min_select=PASS_INTERMEDIATE_MIN,
            )
            await interaction.response.edit_message(
                content=f"**Offensive Install — Intermediate Pass concepts** "
                        f"(pick {PASS_INTERMEDIATE_MIN}–{PASS_INTERMEDIATE_MAX}, then confirm):",
                view=view,
            )

        async def after_run(interaction: discord.Interaction, selected: list[str]):
            data["run_concepts"] = selected
            view = MultiSelectStepView(
                PASS_QUICK_GAME, PASS_QUICK_MAX, "quick game pass concepts",
                after_quick, min_select=PASS_QUICK_MIN,
            )
            await interaction.response.edit_message(
                content=f"**Offensive Install — Quick Pass concepts** "
                        f"(pick {PASS_QUICK_MIN}–{PASS_QUICK_MAX}, then confirm):",
                view=view,
            )

        view = MultiSelectStepView(
            CORE_RUN_CONCEPTS, CORE_RUN_MAX_SELECT, "core run concepts", after_run,
        )
        await interaction.response.send_message(
            f"**Offensive Install — Run Concepts** (up to {CORE_RUN_MAX_SELECT}, then confirm):",
            view=view,
            ephemeral=True,
        )


async def _save_and_show(interaction: discord.Interaction, abbr: str, data: dict):
    submitted = datetime.now(timezone.utc).strftime("%B %d, %Y")
    data["last_updated"] = submitted

    installs = load_offense_installs()
    installs[abbr] = data
    save_offense_installs(installs)

    teams = load_teams()
    team = teams.get(abbr, {})
    team_color = int(team.get("color", "C9A227"), 16)
    team_name = team.get("name", abbr)

    formations = data.get("formations", [])
    left_val  = "\n".join(f"`{i+1:02d}` {f}" for i, f in enumerate(formations[:8]))
    right_val = "\n".join(f"`{i+9:02d}` {f}" for i, f in enumerate(formations[8:])) or "\u200b"

    def fmt(items: list[str]) -> str:
        return "\n".join(f"`{i+1:02d}` {v}" for i, v in enumerate(items))

    embed = discord.Embed(title=f"{team_name} — Offensive Install", color=team_color)
    logo = team.get("logoDark") or team.get("logo")
    if logo:
        embed.set_thumbnail(url=logo)
    embed.add_field(name="Base Formations", value=left_val, inline=True)
    embed.add_field(name="\u200b", value=right_val, inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=False)
    embed.add_field(name="Run Concepts", value=fmt(data.get("run_concepts", [])), inline=True)
    embed.add_field(name="Quick Pass", value=fmt(data.get("quick_pass", [])), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=False)
    embed.add_field(name="Intermediate Pass", value=fmt(data.get("intermediate_pass", [])), inline=True)
    embed.add_field(name="Deep Pass", value=fmt(data.get("deep_pass", [])), inline=True)
    embed.set_footer(text=f"Last updated: {submitted}")

    await interaction.response.edit_message(content=None, embed=embed, view=None)


# ---- Cog ----

class InstallOffense(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="install_offense",
        description="Submit your team's offensive formations and play concepts (8–14 formations required)",
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
