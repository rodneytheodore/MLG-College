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


ADMIN_ROLE_NAME = "Admin"


def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    return any(role.name == ADMIN_ROLE_NAME for role in interaction.user.roles)


def resolve_team(query: str, teams: dict):
    """Resolve user input to a team abbreviation.
    Tries exact abbreviation match first, then exact name match,
    then a unique partial name match. Returns (abbr, error_message)."""
    query = query.strip()
    upper = query.upper()

    if upper in teams:
        return upper, None

    lower = query.lower()
    exact_name_matches = [abbr for abbr, t in teams.items() if t["name"].lower() == lower]
    if len(exact_name_matches) == 1:
        return exact_name_matches[0], None

    partial_matches = [abbr for abbr, t in teams.items() if lower in t["name"].lower()]
    if len(partial_matches) == 1:
        return partial_matches[0], None
    if len(partial_matches) > 1:
        names = ", ".join(teams[a]["name"] for a in partial_matches[:8])
        return None, f"That matches multiple teams: {names}. Try being more specific or use the abbreviation."

    return None, f"Couldn't find a team matching `{query}`. Try the team name or abbreviation, e.g. `UGA`."


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
                display_name = team.get("school", team["name"])
                padded_name = display_name.ljust(40, "\u2003")  # pad with invisible em-spaces for consistent width
                embed = discord.Embed(
                    description=f"**{padded_name}** — Owner: <@{owner_id}>",
                    color=int(team["color"], 16) if team.get("color") else discord.Color.default(),
                )
                if team.get("logo"):
                    embed.set_thumbnail(url=team["logo"])
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
    @app_commands.describe(team="Team name or abbreviation, e.g. Georgia or UGA", user="The user to assign")
    async def assign_team(self, interaction: discord.Interaction, team: str, user: discord.Member):
        if not is_admin(interaction):
            await interaction.response.send_message("Only admins can assign teams.", ephemeral=True)
            return

        abbr, error = resolve_team(team, self.teams)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
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
    @app_commands.describe(team="Team name or abbreviation, e.g. Georgia or UGA")
    async def vacate_team(self, interaction: discord.Interaction, team: str):
        if not is_admin(interaction):
            await interaction.response.send_message("Only admins can vacate teams.", ephemeral=True)
            return

        abbr, error = resolve_team(team, self.teams)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
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
