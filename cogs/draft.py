import json
import os

import discord
from discord import app_commands
from discord.ext import commands

from utils.data import is_admin, load_roster, save_roster, load_teams, resolve_team
from utils.responses import send_ephemeral
from cogs.scheduling import refresh_dashboard

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


async def _post_draft_order(guild: discord.Guild, embed: discord.Embed) -> discord.TextChannel | None:
    """Finds the draft channel by name (case-insensitive) and posts the embed there.
    Returns the channel if found and posted, else None."""
    channel = discord.utils.find(
        lambda c: isinstance(c, discord.TextChannel) and c.name.lower() in DRAFT_CHANNEL_NAMES,
        guild.channels,
    )
    if channel is None:
        return None

    await channel.purge(limit=50, check=lambda m: m.author == guild.me)
    await channel.send(embed=embed)
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

    embed = discord.Embed(
        title="🏈 Draft Order" if status != "complete" else "🏈 Draft Complete",
        description="\n".join(lines_out),
        color=discord.Color.green() if status == "complete" else discord.Color.blurple(),
    )
    embed.set_footer(text=f"{len(order)} participants")
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
        draft = {
            "team_count": self.team_count,
            "order": [{"user_id": m.id, "username": str(m)} for m in self.picks],
            "current_pick": 0,
            "status": "order_set",
        }
        save_draft(draft)

        embed = build_draft_order_embed(draft)
        posted_channel = await _post_draft_order(interaction.guild, embed)

        if posted_channel:
            summary = f"✅ **Draft order set.** Posted to {posted_channel.mention}."
        else:
            summary = (
                "✅ **Draft order set,** but no `#team-draft` (or `#dynasty-team-draft`) "
                "channel was found. Create one and re-run `/view_draft_order` to post it there."
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


class TeamPickModal(discord.ui.Modal, title="Make Your Draft Pick"):
    team_input = discord.ui.TextInput(
        label="Team",
        placeholder="e.g. Georgia or UGA",
        required=True,
        max_length=60,
    )

    async def on_submit(self, interaction: discord.Interaction):
        # Reload fresh — another admin/user action may have changed state since the button was shown.
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

        teams = load_teams()
        abbr, error = resolve_team(self.team_input.value, teams)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        roster = load_roster()
        if abbr in roster:
            owner_id = roster[abbr]["user_id"]
            await interaction.response.send_message(
                f"`{abbr}` is already claimed by <@{owner_id}>. Click **Make Your Pick** again to try a different team.",
                ephemeral=True,
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
        await interaction.response.send_message(f"✅ You picked **{team_name}**!", ephemeral=True)

        channel = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel) and c.name.lower() in DRAFT_CHANNEL_NAMES,
            interaction.guild.channels,
        )
        if channel is None:
            return

        await channel.send(embed=build_draft_order_embed(draft))

        if draft["status"] == "complete":
            await channel.send("🎉 **The draft is complete!** All teams have been claimed.")
        else:
            await _announce_current_pick(channel, draft)


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

        await interaction.response.send_modal(TeamPickModal())


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

    @app_commands.command(name="view_draft_order", description="View the current draft order")
    async def view_draft_order(self, interaction: discord.Interaction):
        draft = load_draft()
        if not draft.get("order"):
            await send_ephemeral(interaction, "No draft order has been set yet.")
            return

        await send_ephemeral(interaction, embed=build_draft_order_embed(draft))

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

        posted_channel = await _post_draft_order(interaction.guild, build_draft_order_embed(draft))
        if posted_channel:
            await send_ephemeral(interaction, f"Posted to {posted_channel.mention}.")
        else:
            await send_ephemeral(
                interaction,
                "No `#team-draft` (or `#dynasty-team-draft`) channel found. Create one and try again.",
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
        posted_channel = await _post_draft_order(interaction.guild, embed)

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
