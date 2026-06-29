import discord
from discord import app_commands
from discord.ext import commands

from utils.data import (
    load_roster,
    save_roster,
    load_season,
    save_season,
    archive_dynasty,
    is_admin,
)


class NewDynastyConfirmView(discord.ui.View):
    def __init__(self, new_year: int):
        super().__init__(timeout=60)
        self.new_year = new_year

    @discord.ui.button(label="Confirm — Start New Dynasty", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("Only admins can confirm this.", ephemeral=True)
            return

        current_season = load_season()
        current_roster = load_roster()

        archive_dynasty(current_season, current_roster)

        save_roster({})
        save_season({
            "year": self.new_year,
            "current_phase": "preseason",
            "current_week": None,
            "weeks": {},
        })

        roster_cog = interaction.client.get_cog("Roster")
        if roster_cog is not None:
            await roster_cog.refresh_roster_channel()

        for child in self.children:
            child.disabled = True
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

    @app_commands.command(name="dynasty_info", description="Show the current dynasty year and season status")
    async def dynasty_info(self, interaction: discord.Interaction):
        season = load_season()
        year = season.get("year") or "Not set yet"
        phase = season.get("current_phase", "preseason").replace("_", " ").title()
        current_week = season.get("current_week")
        week_text = f"Week {current_week}" if current_week is not None else "No active week"

        await interaction.response.send_message(
            f"**Dynasty Year:** {year}\n**Phase:** {phase}\n**{week_text}**",
            ephemeral=True,
        )

    @app_commands.command(name="new_dynasty", description="Start a fresh dynasty for a new year (admin only)")
    @app_commands.describe(year="The new dynasty year, e.g. 2027")
    async def new_dynasty(self, interaction: discord.Interaction, year: int):
        if not is_admin(interaction):
            await interaction.response.send_message("Only admins can start a new dynasty.", ephemeral=True)
            return

        current_season = load_season()
        current_year = current_season.get("year") or "unset"
        claimed_count = len(load_roster())

        view = NewDynastyConfirmView(new_year=year)
        await interaction.response.send_message(
            f"⚠️ This will archive the current dynasty (year: `{current_year}`, "
            f"{claimed_count} team(s) claimed) and reset everything for **{year}**:\n"
            f"- All team assignments will be cleared\n"
            f"- The season will reset to Preseason, Week 0\n"
            f"- Previous weeks' Discord channels/categories are **not** deleted automatically\n\n"
            f"Are you sure?",
            view=view,
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Scheduling(bot))
