from datetime import timedelta
import discord
from discord import app_commands
from discord.ext import commands
from database import get_db

async def send_mod_log(guild: discord.Guild, embed: discord.Embed):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT logs_channel FROM guild_configs WHERE guild_id = ?", (guild.id,))
        cfg = cursor.fetchone()
        if cfg and cfg["logs_channel"]:
            chan = guild.get_channel(cfg["logs_channel"])
            if chan:
                await chan.send(embed=embed)

class ModerationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def is_hierarchy_safe(self, moderator: discord.Member, target: discord.Member, bot_member: discord.Member) -> (bool, str):
        if target.id == moderator.guild.owner_id:
            return False, "Você não pode punir o Dono do servidor!"
        if target.top_role >= moderator.top_role and moderator.id != moderator.guild.owner_id:
            return False, "Sua hierarquia é menor ou igual à do alvo!"
        if target.top_role >= bot_member.top_role:
            return False, "A hierarquia do bot é menor que a do alvo!"
        return True, ""

    @app_commands.command(name="ban", description="Bane um membro.")
    @app_commands.describe(usuario="Membro", motivo="Motivo")
    async def ban(self, interaction: discord.Interaction, usuario: discord.Member, motivo: str = "Não especificado"):
        if not interaction.user.guild_permissions.ban_members:
            return await interaction.response.send_message("❌ Sem permissão de `Banir Membros`.", ephemeral=True)

        safe, msg = self.is_hierarchy_safe(interaction.user, usuario, interaction.guild.me)
        if not safe:
            return await interaction.response.send_message(f"❌ {msg}", ephemeral=True)

        try:
            await usuario.ban(reason=motivo)
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO punishments (guild_id, user_id, moderator_id, type, reason) VALUES (?, ?, ?, 'BAN', ?)",
                    (interaction.guild.id, usuario.id, interaction.user.id, motivo)
                )

            embed = discord.Embed(title="🔨 Membro Banido", color=discord.Color.dark_red())
            embed.add_field(name="Usuário", value=f"{usuario} ({usuario.id})", inline=False)
            embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
            embed.add_field(name="Motivo", value=motivo, inline=True)
            
            await send_mod_log(interaction.guild, embed)
            await interaction.response.send_message(f"✅ {usuario.mention} banido.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Erro ao banir: {e}", ephemeral=True)

    @app_commands.command(name="expulsar", description="Expulsa um membro.")
    @app_commands.describe(usuario="Membro", motivo="Motivo")
    async def expulsar(self, interaction: discord.Interaction, usuario: discord.Member, motivo: str = "Não especificado"):
        if not interaction.user.guild_permissions.kick_members:
            return await interaction.response.send_message("❌ Sem permissão de `Expulsar Membros`.", ephemeral=True)

        safe, msg = self.is_hierarchy_safe(interaction.user, usuario, interaction.guild.me)
        if not safe:
            return await interaction.response.send_message(f"❌ {msg}", ephemeral=True)

        try:
            await usuario.kick(reason=motivo)
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO punishments (guild_id, user_id, moderator_id, type, reason) VALUES (?, ?, ?, 'KICK', ?)",
                    (interaction.guild.id, usuario.id, interaction.user.id, motivo)
                )

            embed = discord.Embed(title="🚪 Membro Expulso", color=discord.Color.orange())
            embed.add_field(name="Usuário", value=f"{usuario} ({usuario.id})", inline=False)
            embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
            embed.add_field(name="Motivo", value=motivo, inline=True)

            await send_mod_log(interaction.guild, embed)
            await interaction.response.send_message(f"✅ {usuario.mention} expulsão.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Erro ao expulsar: {e}", ephemeral=True)

    @app_commands.command(name="timeout", description="Silencia um membro temporariamente.")
    @app_commands.describe(usuario="Membro", minutos="Duração em minutos", motivo="Motivo")
    async def timeout(self, interaction: discord.Interaction, usuario: discord.Member, minutos: int, motivo: str = "Não especificado"):
        if not interaction.user.guild_permissions.moderate_members:
            return await interaction.response.send_message("❌ Sem permissão de `Gerenciar Castigos`.", ephemeral=True)

        safe, msg = self.is_hierarchy_safe(interaction.user, usuario, interaction.guild.me)
        if not safe:
            return await interaction.response.send_message(f"❌ {msg}", ephemeral=True)

        try:
            duracao = timedelta(minutes=minutos)
            await usuario.timeout(duracao, reason=motivo)

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO punishments (guild_id, user_id, moderator_id, type, reason, duration) VALUES (?, ?, ?, 'TIMEOUT', ?, ?)",
                    (interaction.guild.id, usuario.id, interaction.user.id, motivo, f"{minutos} mins")
                )

            embed = discord.Embed(title="⏱️ Castigo Aplicado", color=discord.Color.gold())
            embed.add_field(name="Usuário", value=usuario.mention, inline=True)
            embed.add_field(name="Duração", value=f"{minutos} min", inline=True)
            embed.add_field(name="Motivo", value=motivo, inline=False)

            await send_mod_log(interaction.guild, embed)
            await interaction.response.send_message(f"✅ {usuario.mention} silenciado por {minutos}m.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Erro ao aplicar timeout: {e}", ephemeral=True)

    @app_commands.command(name="punições", description="Consulta o histórico de punições do usuário.")
    async def punicoes(self, interaction: discord.Interaction, usuario: discord.Member):
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM punishments WHERE guild_id = ? AND user_id = ? ORDER BY timestamp DESC", (interaction.guild.id, usuario.id))
            rows = cursor.fetchall()

        if not rows:
            return await interaction.response.send_message(f"ℹ️ O usuário {usuario.mention} não tem punições registradas.", ephemeral=True)

        embed = discord.Embed(title=f"📋 Histórico de Punições - {usuario.display_name}", color=discord.Color.red())
        for row in rows[:10]:
            embed.add_field(
                name=f"[{row['type']}] - ID #{row['id']}",
                value=f"**Motivo:** {row['reason']}\n**Data:** {row['timestamp']}",
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(ModerationCog(bot))
 
