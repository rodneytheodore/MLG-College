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
)


def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.administrator


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

            await channel.send(f"**{conf_name}**")

            for team in claimed:
                owner_id = roster[team["abbr"].upper()]["user_id"]
                embed = discord.Embed(
                    title=team["name"],
                    description=f"Owner: <@{owner_id}>",
                    color=int(team["color"], 16) if team.get("color") else discord.Color.default(),
                )
                if team.get("logo"):
                    embed.set_thumbnail(url=team["logo"])
                await channel.send(embed=embed)

    # ---------- Commands ----------

    @app_commands.command(name="post_roster", description="Set this channel as the live roster display (admin only)")
    async def post_roster(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("Only admins can do that.", ephemeral=True)
            return

        settings = load_settings()
        settings["roster_channel_id"] = interaction.channel_id
        save_settings(settings)

        await interaction.response.send_message(
            "This channel is now the live roster display. Building it now...", ephemeral=True
        )
        await self.refresh_roster_channel()

    @app_commands.command(name="assign_team", description="Assign a user to a team (admin only)")
    @app_commands.describe(team="Team abbreviation, e.g. OSU", user="The user to assign")
    async def assign_team(self, interaction: discord.Interaction, team: str, user: discord.Member):
        if not is_admin(interaction):
            await interaction.response.send_message("Only admins can assign teams.", ephemeral=True)
            return

        abbr = team.upper()
        if abbr not in self.teams:
            await interaction.response.send_message(
                f"Couldn't find a team with abbreviation `{abbr}`. Double check the spelling.",
                ephemeral=True,
            )
            return

        roster = load_roster()

        if abbr in roster:
            current_owner_id = roster[abbr]["user_id"]
            await interaction.response.send_message(
                f"`{abbr}` is already assigned to <@{current_owner_id}>. "
                f"Use `/vacate_team` first if you want to reassign it.",
                ephemeral=True,
            )
            return

        roster[abbr] = {"user_id": user.id, "username": str(user)}
        save_roster(roster)

        team_info = self.teams[abbr]
        await interaction.response.send_message(
            f"Assigned **{team_info['name']}** to {user.mention}.", ephemeral=True
        )
        await self.refresh_roster_channel()

    @app_commands.command(name="vacate_team", description="Remove a team's current owner (admin only)")
    @app_commands.describe(team="Team abbreviation, e.g. OSU")
    async def vacate_team(self, interaction: discord.Interaction, team: str):
        if not is_admin(interaction):
            await interaction.response.send_message("Only admins can vacate teams.", ephemeral=True)
            return

        abbr = team.upper()
        if abbr not in self.teams:
            await interaction.response.send_message(
                f"Couldn't find a team with abbreviation `{abbr}`. Double check the spelling.",
                ephemeral=True,
            )
            return

        roster = load_roster()

        if abbr not in roster:
            await interaction.response.send_message(f"`{abbr}` doesn't currently have an owner.", ephemeral=True)
            return

        previous_owner_id = roster[abbr]["user_id"]
        del roster[abbr]
        save_roster(roster)

        team_info = self.teams[abbr]
        await interaction.response.send_message(
            f"Vacated **{team_info['name']}** (was assigned to <@{previous_owner_id}>).", ephemeral=True
        )
        await self.refresh_roster_channel()

    @app_commands.command(name="vacate_all", description="Remove every team's owner, clearing the whole roster (admin only)")
    async def vacate_all(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("Only admins can do that.", ephemeral=True)
            return

        roster = load_roster()
        count = len(roster)
        save_roster({})

        await interaction.response.send_message(
            f"Vacated all {count} claimed team(s). Roster is now empty.", ephemeral=True
        )
        await self.refresh_roster_channel()


async def setup(bot: commands.Bot):
    await bot.add_cog(Roster(bot))
