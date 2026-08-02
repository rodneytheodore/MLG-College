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
from utils.responses import send_ephemeral, send_ephemeral_followup
from utils.roster_render import build_roster_file, compute_column_widths
from cogs.scheduling import refresh_dashboard


class Roster(commands.Cog):
    """Commands for assigning, vacating, and displaying the league roster."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.teams = load_teams()

    # ---------- Shared display logic ----------

    async def refresh_roster_channel(self):
        """Edits each conference's roster image in place (tracked by message
        ID in settings), or sends a new one if none exists yet or the
        stored message was deleted. Called after any roster change."""
        settings = load_settings()
        channel_id = settings.get("roster_channel_id")
        if not channel_id:
            return  # no channel configured yet, nothing to update

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return

        roster = load_roster()
        by_conference = load_teams_by_conference()
        message_ids = settings.get("roster_message_ids", {})
        updated_ids = {}

        # Build every conference's row list first so column widths can be
        # measured across ALL claimed teams at once -- otherwise each
        # conference's image sizes its columns independently and a
        # short-named conference (e.g. all "Miami"/"Duke") ends up visibly
        # narrower than one with a long name like "Florida International",
        # making the stack of images in the channel look inconsistent.
        conf_rows = {}
        all_rows = []
        for conf_name, conf_teams in by_conference.items():
            claimed = sorted(
                (t for t in conf_teams if t["abbr"].upper() in roster),
                key=lambda t: t["name"],
            )
            if not claimed:
                conf_rows[conf_name] = []
                continue
            rows = [
                {
                    "team_name": t["name"],
                    "owner_name": roster[t["abbr"].upper()].get("username", "Unknown"),
                    "logo_url": t.get("logoDark") or t.get("logo"),
                }
                for t in claimed
            ]
            conf_rows[conf_name] = rows
            all_rows.extend(rows)

        team_col_width, owner_col_width = compute_column_widths(all_rows) if all_rows else (None, None)

        for conf_name, rows in conf_rows.items():
            if not rows:
                # No claimed teams left in this conference -- remove its
                # stale message instead of leaving an outdated image up.
                old_id = message_ids.get(conf_name)
                if old_id:
                    try:
                        old_message = await channel.fetch_message(old_id)
                        await old_message.delete()
                    except (discord.NotFound, discord.HTTPException):
                        pass
                continue

            file = await build_roster_file(rows, team_col_width=team_col_width, owner_col_width=owner_col_width)
            embed = discord.Embed(title=conf_name, color=discord.Color.dark_grey())
            if file is not None:
                embed.set_image(url="attachment://roster.png")

            existing_id = message_ids.get(conf_name)
            message = None
            if existing_id:
                try:
                    message = await channel.fetch_message(existing_id)
                except (discord.NotFound, discord.HTTPException):
                    message = None

            if message is not None:
                edit_kwargs = {"embed": embed}
                if file is not None:
                    edit_kwargs["attachments"] = [file]
                await message.edit(**edit_kwargs)
                updated_ids[conf_name] = message.id
            else:
                send_kwargs = {"embed": embed, "allowed_mentions": discord.AllowedMentions.none()}
                if file is not None:
                    send_kwargs["file"] = file
                new_message = await channel.send(**send_kwargs)
                updated_ids[conf_name] = new_message.id

        settings["roster_message_ids"] = updated_ids

        claimed_count = len(roster)
        summary_text = f"**{claimed_count}/32 teams claimed**"
        summary_id = settings.get("roster_summary_message_id")
        summary_message = None
        if summary_id:
            try:
                summary_message = await channel.fetch_message(summary_id)
            except (discord.NotFound, discord.HTTPException):
                summary_message = None

        if summary_message is not None:
            await summary_message.edit(content=summary_text)
        else:
            new_summary = await channel.send(summary_text, allowed_mentions=discord.AllowedMentions.none())
            settings["roster_summary_message_id"] = new_summary.id

        save_settings(settings)

    # ---------- Commands ----------

    @app_commands.command(name="post_roster", description="Set this channel as the live roster display (admin only)")
    async def post_roster(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await send_ephemeral(interaction, "Only admins can do that.")
            return

        # Purging the old channel and rebuilding every conference's image
        # can exceed Discord's 3-second interaction window, so defer
        # immediately -- the same fix /post_scheme_cards needed after it hit
        # "Unknown interaction" (10062) doing this in the wrong order.
        await interaction.response.defer(ephemeral=True)

        settings = load_settings()
        old_channel_id = settings.get("roster_channel_id")

        if old_channel_id:
            old_channel = self.bot.get_channel(old_channel_id)
            if old_channel is not None:
                await old_channel.purge(limit=300, check=lambda m: m.author == self.bot.user)

        settings["roster_channel_id"] = interaction.channel_id
        settings["roster_message_ids"] = {}
        settings.pop("roster_summary_message_id", None)
        save_settings(settings)

        await self.refresh_roster_channel()
        await send_ephemeral_followup(interaction, "This channel is now the live roster display.")

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
