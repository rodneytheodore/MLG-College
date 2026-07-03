import json
import os

import discord
from discord import app_commands
from discord.ext import commands

from utils.data import is_admin
from utils.responses import send_ephemeral

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

        lines_out = "\n".join(f"`{i + 1:02d}` {m.mention}" for i, m in enumerate(self.picks))
        embed = discord.Embed(
            title="🏈 Draft Order Set",
            description=lines_out,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{len(self.picks)} participants")
        await interaction.response.edit_message(content=None, embed=embed, view=None)


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


class Draft(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
        order = draft.get("order", [])

        if not order:
            await send_ephemeral(interaction, "No draft order has been set yet.")
            return

        current_pick = draft.get("current_pick", 0)
        lines_out = []
        for i, entry in enumerate(order):
            marker = "➡️ " if i == current_pick and draft.get("status") == "drafting" else ""
            lines_out.append(f"{marker}`{i + 1:02d}` <@{entry['user_id']}>")

        embed = discord.Embed(
            title="🏈 Draft Order",
            description="\n".join(lines_out),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{len(order)} participants")
        await send_ephemeral(interaction, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Draft(bot))
