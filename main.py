import asyncio
import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
from database import init_db

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True

class LeagueBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        init_db()
        
        cogs = [
            "cogs.config",
            "cogs.tickets",
            "cogs.teams",
            "cogs.admin",
            "cogs.moderation"
        ]
        
        for cog in cogs:
            try:
                await self.load_extension(cog)
                print(f"✅ Cog carregado: {cog}")
            except Exception as e:
                print(f"❌ Erro no cog {cog}: {e}")
                
        try:
            synced = await self.tree.sync()
            print(f"🚀 Sincronizados {len(synced)} comandos slash!")
        except Exception as e:
            print(f"❌ Erro ao sincronizar comandos: {e}")

    async def on_ready(self):
        print(f"🤖 Bot online como {self.user} (ID: {self.user.id})")

bot = LeagueBot()

if __name__ == "__main__":
    if not TOKEN:
        print("❌ Token não encontrado no arquivo .env!")
    else:
        bot.run(TOKEN)
