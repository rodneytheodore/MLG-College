import json
import os

import discord
from discord import app_commands
from discord.ext import commands

from utils.data import is_admin, load_roster, save_roster, load_teams, load_teams_by_conference
from utils.responses import send_ephemeral, send_ephemeral_followup
from cogs.scheduling import refresh_dashboard, find_mlg_mention

DATA_DIR = os.environ.get("DATA_DIR", "data").strip()
DRAFT_PATH_NAME = "draft.json"
MAX_TEAMS = 32


def load_draft() -> dict:
    path = os.path.join(DATA_DIR, DRAFT_PATH_NAME)
    if not os.path.exists(path):
        return {"order": [], "current_pick": 0, "status": "not_set"}
    with open(path) as f:
        return json.load(f)


def save_draft(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, DRAFT_PATH_NAME)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


DRAFT_CHANNEL_NAMES = ("team-draft", "dynasty-team-draft")


async def _post_draft_order(guild: discord.Guild, embeds: list[discord.Embed]) -> discord.TextChannel | None:
    """Finds the draft channel by name (case-insensitive) and posts the embeds there.
    Returns the channel if found and posted, else None."""
    channel = discord.utils.find(
        lambda c: isinstance(c, discord.TextChannel) and c.name.lower() in DRAFT_CHANNEL_NAMES,
        guild.channels,
    )
    if channel is None:
        return None

    await channel.purge(limit=50, check=lambda m: m.author == guild.me)
    await channel.send(embeds=embeds)
    return channel


def build_draft_order_embed(draft: dict) -> discord.Embed:
    order = draft.get("order", [])
    current_pick = draft.get("current_pick", 0)
    status = draft.get("status")

    lines_out = []
    for i, entry in enumerate(order):
        marker = "➡️ " if i == current_pick and status == "drafting" else ""
        picked_note = ""
        if status in ("drafting", "complete") and i < current_pick:
            team_abbr = entry.get("picked_team")
            if team_abbr:
                picked_note = f" — **{team_abbr}**"
        lines_out.append(f"{marker}`{i + 1:02d}` <@{entry['user_id']}>{picked_note}")

    waitlist = draft.get("waitlist", [])
    show_waitlist = waitlist and status not in ("drafting", "complete")
    if show_waitlist:
        lines_out.append("")
        lines_out.append("**Waitlist:**")
        for i, entry in enumerate(waitlist):
            lines_out.append(f"`{i + 1:02d}` <@{entry['user_id']}>")

    embed = discord.Embed(
        title="🏈 Draft Order" if status != "complete" else "🏈 Draft Complete",
        description="\n".join(lines_out),
        color=discord.Color.green() if status == "complete" else discord.Color.gold(),
    )
    claimed_count = sum(1 for e in order if e.get("picked_team"))
    footer = f"{claimed_count}/{len(order)} teams claimed"
    if show_waitlist:
        footer += f" · {len(waitlist)} on waitlist"
    embed.set_footer(text=footer)
    return embed


def build_eligible_teams_embed(draft: dict) -> discord.Embed:
    """Running list of every eligible team, grouped by conference, showing
    who picked it (if anyone). Falls back to the full team pool when no
    eligible_teams restriction is set."""
    by_conf = load_teams_by_conference()
    eligible = draft.get("eligible_teams")
    eligible_set = set(eligible) if eligible else None

    picked_map = {
        entry["picked_team"]: entry["user_id"]
        for entry in draft.get("order", [])
        if entry.get("picked_team")
    }

    total_teams = 0
    section_blocks = []

    for conf in sorted(by_conf.keys()):
        conf_teams = by_conf[conf]
        if eligible_set is not None:
            conf_teams = [t for t in conf_teams if t["abbr"].upper() in eligible_set]
        if not conf_teams:
            continue

        conf_teams_sorted = sorted(conf_teams, key=lambda t: t.get("school") or t["name"])
        lines = []
        for t in conf_teams_sorted:
            abbr = t["abbr"].upper()
            name = t.get("school") or t["name"]
            total_teams += 1
            if abbr in picked_map:
                lines.append(f"✅ {name} — <@{picked_map[abbr]}>")
            else:
                lines.append(f"⬜ {name}")

        section_blocks.append(f"**{conf}**\n" + "\n".join(lines))

    description = "\n\n".join(section_blocks) if section_blocks else "*(no eligible teams set)*"

    embed = discord.Embed(title="🏈 Eligible Teams", description=description, color=discord.Color.gold())
    embed.set_footer(text=f"{len(picked_map)}/{total_teams} teams picked")
    return embed


def build_pick_announcement_embed(team_info: dict, member: discord.Member, pick_number: int) -> discord.Embed:
    """Posted to #team-draft the moment a pick is made."""
    embed = discord.Embed(
        title=f"🏈 Pick {pick_number} — {team_info['name']}",
        description=f"Drafted by {member.mention}",
        color=int(team_info["color"], 16) if team_info.get("color") else discord.Color.gold(),
    )
    logo = team_info.get("logoDark") or team_info.get("logo")
    if logo:
        embed.set_thumbnail(url=logo)
    return embed


# ---- Step 1: how many teams (modal — just a number, no lookup needed) ----

class TeamCountModal(discord.ui.Modal, title="Draft Setup — Team Count"):
    team_count_input = discord.ui.TextInput(
        label="Number of teams drafting",
        placeholder=f"e.g. 32 (max {MAX_TEAMS})",
        required=True,
        max_length=2,
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.team_count_input.value.strip()

        if not raw.isdigit():
            await interaction.response.send_message("Please enter a whole number.", ephemeral=True)
            return

        count = int(raw)
        if count < 2 or count > MAX_TEAMS:
            await interaction.response.send_message(
                f"Number of teams must be between 2 and {MAX_TEAMS}.", ephemeral=True
            )
            return

        wizard = DraftOrderWizard(team_count=count)
        await wizard.start(interaction)


# ---- Step 2: sequential native user picker, one pick at a time ----

class DraftOrderWizard:
    def __init__(self, team_count: int):
        self.team_count = team_count
        self.picks: list[discord.Member] = []

    async def start(self, interaction: discord.Interaction):
        view = PickUserView(self)
        await interaction.response.send_message(
            f"**Pick 1 of {self.team_count}** — who picks first?",
            view=view,
            ephemeral=True,
        )

    async def _advance(self, interaction: discord.Interaction):
        next_pick_number = len(self.picks) + 1

        if next_pick_number > self.team_count:
            await self._finish(interaction)
            return

        view = PickUserView(self)
        await interaction.response.edit_message(
            content=f"**Pick {next_pick_number} of {self.team_count}** — who's next?",
            view=view,
        )

    async def _finish(self, interaction: discord.Interaction):
        draft = load_draft()
        draft.update({
            "team_count": self.team_count,
            "order": [{"user_id": m.id, "username": str(m)} for m in self.picks],
            "current_pick": 0,
            "status": "order_set",
        })
        save_draft(draft)

        embed = build_draft_order_embed(draft)
        posted_channel = await _post_draft_order(interaction.guild, [embed, build_eligible_teams_embed(draft)])

        team_draft_ref = f"<#{posted_channel.id}>" if posted_channel else "#team-draft"
        eligible = draft.get("eligible_teams")
        if eligible:
            eligible_note = f"**{len(eligible)} eligible teams** have been set for this draft."
        else:
            eligible_note = "No eligible teams restriction has been set — the full team pool is currently in play."

        waitlist = draft.get("waitlist", [])
        waitlist_note = ""
        if waitlist:
            waitlist_note = (
                f"\n\nTo everyone on the waitlist — the participant list was a tough decision, and we know "
                f"it doesn't feel great to be on the outside looking in. Stick around, though — spots in this "
                f"league always open up, and we'd love to have you in when one does."
            )

        if posted_channel:
            await posted_channel.send(
                f"**How the draft works:**\n"
                f"• When it's your turn, you'll get pinged here with a **Make Your Pick** button.\n"
                f"• Click it, pick a conference, then pick your team from that conference.\n"
                f"• Only the person on the clock can pick — the draft won't let anyone go out of turn.\n"
                f"• Once you pick, the draft automatically moves to the next person."
            )

        ann_channel = discord.utils.find(
            lambda c: c.name.lower() in ("announcements", "announcement"),
            interaction.guild.text_channels,
        )
        if ann_channel:
            mlg_mention = find_mlg_mention(interaction.guild)
            prefix = f"{mlg_mention}\n" if mlg_mention else ""
            await ann_channel.send(
                f"{prefix}📋 **Draft order is set!** {len(self.picks)} participant(s) are locked in.\n"
                f"{eligible_note} Head to {team_draft_ref} to see the full draft order and browse eligible teams."
                f"{waitlist_note}",
                allowed_mentions=discord.AllowedMentions(roles=True),
            )

        warning = ""
        eligible = draft.get("eligible_teams")
        if eligible and len(eligible) != self.team_count:
            warning = (
                f"\n⚠️ **{len(eligible)} eligible teams** were set previously, but this draft has "
                f"**{self.team_count} participants**. Run `/set_eligible_teams` again to fix the mismatch."
            )

        if posted_channel:
            summary = f"✅ **Draft order set.** Posted to {posted_channel.mention}.{warning}"
        else:
            summary = (
                "✅ **Draft order set,** but no `#team-draft` (or `#dynasty-team-draft`) "
                f"channel was found. Create one and re-run `/view_draft_order` to post it there.{warning}"
            )

        await interaction.response.edit_message(content=summary, embed=embed, view=None)


class PickUserView(discord.ui.View):
    def __init__(self, wizard: DraftOrderWizard):
        super().__init__(timeout=300)
        self.wizard = wizard

        select = discord.ui.UserSelect(
            placeholder="Search for a user...",
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

        cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _on_select(self, interaction: discord.Interaction):
        # discord.py resolves UserSelect values directly on the component
        member = self.children[0].values[0]

        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Couldn't resolve that selection to a server member. Try again.", ephemeral=True
            )
            return

        if any(m.id == member.id for m in self.wizard.picks):
            await interaction.response.send_message(
                f"{member.mention} is already in the draft order. Pick someone else.",
                ephemeral=True,
            )
            return

        self.wizard.picks.append(member)
        await self.wizard._advance(interaction)

    async def _on_cancel(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Draft order setup cancelled. Nothing was saved.", view=self
        )


async def _announce_current_pick(channel: discord.TextChannel, draft: dict):
    """Posts the on-the-clock ping with a Make Your Pick button for the current picker."""
    order = draft["order"]
    current_pick = draft["current_pick"]
    picker_id = order[current_pick]["user_id"]
    view = DraftPickButtonView()
    await channel.send(
        content=f"<@{picker_id}> you're on the clock! Pick {current_pick + 1} of {len(order)}.",
        view=view,
    )


def _eligible_set(draft: dict) -> set[str] | None:
    """Returns the eligible abbr set, or None if no restriction is set (all teams eligible)."""
    eligible = draft.get("eligible_teams")
    return set(eligible) if eligible else None


def _abbr_to_conference() -> dict[str, str]:
    by_conf = load_teams_by_conference()
    mapping = {}
    for conf, teams in by_conf.items():
        for t in teams:
            mapping[t["abbr"].upper()] = conf
    return mapping


def _picked_counts_by_conference(draft: dict) -> dict[str, int]:
    """How many picks have already been made from each conference, based on
    the draft order's picked_team fields (not the whole league roster —
    this stays accurate even if /assign_team was used outside the draft)."""
    abbr_to_conf = _abbr_to_conference()
    counts: dict[str, int] = {}
    for entry in draft.get("order", []):
        abbr = entry.get("picked_team")
        if not abbr:
            continue
        conf = abbr_to_conf.get(abbr.upper())
        if conf:
            counts[conf] = counts.get(conf, 0) + 1
    return counts


def _effective_conference_limits(draft: dict) -> dict[str, dict]:
    """Derives per-conference {min, max} from the single global min/max,
    clamped to how many eligible teams actually exist in each conference
    (so a global min never exceeds what a conference can supply)."""
    global_min = draft.get("team_min_per_conference")
    global_max = draft.get("team_max_per_conference")
    if global_min is None and global_max is None:
        return {}

    eligible = draft.get("eligible_teams")
    if not eligible:
        return {}

    abbr_to_conf = _abbr_to_conference()
    eligible_counts: dict[str, int] = {}
    for abbr in eligible:
        conf = abbr_to_conf.get(abbr.upper())
        if conf:
            eligible_counts[conf] = eligible_counts.get(conf, 0) + 1

    limits = {}
    for conf, count in eligible_counts.items():
        eff_min = min(global_min if global_min is not None else 0, count)
        eff_max = min(global_max if global_max is not None else count, count)
        limits[conf] = {"min": eff_min, "max": eff_max}
    return limits


def _teams_by_conference_available(draft: dict) -> dict[str, list[dict]]:
    """Conference -> list of team dicts, filtered to unclaimed, eligible,
    and NOT locked out by having already hit its configured max."""
    by_conf = load_teams_by_conference()
    roster = load_roster()
    eligible = _eligible_set(draft)
    limits = _effective_conference_limits(draft)
    counts = _picked_counts_by_conference(draft)

    result = {}
    for conf, teams in by_conf.items():
        max_cap = limits.get(conf, {}).get("max")
        if max_cap is not None and counts.get(conf, 0) >= max_cap:
            continue  # conference locked — max already reached

        avail = [
            t for t in teams
            if t["abbr"].upper() not in roster
            and (eligible is None or t["abbr"].upper() in eligible)
        ]
        if avail:
            result[conf] = avail
    return result


def _available_conferences(draft: dict) -> list[str]:
    """Conferences that still have at least one unclaimed, eligible, unlocked team."""
    return list(_teams_by_conference_available(draft).keys())


def _min_deficit_after_pick(draft: dict, picked_conference: str | None) -> int:
    """Total remaining minimum-team obligations across all conferences,
    assuming the hypothetical pick from picked_conference has just happened.
    Used to block a pick that would make some other conference's minimum
    impossible to reach with the slots left."""
    limits = _effective_conference_limits(draft)
    counts = _picked_counts_by_conference(draft)
    if picked_conference:
        counts[picked_conference] = counts.get(picked_conference, 0) + 1

    deficit = 0
    for conf, lim in limits.items():
        min_v = lim.get("min", 0)
        deficit += max(0, min_v - counts.get(conf, 0))
    return deficit


async def _finalize_pick(interaction: discord.Interaction, abbr: str):
    """Reloads draft state fresh, re-validates, and completes the pick.
    Called from the team select — this is the single source of truth for
    committing a pick, whichever path got here."""
    draft = load_draft()

    if draft.get("status") != "drafting":
        await interaction.response.edit_message(content="The draft isn't currently in progress.", view=None)
        return

    order = draft.get("order", [])
    current_pick = draft.get("current_pick", 0)

    if current_pick >= len(order):
        await interaction.response.edit_message(content="The draft is already complete.", view=None)
        return

    expected_user_id = order[current_pick]["user_id"]
    if interaction.user.id != expected_user_id:
        await interaction.response.edit_message(
            content=f"It's not your turn. <@{expected_user_id}> is currently picking.", view=None
        )
        return

    teams = load_teams()
    if abbr not in teams:
        await interaction.response.edit_message(content="That team couldn't be found. Try again.", view=None)
        return

    eligible = _eligible_set(draft)
    if eligible is not None and abbr not in eligible:
        await interaction.response.edit_message(
            content=f"`{abbr}` isn't in the eligible teams list for this draft.", view=None
        )
        return

    roster = load_roster()
    if abbr in roster:
        owner_id = roster[abbr]["user_id"]
        await interaction.response.edit_message(
            content=f"`{abbr}` was just claimed by <@{owner_id}>. Click **Make Your Pick** again to choose another team.",
            view=None,
        )
        return

    abbr_to_conf = _abbr_to_conference()
    picked_conference = abbr_to_conf.get(abbr)

    limits = _effective_conference_limits(draft)
    conf_max = limits.get(picked_conference, {}).get("max") if picked_conference else None
    if conf_max is not None:
        current_conf_count = _picked_counts_by_conference(draft).get(picked_conference, 0)
        if current_conf_count >= conf_max:
            await interaction.response.edit_message(
                content=f"**{picked_conference}** has already reached its max of {conf_max} team(s). "
                        f"Click **Make Your Pick** again to choose a different conference.",
                view=None,
            )
            return

    remaining_slots_after = len(order) - (current_pick + 1)
    deficit_after = _min_deficit_after_pick(draft, picked_conference)
    if deficit_after > remaining_slots_after:
        await interaction.response.edit_message(
            content=(
                f"Picking **{teams[abbr]['name']}** would leave only {remaining_slots_after} pick(s) remaining, "
                f"but {deficit_after} more pick(s) are still required to satisfy conference minimums. "
                f"Click **Make Your Pick** again and choose a team from a conference that still needs coverage."
            ),
            view=None,
        )
        return

    roster[abbr] = {"user_id": interaction.user.id, "username": str(interaction.user)}
    save_roster(roster)

    order[current_pick]["picked_team"] = abbr
    draft["current_pick"] = current_pick + 1
    if draft["current_pick"] >= len(order):
        draft["status"] = "complete"
    save_draft(draft)

    bot = interaction.client
    roster_cog = bot.get_cog("Roster")
    if roster_cog is not None:
        await roster_cog.refresh_roster_channel()
    await refresh_dashboard(bot)

    team_name = teams[abbr]["name"]
    await interaction.response.edit_message(content=f"✅ You picked **{team_name}**!", view=None)

    channel = discord.utils.find(
        lambda c: isinstance(c, discord.TextChannel) and c.name.lower() in DRAFT_CHANNEL_NAMES,
        interaction.guild.channels,
    )
    if channel is None:
        return

    pick_number = current_pick + 1
    await channel.send(embed=build_pick_announcement_embed(teams[abbr], interaction.user, pick_number))
    await channel.send(embeds=[build_draft_order_embed(draft), build_eligible_teams_embed(draft)])

    if draft["status"] == "complete":
        await channel.send("🎉 **The draft is complete!** All teams have been claimed.")
    else:
        await _announce_current_pick(channel, draft)


class TeamPickSelectView(discord.ui.View):
    """Step 2 — pick an unclaimed team within the chosen conference."""

    def __init__(self, conference: str, available_teams: list[dict]):
        super().__init__(timeout=120)
        self.conference = conference

        options = [
            discord.SelectOption(label=t["name"][:100], value=t["abbr"][:100])
            for t in available_teams[:25]
        ]
        select = discord.ui.Select(placeholder="Select your team...", min_values=1, max_values=1, options=options)
        select.callback = self._on_select
        self.add_item(select)

        back_btn = discord.ui.Button(label="← Back to Conferences", style=discord.ButtonStyle.secondary)
        back_btn.callback = self._on_back
        self.add_item(back_btn)

    async def _on_select(self, interaction: discord.Interaction):
        abbr = self.children[0].values[0]
        await _finalize_pick(interaction, abbr)

    async def _on_back(self, interaction: discord.Interaction):
        draft = load_draft()
        conferences = _available_conferences(draft)
        view = ConferencePickView(conferences)
        await interaction.response.edit_message(content="**Select a conference:**", view=view)


class ConferencePickView(discord.ui.View):
    """Step 1 — pick a conference, filtered to only those with unclaimed teams."""

    def __init__(self, conferences: list[str]):
        super().__init__(timeout=120)
        options = [discord.SelectOption(label=conf[:100], value=conf[:100]) for conf in conferences[:25]]
        select = discord.ui.Select(placeholder="Select a conference...", min_values=1, max_values=1, options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        conference = self.children[0].values[0]

        # Re-validate turn fresh before showing teams, in case state changed
        # since the button was clicked.
        draft = load_draft()
        if draft.get("status") != "drafting":
            await interaction.response.edit_message(content="The draft isn't currently in progress.", view=None)
            return
        order = draft.get("order", [])
        current_pick = draft.get("current_pick", 0)
        if current_pick >= len(order) or interaction.user.id != order[current_pick]["user_id"]:
            await interaction.response.edit_message(content="It's no longer your turn.", view=None)
            return

        by_conf_available = _teams_by_conference_available(draft)
        available = by_conf_available.get(conference, [])

        if not available:
            conferences = _available_conferences(draft)
            view = ConferencePickView(conferences)
            await interaction.response.edit_message(
                content=f"No teams left in **{conference}**. Pick a different conference:",
                view=view,
            )
            return

        view = TeamPickSelectView(conference, available)
        await interaction.response.edit_message(content=f"**{conference}** — pick your team:", view=view)


class DraftPickButtonView(discord.ui.View):
    """Persistent button posted alongside each on-the-clock announcement.
    Reloads draft state fresh on every click, so it works correctly even
    after a bot restart or if multiple messages are visible at once."""

    def __init__(self):
        super().__init__(timeout=None)
        btn = discord.ui.Button(
            label="Make Your Pick",
            style=discord.ButtonStyle.success,
            custom_id="draft_make_pick",
        )
        btn.callback = self._on_click
        self.add_item(btn)

    async def _on_click(self, interaction: discord.Interaction):
        draft = load_draft()

        if draft.get("status") != "drafting":
            await interaction.response.send_message("The draft isn't currently in progress.", ephemeral=True)
            return

        order = draft.get("order", [])
        current_pick = draft.get("current_pick", 0)

        if current_pick >= len(order):
            await interaction.response.send_message("The draft is already complete.", ephemeral=True)
            return

        expected_user_id = order[current_pick]["user_id"]
        if interaction.user.id != expected_user_id:
            await interaction.response.send_message(
                f"It's not your turn. <@{expected_user_id}> is currently picking.", ephemeral=True
            )
            return

        conferences = _available_conferences(draft)
        if not conferences:
            await interaction.response.send_message("No teams are currently available to pick.", ephemeral=True)
            return

        view = ConferencePickView(conferences)
        await interaction.response.send_message("**Select a conference:**", view=view, ephemeral=True)


def _finish_eligible_teams(
    draft: dict, selected_abbrs: list[str], global_min: int, global_max: int
) -> tuple[discord.Embed, str]:
    """Saves eligible_teams + global min/max and builds the confirmation embed + optional warning text."""
    draft["eligible_teams"] = selected_abbrs
    draft["team_min_per_conference"] = global_min
    draft["team_max_per_conference"] = global_max
    save_draft(draft)

    teams = load_teams()
    if selected_abbrs:
        lines_out = "\n".join(f"`{i + 1:02d}` {teams[a]['name']}" for i, a in enumerate(selected_abbrs))
    else:
        lines_out = "*(none selected — draft pool restriction cleared)*"

    embed = discord.Embed(title="🏈 Eligible Teams Set", description=lines_out, color=discord.Color.blurple())
    embed.set_footer(
        text=f"{len(selected_abbrs)} teams — {global_min}-{global_max} teams per conference"
    )

    warning = ""
    existing_order = draft.get("order")
    team_count = draft.get("team_count")
    if existing_order and team_count and team_count != len(selected_abbrs):
        warning = (
            f"⚠️ Draft order is already set for **{team_count}** participants — "
            f"this list has **{len(selected_abbrs)}**. Adjust one to match."
        )

    return embed, warning


class EligibleTeamsWizard:
    """First sets a single global min/max eligible-team range, then walks
    through admin-chosen conferences one at a time, multi-selecting teams
    within that range (clamped per conference to however many teams it has)."""

    def __init__(self, conferences: list[str]):
        self.conferences = conferences
        self.index = 0
        self.selected_abbrs: list[str] = []
        self.global_min = 0
        self.global_max = 0
        self.by_conf = load_teams_by_conference()

    def current_conference(self) -> str:
        return self.conferences[self.index]

    def team_count_for(self, conference: str) -> int:
        return len(self.by_conf.get(conference, []))

    def max_possible_across_selected(self) -> int:
        return max((self.team_count_for(c) for c in self.conferences), default=0)

    async def start(self, interaction: discord.Interaction):
        view = GlobalMinMaxView(self)
        await interaction.response.send_message(
            "**Set a global min/max eligible teams per conference:**", view=view, ephemeral=True
        )

    async def start_conference_selection(self, interaction: discord.Interaction, min_v: int, max_v: int):
        self.global_min = min_v
        self.global_max = max_v
        await self._show_team_select(interaction, first=True)

    async def _show_team_select(self, interaction: discord.Interaction, first: bool = False):
        conference = self.current_conference()
        teams = self.by_conf.get(conference, [])

        if not teams:
            # No teams exist in this conference at all — nothing to select.
            await self.after_conference_selection(interaction, [])
            return

        view = ConferenceTeamMultiSelectView(self, conference, teams, self.global_min, self.global_max)
        content = (
            f"**{conference}** ({self.index + 1}/{len(self.conferences)}) — select eligible teams. "
            f"*(The draft will still cap this conference at {self.global_min}-{self.global_max} pick(s) — "
            f"you can add more teams here than that to give a bigger pool to choose from.)*"
        )
        await interaction.response.edit_message(content=content, view=view)

    async def after_conference_selection(
        self, interaction: discord.Interaction, selected: list[str], first: bool = False
    ):
        """Called once a conference's teams have been chosen (or skipped)."""
        self.selected_abbrs.extend(selected)
        self.index += 1
        if self.index >= len(self.conferences):
            await self._finish(interaction)
            return
        await self._show_team_select(interaction)

    async def _finish(self, interaction: discord.Interaction):
        draft = load_draft()
        embed, warning = _finish_eligible_teams(draft, self.selected_abbrs, self.global_min, self.global_max)
        await interaction.response.edit_message(content=warning or None, embed=embed, view=None)


class GlobalMinMaxView(discord.ui.View):
    """Step 2 — pick ONE min/max eligible team range that applies uniformly
    to every conference chosen in step 1."""

    def __init__(self, wizard: EligibleTeamsWizard):
        super().__init__(timeout=180)
        self.wizard = wizard

        max_possible = wizard.max_possible_across_selected()
        self.min_value = 0
        self.max_value = max_possible

        count_options = [discord.SelectOption(label=str(i), value=str(i)) for i in range(max_possible + 1)]

        self.min_select = discord.ui.Select(
            placeholder="Minimum teams per conference (default 0)",
            min_values=1, max_values=1, options=count_options, row=0,
        )
        self.min_select.callback = self._on_min
        self.add_item(self.min_select)

        self.max_select = discord.ui.Select(
            placeholder=f"Maximum teams per conference (default {max_possible})",
            min_values=1, max_values=1, options=count_options, row=1,
        )
        self.max_select.callback = self._on_max
        self.add_item(self.max_select)

        confirm_btn = discord.ui.Button(label="Continue →", style=discord.ButtonStyle.primary, row=2)
        confirm_btn.callback = self._on_confirm
        self.add_item(confirm_btn)

    async def _on_min(self, interaction: discord.Interaction):
        self.min_value = int(self.min_select.values[0])
        await interaction.response.defer()

    async def _on_max(self, interaction: discord.Interaction):
        self.max_value = int(self.max_select.values[0])
        await interaction.response.defer()

    async def _on_confirm(self, interaction: discord.Interaction):
        if self.min_value > self.max_value:
            await interaction.response.send_message(
                f"Minimum ({self.min_value}) can't exceed maximum ({self.max_value}). Adjust and try again.",
                ephemeral=True,
            )
            return

        await self.wizard.start_conference_selection(interaction, self.min_value, self.max_value)


class ConferenceTeamMultiSelectView(discord.ui.View):
    """Step 3 (repeated per conference) — multi-select which teams from this
    conference go into the eligible pool. Unconstrained by the draft-time
    min/max — the pool can be larger than what's actually draftable."""

    def __init__(self, wizard: EligibleTeamsWizard, conference: str, teams: list[dict], draft_min: int, draft_max: int):
        super().__init__(timeout=180)
        self.wizard = wizard

        options = [discord.SelectOption(label=t["name"][:100], value=t["abbr"][:100]) for t in teams[:25]]
        select = discord.ui.Select(
            placeholder=f"Select eligible teams in {conference}...",
            min_values=0,
            max_values=max(len(options), 1),
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

        skip_btn = discord.ui.Button(label="Skip this conference", style=discord.ButtonStyle.secondary)
        skip_btn.callback = self._on_skip
        self.add_item(skip_btn)

    async def _on_select(self, interaction: discord.Interaction):
        await self.wizard.after_conference_selection(interaction, self.children[0].values)

    async def _on_skip(self, interaction: discord.Interaction):
        await self.wizard.after_conference_selection(interaction, [])


class EligibleConferenceSelectView(discord.ui.View):
    """Step 1 — multi-select which conferences to pull eligible teams from."""

    def __init__(self):
        super().__init__(timeout=180)
        by_conf = load_teams_by_conference()
        conference_names = list(by_conf.keys())

        options = [discord.SelectOption(label=c[:100], value=c[:100]) for c in conference_names[:25]]
        select = discord.ui.Select(
            placeholder="Select one or more conferences...",
            min_values=1,
            max_values=len(options),
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        chosen_conferences = self.children[0].values
        wizard = EligibleTeamsWizard(chosen_conferences)
        await wizard.start(interaction)


class Draft(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def register_active_views(self):
        """Registers the persistent Make Your Pick button so it keeps working
        after a bot restart. Safe to call even when no draft is active."""
        self.bot.add_view(DraftPickButtonView())

    @app_commands.command(
        name="set_draft_order",
        description="Set the team draft order (admin only)",
    )
    async def set_draft_order(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can set the draft order.")
            return
        await interaction.response.send_modal(TeamCountModal())

    @app_commands.command(
        name="set_eligible_teams",
        description="Restrict which teams are available in the draft (admin only)",
    )
    async def set_eligible_teams(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can set eligible teams.")
            return

        draft = load_draft()
        if draft.get("status") in ("drafting", "complete"):
            await send_ephemeral(interaction, "Can't change eligible teams after the draft has started.")
            return

        await interaction.response.send_message(
            "**Select conferences to pull eligible teams from:**",
            view=EligibleConferenceSelectView(),
            ephemeral=True,
        )

    @app_commands.command(name="view_draft_order", description="View the current draft order")
    async def view_draft_order(self, interaction: discord.Interaction):
        draft = load_draft()
        if not draft.get("order"):
            await send_ephemeral(interaction, "No draft order has been set yet.")
            return

        await send_ephemeral(interaction, embed=build_draft_order_embed(draft))
        await send_ephemeral_followup(interaction, embed=build_eligible_teams_embed(draft))

    @app_commands.command(
        name="post_draft_order",
        description="(Re)post the draft order to the team-draft channel (admin only)",
    )
    async def post_draft_order(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can do that.")
            return

        draft = load_draft()
        if not draft.get("order"):
            await send_ephemeral(interaction, "No draft order has been set yet.")
            return

        posted_channel = await _post_draft_order(
            interaction.guild, [build_draft_order_embed(draft), build_eligible_teams_embed(draft)]
        )
        if posted_channel:
            await send_ephemeral(interaction, f"Posted to {posted_channel.mention}.")
        else:
            await send_ephemeral(
                interaction,
                "No `#team-draft` (or `#dynasty-team-draft`) channel found. Create one and try again.",
            )

    @app_commands.command(
        name="add_waitlist",
        description="Add a user to the draft waitlist (admin only)",
    )
    @app_commands.describe(user="The user to add to the waitlist")
    async def add_waitlist(self, interaction: discord.Interaction, user: discord.Member):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can manage the waitlist.")
            return

        draft = load_draft()
        waitlist = draft.setdefault("waitlist", [])

        if any(w["user_id"] == user.id for w in waitlist):
            await send_ephemeral(interaction, f"{user.mention} is already on the waitlist.")
            return

        waitlist.append({"user_id": user.id, "username": str(user)})
        save_draft(draft)

        await send_ephemeral(interaction, f"Added {user.mention} to the waitlist (#{len(waitlist)}).")

        if draft.get("order"):
            await _post_draft_order(
                interaction.guild, [build_draft_order_embed(draft), build_eligible_teams_embed(draft)]
            )

    @app_commands.command(
        name="remove_waitlist",
        description="Remove a user from the draft waitlist (admin only)",
    )
    @app_commands.describe(user="The user to remove from the waitlist")
    async def remove_waitlist(self, interaction: discord.Interaction, user: discord.Member):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can manage the waitlist.")
            return

        draft = load_draft()
        waitlist = draft.get("waitlist", [])
        new_waitlist = [w for w in waitlist if w["user_id"] != user.id]

        if len(new_waitlist) == len(waitlist):
            await send_ephemeral(interaction, f"{user.mention} isn't on the waitlist.")
            return

        draft["waitlist"] = new_waitlist
        save_draft(draft)

        await send_ephemeral(interaction, f"Removed {user.mention} from the waitlist.")

        if draft.get("order"):
            await _post_draft_order(
                interaction.guild, [build_draft_order_embed(draft), build_eligible_teams_embed(draft)]
            )

    @app_commands.command(
        name="start_draft",
        description="Kick off the team draft — opens picking for pick #1 (admin only)",
    )
    async def start_draft(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can start the draft.")
            return

        draft = load_draft()
        if not draft.get("order"):
            await send_ephemeral(interaction, "No draft order has been set yet. Run `/set_draft_order` first.")
            return
        if draft.get("status") == "drafting":
            await send_ephemeral(interaction, "The draft is already in progress.")
            return
        if draft.get("status") == "complete":
            await send_ephemeral(interaction, "The draft has already been completed.")
            return

        draft["status"] = "drafting"
        draft["current_pick"] = 0
        save_draft(draft)

        embed = build_draft_order_embed(draft)
        posted_channel = await _post_draft_order(interaction.guild, [embed, build_eligible_teams_embed(draft)])

        if posted_channel:
            await _announce_current_pick(posted_channel, draft)
            await send_ephemeral(interaction, f"✅ Draft started. Announced in {posted_channel.mention}.")
        else:
            await send_ephemeral(
                interaction,
                "✅ Draft started, but no `#team-draft` channel was found to announce it. "
                "Create the channel and run `/post_draft_order`.",
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Draft(bot))
