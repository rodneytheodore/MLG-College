import os
import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True  # only needed if you read message text, not just slash commands

bot = commands.Bot(command_prefix="!", intents=intents)


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
