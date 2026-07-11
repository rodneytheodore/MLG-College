import os
import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Add the filenames (without .py) of any cogs in the cogs/ folder here
COGS = [
    "cogs.roster",
    "cogs.scheduling",
    "cogs.scheme_cards",
    "cogs.install_offense",
    "cogs.install_defense",
    "cogs.draft",
]

GUILD_ID = 1207738346424770631  # your server's ID, for instant command sync


class MLGBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        for cog in COGS:
            await self.load_extension(cog)
            print(f"Loaded cog: {cog}")

        scheduling_cog = self.get_cog("Scheduling")
        if scheduling_cog is not None:
            scheduling_cog.register_active_views()
            print("Registered persistent views for active CPU games.")

        scheme_cards_cog = self.get_cog("SchemeCards")
        if scheme_cards_cog is not None:
            scheme_cards_cog.register_active_views()
            print("Registered persistent views for scheme card buttons.")

        draft_cog = self.get_cog("Draft")
        if draft_cog is not None:
            draft_cog.register_active_views()
            print("Registered persistent view for draft pick button.")

        install_offense_cog = self.get_cog("InstallOffense")
        if install_offense_cog is not None:
            install_offense_cog.register_active_views()
            print("Registered persistent views for in-progress offense installs.")

        guild = discord.Object(id=GUILD_ID)

        # Copy the currently-registered global commands into the guild bucket
        # FIRST, while they still exist, then sync that to Discord (fast, near-instant).
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        print(f"Synced {len(synced)} command(s) to guild {GUILD_ID}")

        # Now clear the global registration so it doesn't show up duplicated
        # alongside the guild-specific one.
        self.tree.clear_commands(guild=None)
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
