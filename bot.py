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

GUILD_ID = 1207738346424770631  # your server's ID, for instant command sync


class MLGBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        for cog in COGS:
            await self.load_extension(cog)
            print(f"Loaded cog: {cog}")

        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        print(f"Synced {len(synced)} command(s) to guild {GUILD_ID}")


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
