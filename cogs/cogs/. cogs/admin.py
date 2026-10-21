import discord
from discord import app_commands
from discord.ext import commands
from database import get_db

class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="scrim", description="Publica um anúncio de desafio/scrim.")
    @app_commands.describe(pingar_everyone="Marcar @everyone?", informacoes="Detalhes da partida")
    async def scrim(self, interaction: discord.Interaction, pingar_everyone: bool, informacoes: str):
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT scrims_channel FROM guild_configs WHERE guild_id = ?", (interaction.guild.id,))
            cfg = cursor.fetchone()

        if not cfg or not cfg["scrims_channel"]:
            return await interaction.response.send_message("❌ Canal de Scrims não configurado.", ephemeral=True)

        channel = interaction.guild.get_channel(cfg["scrims_channel"])
        if not channel:
            return await interaction.response.send_message("❌ Canal de Scrims inválido.", ephemeral=True)

        embed = discord.Embed(
            title="⚔️ NOVO DESAFIO / SCRIM",
            description=informacoes,
            color=discord.Color.dark_purple()
        )
        embed.set_footer(text=f"Publicado por: {interaction.user.display_name}")

        content = "@everyone" if pingar_everyone else ""
        await channel.send(content=content, embed=embed)
        await interaction.response.send_message("✅ Scrim enviada!", ephemeral=True)

    @app_commands.command(name="freeagent", description="Anuncia disponibilidade no mercado.")
    @app_commands.describe(informacoes="Posição, horários e histórico")
    async def freeagent(self, interaction: discord.Interaction, informacoes: str):
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT freeagents_channel FROM guild_configs WHERE guild_id = ?", (interaction.guild.id,))
            cfg = cursor.fetchone()

        if not cfg or not cfg["freeagents_channel"]:
            return await interaction.response.send_message("❌ Canal de Free Agents não configurado.", ephemeral=True)

        channel = interaction.guild.get_channel(cfg["freeagents_channel"])
        if not channel:
            return await interaction.response.send_message("❌ Canal de Free Agents inválido.", ephemeral=True)

        embed = discord.Embed(
            title="🏃 JOGADOR DISPONÍVEL (FREE AGENT)",
            description=informacoes,
            color=discord.Color.gold()
        )
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)

        await channel.send(embed=embed)
        await interaction.response.send_message("✅ Anúncio enviado!", ephemeral=True)

async def setup(bot):
    await bot.add_cog(AdminCog(bot))
