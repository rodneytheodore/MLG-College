import os
import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True

# Add the filenames (without .py) of any cogs in the cogs/ folder here
COGS = [
    "cogs.roster",
    "cogs.scheduling",
]


class MLGBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        for cog in COGS:
            await self.load_extension(cog)
            print(f"Loaded cog: {cog}")
        await self.tree.sync()


bot = MLGBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print("Bot is online and connected.")


@bot.command(name="ping")
async def ping(ctx):
    await ctx.send("Pong! Bot is alive.")


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set.")
    bot.run(token)
