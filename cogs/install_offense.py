import json
import os
import traceback
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils.data import load_roster, load_teams
from utils.responses import send_ephemeral

DATA_DIR = os.environ.get("DATA_DIR", "data").strip()

# ---- Constants (also imported by scheme_cards) ----

CORE_RUN_CONCEPTS = [(c, c) for c in [
    "Inside Zone/Split Zone", "Outside Zone/Wide Zone", "Zone Read",
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
    ("Shallow Cross - Flat shallow crosser paired with a dig route breaking the opposite direction", "Shallow Cross"),
    ("Smash - Corner and hitch high-low read vs Cover 2", "Smash"),
    ("Spot - Corner, spot, flat triangle read vs zone", "Spot"),
    ("Texas - Deep post plus RB angle route for a high-low read", "Texas"),
    ("Y Cross - Deep crossing route from the tight end/Y receiver", "Y Cross"),
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
                 on_confirm, min_select: int = 1, on_back=None, preselected: list[str] | None = None):
        super().__init__(timeout=600)
        self.values_order = [value for _, value in options]
        self.on_confirm = on_confirm
        self.min_select = min_select
        self.selected: list[str] = list(preselected) if preselected else []
        self.message: discord.Message | None = None

        preselected_set = set(preselected or [])
        self.select = discord.ui.Select(
            placeholder=f"Select {min_select}-{max_select} {label.lower()}...",
            min_values=min_select,
            max_values=min(max_select, len(options)),
            options=[
                discord.SelectOption(label=lbl[:100], value=val[:100], default=val in preselected_set)
                for lbl, val in options
            ],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        if self.selected:
            ordered = sorted(self.selected, key=self.values_order.index)
            preview = ", ".join(ordered)
            self.select.placeholder = preview[:92] + "..." if len(preview) > 95 else preview

        if on_back is not None:
            back_btn = discord.ui.Button(label="← Back", style=discord.ButtonStyle.secondary)
            back_btn.callback = on_back
            self.add_item(back_btn)

        confirm_btn = discord.ui.Button(label="Confirm Selection", style=discord.ButtonStyle.success)
        confirm_btn.callback = self._on_confirm_click
        self.add_item(confirm_btn)

    async def _on_select(self, interaction: discord.Interaction):
        self.selected = self.select.values
        ordered = sorted(self.selected, key=self.values_order.index)
        preview = ", ".join(ordered)
        if len(preview) > 95:
            preview = preview[:92] + "..."
        self.select.placeholder = preview
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

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    content="⏱️ This install session timed out from inactivity. Run `/install_offense` again to pick back up.",
                    view=self,
                )
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        print(f"[install_offense] MultiSelectStepView error on {item!r}: {error!r}")
        traceback.print_exc()
        message = "Something went wrong with that step. Please run `/install_offense` again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


# ---- Continue button (bridges modal → modal since Discord forbids modal → modal) ----

class _ContinueView(discord.ui.View):
    """Ephemeral 'Continue' button used between chained modals."""

    def __init__(self, label: str, on_click):
        super().__init__(timeout=300)
        self._on_click = on_click
        self.message: discord.Message | None = None
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

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    content="⏱️ This install session timed out from inactivity. Run `/install_offense` again to pick back up.",
                    view=self,
                )
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        print(f"[install_offense] _ContinueView error on {item!r}: {error!r}")
        traceback.print_exc()
        message = "Something went wrong continuing that step. Please run `/install_offense` again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


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
        view.message = await interaction.original_response()


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
        view.message = await interaction.original_response()


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
        wizard = OffenseConceptsWizard(abbr, all_formations)
        await wizard.start(interaction)


CONCEPT_STEPS = [
    ("run_concepts", CORE_RUN_CONCEPTS, CORE_RUN_MAX_SELECT, 1, "run concepts"),
    ("quick_pass", PASS_QUICK_GAME, PASS_QUICK_MAX, PASS_QUICK_MIN, "quick pass concepts"),
    ("intermediate_pass", PASS_INTERMEDIATE, PASS_INTERMEDIATE_MAX, PASS_INTERMEDIATE_MIN, "intermediate pass concepts"),
    ("deep_pass", PASS_DEEP, PASS_DEEP_MAX, PASS_DEEP_MIN, "deep pass concepts"),
]


class OffenseConceptsWizard:
    """Run Concepts → Quick Pass → Intermediate Pass → Deep Pass, with Back
    support at every step after the first. Going back re-shows that step
    with its previous selection pre-checked."""

    def __init__(self, abbr: str, formations: list[str]):
        self.abbr = abbr
        self.formations = formations
        self.data: dict = {}
        self.index = 0

    async def start(self, interaction: discord.Interaction):
        await self._show_step(interaction, first=True)

    async def _show_step(self, interaction: discord.Interaction, first: bool = False):
        key, options, max_select, min_select, label = CONCEPT_STEPS[self.index]
        view = MultiSelectStepView(
            options, max_select, label, self._on_forward, min_select=min_select,
            on_back=self._on_back if self.index > 0 else None,
            preselected=self.data.get(key),
        )
        step_num = self.index + 1
        content = (
            f"**Offensive Install — {label.title()}** "
            f"(step {step_num}/{len(CONCEPT_STEPS)}, pick {min_select}-{max_select}, then confirm):"
        )
        if first:
            await interaction.response.send_message(content, view=view, ephemeral=True)
            view.message = await interaction.original_response()
        else:
            await interaction.response.edit_message(content=content, view=view)
            view.message = interaction.message

    async def _on_forward(self, interaction: discord.Interaction, selected: list[str]):
        key = CONCEPT_STEPS[self.index][0]
        self.data[key] = selected
        self.index += 1
        if self.index >= len(CONCEPT_STEPS):
            full_data = {"formations": self.formations, **self.data}
            await _save_and_show(interaction, self.abbr, full_data)
        else:
            await self._show_step(interaction)

    async def _on_back(self, interaction: discord.Interaction):
        self.index -= 1
        await self._show_step(interaction)


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
