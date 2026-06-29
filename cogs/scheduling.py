import discord
from discord import app_commands
from discord.ext import commands

from utils.data import (
    load_roster,
    save_roster,
    load_season,
    save_season,
    load_settings,
    save_settings,
    archive_dynasty,
    is_admin,
)
from utils.responses import send_ephemeral


def build_dashboard_embed(season: dict, roster: dict) -> discord.Embed:
    year = season.get("year") or "Not set yet"
    stage = season.get("current_stage", "preseason").replace("_", " ").title()
    current_week = season.get("current_week")
    week_text = f"Week {current_week}" if current_week is not None else "No active week"
    claimed_count = len(roster)

    embed = discord.Embed(title="🏈 League Status", color=discord.Color.blurple())
    embed.add_field(name="Dynasty Year", value=str(year), inline=True)
    embed.add_field(name="Stage", value=stage, inline=True)
    embed.add_field(name="Current Week", value=week_text, inline=True)
    embed.add_field(name="Teams Claimed", value=f"{claimed_count}/32", inline=True)
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
    embed = build_dashboard_embed(season, roster)

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


class Scheduling(commands.Cog):
    """Season/dynasty lifecycle commands. Weekly scheduling commands added separately."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
        stage = season.get("current_stage", "preseason").replace("_", " ").title()
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
