import asyncio
import discord

AUTO_CLEAR_SECONDS = 60


async def send_ephemeral(interaction: discord.Interaction, content: str = None, *, embed: discord.Embed = None,
                          view: discord.ui.View = None, file: discord.File = None, delete_after: int = AUTO_CLEAR_SECONDS):
    """Sends an ephemeral interaction response, then deletes it automatically after delete_after seconds.
    Use this instead of interaction.response.send_message(..., ephemeral=True) directly."""
    kwargs = {"ephemeral": True}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    if file is not None:
        kwargs["file"] = file

    await interaction.response.send_message(**kwargs)

    async def _delete_later():
        await asyncio.sleep(delete_after)
        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException):
            pass

    asyncio.create_task(_delete_later())


async def send_ephemeral_followup(interaction: discord.Interaction, content: str = None, *,
                                   embed: discord.Embed = None, delete_after: int = AUTO_CLEAR_SECONDS):
    """Sends an ephemeral followup message, then deletes it automatically after delete_after seconds.
    Use this instead of interaction.followup.send(..., ephemeral=True) directly."""
    kwargs = {"ephemeral": True}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed

    message = await interaction.followup.send(**kwargs)

    async def _delete_later():
        await asyncio.sleep(delete_after)
        try:
            await message.delete()
        except (discord.NotFound, discord.HTTPException):
            pass

    asyncio.create_task(_delete_later())
