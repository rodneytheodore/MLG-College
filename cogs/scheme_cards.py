from typing import Literal

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


def can_edit_card(interaction: discord.Interaction, team_abbr: str, roster: dict) -> bool:
    if is_admin(interaction):
        return True
    owner = roster.get(team_abbr, {})
    return owner.get("user_id") == interaction.user.id


def build_scheme_card_embed(team_info: dict, card: dict) -> discord.Embed:
    embed = discord.Embed(
        title=team_info["name"],
        color=int(team_info["color"], 16) if team_info.get("color") else discord.Color.default(),
    )
    logo = team_info.get("logoDark") or team_info.get("logo")
    if logo:
        embed.set_thumbnail(url=logo)

    offense = card.get("offense")
    if offense:
        header_line = f"**Coach:** {offense['coach']}"
        if offense.get("film"):
            header_line += f"  \u2022  **Stream Link:** {offense['film']}"
        embed.description = header_line

        lines = [f"**Scheme:** {offense['scheme']}  \u2022  **Coaching Tree:** {offense['coaching_tree']}"]
        lines.append(f"**Personnel:** {offense['personnel']}  \u2022  **Tempo:** {offense['tempo']}")
        lines.append(f"*\u201c{offense['summary']}\u201d*")
        embed.add_field(name="OFFENSE", value="\n".join(lines), inline=False)

    defense = card.get("defense")
    if defense:
        lines = [f"**Scheme:** {defense['scheme']}  \u2022  **Coaching Tree:** {defense['coaching_tree']}"]
        lines.append(f"**Shell:** {defense['coverage_shell']}  \u2022  **Coverage:** {defense['coverage_type']}")
        lines.append(f"**Pressure:** {defense['pressure']}")
        lines.append(f"*\u201c{defense['summary']}\u201d*")
        embed.add_field(name="DEFENSE", value="\n".join(lines), inline=False)

    return embed


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
        """Wipes and rebuilds the scheme cards channel, posting one embed per
        team that has at least one half (offense or defense) filled in."""
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

    def resolve_team_for_user(self, interaction: discord.Interaction, team_input: str, roster: dict):
        """Resolves which team abbreviation a set_*_scheme command should act on.
        If team_input is given, resolves it and checks the caller can edit it
        (owns it, or is admin). If omitted, auto-detects from roster ownership,
        requiring the caller to own exactly one team. Returns (abbr, error)."""
        if team_input:
            abbr, error = resolve_team(team_input, self.teams)
            if error:
                return None, error
            if not can_edit_card(interaction, abbr, roster):
                return None, "You can only set the scheme card for a team you own (or be an admin)."
            return abbr, None

        owned = [a for a, info in roster.items() if info.get("user_id") == interaction.user.id]
        if len(owned) == 0:
            return None, (
                "You don't own a team yet, so I can't auto-detect which one to use. "
                "If you're an admin setting this for someone else, specify the `team` option."
            )
        if len(owned) > 1:
            names = ", ".join(self.teams[a]["name"] for a in owned)
            return None, f"You own multiple teams ({names}) \u2014 please specify which one with the `team` option."
        return owned[0], None

    # ---------- Commands ----------

    @app_commands.command(name="set_offense_scheme", description="Set your team's offensive scheme card")
    @app_commands.describe(
        scheme="Offensive scheme",
        coaching_tree="Coaching Tree (1 or 2 coaches)",
        personnel="Personnel grouping",
        tempo="Tempo/philosophy",
        summary="Short summary of your offensive approach",
        film="Stream link (Twitch, YouTube, etc.)",
        team="Your team (auto-detected if you own one \u2014 only specify to set it for someone else as admin)",
    )
    @app_commands.choices(personnel=[
        app_commands.Choice(name="Traditional (Base 21/12 Personnel)", value="Traditional"),
        app_commands.Choice(name="Modern (3 WR most of time)", value="Modern"),
        app_commands.Choice(name="Spread (Use if Air Raid, Run and Shoot, or Spread)", value="Spread"),
    ])
    async def set_offense_scheme(
        self, interaction: discord.Interaction,
        scheme: Literal["Air Raid", "Spread", "Spread Option", "Option", "Pro Style", "Power Spread", "Pistol", "Multiple"],
        coaching_tree: str,
        personnel: str,
        tempo: Literal["Ball Control", "No Huddle", "Turbo"],
        summary: str,
        film: str, team: str = None,
    ):
        roster = load_roster()
        abbr, error = self.resolve_team_for_user(interaction, team, roster)
        if error:
            await send_ephemeral(interaction, error)
            return

        coach = true_display_name(interaction.user)

        cards = load_scheme_cards()
        card = cards.setdefault(abbr, {})
        card["offense"] = {
            "coach": coach,
            "scheme": scheme,
            "coaching_tree": coaching_tree,
            "personnel": personnel,
            "tempo": tempo,
            "film": film,
            "summary": summary,
        }
        card["submitted_by"] = true_display_name(interaction.user)
        save_scheme_cards(cards)

        await send_ephemeral(interaction, f"Offense scheme saved for **{self.teams[abbr]['name']}**.")
        await self.refresh_scheme_cards_channel()

    @set_offense_scheme.autocomplete("team")
    async def set_offense_scheme_team_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.team_autocomplete(interaction, current)

    @app_commands.command(name="set_defense_scheme", description="Set your team's defensive scheme card")
    @app_commands.describe(
        scheme="Defensive scheme",
        coaching_tree="Coaching Tree (1 or 2 coaches)",
        coverage_shell="Coverage shell",
        coverage_type="Coverage type",
        pressure="Pressure package/approach",
        summary="Short summary of your defensive approach",
        team="Your team (auto-detected if you own one \u2014 only specify to set it for someone else as admin)",
    )
    @app_commands.choices(coverage_type=[
        app_commands.Choice(name="Man (C2 Man, C1, C0, Man Blitz)", value="Man Coverage"),
        app_commands.Choice(name="Zone (C3, Tampa 2, C4 Drop, Zone Blitz)", value="Zone Coverage"),
        app_commands.Choice(name="Match (C4, C3 Seam, C6, C9, C2 Sink)", value="Match Coverage"),
    ])
    async def set_defense_scheme(
        self, interaction: discord.Interaction,
        scheme: Literal["4-3", "4-3 Multiple", "3-4", "3-4 Multiple", "Multiple", "3-3-5", "3-3-5 Tite", "4-2-5", "3-2-6"],
        coaching_tree: str,
        coverage_shell: Literal["Single High Safety", "Two High Safety", "Hybrid Safety Shell"],
        coverage_type: str,
        pressure: Literal["Bring Pressure/Blitz", "Rush Four/Play Coverage"],
        summary: str,
        team: str = None,
    ):
        roster = load_roster()
        abbr, error = self.resolve_team_for_user(interaction, team, roster)
        if error:
            await send_ephemeral(interaction, error)
            return

        cards = load_scheme_cards()
        card = cards.setdefault(abbr, {})
        card["defense"] = {
            "scheme": scheme,
            "coaching_tree": coaching_tree,
            "coverage_shell": coverage_shell,
            "coverage_type": coverage_type,
            "pressure": pressure,
            "summary": summary,
        }
        card["submitted_by"] = true_display_name(interaction.user)
        save_scheme_cards(cards)

        await send_ephemeral(interaction, f"Defense scheme saved for **{self.teams[abbr]['name']}**.")
        await self.refresh_scheme_cards_channel()

    @set_defense_scheme.autocomplete("team")
    async def set_defense_scheme_team_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.team_autocomplete(interaction, current)

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
