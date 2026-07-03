import json
import os

import discord
from discord import app_commands
from discord.ext import commands

from utils.data import is_admin
from utils.responses import send_ephemeral
from cogs.roster import resolve_member

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


# ---- Continue button (bridges modal → modal since Discord forbids modal → modal) ----

class _ContinueView(discord.ui.View):
    """Ephemeral button used between chained modals."""

    def __init__(self, label: str, on_click, style: discord.ButtonStyle = discord.ButtonStyle.primary):
        super().__init__(timeout=300)
        self._on_click = on_click
        btn = discord.ui.Button(label=label, style=style)
        btn.callback = self._handle
        self.add_item(btn)

    async def _handle(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        await self._on_click(interaction)


# ---- Step 1: how many teams ----

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
            await interaction.response.send_message(
                "Please enter a whole number.", ephemeral=True
            )
            return

        count = int(raw)
        if count < 2 or count > MAX_TEAMS:
            await interaction.response.send_message(
                f"Number of teams must be between 2 and {MAX_TEAMS}.", ephemeral=True
            )
            return

        async def on_continue(interaction: discord.Interaction):
            await interaction.response.send_modal(SetDraftOrderModal(team_count=count))

        view = _ContinueView(f"Continue → Enter Draft Order ({count} teams)", on_continue)
        await interaction.response.send_message(
            f"**{count} teams** set for this draft. Click to enter the draft order:",
            view=view,
            ephemeral=True,
        )


# ---- Step 2: enter the order ----

class SetDraftOrderModal(discord.ui.Modal, title="Set Draft Order"):
    order_input = discord.ui.TextInput(
        label="Draft order — one user per line",
        style=discord.TextStyle.paragraph,
        placeholder="rodneytheodore\nnick_saban99\njdawg42\n...",
        required=True,
        max_length=4000,
    )

    def __init__(self, team_count: int):
        super().__init__()
        self.team_count = team_count
        self.order_input.label = f"Draft order — exactly {team_count} lines"

    async def on_submit(self, interaction: discord.Interaction):
        lines = [line.strip() for line in self.order_input.value.splitlines() if line.strip()]

        if len(lines) != self.team_count:
            count = self.team_count

            async def on_retry(interaction: discord.Interaction):
                await interaction.response.send_modal(SetDraftOrderModal(team_count=count))

            view = _ContinueView(f"Retry → Enter Draft Order ({count} teams)", on_retry, discord.ButtonStyle.danger)
            await interaction.response.send_message(
                f"❌ You entered {len(lines)} line(s) but the draft is set for **{count}** teams. "
                f"Click to try again:",
                view=view,
                ephemeral=True,
            )
            return

        resolved: list[discord.Member] = []
        errors: list[str] = []
        seen_ids: set[int] = set()

        for i, line in enumerate(lines, start=1):
            result = await resolve_member(interaction.guild, line)

            if result is None:
                errors.append(f"Pick {i}: no member found matching `{line}`")
                continue

            if isinstance(result, list):
                names = ", ".join(m.global_name or m.name for m in result[:5])
                errors.append(f"Pick {i}: `{line}` matches multiple members ({names})")
                continue

            if result.id in seen_ids:
                errors.append(f"Pick {i}: {result.global_name or result.name} already appears earlier in the list")
                continue

            seen_ids.add(result.id)
            resolved.append(result)

        if errors:
            count = self.team_count

            async def on_retry(interaction: discord.Interaction):
                await interaction.response.send_modal(SetDraftOrderModal(team_count=count))

            error_text = "\n".join(errors[:15])
            more = f"\n...and {len(errors) - 15} more" if len(errors) > 15 else ""
            view = _ContinueView(f"Retry → Enter Draft Order ({count} teams)", on_retry, discord.ButtonStyle.danger)
            await interaction.response.send_message(
                f"❌ Couldn't set the draft order — fix these entries and resubmit:\n{error_text}{more}",
                view=view,
                ephemeral=True,
            )
            return

        draft = {
            "team_count": self.team_count,
            "order": [{"user_id": m.id, "username": str(m)} for m in resolved],
            "current_pick": 0,
            "status": "order_set",
        }
        save_draft(draft)

        lines_out = "\n".join(f"`{i + 1:02d}` {m.mention}" for i, m in enumerate(resolved))
        embed = discord.Embed(
            title="🏈 Draft Order Set",
            description=lines_out,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{len(resolved)} participants")
        await interaction.response.send_message(embed=embed, ephemeral=True)


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
