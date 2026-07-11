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


def load_offense_install_drafts() -> dict:
    """In-progress /install_offense sessions, keyed by team abbr. Persisted
    to disk (not just held in memory) so a bot restart mid-wizard doesn't
    strand the user with dead buttons — see PersistentConceptStepView."""
    path = os.path.join(DATA_DIR, "offense_install_drafts.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save_offense_install_drafts(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "offense_install_drafts.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---- Persistent multi-select step view ----
#
# Unlike a plain discord.ui.View (which only lives in memory for the
# process that created it), this view is registered with timeout=None and
# stable custom_ids, and its state is read fresh from disk on every
# interaction rather than from Python closures. That means it keeps working
# even if the bot restarts between when a step is shown and when the user
# clicks it — the exact scenario that caused silent "Interaction failed"
# clicks with nothing in the logs.

class PersistentConceptStepView(discord.ui.View):
    def __init__(self, abbr: str, step_index: int):
        super().__init__(timeout=None)
        self.abbr = abbr
        self.step_index = step_index
        key, options, max_select, min_select, label = CONCEPT_STEPS[step_index]
        self.key = key
        self.min_select = min_select
        self.values_order = [value for _, value in options]

        drafts = load_offense_install_drafts()
        draft = drafts.get(abbr) or {}
        preselected = draft.get("data", {}).get(key) or []
        preselected_set = set(preselected)

        self.select = discord.ui.Select(
            custom_id=f"oi_select:{abbr}:{step_index}",
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

        if preselected:
            ordered = sorted(preselected, key=self.values_order.index)
            preview = ", ".join(ordered)
            self.select.placeholder = preview[:92] + "..." if len(preview) > 95 else preview

        if step_index > 0:
            back_btn = discord.ui.Button(
                label="← Back", style=discord.ButtonStyle.secondary,
                custom_id=f"oi_back:{abbr}:{step_index}",
            )
            back_btn.callback = self._on_back
            self.add_item(back_btn)

        confirm_btn = discord.ui.Button(
            label="Confirm Selection", style=discord.ButtonStyle.success,
            custom_id=f"oi_confirm:{abbr}:{step_index}",
        )
        confirm_btn.callback = self._on_confirm_click
        self.add_item(confirm_btn)

    @staticmethod
    def content_for(step_index: int) -> str:
        _, _, max_select, min_select, label = CONCEPT_STEPS[step_index]
        return (
            f"**Offensive Install — {label.title()}** "
            f"(step {step_index + 1}/{len(CONCEPT_STEPS)}, pick {min_select}-{max_select}, then confirm):"
        )

    async def _guard_owner(self, interaction: discord.Interaction) -> bool:
        roster = load_roster()
        owner_id = (roster.get(self.abbr) or {}).get("user_id")
        if owner_id != interaction.user.id:
            await interaction.response.send_message(
                "Only the team owner who started this install can use it. Run `/install_offense` yourself to start your own.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        if not await self._guard_owner(interaction):
            return
        selected = self.select.values
        drafts = load_offense_install_drafts()
        draft = drafts.setdefault(self.abbr, {"formations": [], "data": {}, "step_index": self.step_index})
        draft["data"][self.key] = selected
        save_offense_install_drafts(drafts)

        ordered = sorted(selected, key=self.values_order.index)
        preview = ", ".join(ordered)
        if len(preview) > 95:
            preview = preview[:92] + "..."
        self.select.placeholder = preview
        await interaction.response.edit_message(view=self)

    async def _on_confirm_click(self, interaction: discord.Interaction):
        if not await self._guard_owner(interaction):
            return
        drafts = load_offense_install_drafts()
        draft = drafts.get(self.abbr) or {}
        selected = draft.get("data", {}).get(self.key) or []
        if len(selected) < self.min_select:
            await interaction.response.send_message(
                f"Select at least {self.min_select} option(s) first.", ephemeral=True
            )
            return

        next_index = self.step_index + 1
        draft["step_index"] = next_index
        drafts[self.abbr] = draft
        save_offense_install_drafts(drafts)

        if next_index >= len(CONCEPT_STEPS):
            data = {"formations": draft.get("formations", []), **draft.get("data", {})}
            drafts.pop(self.abbr, None)
            save_offense_install_drafts(drafts)
            await _save_and_show(interaction, self.abbr, data)
        else:
            view = PersistentConceptStepView(self.abbr, next_index)
            await interaction.response.edit_message(content=view.content_for(next_index), view=view)

    async def _on_back(self, interaction: discord.Interaction):
        if not await self._guard_owner(interaction):
            return
        prev_index = self.step_index - 1
        drafts = load_offense_install_drafts()
        draft = drafts.get(self.abbr)
        if draft is not None:
            draft["step_index"] = prev_index
            drafts[self.abbr] = draft
            save_offense_install_drafts(drafts)
        view = PersistentConceptStepView(self.abbr, prev_index)
        await interaction.response.edit_message(content=view.content_for(prev_index), view=view)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        print(f"[install_offense] PersistentConceptStepView error on {item!r}: {error!r}")
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
        await start_offense_concepts(interaction, abbr, all_formations)


CONCEPT_STEPS = [
    ("run_concepts", CORE_RUN_CONCEPTS, CORE_RUN_MAX_SELECT, 1, "run concepts"),
    ("quick_pass", PASS_QUICK_GAME, PASS_QUICK_MAX, PASS_QUICK_MIN, "quick pass concepts"),
    ("intermediate_pass", PASS_INTERMEDIATE, PASS_INTERMEDIATE_MAX, PASS_INTERMEDIATE_MIN, "intermediate pass concepts"),
    ("deep_pass", PASS_DEEP, PASS_DEEP_MAX, PASS_DEEP_MIN, "deep pass concepts"),
]


async def start_offense_concepts(interaction: discord.Interaction, abbr: str, formations: list[str]):
    """Creates the on-disk draft (overwriting any stale one for this abbr)
    and shows step 0. From here on, all state lives on disk keyed by abbr,
    not in a Python object — see PersistentConceptStepView."""
    drafts = load_offense_install_drafts()
    drafts[abbr] = {"formations": formations, "data": {}, "step_index": 0}
    save_offense_install_drafts(drafts)

    view = PersistentConceptStepView(abbr, 0)
    await interaction.response.send_message(view.content_for(0), view=view, ephemeral=True)


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

    def register_active_views(self):
        """Re-registers a persistent view for every in-progress install
        draft so its buttons keep working across a bot restart. Must be
        called once after the bot logs in, same as the other cogs' views."""
        drafts = load_offense_install_drafts()
        for abbr, draft in drafts.items():
            step_index = draft.get("step_index", 0)
            if 0 <= step_index < len(CONCEPT_STEPS):
                self.bot.add_view(PersistentConceptStepView(abbr, step_index))

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
