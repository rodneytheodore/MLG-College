import json
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils.data import (
    load_teams,
    load_teams_by_conference,
    load_roster,
    load_scheme_cards,
    save_scheme_cards,
    load_settings,
    save_settings,
    is_admin,
    resolve_team,
    true_display_name,
)
from utils.responses import send_ephemeral
# NOTE: refresh_dashboard is imported lazily (inside the functions that use
# it, not here at module level) because cogs.scheduling itself imports from
# this module at import time (build_compact_scheme_card_embed,
# ExpandSchemeCardView). A top-level import here would create a circular
# import and crash the bot on startup.


# ---------- Option lists: (label, value) ----------

OFFENSE_SCHEME_OPTIONS = [(s, s) for s in [
    "Air Raid", "Spread", "Spread Option", "Option",
    "Pro Style", "Power Spread", "Pistol", "Multiple", "Run & Shoot", "Veer & Shoot",
]]
OFFENSE_TEMPO_OPTIONS = [(t, t) for t in ["Hurry Up / No Huddle", "Controlled / Deliberate", "Mixed / Situational"]]
RUN_PASS_OPTIONS = [(r, r) for r in ["Run Heavy (60%+ run)", "Balanced", "Pass Heavy (60%+ pass)"]]
PLAYBOOK_TYPE_OPTIONS = [
    ("Stock", "Stock"),
    ("Custom (<5 Formation Difference)", "Custom"),
    ("Full Custom (>=5 Formation Difference)", "Full Custom"),
]
PERSONNEL_GROUPINGS = [(g, g) for g in [
    "11 — 1 RB 1 TE",
    "12 — 1 RB 2 TE",
    "21 — 2 RB 1 TE",
    "22 — 2 RB 2 TE",
    "10 — 1 RB 0 TE",
    "20 — 2 RB 0 TE",
    "13 — 1 RB 3 TE",
]]
PERSONNEL_MAX_SELECT = 3

from cogs.install_offense import (
    CORE_RUN_CONCEPTS, CORE_RUN_MAX_SELECT,
    PASS_QUICK_GAME, PASS_QUICK_MIN, PASS_QUICK_MAX,
    PASS_INTERMEDIATE, PASS_INTERMEDIATE_MIN, PASS_INTERMEDIATE_MAX,
    PASS_DEEP, PASS_DEEP_MIN, PASS_DEEP_MAX,
    DATA_DIR,
)

DEFENSE_SCHEME_OPTIONS = [(s, s) for s in [
    "4-3", "4-3 Multiple", "3-4", "3-4 Multiple", "Multiple",
    "3-3-5", "3-3-5 Tite", "4-2-5", "3-2-6",
]]
COVERAGE_SHELL_OPTIONS = [(s, s) for s in ["Single High Safety", "Two High Safety", "Hybrid Safety Shell"]]
COVERAGE_TYPE_OPTIONS = [
    ("Man (C2 Man, C1, C0, Man Blitz)", "Man Coverage"),
    ("Zone (C3, Tampa 2, C4 Drop, Zone Blitz)", "Zone Coverage"),
    ("Match (C4, C3 Seam, C6, C9, C2 Sink)", "Match Coverage"),
]


OFFENSE_STEP_NAMES = [
    "Scheme", "Tempo", "Run/Pass Tendency", "Playbook Type", "Personnel Groupings",
]
DEFENSE_STEP_NAMES = ["Scheme", "Coverage Shell", "Coverage Type"]


EMBED_FIELD_LIMIT = 1024


def _trunc_field(value: str, limit: int = EMBED_FIELD_LIMIT) -> str:
    """Defensively keep an embed field value under Discord's 1024-char cap,
    even if older saved data (from before input was capped) is longer."""
    value = value or "Not set"
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "\u2026"


def build_scheme_card_embed(team_info: dict, card: dict) -> discord.Embed:
    embed = discord.Embed(
        title=team_info["name"],
        color=int(team_info["color"], 16) if team_info.get("color") else discord.Color.default(),
    )
    logo = team_info.get("logoDark") or team_info.get("logo")
    if logo:
        embed.set_thumbnail(url=logo)

    offense = card.get("offense")
    if offense and offense.get("personnel"):
        header_line = f"**Coach:** {offense.get('coach', 'Unknown')}"
        if offense.get("film"):
            header_line += f"  \u2022  **Stream Link:** {offense['film']}"
        embed.description = header_line

        lines = [f"**Scheme:** {offense.get('scheme', 'Not set')}  \u2022  **Coaching Tree:** {offense.get('coaching_tree', 'Not set')}"]
        lines.append(f"**Playbook Type:** {offense.get('playbook_type', 'Not set')}  \u2022  **Base Playbook:** {offense.get('base_playbook', 'Not set')}")
        lines.append(f"**Personnel:** {offense.get('personnel', 'Not set')}")
        lines.append(f"**Tendency:** {offense.get('run_pass', 'Not set')}")
        lines.append(f"**Tempo:** {offense.get('tempo', 'Not set')}")
        embed.add_field(name="OFFENSE", value=_trunc_field("\n".join(lines)), inline=False)
        embed.add_field(name="Offense Summary", value=_trunc_field(offense.get("summary")), inline=False)

    defense = card.get("defense")
    if defense:
        lines = [f"**Scheme:** {defense.get('scheme', 'Not set')}  \u2022  **Identity:** {defense.get('coverage_type', 'Not set')}"]
        lines.append(f"**Coaching Tree:** {defense.get('coaching_tree', 'Not set')}  \u2022  **Shell:** {defense.get('coverage_shell', 'Not set')}")
        embed.add_field(name="DEFENSE", value=_trunc_field("\n".join(lines)), inline=False)
        embed.add_field(name="Defense Summary", value=_trunc_field(defense.get("summary")), inline=False)

    if card.get("last_updated"):
        embed.set_footer(text=f"Last updated: {card['last_updated']}")

    return embed


def build_compact_scheme_card_embed(team_info: dict, card: dict) -> discord.Embed:
    """Short summary version shown in the scheme cards channel — full detail
    is only shown when the buttons are clicked."""
    embed = discord.Embed(
        title=team_info["name"],
        color=int(team_info["color"], 16) if team_info.get("color") else discord.Color.default(),
    )
    logo = team_info.get("logoDark") or team_info.get("logo")
    if logo:
        embed.set_thumbnail(url=logo)

    offense = card.get("offense")
    defense = card.get("defense")

    coach_val = card.get("submitted_by", "Unknown")
    offense_val = offense.get("scheme") if offense else None
    defense_val = defense.get("scheme") if defense else None

    label_w = 9   # "Offense: " = 9 chars
    val_w   = 25  # fixed value column width

    lines = [
        f"`{'Coach:':<{label_w}}{coach_val:<{val_w}}`",
        f"`{'Offense:':<{label_w}}{(offense_val or 'Not set'):<{val_w}}`",
        f"`{'Defense:':<{label_w}}{(defense_val or 'Not set'):<{val_w}}`",
    ]

    embed.description = "\n".join(lines)

    if card.get("last_updated"):
        embed.set_footer(text=f"Last updated: {card['last_updated']}")

    return embed


def _build_offense_install_embed(team_info: dict, install: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"{team_info['name']} — Offensive Install",
        color=int(team_info.get("color", "C9A227"), 16),
    )
    logo = team_info.get("logoDark") or team_info.get("logo")
    if logo:
        embed.set_thumbnail(url=logo)

    def fmt(items: list) -> str:
        return "\n".join(f"`{i+1:02d}` {v}" for i, v in enumerate(items)) or "\u200b"

    formations = install.get("formations", [])
    left_val  = "\n".join(f"`{i+1:02d}` {f}" for i, f in enumerate(formations[:8]))
    right_val = "\n".join(f"`{i+9:02d}` {f}" for i, f in enumerate(formations[8:])) or "\u200b"

    embed.add_field(name="Base Formations", value=left_val, inline=True)
    embed.add_field(name="\u200b", value=right_val, inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=False)
    embed.add_field(name="Run Concepts", value=fmt(install.get("run_concepts", [])), inline=True)
    embed.add_field(name="Quick Pass", value=fmt(install.get("quick_pass", [])), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=False)
    embed.add_field(name="Intermediate Pass", value=fmt(install.get("intermediate_pass", [])), inline=True)
    embed.add_field(name="Deep Pass", value=fmt(install.get("deep_pass", [])), inline=True)
    if install.get("last_updated"):
        embed.set_footer(text=f"Last updated: {install['last_updated']}")
    return embed


def _build_defense_install_embed(team_info: dict, install: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"{team_info['name']} — Defensive Install",
        color=int(team_info.get("color", "BA0C2F"), 16),
    )
    logo = team_info.get("logoDark") or team_info.get("logo")
    if logo:
        embed.set_thumbnail(url=logo)

    def fmt(items: list) -> str:
        return "\n".join(f"`{i+1:02d}` {v}" for i, v in enumerate(items)) or "\u200b"

    embed.add_field(name="Base Formations", value=fmt(install.get("formations", [])), inline=True)
    embed.add_field(name="Sub Packages", value=fmt(install.get("sub_packages", [])), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=False)
    embed.add_field(name="Base Coverages", value=fmt(install.get("coverages", [])), inline=True)
    embed.add_field(name="Pressure Packages", value=fmt(install.get("pressures", [])), inline=True)
    if install.get("last_updated"):
        embed.set_footer(text=f"Last updated: {install['last_updated']}")
    return embed


class ExpandSchemeCardView(discord.ui.View):
    """Persistent buttons on each scheme card post in the channel."""

    def __init__(self, abbr: str):
        super().__init__(timeout=None)
        self.abbr = abbr

        btn_scheme = discord.ui.Button(
            label="Full Scheme Card",
            style=discord.ButtonStyle.primary,
            custom_id=f"expand_scheme:{abbr}",
            row=0,
        )
        btn_scheme.callback = self._on_scheme_click
        self.add_item(btn_scheme)

        btn_off = discord.ui.Button(
            label="Offensive Install",
            style=discord.ButtonStyle.secondary,
            custom_id=f"off_install:{abbr}",
            row=0,
        )
        btn_off.callback = self._on_offense_click
        self.add_item(btn_off)

        btn_def = discord.ui.Button(
            label="Defensive Install",
            style=discord.ButtonStyle.secondary,
            custom_id=f"def_install:{abbr}",
            row=0,
        )
        btn_def.callback = self._on_defense_click
        self.add_item(btn_def)

    async def _on_scheme_click(self, interaction: discord.Interaction):
        cards = load_scheme_cards()
        card = cards.get(self.abbr)
        if not card or (not card.get("offense") and not card.get("defense")):
            await interaction.response.send_message("No scheme card set yet for this team.", ephemeral=True)
            return
        teams = load_teams()
        embed = build_scheme_card_embed(teams[self.abbr], card)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _on_offense_click(self, interaction: discord.Interaction):
        from cogs.install_offense import load_offense_installs
        installs = load_offense_installs()
        install = installs.get(self.abbr)
        if not install:
            await interaction.response.send_message("No offensive install submitted yet.", ephemeral=True)
            return
        teams = load_teams()
        embed = _build_offense_install_embed(teams.get(self.abbr, {}), install)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _on_defense_click(self, interaction: discord.Interaction):
        from cogs.install_defense import load_defense_installs
        installs = load_defense_installs()
        install = installs.get(self.abbr)
        if not install:
            await interaction.response.send_message("No defensive install submitted yet.", ephemeral=True)
            return
        teams = load_teams()
        embed = _build_defense_install_embed(teams.get(self.abbr, {}), install)
        await interaction.response.send_message(embed=embed, ephemeral=True)


def build_step_prompt(step_names: list[str], index: int, label: str) -> str:
    total = len(step_names)
    text = f"**(Part 2 of 2) Step {index + 1}/{total} — Pick your {label.lower()}:**"
    return text


# ---------- Persisted wizard drafts ----------
#
# The Set Offense/Defense Scheme wizards used to hold all progress in a
# plain Python object (OffenseWizard/DefenseWizard) tied to a timeout=180
# view. That meant a bot restart -- or just clicking "Confirm Selection"
# after the process had restarted for any other reason -- left a dead
# button with nothing server-side to catch the click, producing Discord's
# generic "didn't respond in time" error. This mirrors the same fix
# already applied to /install_offense and /install_defense: every step
# writes its progress to disk before showing the next control, and every
# control is a persistent (timeout=None), custom_id-based view read fresh
# from disk -- so a restart at any point leaves a resumable, clickable
# control instead of a dead one. See SchemeCards.register_active_views().

def load_scheme_card_drafts() -> dict:
    path = os.path.join(DATA_DIR, "scheme_card_drafts.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save_scheme_card_drafts(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "scheme_card_drafts.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _draft_key(kind: str, abbr: str) -> str:
    # Composite key so a team owner can have an in-progress offense draft
    # and an in-progress defense draft at the same time without colliding.
    return f"{abbr}:{kind}"


def _steps_for(kind: str) -> list:
    return OffenseWizardSteps if kind == "offense" else DefenseWizardSteps


def _step_names_for(kind: str) -> list:
    return OFFENSE_STEP_NAMES if kind == "offense" else DEFENSE_STEP_NAMES


async def _guard_scheme_owner(interaction: discord.Interaction, abbr: str) -> bool:
    roster = load_roster()
    owner_id = (roster.get(abbr) or {}).get("user_id")
    if owner_id != interaction.user.id:
        await interaction.response.send_message(
            "Only the team owner who started this can use it. Run the command yourself to start your own.",
            ephemeral=True,
        )
        return False
    return True


async def _on_scheme_view_error(interaction: discord.Interaction, source: str, error: Exception, resume_cmd: str):
    import traceback
    print(f"[scheme_cards] {source} error: {error!r}")
    traceback.print_exc()
    message = f"Something went wrong with that step. Please run `{resume_cmd}` again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


OffenseWizardSteps = [
    ("scheme", OFFENSE_SCHEME_OPTIONS),
    ("tempo", OFFENSE_TEMPO_OPTIONS),
    ("run_pass", RUN_PASS_OPTIONS),
    ("playbook_type", PLAYBOOK_TYPE_OPTIONS),
]
DefenseWizardSteps = [
    ("scheme", DEFENSE_SCHEME_OPTIONS),
    ("coverage_shell", COVERAGE_SHELL_OPTIONS),
    ("coverage_type", COVERAGE_TYPE_OPTIONS),
]


# ---------- Generic single-select dropdown step (persistent, disk-backed) ----------

class PersistentSchemeChoiceStepView(discord.ui.View):
    """A select menu where picking one option advances the wizard. Persistent
    (timeout=None) and reads/writes its progress from scheme_card_drafts.json
    so it keeps working across a bot restart."""

    def __init__(self, kind: str, abbr: str, step_index: int):
        super().__init__(timeout=None)
        self.kind = kind
        self.abbr = abbr
        self.step_index = step_index
        field_key, choices = _steps_for(kind)[step_index]
        step_name = _step_names_for(kind)[step_index]

        drafts = load_scheme_card_drafts()
        draft = drafts.get(_draft_key(kind, abbr)) or {}
        selected_value = draft.get("data", {}).get(field_key)

        self.select = discord.ui.Select(
            custom_id=f"sc_select:{kind}:{abbr}:{step_index}",
            placeholder=f"Select your {step_name.lower()}...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label[:100], value=value[:100], default=value == selected_value)
                for label, value in choices
            ],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        if step_index > 0:
            back_btn = discord.ui.Button(
                label="← Back", style=discord.ButtonStyle.secondary,
                custom_id=f"sc_back:{kind}:{abbr}:{step_index}",
            )
            back_btn.callback = self._on_back
            self.add_item(back_btn)

    @staticmethod
    def content_for(kind: str, step_index: int) -> str:
        step_names = _step_names_for(kind)
        return build_step_prompt(step_names, step_index, step_names[step_index])

    async def _on_select(self, interaction: discord.Interaction):
        if not await _guard_scheme_owner(interaction, self.abbr):
            return
        value = self.select.values[0]
        key = _draft_key(self.kind, self.abbr)
        drafts = load_scheme_card_drafts()
        draft = drafts.setdefault(key, {"kind": self.kind, "data": {}, "step_index": self.step_index})
        field_key = _steps_for(self.kind)[self.step_index][0]
        draft["data"][field_key] = value
        next_index = self.step_index + 1
        draft["step_index"] = next_index
        drafts[key] = draft
        save_scheme_card_drafts(drafts)

        steps = _steps_for(self.kind)
        if next_index < len(steps):
            view = PersistentSchemeChoiceStepView(self.kind, self.abbr, next_index)
            await interaction.response.edit_message(content=view.content_for(self.kind, next_index), view=view)
        elif self.kind == "offense":
            view = PersistentPersonnelStepView(self.abbr)
            await interaction.response.edit_message(content=view.content_for(), view=view)
        else:
            await _save_defense(interaction, self.abbr)

    async def _on_back(self, interaction: discord.Interaction):
        if not await _guard_scheme_owner(interaction, self.abbr):
            return
        prev_index = self.step_index - 1
        key = _draft_key(self.kind, self.abbr)
        drafts = load_scheme_card_drafts()
        draft = drafts.get(key)
        if draft is not None:
            draft["step_index"] = prev_index
            drafts[key] = draft
            save_scheme_card_drafts(drafts)
        view = PersistentSchemeChoiceStepView(self.kind, self.abbr, prev_index)
        await interaction.response.edit_message(content=view.content_for(self.kind, prev_index), view=view)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        resume_cmd = "/set_offense_scheme" if self.kind == "offense" else "/set_defense_scheme"
        await _on_scheme_view_error(interaction, "PersistentSchemeChoiceStepView", error, resume_cmd)


# ---------- Personnel groupings: multi-select dropdown, max 3 (offense only, final step) ----------

class PersistentPersonnelStepView(discord.ui.View):
    """The offense wizard's final step. Persistent and disk-backed, same as
    PersistentSchemeChoiceStepView above."""

    def __init__(self, abbr: str):
        super().__init__(timeout=None)
        self.abbr = abbr
        self.values_order = [value for _, value in PERSONNEL_GROUPINGS]

        drafts = load_scheme_card_drafts()
        draft = drafts.get(_draft_key("offense", abbr)) or {}
        preselected = draft.get("personnel_selected") or []
        preselected_set = set(preselected)

        self.select = discord.ui.Select(
            custom_id=f"sc_personnel_select:{abbr}",
            placeholder=f"Select 1-{PERSONNEL_MAX_SELECT} personnel groupings...",
            min_values=1,
            max_values=min(PERSONNEL_MAX_SELECT, len(PERSONNEL_GROUPINGS)),
            options=[
                discord.SelectOption(label=lbl[:100], value=val[:100], default=val in preselected_set)
                for lbl, val in PERSONNEL_GROUPINGS
            ],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        if preselected:
            ordered = sorted(preselected, key=self.values_order.index)
            preview = ", ".join(ordered)
            self.select.placeholder = preview[:92] + "..." if len(preview) > 95 else preview

        back_btn = discord.ui.Button(
            label="← Back", style=discord.ButtonStyle.secondary,
            custom_id=f"sc_personnel_back:{abbr}",
        )
        back_btn.callback = self._on_back
        self.add_item(back_btn)

        confirm_btn = discord.ui.Button(
            label="Confirm Selection", style=discord.ButtonStyle.success,
            custom_id=f"sc_personnel_confirm:{abbr}",
        )
        confirm_btn.callback = self._on_confirm_click
        self.add_item(confirm_btn)

    @staticmethod
    def content_for() -> str:
        return (
            f"**(Part 2 of 2) Step 5/9 — Select your primary personnel groupings** "
            f"(up to {PERSONNEL_MAX_SELECT}, then confirm):"
        )

    async def _on_select(self, interaction: discord.Interaction):
        if not await _guard_scheme_owner(interaction, self.abbr):
            return
        selected = self.select.values
        key = _draft_key("offense", self.abbr)
        drafts = load_scheme_card_drafts()
        draft = drafts.setdefault(key, {"kind": "offense", "data": {}, "step_index": len(OffenseWizardSteps)})
        draft["personnel_selected"] = selected
        drafts[key] = draft
        save_scheme_card_drafts(drafts)

        ordered = sorted(selected, key=self.values_order.index)
        preview = ", ".join(ordered)
        self.select.placeholder = preview[:92] + "..." if len(preview) > 95 else preview
        await interaction.response.edit_message(view=self)

    async def _on_confirm_click(self, interaction: discord.Interaction):
        if not await _guard_scheme_owner(interaction, self.abbr):
            return
        drafts = load_scheme_card_drafts()
        draft = drafts.get(_draft_key("offense", self.abbr)) or {}
        selected = draft.get("personnel_selected") or []
        if not selected:
            await interaction.response.send_message("Select at least 1 option first.", ephemeral=True)
            return
        await _save_offense(interaction, self.abbr, draft)

    async def _on_back(self, interaction: discord.Interaction):
        if not await _guard_scheme_owner(interaction, self.abbr):
            return
        prev_index = len(OffenseWizardSteps) - 1
        key = _draft_key("offense", self.abbr)
        drafts = load_scheme_card_drafts()
        draft = drafts.get(key)
        if draft is not None:
            draft["step_index"] = prev_index
            drafts[key] = draft
            save_scheme_card_drafts(drafts)
        view = PersistentSchemeChoiceStepView("offense", self.abbr, prev_index)
        await interaction.response.edit_message(content=view.content_for("offense", prev_index), view=view)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        await _on_scheme_view_error(interaction, "PersistentPersonnelStepView", error, "/set_offense_scheme")


async def _save_offense(interaction: discord.Interaction, abbr: str, draft: dict):
    selected = draft.get("personnel_selected") or []
    data = dict(draft.get("data", {}))
    data["personnel"] = ", ".join(selected)

    cards = load_scheme_cards()
    card = cards.setdefault(abbr, {})
    card["offense"] = data
    card["submitted_by"] = true_display_name(interaction.user)
    card["last_updated"] = datetime.now(timezone.utc).strftime("%B %d, %Y")
    save_scheme_cards(cards)

    drafts = load_scheme_card_drafts()
    drafts.pop(_draft_key("offense", abbr), None)
    save_scheme_card_drafts(drafts)

    teams = load_teams()
    team_name = teams.get(abbr, {}).get("name", abbr)
    await interaction.response.edit_message(
        content=f"✅ Offense scheme saved for **{team_name}**.", view=None,
    )

    cog = interaction.client.get_cog("SchemeCards")
    if cog is not None:
        await cog.refresh_scheme_cards_channel()
    from cogs.scheduling import refresh_dashboard
    await refresh_dashboard(interaction.client)


async def _save_defense(interaction: discord.Interaction, abbr: str):
    key = _draft_key("defense", abbr)
    drafts = load_scheme_card_drafts()
    draft = drafts.get(key) or {}
    data = dict(draft.get("data", {}))

    cards = load_scheme_cards()
    card = cards.setdefault(abbr, {})
    card["defense"] = data
    card["submitted_by"] = true_display_name(interaction.user)
    card["last_updated"] = datetime.now(timezone.utc).strftime("%B %d, %Y")
    save_scheme_cards(cards)

    drafts.pop(key, None)
    save_scheme_card_drafts(drafts)

    teams = load_teams()
    team_name = teams.get(abbr, {}).get("name", abbr)
    await interaction.response.edit_message(
        content=f"✅ Defense scheme saved for **{team_name}**.", view=None,
    )

    cog = interaction.client.get_cog("SchemeCards")
    if cog is not None:
        await cog.refresh_scheme_cards_channel()
    from cogs.scheduling import refresh_dashboard
    await refresh_dashboard(interaction.client)


# ---------- Initial detail modals (popup text fields, shown first) ----------

class OffenseDetailsModal(discord.ui.Modal, title="Offense Details (Part 1 of 2)"):
    coaching_tree = discord.ui.TextInput(label="Coaching Tree (1 or 2 coaches)", required=True, max_length=100)
    base_playbook = discord.ui.TextInput(label="Base Playbook (e.g. Air Raid)", required=True, max_length=60)
    summary = discord.ui.TextInput(label="Summary", style=discord.TextStyle.paragraph, required=True, max_length=1000)
    film = discord.ui.TextInput(label="Stream Link (Twitch, YouTube, etc.)", required=True, max_length=300)

    def __init__(self, cog: "SchemeCards", abbr: str):
        super().__init__()
        self.cog = cog
        self.abbr = abbr

    async def on_submit(self, interaction: discord.Interaction):
        base_data = {
            "coach": true_display_name(interaction.user),
            "coaching_tree": str(self.coaching_tree),
            "base_playbook": str(self.base_playbook),
            "summary": str(self.summary),
            "film": str(self.film),
        }
        key = _draft_key("offense", self.abbr)
        drafts = load_scheme_card_drafts()
        drafts[key] = {"kind": "offense", "data": base_data, "step_index": 0}
        save_scheme_card_drafts(drafts)

        view = PersistentSchemeChoiceStepView("offense", self.abbr, 0)
        await interaction.response.send_message(
            view.content_for("offense", 0), view=view, ephemeral=True
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await _on_scheme_view_error(interaction, "OffenseDetailsModal", error, "/set_offense_scheme")


class DefenseDetailsModal(discord.ui.Modal, title="Defense Details (Part 1 of 2)"):
    coaching_tree = discord.ui.TextInput(label="Coaching Tree (1 or 2 coaches)", required=True, max_length=100)
    summary = discord.ui.TextInput(label="Summary", style=discord.TextStyle.paragraph, required=True, max_length=1000)

    def __init__(self, cog: "SchemeCards", abbr: str):
        super().__init__()
        self.cog = cog
        self.abbr = abbr

    async def on_submit(self, interaction: discord.Interaction):
        base_data = {"coaching_tree": str(self.coaching_tree), "summary": str(self.summary)}
        key = _draft_key("defense", self.abbr)
        drafts = load_scheme_card_drafts()
        drafts[key] = {"kind": "defense", "data": base_data, "step_index": 0}
        save_scheme_card_drafts(drafts)

        view = PersistentSchemeChoiceStepView("defense", self.abbr, 0)
        await interaction.response.send_message(
            view.content_for("defense", 0), view=view, ephemeral=True
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await _on_scheme_view_error(interaction, "DefenseDetailsModal", error, "/set_defense_scheme")



class SchemeCards(commands.Cog):
    """Offense/defense scheme cards per team, set by the team's owner."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.teams = load_teams()

    def register_active_views(self):
        """Re-registers persistent ExpandSchemeCardView buttons for every team
        with a saved card, PLUS a persistent step view for every in-progress
        /set_offense_scheme or /set_defense_scheme wizard. Must be called
        once after the bot logs in, since persistent views don't survive a
        restart on their own."""
        cards = load_scheme_cards()
        for abbr, card in cards.items():
            if not card.get("offense") or not card.get("defense"):
                continue
            if abbr not in self.teams:
                continue
            view = ExpandSchemeCardView(abbr=abbr)
            self.bot.add_view(view)

        drafts = load_scheme_card_drafts()
        for draft_key, draft in drafts.items():
            abbr, _, kind = draft_key.rpartition(":")
            if kind not in ("offense", "defense"):
                continue
            step_index = draft.get("step_index", 0)
            steps = _steps_for(kind)
            if step_index < len(steps):
                self.bot.add_view(PersistentSchemeChoiceStepView(kind, abbr, step_index))
            elif kind == "offense":
                self.bot.add_view(PersistentPersonnelStepView(abbr))

    async def team_autocomplete(self, interaction: discord.Interaction, current: str):
        current_lower = current.lower()
        matches = [
            t for abbr, t in self.teams.items()
            if current_lower in t["name"].lower() or current_lower in abbr.lower()
        ]
        return [app_commands.Choice(name=t["name"], value=t["abbr"]) for t in matches[:25]]

    async def refresh_scheme_cards_channel(self):
        settings = load_settings()
        channel_id = settings.get("scheme_cards_channel_id")
        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return

        await channel.purge(limit=300, check=lambda m: m.author == self.bot.user)

        cards = load_scheme_cards()
        by_conference = load_teams_by_conference()

        for conf_name, conf_teams in by_conference.items():
            ready = []
            for team in conf_teams:
                abbr = team["abbr"].upper()
                card = cards.get(abbr)
                if card and card.get("offense") and card.get("defense"):
                    ready.append(team)
            if not ready:
                continue

            await channel.send(f"**{conf_name}**", allowed_mentions=discord.AllowedMentions.none())

            for team in sorted(ready, key=lambda t: t["name"]):
                abbr = team["abbr"].upper()
                embed = build_compact_scheme_card_embed(self.teams[abbr], cards[abbr])
                view = ExpandSchemeCardView(abbr=abbr)
                await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())

    def resolve_owned_team(self, interaction: discord.Interaction, roster: dict):
        owned = [a for a, info in roster.items() if info.get("user_id") == interaction.user.id]
        if len(owned) == 0:
            return None, "You haven't been assigned a team yet. Contact an admin to get one assigned."
        if len(owned) > 1:
            names = ", ".join(self.teams[a]["name"] for a in owned)
            return None, f"You own multiple teams ({names}) — contact an admin to sort this out."
        return owned[0], None

    # ---------- Commands ----------

    @app_commands.command(name="set_offense_scheme", description="Set your team's offensive scheme card")
    async def set_offense_scheme(self, interaction: discord.Interaction):
        roster = load_roster()
        abbr, error = self.resolve_owned_team(interaction, roster)
        if error:
            await send_ephemeral(interaction, error)
            return

        await interaction.response.send_modal(OffenseDetailsModal(cog=self, abbr=abbr))

    @app_commands.command(name="set_defense_scheme", description="Set your team's defensive scheme card")
    async def set_defense_scheme(self, interaction: discord.Interaction):
        roster = load_roster()
        abbr, error = self.resolve_owned_team(interaction, roster)
        if error:
            await send_ephemeral(interaction, error)
            return

        await interaction.response.send_modal(DefenseDetailsModal(cog=self, abbr=abbr))

    @app_commands.command(name="clear_scheme_card", description="Clear a team's scheme card (admin only)")
    @app_commands.describe(team="Team to clear", side="Which scheme(s) to clear")
    @app_commands.choices(side=[
        app_commands.Choice(name="Both (offense + defense)", value="both"),
        app_commands.Choice(name="Offense only", value="offense"),
        app_commands.Choice(name="Defense only", value="defense"),
    ])
    async def clear_scheme_card(self, interaction: discord.Interaction, team: str, side: app_commands.Choice[str] = None):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can clear scheme cards.")
            return

        abbr, error = resolve_team(team, self.teams)
        if error:
            await send_ephemeral(interaction, error)
            return

        side_value = side.value if side else "both"

        cards = load_scheme_cards()
        card = cards.get(abbr)
        if not card or (not card.get("offense") and not card.get("defense")):
            await send_ephemeral(interaction, f"**{self.teams[abbr]['name']}** doesn't have a scheme card set.")
            return

        if side_value in ("both", "offense"):
            card.pop("offense", None)
        if side_value in ("both", "defense"):
            card.pop("defense", None)

        if not card.get("offense") and not card.get("defense"):
            cards.pop(abbr, None)
        else:
            cards[abbr] = card
        save_scheme_cards(cards)

        cleared = {"both": "offense and defense schemes", "offense": "offense scheme", "defense": "defense scheme"}[side_value]
        await send_ephemeral(interaction, f"Cleared the {cleared} for **{self.teams[abbr]['name']}**.")

        await self.refresh_scheme_cards_channel()
        from cogs.scheduling import refresh_dashboard
        await refresh_dashboard(self.bot)

    @clear_scheme_card.autocomplete("team")
    async def clear_scheme_card_team_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.team_autocomplete(interaction, current)

    @app_commands.command(name="set_team_scheme", description="Directly correct a team's offense or defense Scheme (admin only)")
    @app_commands.describe(team="Team to update", side="Offense or defense", scheme="New scheme value")
    @app_commands.choices(side=[
        app_commands.Choice(name="Offense", value="offense"),
        app_commands.Choice(name="Defense", value="defense"),
    ])
    async def set_team_scheme(self, interaction: discord.Interaction, team: str, side: app_commands.Choice[str], scheme: str):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can update a team's scheme.")
            return

        abbr, error = resolve_team(team, self.teams)
        if error:
            await send_ephemeral(interaction, error)
            return

        options = OFFENSE_SCHEME_OPTIONS if side.value == "offense" else DEFENSE_SCHEME_OPTIONS
        valid_values = {value for _, value in options}
        if scheme not in valid_values:
            await send_ephemeral(
                interaction,
                f"`{scheme}` isn't a valid {side.name.lower()} scheme option. "
                f"Start typing to pick from the list.",
            )
            return

        cards = load_scheme_cards()
        card = cards.get(abbr)
        if not card or not card.get(side.value):
            await send_ephemeral(
                interaction,
                f"**{self.teams[abbr]['name']}** doesn't have a {side.name.lower()} scheme card set yet — "
                f"use `/set_{side.value}_scheme` to create one first.",
            )
            return

        card[side.value]["scheme"] = scheme
        card["last_updated"] = datetime.now(timezone.utc).strftime("%B %d, %Y")
        cards[abbr] = card
        save_scheme_cards(cards)

        await send_ephemeral(
            interaction,
            f"Updated **{self.teams[abbr]['name']}**'s {side.name.lower()} scheme to **{scheme}**.",
        )

        await self.refresh_scheme_cards_channel()
        from cogs.scheduling import refresh_dashboard
        await refresh_dashboard(self.bot)

    @set_team_scheme.autocomplete("team")
    async def set_team_scheme_team_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.team_autocomplete(interaction, current)

    @set_team_scheme.autocomplete("scheme")
    async def set_team_scheme_scheme_autocomplete(self, interaction: discord.Interaction, current: str):
        side_value = getattr(interaction.namespace, "side", None) or "offense"
        options = OFFENSE_SCHEME_OPTIONS if side_value == "offense" else DEFENSE_SCHEME_OPTIONS
        current_lower = (current or "").lower()
        matches = [label for label, value in options if current_lower in value.lower()]
        return [app_commands.Choice(name=m, value=m) for m in matches[:25]]

    @app_commands.command(name="post_scheme_cards", description="Set this channel as the live scheme cards display (admin only)")
    async def post_scheme_cards(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can do that.")
            return

        settings = load_settings()
        settings["scheme_cards_channel_id"] = interaction.channel_id
        save_settings(settings)

        await send_ephemeral(interaction, "This channel is now the live scheme cards display. Building it now...")
        await self.refresh_scheme_cards_channel()

    @app_commands.command(name="view_scheme_card", description="View a team's scheme card")
    @app_commands.describe(team="Team to view")
    async def view_scheme_card(self, interaction: discord.Interaction, team: str):
        abbr, error = resolve_team(team, self.teams)
        if error:
            await send_ephemeral(interaction, error)
            return

        cards = load_scheme_cards()
        card = cards.get(abbr)
        if not card or (not card.get("offense") and not card.get("defense")):
            await send_ephemeral(interaction, f"No scheme card set yet for **{self.teams[abbr]['name']}**.")
            return

        embed = build_scheme_card_embed(self.teams[abbr], card)
        await interaction.response.send_message(embed=embed)

    @view_scheme_card.autocomplete("team")
    async def view_scheme_card_team_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.team_autocomplete(interaction, current)

    @app_commands.command(
        name="missing_scheme_cards",
        description="List rostered teams missing an offense and/or defense scheme card (admin only)",
    )
    async def missing_scheme_cards(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can do that.")
            return

        roster = load_roster()
        if not roster:
            await send_ephemeral(interaction, "No teams are currently rostered.")
            return

        cards = load_scheme_cards()

        incomplete = []  # (team_name, username, missing_label)
        for abbr, entry in roster.items():
            if abbr not in self.teams:
                continue
            card = cards.get(abbr, {})
            has_offense = bool(card.get("offense") and card["offense"].get("scheme"))
            has_defense = bool(card.get("defense") and card["defense"].get("scheme"))

            if has_offense and has_defense:
                continue

            if not has_offense and not has_defense:
                missing = "Offense + Defense"
            elif not has_offense:
                missing = "Offense"
            else:
                missing = "Defense"

            team_name = self.teams[abbr]["name"]
            username = entry.get("username", "Unknown")
            incomplete.append((team_name, username, missing))

        if not incomplete:
            embed = discord.Embed(
                title="✅ All Scheme Cards Complete",
                description="Every rostered team has both an offense and defense scheme card set.",
                color=discord.Color.green(),
            )
            embed.set_footer(text="This message will disappear in 5 minutes.")
            await send_ephemeral(interaction, embed=embed, delete_after=300)
            return

        incomplete.sort(key=lambda x: x[0])
        lines = []
        for team_name, username, missing in incomplete:
            lines.append(f"`{team_name}`\n{username} — *missing {missing}*")

        embed = discord.Embed(
            title=f"Missing Scheme Cards ({len(incomplete)})",
            description="\n\n".join(lines),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="This message will disappear in 5 minutes.")

        await send_ephemeral(interaction, embed=embed, delete_after=300)


    @app_commands.command(
        name="missing_installs",
        description="List rostered teams missing an offense and/or defense install (admin only)",
    )
    async def missing_installs(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can do that.")
            return

        roster = load_roster()
        if not roster:
            await send_ephemeral(interaction, "No teams are currently rostered.")
            return

        from cogs.install_offense import load_offense_installs
        from cogs.install_defense import load_defense_installs
        offense_installs = load_offense_installs()
        defense_installs = load_defense_installs()

        incomplete = []  # (team_name, username, missing_label)
        for abbr, entry in roster.items():
            if abbr not in self.teams:
                continue
            has_offense = abbr in offense_installs
            has_defense = abbr in defense_installs

            if has_offense and has_defense:
                continue

            if not has_offense and not has_defense:
                missing = "Offense + Defense"
            elif not has_offense:
                missing = "Offense"
            else:
                missing = "Defense"

            team_name = self.teams[abbr]["name"]
            username = entry.get("username", "Unknown")
            incomplete.append((team_name, username, missing))

        if not incomplete:
            embed = discord.Embed(
                title="✅ All Installs Complete",
                description="Every rostered team has both an offense and defense install submitted.",
                color=discord.Color.green(),
            )
            embed.set_footer(text="This message will disappear in 5 minutes.")
            await send_ephemeral(interaction, embed=embed, delete_after=300)
            return

        incomplete.sort(key=lambda x: x[0])
        lines = []
        for team_name, username, missing in incomplete:
            lines.append(f"`{team_name}`\n{username} — *missing {missing}*")

        embed = discord.Embed(
            title=f"Missing Installs ({len(incomplete)})",
            description="\n\n".join(lines),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="This message will disappear in 5 minutes.")

        await send_ephemeral(interaction, embed=embed, delete_after=300)


async def setup(bot: commands.Bot):
    await bot.add_cog(SchemeCards(bot))
