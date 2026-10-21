import os
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DB_NAME = "database.db"

# ==================== BANCO DE DADOS ====================
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS guild_configs (
                guild_id INTEGER PRIMARY KEY,
                scrims_channel INTEGER,
                freeagents_channel INTEGER,
                contracts_channel INTEGER,
                dismissals_channel INTEGER,
                logs_channel INTEGER,
                ticket_channel INTEGER,
                ticket_category INTEGER,
                admin_role INTEGER
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                channel_id INTEGER,
                user_id INTEGER,
                status TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS punishments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                user_id INTEGER,
                moderator_id INTEGER,
                type TEXT,
                reason TEXT,
                duration TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                name TEXT,
                acronym TEXT,
                owner_id INTEGER,
                logo_url TEXT,
                UNIQUE(guild_id, name),
                UNIQUE(guild_id, acronym)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS contracts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                team_id INTEGER,
                player_id INTEGER,
                role TEXT,
                position TEXT,
                status TEXT DEFAULT 'active'
            )
        """)

# ==================== FUNÇÕES AUXILIARES ====================
async def check_admin_permission(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT admin_role FROM guild_configs WHERE guild_id = ?", (interaction.guild.id,))
        row = cursor.fetchone()
        if row and row["admin_role"]:
            role = interaction.guild.get_role(row["admin_role"])
            if role and role in interaction.user.roles:
                return True
    return False

async def log_action(guild: discord.Guild, text: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT logs_channel FROM guild_configs WHERE guild_id = ?", (guild.id,))
        row = cursor.fetchone()
        if row and row["logs_channel"]:
            channel = guild.get_channel(row["logs_channel"])
            if channel:
                await channel.send(text)

# ==================== VIEWS INTERATIVAS ====================
class ConfigSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    async def save_config(self, interaction: discord.Interaction, field: str, value: int, message: str):
        if not await check_admin_permission(interaction):
            return await interaction.response.send_message("Você não tem permissão para alterar as configurações.", ephemeral=True)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO guild_configs (guild_id) VALUES (?)", (interaction.guild.id,))
            cursor.execute(f"UPDATE guild_configs SET {field} = ? WHERE guild_id = ?", (value, interaction.guild.id))
        await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Selecione o canal de Scrims", channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
    async def select_scrims(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self.save_config(interaction, "scrims_channel", select.values[0].id, "Canal de Scrims configurado!")

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Selecione o canal de Free Agents", channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
    async def select_freeagents(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self.save_config(interaction, "freeagents_channel", select.values[0].id, "Canal de Free Agents configurado!")

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Selecione o canal de Contratos", channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
    async def select_contracts(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self.save_config(interaction, "contracts_channel", select.values[0].id, "Canal de Contratos configurado!")

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Selecione o canal de Demissões", channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
    async def select_dismissals(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self.save_config(interaction, "dismissals_channel", select.values[0].id, "Canal de Demissões configurado!")

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Selecione o canal de Logs", channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
    async def select_logs(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self.save_config(interaction, "logs_channel", select.values[0].id, "Canal de Logs configurado!")

class ConfirmCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Confirmar Fechamento", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        channel = interaction.channel
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT logs_channel FROM guild_configs WHERE guild_id = ?", (interaction.guild.id,))
            cfg = cursor.fetchone()
            cursor.execute("DELETE FROM tickets WHERE channel_id = ?", (channel.id,))
            
        if cfg and cfg["logs_channel"]:
            log_chan = interaction.guild.get_channel(cfg["logs_channel"])
            if log_chan:
                await log_chan.send(f"🔒 **Ticket Fechado**: O canal `{channel.name}` foi fechado por {interaction.user.mention}.")

        await channel.send("Encerrando o ticket em 5 segundos...")
        import asyncio
        await asyncio.sleep(5)
        await channel.delete()

class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fechar Ticket", style=discord.ButtonStyle.danger, custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Tem certeza que deseja fechar este ticket?", view=ConfirmCloseView(), ephemeral=True)

class TicketOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Abrir Ticket", style=discord.ButtonStyle.success, custom_id="open_ticket_btn")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM guild_configs WHERE guild_id = ?", (guild.id,))
            cfg = cursor.fetchone()
            if not cfg or not cfg["ticket_category"]:
                return await interaction.response.send_message("O sistema de tickets não está totalmente configurado neste servidor.", ephemeral=True)
            
            cursor.execute("SELECT * FROM tickets WHERE guild_id = ? AND user_id = ? AND status = 'open'", (guild.id, user.id))
            if cursor.fetchone():
                return await interaction.response.send_message("Você já possui um ticket aberto!", ephemeral=True)

        category = guild.get_channel(cfg["ticket_category"])
        if not category:
            return await interaction.response.send_message("Categoria de tickets não encontrada.", ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        if cfg.get("admin_role"):
            role = guild.get_role(cfg["admin_role"])
            if role:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        try:
            ticket_channel = await guild.create_text_channel(name=f"ticket-{user.name}", category=category, overwrites=overwrites)
        except Exception as e:
            return await interaction.response.send_message(f"Erro ao criar canal de ticket: {e}", ephemeral=True)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO tickets (guild_id, channel_id, user_id, status) VALUES (?, ?, ?, 'open')", (guild.id, ticket_channel.id, user.id))

        embed = discord.Embed(title=f"Atendimento de {user.name}", description="Explique detalhadamente o seu problema ou solicitação. A equipe responderá em breve.", color=discord.Color.green())
        await ticket_channel.send(content=user.mention, embed=embed, view=TicketCloseView())
        await interaction.response.send_message(f"Seu ticket foi aberto com sucesso em {ticket_channel.mention}!", ephemeral=True)

# ==================== BOT PRINCIPAL ====================
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True

class LeagueBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        init_db()
        self.add_view(TicketOpenView())
        self.add_view(TicketCloseView())
        try:
            synced = await self.tree.sync()
            print(f"Sincronizados {len(synced)} comandos slash.")
        except Exception as e:
            print(f"Erro ao sincronizar comandos: {e}")

    async def on_ready(self):
        print(f"Bot conectado como {self.user} (ID: {self.user.id})")

bot = LeagueBot()

# ==================== COMANDOS SLASH ====================

@bot.tree.command(name="configurar", description="Painel de configuração do bot para administradores.")
async def configurar(interaction: discord.Interaction):
    if not await check_admin_permission(interaction):
        return await interaction.response.send_message("Apenas administradores ou membros com cargo autorizado podem usar este comando.", ephemeral=True)

    embed = discord.Embed(title="Painel de Configuração da Liga", description="Utilize os seletores abaixo para configurar os canais do servidor.", color=discord.Color.blue())
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM guild_configs WHERE guild_id = ?", (interaction.guild.id,))
        cfg = cursor.fetchone()

    def get_mention(cid, type_str="channel"):
        if not cid: return "Não configurado"
        obj = interaction.guild.get_channel(cid) if type_str=="channel" else interaction.guild.get_role(cid)
        return obj.mention if obj else "Não encontrado"

    if cfg:
        embed.add_field(name="Scrims", value=get_mention(cfg["scrims_channel"]), inline=True)
        embed.add_field(name="Free Agents", value=get_mention(cfg["freeagents_channel"]), inline=True)
        embed.add_field(name="Contratos", value=get_mention(cfg["contracts_channel"]), inline=True)
        embed.add_field(name="Demissões", value=get_mention(cfg["dismissals_channel"]), inline=True)
        embed.add_field(name="Logs", value=get_mention(cfg["logs_channel"]), inline=True)
        embed.add_field(name="Cargo Admin", value=get_mention(cfg["admin_role"], "role"), inline=True)
    else:
        embed.add_field(name="Status", value="Nenhuma configuração salva ainda.", inline=False)

    await interaction.response.send_message(embed=embed, view=ConfigSelectView(), ephemeral=True)

@bot.tree.command(name="ticket", description="Publica o painel de abertura de tickets.")
@app_commands.checks.has_permissions(administrator=True)
async def ticket_panel(interaction: discord.Interaction):
    embed = discord.Embed(title="Central de Atendimento", description="Clique no botão abaixo para abrir um ticket de atendimento com a staff.", color=discord.Color.gold())
    await interaction.channel.send(embed=embed, view=TicketOpenView())
    await interaction.response.send_message("Painel de tickets publicado com sucesso!", ephemeral=True)

@bot.tree.command(name="scrim", description="Publica um anúncio de scrim ou amistoso.")
@app_commands.describe(pingar_everyone="Marcar @everyone?", informacoes="Informações detalhadas da scrim")
async def scrim(interaction: discord.Interaction, pingar_everyone: bool, informacoes: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT scrims_channel FROM guild_configs WHERE guild_id = ?", (interaction.guild.id,))
        cfg = cursor.fetchone()
        
    if not cfg or not cfg["scrims_channel"]:
        return await interaction.response.send_message("Canal de Scrims não configurado.", ephemeral=True)
    
    channel = interaction.guild.get_channel(cfg["scrims_channel"])
    embed = discord.Embed(title="⚔️ Novo Desafio / Scrim", description=informacoes, color=discord.Color.blurple())
    embed.set_footer(text=f"Publicado por {interaction.user.name}")
    
    content = "@everyone" if pingar_everyone else ""
    await channel.send(content=content, embed=embed)
    await log_action(interaction.guild, f"⚔️ **Scrim Publicada** por {interaction.user.mention}")
    await interaction.response.send_message("Scrim publicada com sucesso!", ephemeral=True)

@bot.tree.command(name="freeagent", description="Anuncia jogador disponível no mercado.")
@app_commands.describe(informacoes="Informações e estatísticas do jogador")
async def freeagent(interaction: discord.Interaction, informacoes: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT freeagents_channel FROM guild_configs WHERE guild_id = ?", (interaction.guild.id,))
        cfg = cursor.fetchone()
        
    if not cfg or not cfg["freeagents_channel"]:
        return await interaction.response.send_message("Canal de Free Agents não configurado.", ephemeral=True)
    
    channel = interaction.guild.get_channel(cfg["freeagents_channel"])
    embed = discord.Embed(title="👤 Jogador Disponível (Free Agent)", description=informacoes, color=discord.Color.orange())
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    
    await channel.send(embed=embed)
    await log_action(interaction.guild, f"👤 **Free Agent anunciado**: {interaction.user.mention}")
    await interaction.response.send_message("Anúncio de Free Agent publicado!", ephemeral=True)

@bot.tree.command(name="criar-time", description="Cadastra um novo clube na liga.")
@app_commands.describe(nome="Nome do time", sigla="Sigla (tag) do time", responsavel="Dono ou manager do time", logo_url="URL do logotipo opcional")
async def criar_time(interaction: discord.Interaction, nome: str, sigla: str, responsavel: discord.Member, logo_url: str = None):
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO teams (guild_id, name, acronym, owner_id, logo_url) VALUES (?, ?, ?, ?, ?)",
                (interaction.guild.id, nome.lower(), sigla.upper(), responsavel.id, logo_url)
            )
        await interaction.response.send_message(f"Time **{nome}** ({sigla.upper()}) cadastrado com sucesso sob a responsabilidade de {responsavel.mention}!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Erro ao cadastrar time (Nome ou Sigla já podem existir): {e}", ephemeral=True)

@bot.tree.command(name="contrato", description="Registra a contratação de um jogador por um time.")
@app_commands.describe(jogador="Jogador contratado", time_nome="Nome exato do time", posicao="Posição (ex: Atacante)", role="Titular ou Reserva")
@app_commands.choices(role=[
    app_commands.Choice(name="Titular", value="Titular"),
    app_commands.Choice(name="Reserva", value="Reserva")
])
async def contrato(interaction: discord.Interaction, jogador: discord.Member, time_nome: str, posicao: str, role: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM teams WHERE guild_id = ? AND name = ?", (interaction.guild.id, time_nome.lower()))
        team = cursor.fetchone()
        
        if not team:
            return await interaction.response.send_message("Time não encontrado.", ephemeral=True)
        
        cursor.execute("SELECT contracts_channel FROM guild_configs WHERE guild_id = ?", (interaction.guild.id,))
        cfg = cursor.fetchone()
        
        cursor.execute("INSERT INTO contracts (guild_id, team_id, player_id, role, position) VALUES (?, ?, ?, ?, ?)",
                       (interaction.guild.id, team["id"], jogador.id, role, posicao))

    if cfg and cfg["contracts_channel"]:
        channel = interaction.guild.get_channel(cfg["contracts_channel"])
        embed = discord.Embed(title="📝 Novo Contrato Registrado", color=discord.Color.green())
        embed.add_field(name="Jogador", value=jogador.mention, inline=True)
        embed.add_field(name="Time", value=time_nome.upper(), inline=True)
        embed.add_field(name="Posição / Função", value=f"{posicao} ({role})", inline=True)
        await channel.send(embed=embed)

    await interaction.response.send_message("Contrato registrado com sucesso!", ephemeral=True)

@bot.tree.command(name="demitir", description="Registra a demissão de um jogador de seu clube.")
async def demitir(interaction: discord.Interaction, jogador: discord.Member):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM contracts WHERE guild_id = ? AND player_id = ?", (interaction.guild.id, jogador.id))
        cursor.execute("SELECT dismissals_channel FROM guild_configs WHERE guild_id = ?", (interaction.guild.id,))
        cfg = cursor.fetchone()

    if cfg and cfg["dismissals_channel"]:
        channel = interaction.guild.get_channel(cfg["dismissals_channel"])
        embed = discord.Embed(title="❌ Jogador Dispensado (Demissão)", description=f"O jogador {jogador.mention} foi desligado de sua equipe.", color=discord.Color.red())
        await channel.send(embed=embed)

    await interaction.response.send_message(f"Demissão de {jogador.mention} registrada.", ephemeral=True)

@bot.tree.command(name="ban", description="Bane um usuário do servidor.")
@app_commands.describe(usuario="Usuário para banir", motivo="Motivo do banimento")
async def ban(interaction: discord.Interaction, usuario: discord.Member, motivo: str = "Não informado"):
    if not interaction.user.guild_permissions.ban_members:
        return await interaction.response.send_message("Você não tem permissão para banir membros.", ephemeral=True)
    if usuario.top_role >= interaction.guild.me.top_role:
        return await interaction.response.send_message("Não posso banir um usuário com cargo superior ou igual ao meu.", ephemeral=True)

    try:
        await usuario.ban(reason=motivo)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO punishments (guild_id, user_id, moderator_id, type, reason) VALUES (?, ?, ?, 'BAN', ?)",
                           (interaction.guild.id, usuario.id, interaction.user.id, motivo))
        
        await log_action(interaction.guild, f"🔨 **Banimento**: {usuario.mention} foi banido por {interaction.user.mention}. Motivo: {motivo}")
        await interaction.response.send_message(f"Usuário {usuario.mention} banido com sucesso.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Erro ao banir: {e}", ephemeral=True)

@bot.tree.command(name="timeout", description="Aplica silêncio temporário (timeout) em um usuário.")
@app_commands.describe(usuario="Usuário", minutos="Duração em minutos", motivo="Motivo")
async def timeout(interaction: discord.Interaction, usuario: discord.Member, minutos: int, motivo: str = "Não informado"):
    if not interaction.user.guild_permissions.moderate_members:
        return await interaction.response.send_message("Você não tem permissão para aplicar timeout.", ephemeral=True)

    try:
        duration = timedelta(minutes=minutos)
        await usuario.timeout(duration, reason=motivo)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO punishments (guild_id, user_id, moderator_id, type, reason, duration) VALUES (?, ?, ?, 'TIMEOUT', ?, ?)",
                           (interaction.guild.id, usuario.id, interaction.user.id, motivo, f"{minutos} min"))
        
        await log_action(interaction.guild, f"⏱️ **Timeout**: {usuario.mention} ficou silenciado por {minutos} min por {interaction.user.mention}.")
        await interaction.response.send_message(f"Timeout aplicado em {usuario.mention}.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Erro ao aplicar timeout: {e}", ephemeral=True)

@bot.tree.command(name="punições", description="Consulta o histórico de punições de um usuário.")
async def punicoes(interaction: discord.Interaction, usuario: discord.Member):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM punishments WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, usuario.id))
        rows = cursor.fetchall()

    if not rows:
        return await interaction.response.send_message(f"O usuário {usuario.mention} não possui punições registradas.", ephemeral=True)

    embed = discord.Embed(title=f"Histórico de Punições: {usuario.display_name}", color=discord.Color.red())
    for r in rows:
        embed.add_field(name=f"Tipo: {r['type']}", value=f"**Motivo:** {r['reason']}\n**Data:** {r['timestamp']}", inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

if __name__ == "__main__":
    if not TOKEN:
        print("Erro: Token do bot não encontrado no arquivo .env!")
    else:
        bot.run(TOKEN)
