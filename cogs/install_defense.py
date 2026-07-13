import json
import os
import traceback
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils.data import load_roster, load_teams
from utils.responses import send_ephemeral
# NOTE: refresh_dashboard is imported lazily (inside the confirm handler,
# not here) to match install_offense.py / scheme_cards.py and avoid any
# risk of a circular import with cogs.scheduling.

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
#
# A single drafts file tracks the WHOLE flow now — the 3-modal chain
# (formations → sub packages) as well as the coverage/pressure select
# steps and the final review — not just the select steps. Every step
# writes its progress here before showing the next control, and every
# control that can be clicked later is a persistent, custom_id-based view
# read fresh from this file. A bot restart at ANY point in the flow leaves
# a resumable, clickable control behind instead of a dead one — see
# register_active_views() below.

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


def load_defense_install_drafts() -> dict:
    path = os.path.join(DATA_DIR, "defense_install_drafts.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save_defense_install_drafts(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "defense_install_drafts.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


async def _guard_owner(interaction: discord.Interaction, abbr: str) -> bool:
    roster = load_roster()
    owner_id = (roster.get(abbr) or {}).get("user_id")
    if owner_id != interaction.user.id:
        await interaction.response.send_message(
            "Only the team owner who started this install can use it. Run `/install_defense` yourself to start your own.",
            ephemeral=True,
        )
        return False
    return True


async def _on_view_error(interaction: discord.Interaction, source: str, error: Exception):
    print(f"[install_defense] {source} error: {error!r}")
    traceback.print_exc()
    message = "Something went wrong with that step. Please run `/install_defense` again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


# ---- Persistent "Continue" bridge between chained modals ----
#
# Discord forbids opening a modal directly from a modal's on_submit, so a
# button bridges each transition. This button is registered with
# timeout=None and a stable custom_id, and reads the accumulated
# formations/sub packages from disk rather than a Python closure — so it
# keeps working even if the bot restarts while it's the only thing on
# screen.

CONTINUE_LABELS = {
    2: "Continue → Sub Packages 01–05",
    3: "Continue → Sub Packages 06–08",
}


class PersistentContinueView(discord.ui.View):
    def __init__(self, abbr: str, target_modal: int):
        super().__init__(timeout=None)
        self.abbr = abbr
        self.target_modal = target_modal
        btn = discord.ui.Button(
            label=CONTINUE_LABELS[target_modal],
            style=discord.ButtonStyle.primary,
            custom_id=f"di_continue:{abbr}:{target_modal}",
        )
        btn.callback = self._on_click
        self.add_item(btn)

    async def _on_click(self, interaction: discord.Interaction):
        if not await _guard_owner(interaction, self.abbr):
            return
        drafts = load_defense_install_drafts()
        draft = drafts.get(self.abbr)
        if draft is None:
            await interaction.response.send_message(
                "This install session has expired. Run `/install_defense` again.", ephemeral=True
            )
            return
        formations = draft.get("formations", [])
        if self.target_modal == 2:
            modal = InstallDefenseModal2(self.abbr, formations)
        else:
            sub_packages = draft.get("sub_packages", [])
            modal = InstallDefenseModal3(self.abbr, formations, sub_packages)
        await interaction.response.send_modal(modal)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        await _on_view_error(interaction, "PersistentContinueView", error)


# ---- Persistent multi-select step view ----

DEFENSE_STEPS = [
    (
        "coverages", BASE_COVERAGES, BASE_COVERAGES_MAX_SELECT, 1,
        f"Select base coverages (up to {BASE_COVERAGES_MAX_SELECT})",
        "**Defensive Install (3 of 4)** — Select your base coverages:",
    ),
    (
        "pressures", PRESSURE_TYPES, PRESSURE_TYPES_MAX_SELECT, 1,
        f"Select pressure packages (up to {PRESSURE_TYPES_MAX_SELECT})",
        "**Defensive Install (4 of 4)** — Select your pressure packages:",
    ),
]


class PersistentDefenseStepView(discord.ui.View):
    def __init__(self, abbr: str, step_index: int):
        super().__init__(timeout=None)
        self.abbr = abbr
        self.step_index = step_index
        key, options, max_select, min_select, placeholder, _header = DEFENSE_STEPS[step_index]
        self.key = key
        self.min_select = min_select
        self.values_order = [value for _, value in options]

        drafts = load_defense_install_drafts()
        draft = drafts.get(abbr) or {}
        preselected = draft.get("data", {}).get(key) or []
        preselected_set = set(preselected)

        self.select = discord.ui.Select(
            custom_id=f"di_select:{abbr}:{step_index}",
            placeholder=placeholder,
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
                custom_id=f"di_back:{abbr}:{step_index}",
            )
            back_btn.callback = self._on_back
            self.add_item(back_btn)

        confirm_btn = discord.ui.Button(
            label="Confirm →", style=discord.ButtonStyle.primary,
            custom_id=f"di_confirm:{abbr}:{step_index}",
        )
        confirm_btn.callback = self._on_confirm_click
        self.add_item(confirm_btn)

    @staticmethod
    def content_for(step_index: int) -> str:
        return DEFENSE_STEPS[step_index][5]

    async def _on_select(self, interaction: discord.Interaction):
        if not await _guard_owner(interaction, self.abbr):
            return
        selected = self.select.values
        drafts = load_defense_install_drafts()
        draft = drafts.setdefault(
            self.abbr,
            {"formations": [], "sub_packages": [], "data": {}, "step_index": self.step_index},
        )
        draft["data"][self.key] = selected
        save_defense_install_drafts(drafts)

        ordered = sorted(selected, key=self.values_order.index)
        preview = ", ".join(ordered)
        if len(preview) > 95:
            preview = preview[:92] + "..."
        self.select.placeholder = preview
        await interaction.response.edit_message(view=self)

    async def _on_confirm_click(self, interaction: discord.Interaction):
        if not await _guard_owner(interaction, self.abbr):
            return
        drafts = load_defense_install_drafts()
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
        save_defense_install_drafts(drafts)

        if next_index >= len(DEFENSE_STEPS):
            await _show_final_review(interaction, self.abbr, draft)
        else:
            view = PersistentDefenseStepView(self.abbr, next_index)
            await interaction.response.edit_message(content=view.content_for(next_index), view=view)

    async def _on_back(self, interaction: discord.Interaction):
        if not await _guard_owner(interaction, self.abbr):
            return
        prev_index = self.step_index - 1
        drafts = load_defense_install_drafts()
        draft = drafts.get(self.abbr)
        if draft is not None:
            draft["step_index"] = prev_index
            drafts[self.abbr] = draft
            save_defense_install_drafts(drafts)
        view = PersistentDefenseStepView(self.abbr, prev_index)
        await interaction.response.edit_message(content=view.content_for(prev_index), view=view)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        await _on_view_error(interaction, "PersistentDefenseStepView", error)


async def _show_final_review(interaction: discord.Interaction, abbr: str, draft: dict):
    data = draft.get("data", {})
    coverage_preview = "\n".join(f"• {c}" for c in data.get("coverages", []))
    pressure_preview = "\n".join(f"• {p}" for p in data.get("pressures", []))
    view = PersistentDefenseConfirmView(abbr)
    await interaction.response.edit_message(
        content=(
            "**Defensive Install — review & save:**\n"
            f"**Base Coverages:**\n{coverage_preview}\n\n"
            f"**Pressure Packages:**\n{pressure_preview}"
        ),
        view=view,
    )


# ---- Persistent final confirm view ----

class PersistentDefenseConfirmView(discord.ui.View):
    def __init__(self, abbr: str):
        super().__init__(timeout=None)
        self.abbr = abbr

        confirm_btn = discord.ui.Button(
            label="✅ Save Install", style=discord.ButtonStyle.success,
            custom_id=f"di_final_confirm:{abbr}",
        )
        confirm_btn.callback = self._on_confirm
        self.add_item(confirm_btn)

        cancel_btn = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.secondary,
            custom_id=f"di_final_cancel:{abbr}",
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _on_confirm(self, interaction: discord.Interaction):
        if not await _guard_owner(interaction, self.abbr):
            return

        drafts = load_defense_install_drafts()
        draft = drafts.get(self.abbr)
        if draft is None:
            await interaction.response.send_message(
                "This install session has expired. Run `/install_defense` again.", ephemeral=True
            )
            return

        # Defer immediately: the disk write + dashboard refresh below could
        # be slow enough on a cold volume mount to blow the 3s ack window,
        # which is exactly what produces a silent "Interaction failed".
        await interaction.response.defer()

        formations = draft.get("formations", [])
        sub_packages = draft.get("sub_packages", [])
        data = draft.get("data", {})
        coverages = data.get("coverages", [])
        pressures = data.get("pressures", [])

        submitted = datetime.now(timezone.utc).strftime("%B %d, %Y")
        installs = load_defense_installs()
        installs[self.abbr] = {
            "formations": formations,
            "sub_packages": sub_packages,
            "coverages": coverages,
            "pressures": pressures,
            "last_updated": submitted,
        }
        save_defense_installs(installs)

        drafts.pop(self.abbr, None)
        save_defense_install_drafts(drafts)

        teams = load_teams()
        team = teams.get(self.abbr, {})
        team_color = int(team.get("color", "BA0C2F"), 16)
        team_name = team.get("name", self.abbr)

        formation_val = "\n".join(f"`{i+1:02d}` {f}" for i, f in enumerate(formations)) or "\u200b"
        subs_val = "\n".join(f"`{i+1:02d}` {s}" for i, s in enumerate(sub_packages)) or "\u200b"
        coverage_val = "\n".join(f"`{i+1:02d}` {c}" for i, c in enumerate(coverages)) or "\u200b"
        pressure_val = "\n".join(f"`{i+1:02d}` {p}" for i, p in enumerate(pressures)) or "\u200b"

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

        await interaction.edit_original_response(content=None, embed=embed, view=None)
        from cogs.scheduling import refresh_dashboard
        await refresh_dashboard(interaction.client)

    async def _on_cancel(self, interaction: discord.Interaction):
        if not await _guard_owner(interaction, self.abbr):
            return
        drafts = load_defense_install_drafts()
        drafts.pop(self.abbr, None)
        save_defense_install_drafts(drafts)
        await interaction.response.edit_message(content="Cancelled. Nothing was saved.", view=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        await _on_view_error(interaction, "PersistentDefenseConfirmView", error)


# ---- Modals ----
#
# Each modal writes its batch straight into the shared draft on disk
# before showing the next control, instead of passing state along only in
# memory. The draft on disk is always the single source of truth for what
# has been collected so far.

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
        drafts = load_defense_install_drafts()
        drafts[self.abbr] = {"formations": formations, "sub_packages": [], "data": {}}
        save_defense_install_drafts(drafts)

        view = PersistentContinueView(self.abbr, target_modal=2)
        await interaction.response.send_message(
            "**Base Formations saved.** Click to continue:", view=view, ephemeral=True
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await _on_view_error(interaction, "InstallDefenseModal1", error)


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
        drafts = load_defense_install_drafts()
        draft = drafts.get(self.abbr) or {"formations": self.formations, "data": {}}
        draft["formations"] = self.formations
        draft["sub_packages"] = subs
        drafts[self.abbr] = draft
        save_defense_install_drafts(drafts)

        view = PersistentContinueView(self.abbr, target_modal=3)
        await interaction.response.send_message(
            "**Sub Packages 01–05 saved.** Click to continue:", view=view, ephemeral=True
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await _on_view_error(interaction, "InstallDefenseModal2", error)


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

        await start_defense_steps(interaction, self.abbr, all_formations, all_subs)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await _on_view_error(interaction, "InstallDefenseModal3", error)


async def start_defense_steps(interaction: discord.Interaction, abbr: str, formations: list[str], sub_packages: list[str]):
    """Creates the on-disk draft (overwriting any stale one for this abbr)
    and shows step 0. From here on, all state lives on disk keyed by abbr,
    not in a Python object — see PersistentDefenseStepView."""
    drafts = load_defense_install_drafts()
    drafts[abbr] = {"formations": formations, "sub_packages": sub_packages, "data": {}, "step_index": 0}
    save_defense_install_drafts(drafts)

    view = PersistentDefenseStepView(abbr, 0)
    await interaction.response.send_message(view.content_for(0), view=view, ephemeral=True)


# ---- Cog ----

class InstallDefense(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def register_active_views(self):
        """Re-registers a persistent view for every in-progress install
        draft so its buttons keep working across a bot restart — whether
        the draft was left mid-modal-chain, mid-select, or at final
        review. Must be called once after the bot logs in, same as
        install_offense.py's register_active_views()."""
        drafts = load_defense_install_drafts()
        for abbr, draft in drafts.items():
            if "step_index" in draft:
                step_index = draft.get("step_index", 0)
                if isinstance(step_index, int) and 0 <= step_index < len(DEFENSE_STEPS):
                    self.bot.add_view(PersistentDefenseStepView(abbr, step_index))
                elif step_index == len(DEFENSE_STEPS):
                    self.bot.add_view(PersistentDefenseConfirmView(abbr))
            else:
                # Mid-modal-chain: whether sub packages have been saved yet
                # tells us which modal comes next.
                sub_packages = draft.get("sub_packages", [])
                target_modal = 3 if len(sub_packages) > 0 else 2
                self.bot.add_view(PersistentContinueView(abbr, target_modal))

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
