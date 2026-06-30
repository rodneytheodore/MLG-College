from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils.data import (
    load_teams,
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


# ---------- Option lists: (label, value) ----------

OFFENSE_SCHEME_OPTIONS = [(s, s) for s in [
    "Air Raid", "Spread", "Spread Option", "Option",
    "Pro Style", "Power Spread", "Pistol", "Multiple",
]]
OFFENSE_TEMPO_OPTIONS = [(t, t) for t in ["Ball Control", "No Huddle", "Turbo"]]
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

OFFENSE_STEP_NAMES = [
    "Scheme", "Tempo", "Run/Pass Tendency", "Playbook Type",
    "Personnel Groupings", "Core Run Concepts",
    "Quick Game Pass Concepts", "Intermediate Pass Concepts", "Deep Pass Concepts",
]
DEFENSE_STEP_NAMES = ["Scheme", "Coverage Shell", "Coverage Type", "Base Coverages", "Pressures"]


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
        header_line = f"**Coach:** {offense['coach']}"
        if offense.get("film"):
            header_line += f"  \u2022  **Stream Link:** {offense['film']}"
        embed.description = header_line

        lines = [f"**Scheme:** {offense['scheme']}  \u2022  **Coaching Tree:** {offense['coaching_tree']}"]
        lines.append(f"**Playbook Type:** {offense['playbook_type']}  \u2022  **Base Playbook:** {offense['base_playbook']}")
        lines.append(f"**Personnel:** {offense['personnel']}")
        lines.append(f"**Tendency:** {offense['run_pass']}")
        lines.append(f"**Core Run Concepts:** {offense['core_run_concepts']}")
        lines.append(f"**Quick Pass:** {offense['pass_quick_game']}")
        lines.append(f"**Intermediate Pass:** {offense['pass_intermediate']}")
        lines.append(f"**Deep Pass:** {offense['pass_deep']}")
        lines.append(f"**Tempo:** {offense['tempo']}")
        lines.append(f"**Summary:** {offense['summary']}")
        embed.add_field(name="OFFENSE", value="\n".join(lines), inline=False)

    defense = card.get("defense")
    if defense:
        lines = [f"**Scheme:** {defense['scheme']}  \u2022  **Identity:** {defense['coverage_type']}"]
        lines.append(f"**Coaching Tree:** {defense['coaching_tree']}  \u2022  **Shell:** {defense['coverage_shell']}")
        lines.append(f"**Base Coverages:** {defense['base_coverages']}")
        lines.append(f"**Pressures:** {defense['pressures']}")
        lines.append(f"**Summary:** {defense['summary']}")
        embed.add_field(name="DEFENSE", value="\n".join(lines), inline=False)

    if card.get("last_updated"):
        embed.set_footer(text=f"Last updated: {card['last_updated']}")

    return embed


def build_step_prompt(step_names: list[str], index: int, label: str) -> str:
    total = len(step_names)
    text = f"**(Part 2 of 2) Step {index + 1}/{total} — Pick your {label.lower()}:**"
    if index + 1 < total:
        text += f"\n*Next up: {step_names[index + 1]}*"
    return text


# ---------- Generic single-select dropdown step ----------

class ChoiceStepView(discord.ui.View):
    """A select menu where picking one option advances the wizard."""

    def __init__(self, choices: list[tuple[str, str]], placeholder: str, on_pick):
        super().__init__(timeout=180)
        self.on_pick = on_pick

        select = discord.ui.Select(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label[:100], value=value[:100])
                for label, value in choices
            ],
        )
        select.callback = self._make_callback(select)
        self.add_item(select)

    def _make_callback(self, select: discord.ui.Select):
        async def callback(interaction: discord.Interaction):
            await self.on_pick(interaction, select.values[0])
        return callback


# ---------- Personnel groupings: multi-select dropdown, max 3 ----------

class MultiSelectStepView(discord.ui.View):
    """A capped multi-select dropdown. Confirming calls on_confirm with the
    sorted list of selected values — used for both Personnel Groupings and
    Core Run Concepts, chained one after another."""

    def __init__(self, options: list[tuple[str, str]], max_select: int, label: str, on_confirm, min_select: int = 1):
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


# ---------- Offense wizard: Scheme -> Tempo -> Playbook Type -> Personnel ----------

class OffenseWizard:
    STEPS = [
        ("scheme", OFFENSE_SCHEME_OPTIONS),
        ("tempo", OFFENSE_TEMPO_OPTIONS),
        ("run_pass", RUN_PASS_OPTIONS),
        ("playbook_type", PLAYBOOK_TYPE_OPTIONS),
    ]

    def __init__(self, cog: "SchemeCards", abbr: str, base_data: dict):
        self.cog = cog
        self.abbr = abbr
        self.data = base_data
        self.step_index = 0

    async def start(self, interaction: discord.Interaction):
        field_key, choices = self.STEPS[0]
        prompt = build_step_prompt(OFFENSE_STEP_NAMES, 0, OFFENSE_STEP_NAMES[0])
        view = ChoiceStepView(choices, f"Select your {OFFENSE_STEP_NAMES[0].lower()}...", self._make_advance_callback())
        await interaction.response.send_message(prompt, view=view, ephemeral=True)

    def _make_advance_callback(self):
        async def on_pick(interaction: discord.Interaction, value: str):
            field_key = self.STEPS[self.step_index][0]
            self.data[field_key] = value
            self.step_index += 1

            if self.step_index < len(self.STEPS):
                _, choices = self.STEPS[self.step_index]
                step_name = OFFENSE_STEP_NAMES[self.step_index]
                prompt = build_step_prompt(OFFENSE_STEP_NAMES, self.step_index, step_name)
                view = ChoiceStepView(choices, f"Select your {step_name.lower()}...", self._make_advance_callback())
                await interaction.response.edit_message(content=prompt, view=view)
            else:
                await self._show_personnel_step(interaction)
        return on_pick

    async def _show_personnel_step(self, interaction: discord.Interaction):
        view = MultiSelectStepView(
            PERSONNEL_GROUPINGS, PERSONNEL_MAX_SELECT, "personnel groupings",
            self._after_personnel,
        )
        await interaction.response.edit_message(
            content=f"**(Part 2 of 2) Step 5/9 — Select your primary personnel groupings** "
            f"(up to {PERSONNEL_MAX_SELECT}, then confirm):",
            view=view,
        )

    async def _after_personnel(self, interaction: discord.Interaction, selected: list[str]):
        self.data["personnel"] = ", ".join(selected)
        view = MultiSelectStepView(
            CORE_RUN_CONCEPTS, CORE_RUN_MAX_SELECT, "core run concepts",
            self._after_core_run,
        )
        await interaction.response.edit_message(
            content=f"**(Part 2 of 2) Step 6/9 — Select your core run concepts** "
            f"(up to {CORE_RUN_MAX_SELECT}, then confirm):",
            view=view,
        )

    async def _after_core_run(self, interaction: discord.Interaction, selected: list[str]):
        self.data["core_run_concepts"] = ", ".join(selected)
        view = MultiSelectStepView(
            PASS_QUICK_GAME, PASS_QUICK_MAX, "quick game pass concepts",
            self._after_pass_quick, min_select=PASS_QUICK_MIN,
        )
        await interaction.response.edit_message(
            content=f"**(Part 2 of 2) Step 7/9 — Quick Game pass concepts** "
            f"(pick {PASS_QUICK_MIN}-{PASS_QUICK_MAX}, then confirm):",
            view=view,
        )

    async def _after_pass_quick(self, interaction: discord.Interaction, selected: list[str]):
        self.data["pass_quick_game"] = ", ".join(selected)
        view = MultiSelectStepView(
            PASS_INTERMEDIATE, PASS_INTERMEDIATE_MAX, "intermediate pass concepts",
            self._after_pass_intermediate, min_select=PASS_INTERMEDIATE_MIN,
        )
        await interaction.response.edit_message(
            content=f"**(Part 2 of 2) Step 8/9 — Intermediate pass concepts** "
            f"(pick {PASS_INTERMEDIATE_MIN}-{PASS_INTERMEDIATE_MAX}, then confirm):",
            view=view,
        )

    async def _after_pass_intermediate(self, interaction: discord.Interaction, selected: list[str]):
        self.data["pass_intermediate"] = ", ".join(selected)
        view = MultiSelectStepView(
            PASS_DEEP, PASS_DEEP_MAX, "deep pass concepts",
            self._after_pass_deep, min_select=PASS_DEEP_MIN,
        )
        await interaction.response.edit_message(
            content=f"**(Part 2 of 2) Step 9/9 — Deep pass concepts** "
            f"(pick {PASS_DEEP_MIN}-{PASS_DEEP_MAX}, then confirm):",
            view=view,
        )

    async def _after_pass_deep(self, interaction: discord.Interaction, selected: list[str]):
        self.data["pass_deep"] = ", ".join(selected)

        cards = load_scheme_cards()
        card = cards.setdefault(self.abbr, {})
        card["offense"] = self.data
        card["submitted_by"] = true_display_name(interaction.user)
        card["last_updated"] = datetime.now(timezone.utc).strftime("%B %d, %Y")
        save_scheme_cards(cards)

        await interaction.response.edit_message(
            content=f"\u2705 Offense scheme saved for **{self.cog.teams[self.abbr]['name']}**.",
            view=None,
        )
        await self.cog.refresh_scheme_cards_channel()


# ---------- Defense wizard: Scheme -> Coverage Shell -> Coverage Type -> Pressure ----------

class DefenseWizard:
    STEPS = [
        ("scheme", DEFENSE_SCHEME_OPTIONS),
        ("coverage_shell", COVERAGE_SHELL_OPTIONS),
        ("coverage_type", COVERAGE_TYPE_OPTIONS),
    ]

    def __init__(self, cog: "SchemeCards", abbr: str, base_data: dict):
        self.cog = cog
        self.abbr = abbr
        self.data = base_data
        self.step_index = 0

    async def start(self, interaction: discord.Interaction):
        field_key, choices = self.STEPS[0]
        prompt = build_step_prompt(DEFENSE_STEP_NAMES, 0, DEFENSE_STEP_NAMES[0])
        view = ChoiceStepView(choices, f"Select your {DEFENSE_STEP_NAMES[0].lower()}...", self._make_advance_callback())
        await interaction.response.send_message(prompt, view=view, ephemeral=True)

    def _make_advance_callback(self):
        async def on_pick(interaction: discord.Interaction, value: str):
            field_key = self.STEPS[self.step_index][0]
            self.data[field_key] = value
            self.step_index += 1

            if self.step_index < len(self.STEPS):
                _, choices = self.STEPS[self.step_index]
                step_name = DEFENSE_STEP_NAMES[self.step_index]
                prompt = build_step_prompt(DEFENSE_STEP_NAMES, self.step_index, step_name)
                view = ChoiceStepView(choices, f"Select your {step_name.lower()}...", self._make_advance_callback())
                await interaction.response.edit_message(content=prompt, view=view)
            else:
                await self._show_base_coverages_step(interaction)
        return on_pick

    async def _show_base_coverages_step(self, interaction: discord.Interaction):
        view = MultiSelectStepView(
            BASE_COVERAGES, BASE_COVERAGES_MAX_SELECT, "Base Coverages",
            self._after_base_coverages,
        )
        await interaction.response.edit_message(
            content="**(Part 2 of 2) Step 4/5 — Select up to 4 Base Coverages**, then confirm:",
            view=view,
        )

    async def _after_base_coverages(self, interaction: discord.Interaction, selected: list[str]):
        self.data["base_coverages"] = ", ".join(selected)
        await self._show_pressures_step(interaction)

    async def _show_pressures_step(self, interaction: discord.Interaction):
        view = MultiSelectStepView(
            PRESSURE_TYPES, PRESSURE_TYPES_MAX_SELECT, "pressures",
            self._after_pressures,
        )
        await interaction.response.edit_message(
            content=f"**(Part 2 of 2) Step 5/5 — Select your top pressures** "
            f"(up to {PRESSURE_TYPES_MAX_SELECT}, then confirm):",
            view=view,
        )

    async def _after_pressures(self, interaction: discord.Interaction, selected: list[str]):
        self.data["pressures"] = ", ".join(selected)

        cards = load_scheme_cards()
        card = cards.setdefault(self.abbr, {})
        card["defense"] = self.data
        card["submitted_by"] = true_display_name(interaction.user)
        card["last_updated"] = datetime.now(timezone.utc).strftime("%B %d, %Y")
        save_scheme_cards(cards)

        await interaction.response.edit_message(
            content=f"\u2705 Defense scheme saved for **{self.cog.teams[self.abbr]['name']}**.",
            view=None,
        )
        await self.cog.refresh_scheme_cards_channel()


# ---------- Initial detail modals (popup text fields, shown first) ----------

class OffenseDetailsModal(discord.ui.Modal, title="Offense Details (Part 1 of 2)"):
    coaching_tree = discord.ui.TextInput(label="Coaching Tree (1 or 2 coaches)", required=True)
    base_playbook = discord.ui.TextInput(label="Base Playbook (e.g. Air Raid)", required=True)
    summary = discord.ui.TextInput(label="Summary", style=discord.TextStyle.paragraph, required=True)
    film = discord.ui.TextInput(label="Stream Link (Twitch, YouTube, etc.)", required=True)

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
        wizard = OffenseWizard(cog=self.cog, abbr=self.abbr, base_data=base_data)
        await wizard.start(interaction)


class DefenseDetailsModal(discord.ui.Modal, title="Defense Details (Part 1 of 2)"):
    coaching_tree = discord.ui.TextInput(label="Coaching Tree (1 or 2 coaches)", required=True)
    summary = discord.ui.TextInput(label="Summary", style=discord.TextStyle.paragraph, required=True)

    def __init__(self, cog: "SchemeCards", abbr: str):
        super().__init__()
        self.cog = cog
        self.abbr = abbr

    async def on_submit(self, interaction: discord.Interaction):
        base_data = {"coaching_tree": str(self.coaching_tree), "summary": str(self.summary)}
        wizard = DefenseWizard(cog=self.cog, abbr=self.abbr, base_data=base_data)
        await wizard.start(interaction)


class SchemeCards(commands.Cog):
    """Offense/defense scheme cards per team, set by the team's owner."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.teams = load_teams()

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
        for abbr in sorted(cards.keys(), key=lambda a: self.teams[a]["name"]):
            card = cards[abbr]
            if not card.get("offense") and not card.get("defense"):
                continue
            embed = build_scheme_card_embed(self.teams[abbr], card)
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

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


async def setup(bot: commands.Bot):
    await bot.add_cog(SchemeCards(bot))
