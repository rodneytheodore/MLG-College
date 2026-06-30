import discord
from discord import app_commands
from discord.ext import commands
from typing import Literal
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from discord.ext import tasks

from utils.data import (
    load_teams,
    load_roster,
    save_roster,
    load_season,
    save_season,
    load_settings,
    save_settings,
    load_scheme_cards,
    archive_dynasty,
    is_admin,
    resolve_team,
)
from utils.responses import send_ephemeral
from utils.matchup_image import build_matchup_file
from cogs.scheme_cards import build_compact_scheme_card_embed, ExpandSchemeCardView

EASTERN = ZoneInfo("America/New_York")


def now_eastern() -> datetime:
    return datetime.now(EASTERN)


def format_deadline(dt: datetime) -> str:
    return dt.strftime("%A, %B %d at %I:%M %p %Z")


def compute_deadline(days: int | None, time_str: str | None, ampm: str | None) -> tuple[str | None, str | None]:
    """Returns (user_deadline, cpu_deadline) where cpu is 1 day earlier than user.
    Both return None if not enough info provided."""
    if not days and not time_str:
        return None, None
    now = now_eastern()
    user_date = now + timedelta(days=days) if days else now
    cpu_date = user_date - timedelta(days=1)
    time_label = f"{time_str} {ampm}" if time_str and ampm else (time_str or "")
    tz_label = "EDT" if now.dst().seconds > 0 else "EST"

    def fmt(dt: datetime) -> str:
        if time_label:
            return f"{dt.strftime('%A, %B %d')} at {time_label} {tz_label}"
        return dt.strftime("%A, %B %d")

    return fmt(user_date), fmt(cpu_date)


def deadline_preview() -> str:
    """Shows current Eastern time plus what each day preset resolves to."""
    now = now_eastern()
    lines = [f"**Current date:** {now.strftime('%A, %B %d')}"]
    for d in (1, 2, 3):
        lines.append(f"**+{d} day{'s' if d > 1 else ''}:** {(now + timedelta(days=d)).strftime('%A, %B %d')}")
    return "\n".join(lines)


def build_dashboard_embed(season: dict, roster: dict, scheme_cards: dict) -> discord.Embed:
    year = season.get("year") or "Not set yet"
    current_stage = season.get("current_stage", "preseason")
    stage = PHASE_DISPLAY.get(current_stage, current_stage)
    current_week = season.get("current_week")
    week_text = f"Week {current_week}" if current_week is not None else "No active week"
    claimed_count = len(roster)

    # "Submitted" = both offense and defense halves are filled in for that team
    submitted_count = sum(
        1 for card in scheme_cards.values()
        if card.get("offense") and card.get("defense")
    )

    week_data = season.get("weeks", {}).get(str(current_week)) if current_week is not None else None
    games = week_data.get("games", []) if week_data else []

    cpu_games = [g for g in games if g["type"] == "cpu"]
    cpu_completed = sum(1 for g in cpu_games if g.get("status") == "completed")

    user_games = [g for g in games if g["type"] == "user"]
    user_scheduled = sum(1 for g in user_games if g.get("scheduled"))
    user_completed = sum(1 for g in user_games if g.get("status") == "completed")

    embed = discord.Embed(title="🏈 League Status", color=discord.Color.blurple())
    embed.add_field(name="Dynasty Year", value=str(year), inline=True)
    embed.add_field(name="Stage", value=stage, inline=True)
    embed.add_field(name="Current Week", value=week_text, inline=True)
    embed.add_field(name="Teams Claimed", value=f"{claimed_count}/32", inline=True)
    embed.add_field(name="Scheme Cards Submitted", value=f"{submitted_count}/{claimed_count}", inline=False)
    embed.add_field(name="CPU Games Count", value=str(len(cpu_games)), inline=True)
    embed.add_field(name="CPU Games Completed", value=f"{cpu_completed}/{len(cpu_games)}", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=False)  # invisible spacer, forces a row break
    embed.add_field(name="User Games Count", value=str(len(user_games)), inline=True)
    embed.add_field(name="User Games Scheduled", value=f"{user_scheduled}/{len(user_games)}", inline=True)
    embed.add_field(name="User Games Completed", value=f"{user_completed}/{len(user_games)}", inline=True)
    return embed


async def refresh_dashboard(bot: commands.Bot):
    """Edits the existing dashboard message in place, or sends a new one
    if none exists yet or the stored message was deleted."""
    settings = load_settings()
    channel_id = settings.get("dashboard_channel_id")
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        return

    season = load_season()
    roster = load_roster()
    scheme_cards = load_scheme_cards()
    embed = build_dashboard_embed(season, roster, scheme_cards)

    message_id = settings.get("dashboard_message_id")
    message = None
    if message_id:
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.HTTPException):
            message = None

    if message is not None:
        await message.edit(embed=embed)
    else:
        new_message = await channel.send(embed=embed)
        settings["dashboard_message_id"] = new_message.id
        save_settings(settings)


PHASE_TRANSITIONS = {
    "preseason": {
        "display": "Preseason",
        "next": "regular_season",
        "week_reset": 0,
        "has_weeks": False,
        "week_cap": None,
        "early_switch_allowed": False,
    },
    "regular_season": {
        "display": "Regular Season",
        "next": "conference_championships",
        "week_reset": None,
        "has_weeks": True,
        "week_cap": 16,  # default — variable, may need to be configurable later
        "early_switch_allowed": True,
    },
    "conference_championships": {
        "display": "Conference Championships",
        "next": "postseason",
        "week_reset": 1,
        "has_weeks": False,
        "week_cap": None,
        "early_switch_allowed": False,
    },
    "postseason": {
        "display": "Postseason",
        "next": "offseason_players_leaving",
        "week_reset": None,
        "has_weeks": True,
        "week_cap": 4,
        "early_switch_allowed": False,
    },
    "offseason_players_leaving": {
        "display": "📤 Players Leaving",
        "next": "offseason_transfer_portal",
        "week_reset": None,
        "has_weeks": False,
        "week_cap": None,
        "early_switch_allowed": False,
    },
    "offseason_transfer_portal": {
        "display": "🔄 Transfer Portal",
        "next": "offseason_national_signing_day",
        "week_reset": None,
        "has_weeks": True,
        "week_cap": 4,
        "early_switch_allowed": False,
    },
    "offseason_national_signing_day": {
        "display": "✍️ National Signing Day",
        "next": "offseason_position_changes",
        "week_reset": None,
        "has_weeks": False,
        "week_cap": None,
        "early_switch_allowed": False,
    },
    "offseason_position_changes": {
        "display": "🔀 Position Changes",
        "next": "offseason_training",
        "week_reset": None,
        "has_weeks": False,
        "week_cap": None,
        "early_switch_allowed": False,
    },
    "offseason_training": {
        "display": "💪 Offseason Training",
        "next": "offseason_encourage_transfers",
        "week_reset": None,
        "has_weeks": False,
        "week_cap": None,
        "early_switch_allowed": False,
    },
    "offseason_encourage_transfers": {
        "display": "🚪 Encourage Transfers",
        "next": "preseason",
        "week_reset": None,
        "has_weeks": False,
        "week_cap": None,
        "early_switch_allowed": False,
    },
}

PHASE_DISPLAY = {k: v["display"] for k, v in PHASE_TRANSITIONS.items()}


def get_announcement_message(current_phase: str, new_phase: str | None, week: int | None, deadline: str | None, cpu_deadline: str | None) -> str:
    """Returns the formatted announcement text for a given phase transition or week advance."""
    dl = f"\n📅 **User Games Due:** {deadline}" if deadline else ""
    cpu_dl = f"\n📅 **CPU Games Due:** {cpu_deadline}" if cpu_deadline else ""
    both_dl = f"{cpu_dl}{dl}"

    messages = {
        # Same-phase week advances
        ("regular_season", None): f"🏈 **Week {week} is now live!** Check the game channels to find your game thread and get scheduled.{both_dl}",
        ("postseason", None): f"🏈 **Postseason Week {week} is live!** Next round matchups are set. Find your thread and get scheduled.{both_dl}",

        # Phase switches
        ("preseason", "regular_season"): f"🚨 **The Regular Season has begun!** Week 0 kicks things off. Time to offer scholarships!{both_dl}",
        ("regular_season", "conference_championships"): f"🏆 **Conference Championship Week!** The regular season is over. Conference Championship matchups have been set — this is what you played for. Don't let up now.{both_dl}",
        ("conference_championships", "postseason"): f"🎉 **Postseason is here!** Bowl season and Playoff matchups are live. Check your game thread and get scheduled.{both_dl}",
        ("postseason", "offseason_players_leaving"): f"📤 **The season has concluded and the offseason is now underway.** Players Leaving is now live — review your roster for early departures and transfers out of the program.{both_dl}",
        ("offseason_players_leaving", "offseason_transfer_portal"): f"🔄 **Transfer Portal is now open!** Players are on the move. Check which players have entered the portal and make your decisions accordingly.{both_dl}",
        ("offseason_transfer_portal", "offseason_national_signing_day"): "✍️ **Advanced to National Signing Day!** Position Changes is on deck!",
        ("offseason_national_signing_day", "offseason_position_changes"): f"🔀 **Position Changes** — Finalize any position switches on your roster before the offseason training cycle begins.{both_dl}",
        ("offseason_position_changes", "offseason_training"): "💪 **Offseason Training is complete!** This is where programs are built. Hopefully your players made the most of the offseason!",
        ("offseason_training", "offseason_encourage_transfers"): f"🚪 **Encourage Transfers** — Last chance to move on from players who don't fit your program. Make your decisions and get ready for a new season.{both_dl}",
        ("offseason_encourage_transfers", "preseason"): f"🏈 **A new season is approaching!** Offseason is complete. Preseason is here — rosters are set and the next dynasty year is on the horizon. Time to scout players for the next recruiting class!{both_dl}",
    }

    key = (current_phase, new_phase)
    return messages.get(key, f"🏈 **Week {week} is now live!**{both_dl}")


def get_phase(key: str) -> dict:
    return PHASE_TRANSITIONS.get(key, PHASE_TRANSITIONS["preseason"])


def next_phase(current: str) -> str | None:
    return get_phase(current).get("next")


def week_cap_reached(phase_key: str, current_week: int | None) -> bool:
    """True if this phase has a week cap and the current week has hit it."""
    info = get_phase(phase_key)
    week_cap = info.get("week_cap")
    if not info.get("has_weeks") or week_cap is None:
        return False
    return (current_week or 0) >= week_cap


# ---- Advance Week Wizard option lists ----

DAY_OPTIONS = [
    ("+1 Day", "1"),
    ("+2 Days", "2"),
    ("+3 Days", "3"),
    ("+4 Days", "4"),
    ("+5 Days", "5"),
    ("No Deadline", "no_deadline"),
]

# 24 combined hour options — fits in one Discord select (max 25)
HOUR_OPTIONS = (
    [("12 AM", "12 AM")]
    + [(f"{h} AM", f"{h} AM") for h in range(1, 12)]
    + [("12 PM", "12 PM")]
    + [(f"{h} PM", f"{h} PM") for h in range(1, 12)]
)

MINUTE_OPTIONS = [(":00", ":00"), (":30", ":30")]


def build_deadline_strings(
    day_offset: int,
    hour_str: str | None,
    minute_str: str | None,
) -> tuple[str, str]:
    """Build (user_deadline, cpu_deadline) display strings from wizard picks."""
    now = now_eastern()
    user_date = now + timedelta(days=day_offset)
    cpu_date = user_date - timedelta(days=1)
    tz_label = "EDT" if now.dst().seconds > 0 else "EST"

    def fmt(dt: datetime) -> str:
        base = dt.strftime("%A, %B %d")
        if hour_str and minute_str:
            return f"{base} at {hour_str}{minute_str} {tz_label}"
        return base

    return fmt(user_date), fmt(cpu_date)


class AdvanceSelectView(discord.ui.View):
    """Generic single-select that calls a callback immediately on pick."""

    def __init__(self, options: list[tuple[str, str]], placeholder: str, on_pick):
        super().__init__(timeout=180)

        select = discord.ui.Select(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label[:100], value=value[:100])
                for label, value in options
            ],
        )
        select.callback = self._make_callback(select, on_pick)
        self.add_item(select)

    def _make_callback(self, select: discord.ui.Select, on_pick):
        async def callback(interaction: discord.Interaction):
            await on_pick(interaction, select.values[0])
        return callback


class ConfirmAdvanceView(discord.ui.View):
    """Final confirm/cancel step before executing the advance."""

    def __init__(self, bot: commands.Bot, wizard: "AdvanceWeekWizard"):
        super().__init__(timeout=60)
        self.bot = bot
        self.wizard = wizard

    @discord.ui.button(label="✅ Confirm — Advance", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("Only admins can do this.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await self.wizard.execute(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", view=self)


class AdvanceWeekWizard:
    """Orchestrates the step-by-step /advance_week select-menu flow."""

    def __init__(self, bot: commands.Bot, cog: "Scheduling"):
        self.bot = bot
        self.cog = cog
        self.season = load_season()
        self.action: str | None = None        # "next_week" or "next_phase"
        self.day_offset: int | None = None    # None = no deadline
        self.hour_str: str | None = None
        self.minute_str: str | None = None
        self.week0_has_games: bool | None = None  # only relevant for preseason → regular_season

    # ---- Derived properties ----

    @property
    def current_stage(self) -> str:
        return self.season.get("current_stage", "preseason")

    @property
    def current_week(self) -> int:
        return self.season.get("current_week") or 0

    @property
    def next_week_num(self) -> int:
        return self.current_week + 1

    @property
    def next_phase_key(self) -> str | None:
        return next_phase(self.current_stage)

    @property
    def next_phase_label(self) -> str:
        np = self.next_phase_key
        return PHASE_DISPLAY.get(np, np) if np else "?"

    @property
    def deadline_tuple(self) -> tuple[str | None, str | None]:
        if self.day_offset is None:
            return None, None
        return build_deadline_strings(self.day_offset, self.hour_str, self.minute_str)

    def _action_label(self) -> str:
        if self.action == "next_week":
            stage_label = PHASE_DISPLAY.get(self.current_stage, self.current_stage)
            return f"Week {self.next_week_num} ({stage_label})"
        elif self.action == "next_phase":
            return self.next_phase_label
        return "..."

    # ---- Entry point ----

    async def start(self, interaction: discord.Interaction):
        info = get_phase(self.current_stage)
        has_weeks = info.get("has_weeks", False)
        at_cap = week_cap_reached(self.current_stage, self.current_week)

        # Offer stage switch only for regular_season at week 15+
        needs_choice = (
            has_weeks
            and not at_cap
            and info.get("early_switch_allowed", False)
            and self.current_week >= 15
        )

        if not has_weeks or at_cap:
            self.action = "next_phase"
            await self._send_day_select(interaction)
        elif needs_choice:
            await self._send_action_choice(interaction)
        else:
            self.action = "next_week"
            await self._send_day_select(interaction)

    # ---- Step: action choice (regular_season week 15+) ----

    async def _send_action_choice(self, interaction: discord.Interaction):
        options = [
            (f"Next Week (Week {self.next_week_num})", "next_week"),
            (f"→ {self.next_phase_label}", "next_phase"),
        ]

        async def on_pick(interaction: discord.Interaction, value: str):
            self.action = value
            await self._edit_day_select(interaction)

        view = AdvanceSelectView(options, "What are we advancing to?", on_pick)
        await interaction.response.send_message(
            "**Advance Week** — what's next?",
            view=view,
            ephemeral=True,
        )

    # ---- Step: deadline day ----

    async def _send_day_select(self, interaction: discord.Interaction):
        """Initial send — used when no action choice precedes it."""
        view = AdvanceSelectView(DAY_OPTIONS, "Deadline — how many days out?", self._on_day_pick)
        await interaction.response.send_message(
            f"**Advance → {self._action_label()}** — set a deadline:",
            view=view,
            ephemeral=True,
        )

    async def _edit_day_select(self, interaction: discord.Interaction):
        """Edit — used after action choice."""
        view = AdvanceSelectView(DAY_OPTIONS, "Deadline — how many days out?", self._on_day_pick)
        await interaction.response.edit_message(
            content=f"**Advance → {self._action_label()}** — set a deadline:",
            view=view,
        )

    async def _on_day_pick(self, interaction: discord.Interaction, value: str):
        if value == "no_deadline":
            self.day_offset = None
            await self._after_deadline_set(interaction)
        else:
            self.day_offset = int(value)
            view = AdvanceSelectView(HOUR_OPTIONS, "Deadline — what hour?", self._on_hour_pick)
            await interaction.response.edit_message(
                content=f"**Advance → {self._action_label()}** — what time (hour)?",
                view=view,
            )

    # ---- Step: deadline hour ----

    async def _on_hour_pick(self, interaction: discord.Interaction, value: str):
        self.hour_str = value
        view = AdvanceSelectView(MINUTE_OPTIONS, "Deadline — :00 or :30?", self._on_minute_pick)
        await interaction.response.edit_message(
            content=f"**Advance → {self._action_label()}** — :00 or :30?",
            view=view,
        )

    # ---- Step: deadline minute ----

    async def _on_minute_pick(self, interaction: discord.Interaction, value: str):
        self.minute_str = value
        await self._after_deadline_set(interaction)

    # ---- Step: Week 0 games question (preseason only) ----

    async def _after_deadline_set(self, interaction: discord.Interaction):
        """After deadline is fully set, ask the Week 0 question if in Preseason."""
        if self.current_stage == "preseason" and self.action == "next_phase":
            await self._edit_week0_question(interaction)
        else:
            await self._edit_confirm(interaction)

    async def _edit_week0_question(self, interaction: discord.Interaction):
        options = [
            ("No — scholarship offers only", "no"),
            ("Yes — games are staged", "yes"),
        ]

        async def on_pick(interaction: discord.Interaction, value: str):
            self.week0_has_games = value == "yes"
            await self._edit_confirm(interaction)

        view = AdvanceSelectView(options, "Does Week 0 have any games?", on_pick)
        await interaction.response.edit_message(
            content="**Advance → Regular Season** — does Week 0 have any games?\n"
                    "*If yes, make sure they're already staged via `/add_game` before confirming.*",
            view=view,
        )

    # ---- Confirm screen ----

    async def _edit_confirm(self, interaction: discord.Interaction):
        deadline, cpu_deadline = self.deadline_tuple

        if self.action == "next_week":
            action_line = (
                f"📅 Advance to **Week {self.next_week_num}** "
                f"({PHASE_DISPLAY.get(self.current_stage, self.current_stage)})"
            )
        elif self.week0_has_games is True:
            action_line = "🏈 Advance to **Regular Season — Week 0** (games + channels)"
        elif self.week0_has_games is False:
            action_line = "🏈 Advance to **Regular Season — Week 0** (scholarship offers only)"
        else:
            action_line = f"🔄 Advance to **{self.next_phase_label}**"

        dl_line = (
            f"**User games due:** {deadline}\n**CPU games due:** {cpu_deadline}"
            if deadline else "**No deadline set.**"
        )

        view = ConfirmAdvanceView(bot=self.bot, wizard=self)
        await interaction.response.edit_message(
            content=f"**Ready to advance** — confirm?\n\n{action_line}\n{dl_line}",
            view=view,
        )

    # ---- Execute ----

    async def execute(self, interaction: discord.Interaction):
        """Called when admin hits Confirm."""
        deadline, cpu_deadline = self.deadline_tuple
        season = load_season()  # fresh load at execution time

        if self.action == "next_week":
            await self._execute_week_advance(interaction, season, deadline, cpu_deadline)
        else:
            await self._execute_stage_advance(interaction, season, deadline, cpu_deadline)

    async def _execute_week_advance(
        self, interaction: discord.Interaction, season: dict,
        deadline: str | None, cpu_deadline: str | None,
    ):
        week = self.next_week_num
        week_label = f"Week {week}"
        week_data = season.get("weeks", {}).get(str(week))

        if not week_data or not week_data.get("games"):
            await interaction.followup.send(
                f"No staged games found for {week_label}. Use `/add_game` first.", ephemeral=True
            )
            return

        if week_data.get("status") == "active":
            await interaction.followup.send(f"{week_label} is already active.", ephemeral=True)
            return

        await _do_advance_week(
            interaction, self.bot, week, week_label,
            new_phase=None, deadline=deadline, cpu_deadline=cpu_deadline,
        )

    async def _execute_stage_advance(
        self, interaction: discord.Interaction, season: dict,
        deadline: str | None, cpu_deadline: str | None,
    ):
        old_stage = season.get("current_stage", "preseason")
        new_stage = next_phase(old_stage)

        if new_stage is None:
            await interaction.followup.send("No next phase defined from the current stage.", ephemeral=True)
            return

        # encourage_transfers → preseason requires a year rollover
        if old_stage == "offseason_encourage_transfers":
            await self._execute_year_rollover(interaction, season, deadline, cpu_deadline)
            return

        info = get_phase(new_stage)
        season["current_stage"] = new_stage
        # Stages with weeks reset to 0 so the next /advance_week starts at week 1
        season["current_week"] = 0 if info.get("has_weeks") else None

        # Preseason → Regular Season with Week 0 games: create channels immediately
        if old_stage == "preseason" and self.week0_has_games:
            week_data = season.get("weeks", {}).get("0")
            if not week_data or not week_data.get("games"):
                await interaction.followup.send(
                    "No games staged for Week 0. Use `/add_game` first (while in Preseason, "
                    "all games stage to Week 0), then try again.",
                    ephemeral=True,
                )
                return
            save_season(season)
            await _do_advance_week(
                interaction, self.bot, 0, "Week 0",
                new_phase=None, deadline=deadline, cpu_deadline=cpu_deadline,
            )
            return

        save_season(season)

        await refresh_dashboard(self.bot)

        guild = interaction.guild
        ann_channel = discord.utils.find(
            lambda c: c.name.lower() in ("announcements", "announcement"),
            guild.text_channels,
        )
        if ann_channel:
            msg = get_announcement_message(
                current_phase=old_stage,
                new_phase=new_stage,
                week=None,
                deadline=deadline,
                cpu_deadline=cpu_deadline,
            )
            await ann_channel.send(msg)

        new_label = PHASE_DISPLAY.get(new_stage, new_stage)
        await interaction.followup.send(f"✅ Advanced to **{new_label}**.", ephemeral=True)

    async def _execute_year_rollover(
        self, interaction: discord.Interaction, season: dict,
        deadline: str | None, cpu_deadline: str | None,
    ):
        """Archive current year, increment, reset to Preseason (keeps roster intact)."""
        current_year = season.get("year")
        if current_year is None:
            await interaction.followup.send(
                "No dynasty year set. Run `/new_dynasty` first.", ephemeral=True
            )
            return

        roster = load_roster()
        archive_dynasty(season, roster)

        new_year = current_year + 1
        save_season({
            "year": new_year,
            "current_stage": "preseason",
            "current_week": None,
            "weeks": {},
        })

        await refresh_dashboard(self.bot)

        guild = interaction.guild
        ann_channel = discord.utils.find(
            lambda c: c.name.lower() in ("announcements", "announcement"),
            guild.text_channels,
        )
        if ann_channel:
            msg = get_announcement_message(
                current_phase="offseason_encourage_transfers",
                new_phase="preseason",
                week=None,
                deadline=deadline,
                cpu_deadline=cpu_deadline,
            )
            await ann_channel.send(msg)

        await interaction.followup.send(
            f"✅ Rolled over to **{new_year}**. Season archived, stage reset to Preseason. "
            f"Team assignments unchanged.",
            ephemeral=True,
        )


async def _do_advance_week(
    interaction: discord.Interaction, bot: commands.Bot,
    week: int, week_label: str, new_phase: str | None,
    deadline: str | None, cpu_deadline: str | None
):
    """Shared logic for actually building the week's channels and threads."""
    season = load_season()
    week_key = str(week)
    week_data = season.get("weeks", {}).get(week_key)

    if not week_data or not week_data.get("games"):
        await interaction.followup.send(
            f"No staged games found for {week_label}. Use `/add_game` first.", ephemeral=True
        )
        return

    guild = interaction.guild
    roster = load_roster()
    cog = bot.get_cog("Scheduling")

    existing_category_id = week_data.get("category_id")
    category = None
    if existing_category_id:
        category = guild.get_channel(existing_category_id)
        if category:
            await category.edit(name=f"🏈 WEEK {week}")
    if category is None:
        category = await guild.create_category(f"🏈 WEEK {week}")

    user_channel = await guild.create_text_channel("user-games", category=category)
    cpu_channel = await guild.create_text_channel("cpu-games", category=category)

    user_games = [g for g in week_data["games"] if g["type"] == "user"]
    cpu_games = [g for g in week_data["games"] if g["type"] == "cpu"]

    deadline_line = f"\n**Due:** {deadline}" if deadline else ""

    if cpu_games:
        if cpu_deadline:
            await cpu_channel.send(f"📅 **All CPU games due:** {cpu_deadline}")
        for g in cpu_games:
            embed, file = await cog.build_game_embed(g, week, roster)
            view = CompleteGameView(cog=cog, game_id=g["game_id"])
            send_kwargs = {"embed": embed, "view": view, "allowed_mentions": discord.AllowedMentions.none()}
            if file is not None:
                send_kwargs["file"] = file
            cpu_msg = await cpu_channel.send(**send_kwargs)
            g["message_id"] = cpu_msg.id
    else:
        await cpu_channel.send("No CPU games this week.")

    if user_games:
        for g in user_games:
            embed, file = await cog.build_game_embed(g, week, roster, deadline=deadline)
            view = CompleteGameView(cog=cog, game_id=g["game_id"], show_schedule_button=True)
            send_kwargs = {"embed": embed, "view": view, "allowed_mentions": discord.AllowedMentions.none()}
            if file is not None:
                send_kwargs["file"] = file
            game_msg = await user_channel.send(**send_kwargs)
            thread = await game_msg.create_thread(name=f"{g['home']} vs {g['away']} — Week {week}")
            home_owner_id = roster.get(g["home"], {}).get("user_id")
            away_owner_id = roster.get(g["away"], {}).get("user_id")
            await thread.send(
                f"<@{home_owner_id}> <@{away_owner_id}> use this thread to schedule your game and report completion.{deadline_line}",
                allowed_mentions=discord.AllowedMentions(users=True),
            )

            scheme_cards_cog = cog.bot.get_cog("SchemeCards")
            if scheme_cards_cog is not None:
                all_cards = load_scheme_cards()
                for team_abbr in (g["home"], g["away"]):
                    team_card = all_cards.get(team_abbr)
                    if not team_card or (not team_card.get("offense") and not team_card.get("defense")):
                        continue
                    card_embed = build_compact_scheme_card_embed(scheme_cards_cog.teams[team_abbr], team_card)
                    card_view = ExpandSchemeCardView(cog=scheme_cards_cog, abbr=team_abbr)
                    await thread.send(embed=card_embed, view=card_view, allowed_mentions=discord.AllowedMentions.none())

            g["thread_id"] = thread.id
            g["message_id"] = game_msg.id
    else:
        await user_channel.send("No user games this week.")

    week_data["status"] = "active"
    week_data["deadline"] = deadline
    week_data["cpu_deadline"] = cpu_deadline
    week_data["category_id"] = category.id
    week_data["user_channel_id"] = user_channel.id
    week_data["cpu_channel_id"] = cpu_channel.id
    season["weeks"][week_key] = week_data
    season["current_week"] = week

    original_phase = season.get("current_stage", "preseason")

    if new_phase:
        season["current_stage"] = new_phase
        for key, info in PHASE_TRANSITIONS.items():
            if info.get("next") == new_phase and info.get("week_reset") is not None:
                season["current_week"] = info["week_reset"]
                break

    save_season(season)
    await refresh_dashboard(bot)

    # Post announcement to #announcements channel
    ann_channel = discord.utils.find(
        lambda c: c.name.lower() in ("announcements", "announcement"),
        guild.text_channels
    )
    if ann_channel:
        message_text = get_announcement_message(
            current_phase=original_phase,
            new_phase=new_phase,
            week=week,
            deadline=deadline,
            cpu_deadline=cpu_deadline,
        )
        await ann_channel.send(message_text)

    phase_msg = f" — now in **{PHASE_DISPLAY[new_phase]}**" if new_phase else ""
    await interaction.followup.send(
        f"Advanced to Week {week}{phase_msg}. Channels and threads are live.", ephemeral=True
    )


class AdvanceSeasonConfirmView(discord.ui.View):
    def __init__(self, new_year: int):
        super().__init__(timeout=60)
        self.new_year = new_year

    @discord.ui.button(label="Confirm — Advance Season", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can confirm this.")
            return

        current_season = load_season()
        current_roster = load_roster()

        # Archive the just-finished season's data (rosters carry over, so this
        # is just a record of that year's final games/state, not a reset of teams).
        archive_dynasty(current_season, current_roster)

        save_season({
            "year": self.new_year,
            "current_stage": "preseason",
            "current_week": None,
            "weeks": {},
        })

        await refresh_dashboard(interaction.client)

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"✅ Advanced to **{self.new_year}**. Previous season archived. "
            f"Stage reset to Preseason. Team assignments were left untouched.",
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", view=self)


class NewDynastyConfirmView(discord.ui.View):
    def __init__(self, new_year: int):
        super().__init__(timeout=60)
        self.new_year = new_year

    @discord.ui.button(label="Confirm — Start New Dynasty", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can confirm this.")
            return

        current_season = load_season()
        current_roster = load_roster()

        archive_dynasty(current_season, current_roster)

        save_roster({})
        save_season({
            "year": self.new_year,
            "current_stage": "preseason",
            "current_week": None,
            "weeks": {},
        })

        roster_cog = interaction.client.get_cog("Roster")
        if roster_cog is not None:
            await roster_cog.refresh_roster_channel()

        await refresh_dashboard(interaction.client)

        ann_channel = discord.utils.find(
            lambda c: c.name.lower() in ("announcements", "announcement"),
            interaction.guild.text_channels
        )
        if ann_channel:
            await ann_channel.send(
                f"🏈 **A new dynasty has begun — {self.new_year}!** Details for team selection will be coming soon! "
                f"Once your team is assigned, submit your scheme card in #scheme-declaration and keep an eye on "
                f"#roster for the full league lineup. Let's get it!"
            )

        for child in self.children:
            child.disabled = True
        # Editing the existing ephemeral message in place; the auto-clear timer
        # from when this message was first sent (in /new_dynasty) still applies.
        await interaction.response.edit_message(
            content=f"✅ New dynasty started for **{self.new_year}**. Previous year archived. "
            f"Roster cleared, season reset to Preseason.",
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", view=self)


def classify_game(home_abbr: str, away_abbr: str, roster: dict) -> str:
    """USER if both teams have owners, CPU otherwise."""
    if home_abbr in roster and away_abbr in roster:
        return "user"
    return "cpu"


def resolve_target_week(season: dict, target: str) -> tuple[int, str]:
    """Resolve 'current' or 'next' to an actual week number.
    Returns (week_number, description_for_confirmation).
    In Preseason, all staging targets Week 0."""
    if season.get("current_stage") == "preseason":
        return 0, "Week 0 (Preseason)"
    current_week = season.get("current_week") or 0
    if target == "current":
        week = max(current_week, 1)
        return week, f"Week {week} (current)"
    else:
        week = current_week + 1
        return week, f"Week {week} (next)"


def find_game_by_id(season: dict, game_id: str):
    """Searches all weeks for a game with this ID. Returns (week_number, game_dict) or (None, None)."""
    for week_key, week_data in season.get("weeks", {}).items():
        for g in week_data.get("games", []):
            if g["game_id"] == game_id:
                return int(week_key), g
    return None, None


async def _load_authorized_game(interaction: discord.Interaction, game_id: str):
    """Shared lookup + permission check used by both the Scheduled and
    Completed buttons. Returns (season, week, game, roster) on success,
    or None after sending the appropriate ephemeral error itself."""
    season = load_season()
    week, game = find_game_by_id(season, game_id)
    if game is None:
        await interaction.response.send_message("Couldn't find this game anymore.", ephemeral=True)
        return None

    roster = load_roster()
    home_owner_id = roster.get(game["home"], {}).get("user_id")
    away_owner_id = roster.get(game["away"], {}).get("user_id")
    authorized = is_admin(interaction) or interaction.user.id in (home_owner_id, away_owner_id)

    if not authorized:
        await interaction.response.send_message(
            "Only an admin or one of the teams' owners can do that.", ephemeral=True
        )
        return None

    return season, week, game, roster


class CompleteGameView(discord.ui.View):
    """Persistent buttons for a game: Mark Completed always present, plus an
    optional Mark Scheduled button for user games. Survives bot restarts
    since it's registered with stable custom_ids and timeout=None."""

    def __init__(
        self, cog: "Scheduling", game_id: str,
        completed: bool = False, scheduled: bool = False, show_schedule_button: bool = False,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.game_id = game_id

        if show_schedule_button:
            schedule_btn = discord.ui.Button(
                label="📅 Scheduled" if scheduled else "Mark Scheduled",
                style=discord.ButtonStyle.success if scheduled else discord.ButtonStyle.secondary,
                disabled=scheduled,
                custom_id=f"schedule_game:{game_id}",
            )
            schedule_btn.callback = self._on_schedule_click
            self.add_item(schedule_btn)

        complete_btn = discord.ui.Button(
            label="✅ Completed" if completed else "Mark Completed",
            style=discord.ButtonStyle.success if completed else discord.ButtonStyle.secondary,
            disabled=completed,
            custom_id=f"complete_game:{game_id}",
        )
        complete_btn.callback = self._on_complete_click
        self.add_item(complete_btn)

    async def _on_schedule_click(self, interaction: discord.Interaction):
        result = await _load_authorized_game(interaction, self.game_id)
        if result is None:
            return
        season, week, game, roster = result

        game["scheduled"] = True
        save_season(season)

        week_data = season["weeks"][str(week)]
        relevant_deadline = week_data.get("deadline") if game["type"] == "user" else None

        embed, file = await self.cog.build_game_embed(game, week, roster, deadline=relevant_deadline)
        new_view = CompleteGameView(
            cog=self.cog, game_id=self.game_id,
            completed=(game.get("status") == "completed"), scheduled=True, show_schedule_button=True,
        )
        edit_kwargs = {"embed": embed, "view": new_view}
        if file is not None:
            edit_kwargs["attachments"] = [file]
        await interaction.response.edit_message(**edit_kwargs)
        await interaction.followup.send("📅 Marked as scheduled.", ephemeral=True)

    async def _on_complete_click(self, interaction: discord.Interaction):
        result = await _load_authorized_game(interaction, self.game_id)
        if result is None:
            return
        season, week, game, roster = result

        game["status"] = "completed"
        save_season(season)

        week_data = season["weeks"][str(week)]
        relevant_deadline = week_data.get("deadline") if game["type"] == "user" else None

        embed, file = await self.cog.build_game_embed(game, week, roster, deadline=relevant_deadline)
        new_view = CompleteGameView(
            cog=self.cog, game_id=self.game_id,
            completed=True, scheduled=game.get("scheduled", False), show_schedule_button=(game["type"] == "user"),
        )
        edit_kwargs = {"embed": embed, "view": new_view}
        if file is not None:
            edit_kwargs["attachments"] = [file]
        await interaction.response.edit_message(**edit_kwargs)

        if game["type"] == "user" and game.get("thread_id"):
            thread_id = game["thread_id"]
            try:
                thread = self.cog.bot.get_channel(thread_id) or await self.cog.bot.fetch_channel(thread_id)
                await thread.send("✅ This game has been marked completed. This thread will be deleted in 5 minutes.")
            except (discord.NotFound, discord.HTTPException):
                pass

            # Persist the deletion time so it survives a bot restart — the
            # background cleanup_threads loop picks this up, not an in-memory timer.
            delete_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            game["thread_delete_at"] = delete_at
            save_season(season)



class Scheduling(commands.Cog):
    """Season/dynasty lifecycle and weekly scheduling commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.teams = load_teams()
        self.cleanup_threads.start()

    def cog_unload(self):
        self.cleanup_threads.cancel()

    @tasks.loop(minutes=1)
    async def cleanup_threads(self):
        """Checks every minute for any completed user game whose 5-minute
        thread-deletion timer has passed, and deletes that thread. Reading the
        timestamp from disk (rather than an in-memory timer) means a deletion
        that was due during a restart still gets picked up once the bot is
        back online."""
        season = load_season()
        now = datetime.now(timezone.utc)
        changed = False

        for week_data in season.get("weeks", {}).values():
            for g in week_data.get("games", []):
                delete_at_str = g.get("thread_delete_at")
                if not delete_at_str:
                    continue

                delete_at = datetime.fromisoformat(delete_at_str)
                if now < delete_at:
                    continue

                thread_id = g.get("thread_id")
                if thread_id:
                    try:
                        thread = self.bot.get_channel(thread_id) or await self.bot.fetch_channel(thread_id)
                        await thread.delete()
                    except (discord.NotFound, discord.HTTPException):
                        pass

                g["thread_id"] = None
                g["thread_delete_at"] = None
                changed = True

        if changed:
            save_season(season)

    @cleanup_threads.before_loop
    async def before_cleanup_threads(self):
        await self.bot.wait_until_ready()

    def register_active_views(self):
        """Re-registers persistent CompleteGameView buttons for every CPU game
        in the currently active week that isn't marked completed yet. Must be
        called once after the bot logs in, since persistent views don't
        survive a restart on their own."""
        season = load_season()
        current_week = season.get("current_week")
        if current_week is None:
            return

        week_data = season.get("weeks", {}).get(str(current_week))
        if not week_data or week_data.get("status") != "active":
            return

        for g in week_data.get("games", []):
            completed = g.get("status") == "completed"
            scheduled = g.get("scheduled", False)
            is_user_game = g["type"] == "user"
            view = CompleteGameView(
                cog=self, game_id=g["game_id"],
                completed=completed, scheduled=scheduled, show_schedule_button=is_user_game,
            )
            self.bot.add_view(view)

    async def build_game_embed(
        self, game: dict, week: int, roster: dict, deadline: str | None = None, include_image: bool = True
    ) -> tuple[discord.Embed, discord.File | None]:
        """Returns (embed, file). If file is not None, it must be attached to
        the same message via send(file=file) for the embed's image to render —
        the embed references it internally as attachment://matchup.png.
        Pass include_image=False to skip the logo fetch/composite entirely
        (e.g. when only refreshing text on an embed whose image is unchanged)."""
        home = self.teams[game["home"]]
        away = self.teams[game["away"]]
        home_owner_id = roster.get(game["home"], {}).get("user_id")
        away_owner_id = roster.get(game["away"], {}).get("user_id")

        if game["type"] == "user":
            description = f"<@{home_owner_id}> vs <@{away_owner_id}>"
        else:
            if home_owner_id:
                description = f"<@{home_owner_id}> vs CPU"
            elif away_owner_id:
                description = f"CPU vs <@{away_owner_id}>"
            else:
                description = "CPU vs CPU"

        embed = discord.Embed(
            title=f"{home['name']} vs {away['name']}",
            description=description,
            color=int(home["color"], 16) if home.get("color") else discord.Color.default(),
        )

        home_logo = home.get("logoDark") or home.get("logo")
        away_logo = away.get("logoDark") or away.get("logo")

        file = None
        if include_image and home_logo and away_logo:
            file = await build_matchup_file(home_logo, away_logo)

        if include_image:
            if file is not None:
                embed.set_image(url="attachment://matchup.png")
            elif home_logo:
                # Composite failed (network hiccup, bad URL, etc.) — fall back to
                # the original single-team thumbnail rather than no image at all.
                embed.set_thumbnail(url=home_logo)

        footer_text = f"Week {week}"
        if deadline:
            footer_text += f"  •  Due: {deadline}"
        if game["type"] == "user":
            footer_text += "  •  📅 Scheduled" if game.get("scheduled") else "  •  🕓 Unscheduled"
        if game.get("status") == "completed":
            footer_text += "  •  ✅ Completed"
        embed.set_footer(text=footer_text)
        return embed, file

    async def handle_team_vacated(self, abbr: str):
        """If the vacated team has a game in the currently active week, clean
        up the stale embed/thread. A user game with one side now unowned
        becomes a CPU game (old embed+thread removed, reposted in cpu-games).
        A CPU game just gets its embed text refreshed in place."""
        season = load_season()
        current_week = season.get("current_week")
        if current_week is None:
            return

        week_key = str(current_week)
        week_data = season.get("weeks", {}).get(week_key)
        if not week_data or week_data.get("status") != "active":
            return

        roster = load_roster()
        changed = False

        for g in week_data.get("games", []):
            if abbr not in (g["home"], g["away"]):
                continue

            new_type = classify_game(g["home"], g["away"], roster)

            if g["type"] == "user" and new_type == "cpu":
                user_channel = self.bot.get_channel(week_data.get("user_channel_id"))
                cpu_channel = self.bot.get_channel(week_data.get("cpu_channel_id"))

                if g.get("thread_id"):
                    try:
                        thread = self.bot.get_channel(g["thread_id"]) or await self.bot.fetch_channel(g["thread_id"])
                        await thread.delete()
                    except (discord.NotFound, discord.HTTPException):
                        pass

                if user_channel and g.get("message_id"):
                    try:
                        old_msg = await user_channel.fetch_message(g["message_id"])
                        await old_msg.delete()
                    except (discord.NotFound, discord.HTTPException):
                        pass

                g["type"] = "cpu"
                g["thread_id"] = None
                g["message_id"] = None

                if cpu_channel:
                    embed, file = await self.build_game_embed(g, current_week, roster)
                    view = CompleteGameView(cog=self, game_id=g["game_id"])
                    send_kwargs = {"embed": embed, "view": view, "allowed_mentions": discord.AllowedMentions.none()}
                    if file is not None:
                        send_kwargs["file"] = file
                    new_msg = await cpu_channel.send(**send_kwargs)
                    g["message_id"] = new_msg.id

                changed = True

            elif g["type"] == "cpu" and g.get("message_id"):
                cpu_channel = self.bot.get_channel(week_data.get("cpu_channel_id"))
                if cpu_channel:
                    try:
                        msg = await cpu_channel.fetch_message(g["message_id"])
                        embed, file = await self.build_game_embed(g, current_week, roster)
                        edit_kwargs = {"embed": embed}
                        if file is not None:
                            edit_kwargs["attachments"] = [file]
                        await msg.edit(**edit_kwargs)
                    except (discord.NotFound, discord.HTTPException):
                        pass
                changed = True

        if changed:
            save_season(season)

    async def team_autocomplete(self, interaction: discord.Interaction, current: str):
        current_lower = current.lower()
        matches = [
            t for abbr, t in self.teams.items()
            if current_lower in t["name"].lower() or current_lower in abbr.lower()
        ]
        return [app_commands.Choice(name=t["name"], value=t["abbr"]) for t in matches[:25]]

    # ---------- Game staging commands ----------

    @app_commands.command(name="add_game", description="Add a matchup to a week (admin only)")
    @app_commands.describe(
        target="Which week to add this game to",
        home="Home team",
        away="Away team",
    )
    @app_commands.choices(target=[
        app_commands.Choice(name="Current Week", value="current"),
        app_commands.Choice(name="Next Week", value="next"),
    ])
    async def add_game(self, interaction: discord.Interaction, target: str, home: str, away: str):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can add games.")
            return

        home_abbr, home_error = resolve_team(home, self.teams)
        away_abbr, away_error = resolve_team(away, self.teams)

        if home_error or away_error:
            message = "Couldn't add that game:\n"
            if home_error:
                message += f"- Home: {home_error}\n"
            if away_error:
                message += f"- Away: {away_error}\n"
            await send_ephemeral(interaction, message)
            return

        if home_abbr == away_abbr:
            await send_ephemeral(interaction, "A team can't play itself.")
            return

        season = load_season()
        week, week_label = resolve_target_week(season, target)
        week_key = str(week)

        week_data = season.setdefault("weeks", {}).setdefault(week_key, {
            "status": "upcoming",
            "category_id": None,
            "user_channel_id": None,
            "cpu_channel_id": None,
            "games": [],
        })

        if week_data.get("status") == "active":
            await send_ephemeral(interaction, f"Week {week} is already active. Games can't be added to a live week.")
            return

        # Prevent double-booking a team in the same week
        for g in week_data["games"]:
            if home_abbr in (g["home"], g["away"]) or away_abbr in (g["home"], g["away"]):
                conflict = home_abbr if home_abbr in (g["home"], g["away"]) else away_abbr
                await send_ephemeral(interaction, f"`{conflict}` is already scheduled in a game this week.")
                return

        roster = load_roster()
        game_type = classify_game(home_abbr, away_abbr, roster)
        game_number = len(week_data["games"]) + 1

        week_data["games"].append({
            "game_id": f"w{week}_g{game_number}",
            "home": home_abbr,
            "away": away_abbr,
            "type": game_type,
            "status": "scheduled",
            "scheduled": False,
            "thread_id": None,
            "message_id": None,
        })

        # Create the UPCOMING category in Discord when first game is staged for a week
        if not week_data.get("category_id"):
            guild = interaction.guild
            category = await guild.create_category(f"🏈 WEEK {week} - UPCOMING")
            week_data["category_id"] = category.id

        save_season(season)

        await send_ephemeral(
            interaction,
            f"Added to **{week_label}**: {self.teams[home_abbr]['name']} vs "
            f"{self.teams[away_abbr]['name']} ({game_type.upper()})\n"
            f"Run `/view_week` to see the full staged list."
        )

    @add_game.autocomplete("home")
    async def add_game_home_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.team_autocomplete(interaction, current)

    @add_game.autocomplete("away")
    async def add_game_away_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.team_autocomplete(interaction, current)

    @app_commands.command(name="remove_game", description="Remove a staged matchup from a week (admin only)")
    @app_commands.describe(target="Which week to remove from", game="The matchup to remove")
    @app_commands.choices(target=[
        app_commands.Choice(name="Current Week", value="current"),
        app_commands.Choice(name="Next Week", value="next"),
    ])
    async def remove_game(self, interaction: discord.Interaction, target: str, game: str):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can remove games.")
            return

        season = load_season()
        week, week_label = resolve_target_week(season, target)
        week_data = season.get("weeks", {}).get(str(week))

        if not week_data:
            await send_ephemeral(interaction, f"No staged games found for {week_label}.")
            return
        if week_data.get("status") == "active":
            await send_ephemeral(interaction, f"Week {week} is already active. Games can't be removed from a live week.")
            return

        before_count = len(week_data["games"])
        week_data["games"] = [g for g in week_data["games"] if g["game_id"] != game]

        if len(week_data["games"]) == before_count:
            await send_ephemeral(interaction, "Couldn't find that matchup to remove.")
            return

        save_season(season)
        await send_ephemeral(interaction, f"Removed that matchup from {week_label}.")

    @remove_game.autocomplete("game")
    async def remove_game_autocomplete(self, interaction: discord.Interaction, current: str):
        season = load_season()
        target = getattr(interaction.namespace, "target", "current")
        week, _ = resolve_target_week(season, target or "current")
        week_data = season.get("weeks", {}).get(str(week), {})
        current_lower = current.lower()
        choices = []
        for g in week_data.get("games", []):
            label = f"{self.teams[g['home']]['name']} vs {self.teams[g['away']]['name']} ({g['type'].upper()})"
            if current_lower in label.lower():
                choices.append(app_commands.Choice(name=label, value=g["game_id"]))
        return choices[:25]

    @app_commands.command(name="view_week", description="Preview a week's staged games")
    @app_commands.describe(target="Which week to view")
    @app_commands.choices(target=[
        app_commands.Choice(name="Current Week", value="current"),
        app_commands.Choice(name="Next Week", value="next"),
    ])
    async def view_week(self, interaction: discord.Interaction, target: str):
        season = load_season()
        week, week_label = resolve_target_week(season, target)
        week_data = season.get("weeks", {}).get(str(week))

        if not week_data or not week_data.get("games"):
            await send_ephemeral(interaction, f"No games staged for {week_label} yet.")
            return

        status = week_data.get("status", "upcoming").upper()
        lines = [f"**{week_label}** — `{status}`\n"]
        for g in week_data["games"]:
            home = self.teams[g["home"]]["name"]
            away = self.teams[g["away"]]["name"]
            lines.append(f"- {home} vs {away} ({g['type'].upper()})")

        await send_ephemeral(interaction, "\n".join(lines))

    @app_commands.command(name="advance_week", description="Advance to the next week or stage (admin only)")
    async def advance_week(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can advance the week.")
            return

        season = load_season()
        info = get_phase(season.get("current_stage", "preseason"))
        if not info.get("has_weeks") and not info.get("next"):
            await send_ephemeral(interaction, "No next stage defined from the current stage.")
            return

        wizard = AdvanceWeekWizard(bot=self.bot, cog=self)
        await wizard.start(interaction)

    # ---------- Dynasty/season lifecycle commands ----------

    @app_commands.command(name="post_dashboard", description="Set this channel as the live league status dashboard (admin only)")
    async def post_dashboard(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can do that.")
            return

        settings = load_settings()
        settings["dashboard_channel_id"] = interaction.channel_id
        settings.pop("dashboard_message_id", None)  # force a fresh message in the new channel
        save_settings(settings)

        await send_ephemeral(interaction, "This channel is now the league status dashboard.")
        await refresh_dashboard(self.bot)

    @app_commands.command(name="dynasty_info", description="Show the current dynasty year and season status")
    async def dynasty_info(self, interaction: discord.Interaction):
        season = load_season()
        year = season.get("year") or "Not set yet"
        current_stage = season.get("current_stage", "preseason")
        stage = PHASE_DISPLAY.get(current_stage, current_stage)
        current_week = season.get("current_week")
        week_text = f"Week {current_week}" if current_week is not None else "No active week"

        await send_ephemeral(interaction, f"**Dynasty Year:** {year}\n**Stage:** {stage}\n**{week_text}**")

    @app_commands.command(name="advance_season", description="Move to the next year, reset to Preseason (admin only)")
    async def advance_season(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can advance the season.")
            return

        season = load_season()
        current_year = season.get("year")

        if current_year is None:
            await send_ephemeral(
                interaction, "No dynasty year is set yet. Run `/new_dynasty` first to start one."
            )
            return

        new_year = current_year + 1
        view = AdvanceSeasonConfirmView(new_year=new_year)
        await send_ephemeral(
            interaction,
            f"This will archive **{current_year}**'s season data and advance to **{new_year}**:\n"
            f"- Stage resets to Preseason, no active week\n"
            f"- Team assignments are **kept** \u2014 owners stay with their teams\n\n"
            f"Are you sure?",
            view=view,
        )

    @app_commands.command(name="new_dynasty", description="Start a fresh dynasty for a new year (admin only)")
    @app_commands.describe(year="The new dynasty year, e.g. 2027")
    async def new_dynasty(self, interaction: discord.Interaction, year: int):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can start a new dynasty.")
            return

        current_season = load_season()
        current_year = current_season.get("year") or "unset"
        claimed_count = len(load_roster())

        view = NewDynastyConfirmView(new_year=year)
        await send_ephemeral(
            interaction,
            f"⚠️ This will archive the current dynasty (year: `{current_year}`, "
            f"{claimed_count} team(s) claimed) and reset everything for **{year}**:\n"
            f"- All team assignments will be cleared\n"
            f"- The season will reset to Preseason, Week 0\n"
            f"- Previous weeks' Discord channels/categories are **not** deleted automatically\n\n"
            f"Are you sure?",
            view=view,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Scheduling(bot))
