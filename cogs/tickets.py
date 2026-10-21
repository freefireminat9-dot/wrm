import io
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from database import get_db

class ConfirmCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Confirmar Fechamento", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        channel = interaction.channel
        
        history = []
        async for msg in channel.history(limit=500, oldest_first=True):
            history.append(f"[{msg.created_at.strftime('%d/%m/%Y %H:%M:%S')}] {msg.author} ({msg.author.id}): {msg.clean_content}")
            
        transcript_text = f"--- TRANSCRIPT TICKET: {channel.name} ---\n" + "\n".join(history)
        file_bytes = io.BytesIO(transcript_text.encode('utf-8'))
        file = discord.File(file_bytes, filename=f"transcript-{channel.name}.txt")

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT logs_channel FROM guild_configs WHERE guild_id = ?", (interaction.guild.id,))
            cfg = cursor.fetchone()
            cursor.execute("DELETE FROM tickets WHERE channel_id = ?", (channel.id,))

        if cfg and cfg["logs_channel"]:
            log_chan = interaction.guild.get_channel(cfg["logs_channel"])
            if log_chan:
                embed_log = discord.Embed(
                    title="🔒 Ticket Encerrado",
                    description=f"**Canal:** `{channel.name}`\n**Fechado por:** {interaction.user.mention}",
                    color=discord.Color.red()
                )
                await log_chan.send(embed=embed_log, file=file)

        await channel.send("🔒 Ticket encerrado. Deletando canal em 5 segundos...")
        await asyncio.sleep(5)
        try:
            await channel.delete()
        except discord.NotFound:
            pass

class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fechar Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket_btn", emoji="🔒")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⚠️ Deseja encerrar e gerar o histórico deste ticket?", view=ConfirmCloseView(), ephemeral=True)

class TicketOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Abrir Ticket", style=discord.ButtonStyle.success, custom_id="open_ticket_btn", emoji="🎫")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM guild_configs WHERE guild_id = ?", (guild.id,))
            cfg = cursor.fetchone()
            
            if not cfg or not cfg["ticket_category"]:
                return await interaction.response.send_message("❌ A categoria de tickets não foi configurada.", ephemeral=True)
            
            cursor.execute("SELECT * FROM tickets WHERE guild_id = ? AND user_id = ? AND status = 'open'", (guild.id, user.id))
            if cursor.fetchone():
                return await interaction.response.send_message("❌ Você já possui um ticket aberto!", ephemeral=True)

        category = guild.get_channel(cfg["ticket_category"])
        if not category or not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message("❌ Categoria de tickets inválida.", ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }
        
        if cfg["admin_role"]:
            role = guild.get_role(cfg["admin_role"])
            if role:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        try:
            ticket_channel = await guild.create_text_channel(
                name=f"ticket-{user.name}",
                category=category,
                overwrites=overwrites,
                topic=f"Atendimento do usuário ID: {user.id}"
            )
        except discord.Forbidden:
            return await interaction.response.send_message("❌ O bot não possui permissão para criar canais na categoria.", ephemeral=True)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO tickets (guild_id, channel_id, user_id, status) VALUES (?, ?, ?, 'open')", (guild.id, ticket_channel.id, user.id))

        embed = discord.Embed(
            title=f"🎫 Atendimento - {user.display_name}",
            description="Explique a sua solicitação detalhadamente. A equipe responderá em breve.",
            color=discord.Color.green()
        )
        await ticket_channel.send(content=user.mention, embed=embed, view=TicketCloseView())
        await interaction.response.send_message(f"✅ Ticket criado em {ticket_channel.mention}!", ephemeral=True)

class TicketsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ticket", description="Publica o painel de suporte.")
    async def ticket_panel(self, interaction: discord.Interaction):
        from cogs.config import check_admin_permission
        if not await check_admin_permission(interaction):
            return await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)

        embed = discord.Embed(
            title="📞 Central de Atendimento & Suporte",
            description="Clique no botão abaixo para abrir um ticket de atendimento privado.",
            color=discord.Color.gold()
        )
        embed.set_footer(text="Atendimento privado entre você e a staff.")
        
        await interaction.channel.send(embed=embed, view=TicketOpenView())
        await interaction.response.send_message("✅ Painel enviado com sucesso!", ephemeral=True)

async def setup(bot):
    bot.add_view(TicketOpenView())
    bot.add_view(TicketCloseView())
    await bot.add_cog(TicketsCog(bot))
