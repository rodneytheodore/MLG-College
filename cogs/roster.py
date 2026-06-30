import discord
from discord import app_commands
from discord.ext import commands

from utils.data import (
    load_teams,
    load_teams_by_conference,
    load_roster,
    save_roster,
    load_settings,
    save_settings,
    is_admin,
    resolve_team,
)
from utils.responses import send_ephemeral
from cogs.scheduling import refresh_dashboard


class Roster(commands.Cog):
    """Commands for assigning, vacating, and displaying the league roster."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.teams = load_teams()

    # ---------- Shared display logic ----------

    async def refresh_roster_channel(self):
        """Wipes and rebuilds the roster channel, grouped by conference,
        showing only currently-claimed teams. Called after any change."""
        settings = load_settings()
        channel_id = settings.get("roster_channel_id")
        if not channel_id:
            return  # no channel configured yet, nothing to update

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return

        # Clear out everything the bot previously posted in this channel
        await channel.purge(limit=300, check=lambda m: m.author == self.bot.user)

        roster = load_roster()
        by_conference = load_teams_by_conference()

        for conf_name, conf_teams in by_conference.items():
            claimed = [t for t in conf_teams if t["abbr"].upper() in roster]
            if not claimed:
                continue

            await channel.send(f"**{conf_name}**", allowed_mentions=discord.AllowedMentions.none())

            for team in claimed:
                owner_id = roster[team["abbr"].upper()]["user_id"]
                display_name = team["name"]
                padded_name = display_name.ljust(31)  # monospace, sized to fit longest full name (Florida International Panthers, 30 chars)
                embed = discord.Embed(
                    description=f"`{padded_name}`\n<@{owner_id}>",
                    color=int(team["color"], 16) if team.get("color") else discord.Color.default(),
                )
                logo_url = team.get("logoDark") or team.get("logo")
                if logo_url:
                    embed.set_thumbnail(url=logo_url)
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

        claimed_count = len(roster)
        await channel.send(
            f"**{claimed_count}/32 teams claimed**",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ---------- Commands ----------

    @app_commands.command(name="post_roster", description="Set this channel as the live roster display (admin only)")
    async def post_roster(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can do that.")
            return

        settings = load_settings()
        settings["roster_channel_id"] = interaction.channel_id
        save_settings(settings)

        await send_ephemeral(interaction, "This channel is now the live roster display. Building it now...")
        await self.refresh_roster_channel()

    @app_commands.command(name="assign_team", description="Assign a user to a team (admin only)")
    @app_commands.describe(team="Team name or abbreviation, e.g. Georgia or UGA", user="The user to assign")
    async def assign_team(self, interaction: discord.Interaction, team: str, user: discord.Member):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can assign teams.")
            return

        abbr, error = resolve_team(team, self.teams)
        if error:
            await send_ephemeral(interaction, error)
            return

        roster = load_roster()

        if abbr in roster:
            current_owner_id = roster[abbr]["user_id"]
            await send_ephemeral(
                interaction,
                f"`{abbr}` is already assigned to <@{current_owner_id}>. "
                f"Use `/vacate_team` first if you want to reassign it.",
            )
            return

        roster[abbr] = {"user_id": user.id, "username": str(user)}
        save_roster(roster)

        team_info = self.teams[abbr]
        await send_ephemeral(interaction, f"Assigned **{team_info['name']}** to {user.mention}.")
        await self.refresh_roster_channel()
        await refresh_dashboard(self.bot)

    @app_commands.command(name="vacate_team", description="Remove a team's current owner (admin only)")
    @app_commands.describe(team="Team name or abbreviation, e.g. Georgia or UGA")
    async def vacate_team(self, interaction: discord.Interaction, team: str):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can vacate teams.")
            return

        abbr, error = resolve_team(team, self.teams)
        if error:
            await send_ephemeral(interaction, error)
            return

        roster = load_roster()

        if abbr not in roster:
            await send_ephemeral(interaction, f"`{abbr}` doesn't currently have an owner.")
            return

        previous_owner_id = roster[abbr]["user_id"]
        del roster[abbr]
        save_roster(roster)

        team_info = self.teams[abbr]
        await send_ephemeral(
            interaction, f"Vacated **{team_info['name']}** (was assigned to <@{previous_owner_id}>)."
        )
        await self.refresh_roster_channel()
        await refresh_dashboard(self.bot)

        scheduling_cog = interaction.client.get_cog("Scheduling")
        if scheduling_cog is not None:
            await scheduling_cog.handle_team_vacated(abbr)

    async def team_name_autocomplete(self, interaction: discord.Interaction, current: str):
        current_lower = current.lower()
        matches = [
            t for abbr, t in self.teams.items()
            if current_lower in t["name"].lower() or current_lower in abbr.lower()
        ]
        return [app_commands.Choice(name=t["name"], value=t["abbr"]) for t in matches[:25]]

    @assign_team.autocomplete("team")
    async def assign_team_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.team_name_autocomplete(interaction, current)

    @vacate_team.autocomplete("team")
    async def vacate_team_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.team_name_autocomplete(interaction, current)

    @app_commands.command(name="vacate_all", description="Remove every team's owner, clearing the whole roster (admin only)")
    async def vacate_all(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can do that.")
            return

        roster = load_roster()
        vacated_abbrs = list(roster.keys())
        count = len(roster)
        save_roster({})

        await send_ephemeral(interaction, f"Vacated all {count} claimed team(s). Roster is now empty.")
        await self.refresh_roster_channel()
        await refresh_dashboard(self.bot)

        scheduling_cog = interaction.client.get_cog("Scheduling")
        if scheduling_cog is not None:
            for abbr in vacated_abbrs:
                await scheduling_cog.handle_team_vacated(abbr)


async def setup(bot: commands.Bot):
    await bot.add_cog(Roster(bot))
