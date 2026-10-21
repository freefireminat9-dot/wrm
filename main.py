"""
Bot de administração de Liga MPS — arquivo único.
Dependências: discord.py 2.x, aiosqlite, python-dotenv
Configure DISCORD_TOKEN nas variáveis de ambiente da hospedagem.
Opcional: GUILD_ID para sincronização imediata em servidor de teste.
"""
import os
import io
import logging
from datetime import datetime, timezone, timedelta

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
DB_PATH = os.getenv("DATABASE_PATH", "mps_bot.sqlite3")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mps-bot")

def utcnow():
    return datetime.now(timezone.utc)

def stamp():
    return utcnow().isoformat(timespec="seconds")

def parse_duration(value: str | None):
    """Aceita duração como 10m, 2h, 3d, 1w. Retorna timedelta ou None."""
    if not value:
        return None
    value = value.strip().lower()
    if len(value) < 2 or not value[:-1].isdigit():
        raise ValueError("Use duração como `10m`, `2h`, `3d` ou `1w`.")
    amount, unit = int(value[:-1]), value[-1]
    factors = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    if unit not in factors or amount < 1:
        raise ValueError("Use duração como `10m`, `2h`, `3d` ou `1w`.")
    seconds = amount * factors[unit]
    if seconds > 28 * 86400:
        raise ValueError("A duração máxima é 28 dias.")
    return timedelta(seconds=seconds)

async def db():
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    return conn

async def init_db():
    async with await db() as con:
        await con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings(
          guild_id INTEGER PRIMARY KEY,
          scrims_channel INTEGER, freeagents_channel INTEGER, contracts_channel INTEGER,
          dismissals_channel INTEGER, logs_channel INTEGER, tickets_channel INTEGER,
          ticket_category INTEGER, admin_role INTEGER,
          allow_multiple_tickets INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tickets(
          id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
          channel_id INTEGER NOT NULL UNIQUE, user_id INTEGER NOT NULL,
          opened_at TEXT NOT NULL, closed_at TEXT, status TEXT NOT NULL DEFAULT 'open'
        );
        CREATE INDEX IF NOT EXISTS ticket_open_idx ON tickets(guild_id,user_id,status);
        CREATE TABLE IF NOT EXISTS punishments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
          target_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL, kind TEXT NOT NULL,
          reason TEXT NOT NULL, created_at TEXT NOT NULL, duration TEXT,
          active INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS punish_idx ON punishments(guild_id,target_id);
        CREATE TABLE IF NOT EXISTS teams(
          id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
          name TEXT NOT NULL COLLATE NOCASE, tag TEXT NOT NULL COLLATE NOCASE,
          owner_id INTEGER NOT NULL, logo_url TEXT, active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL, UNIQUE(guild_id,name), UNIQUE(guild_id,tag)
        );
        CREATE TABLE IF NOT EXISTS contracts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
          player_id INTEGER NOT NULL, team_id INTEGER NOT NULL, position TEXT NOT NULL,
          role TEXT NOT NULL, manager_id INTEGER NOT NULL, started_at TEXT NOT NULL,
          ended_at TEXT, end_reason TEXT,
          FOREIGN KEY(team_id) REFERENCES teams(id)
        );
        CREATE INDEX IF NOT EXISTS contract_active_idx ON contracts(guild_id,player_id,ended_at);
        CREATE TABLE IF NOT EXISTS audit_logs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
          actor_id INTEGER, action TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        await con.commit()

async def settings(guild_id):
    async with await db() as con:
        await con.execute("INSERT OR IGNORE INTO settings(guild_id) VALUES(?)", (guild_id,))
        cur = await con.execute("SELECT * FROM settings WHERE guild_id=?", (guild_id,))
        row = await cur.fetchone()
        return dict(row)

async def set_setting(guild_id, key, value):
    allowed = {"scrims_channel","freeagents_channel","contracts_channel","dismissals_channel",
               "logs_channel","tickets_channel","ticket_category","admin_role","allow_multiple_tickets"}
    if key not in allowed:
        raise ValueError("Configuração inválida.")
    async with await db() as con:
        await con.execute("INSERT OR IGNORE INTO settings(guild_id) VALUES(?)", (guild_id,))
        await con.execute(f"UPDATE settings SET {key}=? WHERE guild_id=?", (value, guild_id))
        await con.commit()

async def log_action(bot, guild, actor, action, details):
    actor_id = getattr(actor, "id", actor if isinstance(actor, int) else None)
    async with await db() as con:
        await con.execute(
            "INSERT INTO audit_logs(guild_id,actor_id,action,details,created_at) VALUES(?,?,?,?,?)",
            (guild.id, actor_id, action, str(details)[:1800], stamp()))
        await con.commit()
    cfg = await settings(guild.id)
    channel = guild.get_channel(cfg.get("logs_channel") or 0)
    if channel and channel.permissions_for(guild.me).send_messages:
        embed = discord.Embed(title=f"Registro • {action}", color=discord.Color.blurple(), timestamp=utcnow())
        embed.add_field(name="Responsável", value=f"<@{actor_id}>" if actor_id else "Sistema", inline=True)
        embed.add_field(name="Detalhes", value=str(details)[:1024] or "—", inline=False)
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

async def authorized(interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.id == interaction.guild.owner_id:
        return True
    cfg = await settings(interaction.guild.id)
    role_id = cfg.get("admin_role")
    return bool(role_id and any(role.id == role_id for role in interaction.user.roles))

async def require_admin(interaction):
    if await authorized(interaction):
        return True
    if interaction.guild:
        try:
            await log_action(interaction.client, interaction.guild, interaction.user,
                             "Acesso não autorizado", f"Tentativa de usar /{interaction.command.name if interaction.command else 'comando'}")
        except Exception:
            log.exception("Falha registrando tentativa não autorizada")
    msg = "Você precisa ser o dono do servidor ou ter o cargo administrativo configurado em /configurar."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    return False

async def output_channel(guild, key):
    cfg = await settings(guild.id)
    return guild.get_channel(cfg.get(key) or 0)

async def active_contract(guild_id, player_id):
    async with await db() as con:
        cur = await con.execute("""SELECT c.*,t.name AS team_name,t.tag AS team_tag
          FROM contracts c JOIN teams t ON t.id=c.team_id
          WHERE c.guild_id=? AND c.player_id=? AND c.ended_at IS NULL AND t.active=1
          ORDER BY c.id DESC LIMIT 1""", (guild_id, player_id))
        row = await cur.fetchone()
        return dict(row) if row else None

async def add_punishment(guild_id, target_id, moderator_id, kind, reason, duration=None, active=1):
    async with await db() as con:
        await con.execute("""INSERT INTO punishments
          (guild_id,target_id,moderator_id,kind,reason,created_at,duration,active)
          VALUES(?,?,?,?,?,?,?,?)""",
          (guild_id,target_id,moderator_id,kind,reason,stamp(),duration,active))
        await con.commit()

async def check_bot_permission(interaction, permission):
    me = interaction.guild.me
    if not me or not getattr(me.guild_permissions, permission, False):
        await interaction.response.send_message(
            f"Preciso da permissão **{permission.replace('_',' ')}** para fazer isso.", ephemeral=True)
        return False
    return True

def is_higher_than_bot(guild, member):
    me = guild.me
    return bool(not me or member.id == guild.owner_id or member.top_role >= me.top_role)

# ---------------- Configuração interativa ----------------
CONFIG_OPTIONS = [
    ("scrims_channel","Canal de Scrims","channel"),
    ("freeagents_channel","Canal de Free Agents","channel"),
    ("contracts_channel","Canal de Contratos","channel"),
    ("dismissals_channel","Canal de Demissões","channel"),
    ("logs_channel","Canal de Logs","channel"),
    ("tickets_channel","Canal de Tickets","channel"),
    ("ticket_category","Categoria de Tickets","category"),
    ("admin_role","Cargo Administrativo","role"),
]

def show_config_value(guild, value):
    if not value:
        return "Não configurado"
    obj = guild.get_role(value) or guild.get_channel(value)
    return obj.mention if obj else f"ID inválido ({value})"

class ConfigValueView(discord.ui.View):
    def __init__(self, key, kind):
        super().__init__(timeout=180)
        self.key = key
        if kind == "role":
            self.add_item(ConfigRoleSelect())
        elif kind == "category":
            self.add_item(ConfigCategorySelect())
        else:
            self.add_item(ConfigChannelSelect())

    async def save(self, interaction, value):
        if not await require_admin(interaction):
            return
        await set_setting(interaction.guild.id, self.key, value)
        label = dict((k, label) for k, label, _ in CONFIG_OPTIONS)[self.key]
        await log_action(interaction.client, interaction.guild, interaction.user,
                         "Configuração alterada", f"{label}: {value or 'limpa'}")
        await interaction.response.send_message(f"Configuração **{label}** salva.", ephemeral=True)

    @discord.ui.button(label="Limpar configuração", style=discord.ButtonStyle.secondary, emoji="🧹")
    async def clear(self, interaction, button):
        await self.save(interaction, None)

class ConfigRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Selecione o cargo", min_values=1, max_values=1)
    async def callback(self, interaction):
        await self.view.save(interaction, self.values[0].id)

class ConfigChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="Selecione canal de texto", channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
    async def callback(self, interaction):
        await self.view.save(interaction, self.values[0].id)

class ConfigCategorySelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="Selecione a categoria", channel_types=[discord.ChannelType.category], min_values=1, max_values=1)
    async def callback(self, interaction):
        await self.view.save(interaction, self.values[0].id)

class ConfigMenu(discord.ui.Select):
    def __init__(self):
        opts = [discord.SelectOption(label=label, value=key) for key,label,_ in CONFIG_OPTIONS]
        opts.append(discord.SelectOption(label="Permitir vários tickets", value="allow_multiple_tickets"))
        super().__init__(placeholder="Escolha uma configuração…", options=opts, min_values=1, max_values=1)
    async def callback(self, interaction):
        if not await require_admin(interaction):
            return
        key = self.values[0]
        if key == "allow_multiple_tickets":
            cfg = await settings(interaction.guild.id)
            value = 0 if cfg.get(key) else 1
            await set_setting(interaction.guild.id, key, value)
            await log_action(interaction.client, interaction.guild, interaction.user,
                             "Configuração alterada", f"Vários tickets: {'ativado' if value else 'desativado'}")
            await interaction.response.send_message(f"Vários tickets simultâneos: {'ativado' if value else 'desativado'}.", ephemeral=True)
            return
        kind = next(item[2] for item in CONFIG_OPTIONS if item[0] == key)
        await interaction.response.send_message("Selecione o novo valor:", view=ConfigValueView(key, kind), ephemeral=True)

class ConfigPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(ConfigMenu())

# ---------------- Tickets: botões persistentes ----------------
def ticket_embed(title, description, color=discord.Color.blurple()):
    return discord.Embed(title=title, description=description, color=color, timestamp=utcnow())

async def open_ticket(interaction):
    guild, user = interaction.guild, interaction.user
    cfg = await settings(guild.id)
    category = guild.get_channel(cfg.get("ticket_category") or 0)
    if not isinstance(category, discord.CategoryChannel):
        await interaction.response.send_message("Categoria de tickets não configurada. Use /configurar.", ephemeral=True)
        return
    if not cfg.get("allow_multiple_tickets"):
        async with await db() as con:
            cur = await con.execute("SELECT 1 FROM tickets WHERE guild_id=? AND user_id=? AND status='open' LIMIT 1",
                                    (guild.id, user.id))
            if await cur.fetchone():
                await interaction.response.send_message("Você já tem um ticket aberto.", ephemeral=True)
                return
    me = guild.me
    if not me or not me.guild_permissions.manage_channels:
        await interaction.response.send_message("O bot precisa da permissão Gerenciar Canais.", ephemeral=True)
        return
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                          attach_files=True, embed_links=True),
        me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)
    }
    role = guild.get_role(cfg.get("admin_role") or 0)
    if role:
        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    try:
        channel = await guild.create_text_channel(
            name=f"ticket-{user.name}".lower().replace(" ", "-")[:90],
            category=category, overwrites=overwrites, reason=f"Ticket aberto por {user}")
    except discord.HTTPException as exc:
        await interaction.response.send_message(f"Não consegui criar o ticket. Confira permissões/limite de canais. ({exc})", ephemeral=True)
        return
    async with await db() as con:
        await con.execute("INSERT INTO tickets(guild_id,channel_id,user_id,opened_at,status) VALUES(?,?,?,?, 'open')",
                          (guild.id, channel.id, user.id, stamp()))
        await con.commit()
    await channel.send(user.mention, embed=ticket_embed("🎫 Atendimento aberto",
        f"Olá {user.mention}! Explique como podemos ajudar.\nA equipe responderá por este canal.\nUse o botão abaixo para encerrar."),
        view=CloseTicketView())
    await interaction.response.send_message(f"Ticket criado: {channel.mention}", ephemeral=True)
    await log_action(interaction.client, guild, user, "Ticket aberto", f"Usuário: {user.mention}; canal: {channel.mention}")

class OpenTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="Abrir ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="mps:ticket:open")
    async def open_button(self, interaction, button):
        if not interaction.guild:
            await interaction.response.send_message("Use este botão em um servidor.", ephemeral=True)
            return
        await open_ticket(interaction)

class CloseConfirmView(discord.ui.View):
    def __init__(self, channel_id):
        super().__init__(timeout=60)
        self.channel_id = channel_id
    @discord.ui.button(label="Confirmar fechamento", style=discord.ButtonStyle.danger, emoji="🔒")
    async def confirm(self, interaction, button):
        guild = interaction.guild
        channel = guild.get_channel(self.channel_id) if guild else None
        if not channel:
            await interaction.response.send_message("Canal não encontrado.", ephemeral=True)
            return
        async with await db() as con:
            cur = await con.execute("SELECT user_id FROM tickets WHERE channel_id=? AND status='open'", (channel.id,))
            row = await cur.fetchone()
        if not row:
            await interaction.response.send_message("Ticket já encerrado.", ephemeral=True)
            return
        opener_id = row["user_id"]
        if interaction.user.id != opener_id and not await authorized(interaction):
            await interaction.response.send_message("Somente quem abriu o ticket ou a equipe autorizada pode fechar.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        lines = []
        try:
            async for msg in channel.history(limit=None, oldest_first=True):
                text = msg.content.replace("\n", " ") if msg.content else ""
                attachments = " ".join(a.url for a in msg.attachments)
                lines.append(f"[{msg.created_at:%Y-%m-%d %H:%M:%S UTC}] {msg.author} ({msg.author.id}): {text} {attachments}".strip())
            transcript = discord.File(io.BytesIO(("\n".join(lines) or "Sem mensagens.").encode("utf-8")),
                                      filename=f"transcript-{channel.id}.txt")
            cfg = await settings(guild.id)
            log_channel = guild.get_channel(cfg.get("logs_channel") or 0)
            if log_channel:
                try:
                    await log_channel.send(content=f"Transcript de {channel.name}, encerrado por {interaction.user.mention}", file=transcript)
                except discord.HTTPException:
                    pass
            member = guild.get_member(opener_id)
            if member:
                try:
                    await member.send(f"Seu ticket em **{guild.name}** foi encerrado por {interaction.user}.")
                except discord.HTTPException:
                    pass
            async with await db() as con:
                await con.execute("UPDATE tickets SET status='closed',closed_at=? WHERE channel_id=?", (stamp(), channel.id))
                await con.commit()
            await log_action(interaction.client, guild, interaction.user, "Ticket fechado",
                             f"Canal {channel.name} ({channel.id}); autor <@{opener_id}>")
            await interaction.followup.send("Ticket encerrado. Transcript enviado aos logs quando configurados.", ephemeral=True)
            await channel.delete(reason=f"Ticket fechado por {interaction.user}")
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Erro ao fechar ticket: {exc}", ephemeral=True)
    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.send_message("Fechamento cancelado.", ephemeral=True)

class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="Fechar ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="mps:ticket:close")
    async def close_button(self, interaction, button):
        await interaction.response.send_message("Tem certeza que deseja encerrar?", view=CloseConfirmView(interaction.channel_id), ephemeral=True)

# ---------------- Bot e comandos slash ----------------
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def setup_hook_placeholder():
    pass

@bot.event
async def on_ready():
    log.info("Conectado como %s (%s)", bot.user, bot.user.id)

@bot.tree.error
async def on_tree_error(interaction, error):
    log.exception("Erro de comando slash", exc_info=error)
    msg = "Ocorreu um erro ao executar o comando. Confira as permissões e tente novamente."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass

@bot.tree.command(name="configurar", description="Abre o painel de configuração da liga.")
async def configurar(interaction: discord.Interaction):
    if not await require_admin(interaction): return
    cfg = await settings(interaction.guild.id)
    embed = discord.Embed(title="⚙️ Configuração da Liga MPS",
                          description="Selecione uma opção no menu. As alterações são salvas no SQLite.",
                          color=discord.Color.blurple())
    for key, label, _ in CONFIG_OPTIONS:
        embed.add_field(name=label, value=show_config_value(interaction.guild, cfg.get(key)), inline=True)
    embed.add_field(name="Vários tickets", value="Ativado" if cfg.get("allow_multiple_tickets") else "Desativado", inline=True)
    await interaction.response.send_message(embed=embed, view=ConfigPanel(), ephemeral=True)

@bot.tree.command(name="ticket", description="Publica o painel de abertura de tickets.")
async def ticket(interaction: discord.Interaction):
    if not await require_admin(interaction): return
    channel = await output_channel(interaction.guild, "tickets_channel")
    if not channel:
        await interaction.response.send_message("Configure o canal de tickets em /configurar.", ephemeral=True); return
    if not channel.permissions_for(interaction.guild.me).send_messages:
        await interaction.response.send_message("Não tenho permissão para enviar mensagens nesse canal.", ephemeral=True); return
    await channel.send(embed=ticket_embed("🎫 Central de Atendimento", "Clique no botão abaixo para abrir um canal privado."),
                       view=OpenTicketView())
    await interaction.response.send_message(f"Painel publicado em {channel.mention}.", ephemeral=True)
    await log_action(bot, interaction.guild, interaction.user, "Painel de tickets publicado", channel.mention)

@bot.tree.command(name="scrim", description="Publica um desafio ou amistoso entre equipes.")
@app_commands.describe(equipes="Times que vão jogar", informacoes="Data, formato e regras", pingar_everyone="Mencionar @everyone (se tiver permissão)")
async def scrim(interaction: discord.Interaction, equipes: str, informacoes: str = "", pingar_everyone: bool = False):
    if not await require_admin(interaction): return
    channel = await output_channel(interaction.guild, "scrims_channel")
    if not channel:
        await interaction.response.send_message("Configure o canal de Scrims em /configurar.", ephemeral=True); return
    mention = "@everyone" if pingar_everyone and interaction.user.guild_permissions.mention_everyone else None
    embed = discord.Embed(title="⚔️ DESAFIO / SCRIM", color=discord.Color.orange(), timestamp=utcnow())
    embed.add_field(name="Equipes", value=equipes[:1024], inline=False)
    embed.add_field(name="Informações", value=informacoes[:1024] or "Não informado", inline=False)
    embed.set_footer(text=f"Publicado por {interaction.user}")
    await channel.send(content=mention, embed=embed, allowed_mentions=discord.AllowedMentions(everyone=bool(mention), users=False, roles=False))
    await interaction.response.send_message(f"Scrim publicado em {channel.mention}.", ephemeral=True)
    await log_action(bot, interaction.guild, interaction.user, "Scrim publicado", f"{equipes}; canal {channel.mention}")

@bot.tree.command(name="freeagent", description="Anuncia jogador disponível para contratação.")
async def freeagent(interaction: discord.Interaction, informacoes: str):
    if not await require_admin(interaction): return
    channel = await output_channel(interaction.guild, "freeagents_channel")
    if not channel:
        await interaction.response.send_message("Configure o canal Free Agents em /configurar.", ephemeral=True); return
    embed = discord.Embed(title="🆓 FREE AGENT", description=informacoes[:4000], color=discord.Color.green(), timestamp=utcnow())
    embed.set_footer(text=f"Publicado por {interaction.user}")
    await channel.send(embed=embed)
    await interaction.response.send_message(f"Anúncio publicado em {channel.mention}.", ephemeral=True)
    await log_action(bot, interaction.guild, interaction.user, "Free Agent publicado", informacoes)

@bot.tree.command(name="contrato", description="Registra a contratação de um jogador por um time.")
@app_commands.describe(jogador="Jogador contratado", manager="Manager/responsável do time", clube="ID do time cadastrado ou nome exato", posicao="Posição", role="Titular ou reserva")
@app_commands.choices(role=[app_commands.Choice(name="Titular", value="Titular"), app_commands.Choice(name="Reserva", value="Reserva")])
async def contrato(interaction: discord.Interaction, jogador: discord.Member, manager: discord.Member, clube: str, posicao: str, role: app_commands.Choice[str]):
    if not await require_admin(interaction): return
    async with await db() as con:
        cur = await con.execute("SELECT * FROM teams WHERE guild_id=? AND active=1 AND (CAST(id AS TEXT)=? OR name=? COLLATE NOCASE OR tag=? COLLATE NOCASE) LIMIT 1",
                                (interaction.guild.id, clube.strip(), clube.strip(), clube.strip()))
        team = await cur.fetchone()
    if not team:
        await interaction.response.send_message("Time não encontrado. Use /criar-time e informe o nome, sigla ou ID.", ephemeral=True); return
    current = await active_contract(interaction.guild.id, jogador.id)
    if current:
        await interaction.response.send_message(f"Esse jogador já está vinculado a **{current['team_name']}**. Faça /demitir antes de contratar novamente.", ephemeral=True); return
    async with await db() as con:
        await con.execute("INSERT INTO contracts(guild_id,player_id,team_id,position,role,manager_id,started_at) VALUES(?,?,?,?,?,?,?)",
                          (interaction.guild.id, jogador.id, team["id"], posicao[:100], role.value, manager.id, stamp()))
        await con.commit()
    channel = await output_channel(interaction.guild, "contracts_channel")
    embed = discord.Embed(title="📝 CONTRATO OFICIAL", color=discord.Color.green(), timestamp=utcnow())
    embed.add_field(name="Jogador", value=jogador.mention, inline=True)
    embed.add_field(name="Clube", value=f"{team['name']} [{team['tag']}]", inline=True)
    embed.add_field(name="Manager", value=manager.mention, inline=True)
    embed.add_field(name="Posição", value=posicao[:1024], inline=True)
    embed.add_field(name="Função", value=role.value, inline=True)
    if channel: await channel.send(embed=embed)
    await interaction.response.send_message("Contrato registrado e vínculo atualizado." + (f" Publicado em {channel.mention}." if channel else " Configure o canal de contratos para publicar."), ephemeral=True)
    await log_action(bot, interaction.guild, interaction.user, "Contratação", f"{jogador} contratado por {team['name']}; posição {posicao}; {role.value}; manager {manager}")

@bot.tree.command(name="demitir", description="Registra a saída de um jogador do clube.")
@app_commands.describe(jogador="Jogador que será dispensado", clube="Nome, sigla ou ID do time")
async def demitir(interaction: discord.Interaction, jogador: discord.Member, clube: str):
    if not await require_admin(interaction): return
    current = await active_contract(interaction.guild.id, jogador.id)
    if not current:
        await interaction.response.send_message("Esse jogador não tem contrato ativo.", ephemeral=True); return
    if clube.casefold() not in {current["team_name"].casefold(), current["team_tag"].casefold(), str(current["team_id"])}:
        await interaction.response.send_message(f"O jogador está vinculado a **{current['team_name']}**, não a esse clube.", ephemeral=True); return
    async with await db() as con:
        await con.execute("UPDATE contracts SET ended_at=?,end_reason=? WHERE id=?",
                          (stamp(), f"Dispensa por {interaction.user.id}", current["id"]))
        await con.commit()
    channel = await output_channel(interaction.guild, "dismissals_channel")
    embed = discord.Embed(title="📤 DEMISSÃO / FIM DE CONTRATO", color=discord.Color.red(), timestamp=utcnow())
    embed.add_field(name="Jogador", value=jogador.mention, inline=True)
    embed.add_field(name="Clube", value=f"{current['team_name']} [{current['team_tag']}]", inline=True)
    embed.set_footer(text=f"Registrado por {interaction.user}")
    if channel: await channel.send(embed=embed)
    await interaction.response.send_message("Demissão registrada; o vínculo foi encerrado.", ephemeral=True)
    await log_action(bot, interaction.guild, interaction.user, "Demissão", f"{jogador} saiu de {current['team_name']}")

@bot.tree.command(name="criar-time", description="Cadastra um clube na liga.")
@app_commands.describe(nome="Nome do clube", sigla="Sigla única", dono="Dono/responsável", logo_url="URL HTTPS opcional do logo")
async def criar_time(interaction: discord.Interaction, nome: str, sigla: str, dono: discord.Member, logo_url: str = ""):
    if not await require_admin(interaction): return
    nome, sigla = nome.strip(), sigla.strip().upper()
    if not nome or not sigla or len(nome) > 80 or len(sigla) > 8:
        await interaction.response.send_message("Nome obrigatório (até 80 caracteres) e sigla de até 8 caracteres.", ephemeral=True); return
    if logo_url and (not logo_url.startswith("https://") or len(logo_url) > 500):
        await interaction.response.send_message("A URL do logo deve começar com https:// e ter até 500 caracteres.", ephemeral=True); return
    try:
        async with await db() as con:
            cur = await con.execute("INSERT INTO teams(guild_id,name,tag,owner_id,logo_url,created_at) VALUES(?,?,?,?,?,?)",
                                    (interaction.guild.id, nome, sigla, dono.id, logo_url or None, stamp()))
            team_id = cur.lastrowid
            await con.commit()
    except aiosqlite.IntegrityError:
        await interaction.response.send_message("Já existe um time com esse nome ou sigla neste servidor.", ephemeral=True); return
    embed = discord.Embed(title=f"🏆 {nome} [{sigla}]", description=f"Time cadastrado com ID **{team_id}**.\nResponsável: {dono.mention}", color=discord.Color.blurple())
    if logo_url: embed.set_thumbnail(url=logo_url)
    await interaction.response.send_message(embed=embed, ephemeral=True)
    await log_action(bot, interaction.guild, interaction.user, "Time criado", f"ID {team_id}; {nome} [{sigla}]; dono {dono}")

class DeleteTeamConfirm(discord.ui.View):
    def __init__(self, team_id, actor_id):
        super().__init__(timeout=60); self.team_id=team_id; self.actor_id=actor_id
    @discord.ui.button(label="Confirmar exclusão", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction, button):
        if interaction.user.id != self.actor_id or not await require_admin(interaction): 
            if interaction.user.id != self.actor_id and not interaction.response.is_done():
                await interaction.response.send_message("Somente quem iniciou a operação pode confirmar.", ephemeral=True)
            return
        async with await db() as con:
            cur = await con.execute("SELECT * FROM teams WHERE guild_id=? AND id=? AND active=1", (interaction.guild.id,self.team_id))
            team = await cur.fetchone()
            if not team:
                await interaction.response.send_message("Time não encontrado ou já desativado.", ephemeral=True); return
            # Encerrar contratos ativos antes de desativar o clube.
            await con.execute("UPDATE contracts SET ended_at=?,end_reason=? WHERE guild_id=? AND team_id=? AND ended_at IS NULL",
                              (stamp(),"Time excluído",interaction.guild.id,self.team_id))
            await con.execute("UPDATE teams SET active=0 WHERE guild_id=? AND id=?", (interaction.guild.id,self.team_id))
            await con.commit()
        # Remover cargo associado não é automático: o cadastro não armazena um ID de cargo.
        await interaction.response.edit_message(content=f"Time **{team['name']} [{team['tag']}]** desativado. Todos os contratos ativos foram encerrados.", embed=None, view=None)
        await log_action(bot, interaction.guild, interaction.user, "Time excluído/desativado",
                         f"Time ID {self.team_id}: {team['name']} [{team['tag']}]. Contratos encerrados.")
    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Somente quem iniciou a operação pode cancelar.", ephemeral=True); return
        await interaction.response.edit_message(content="Operação cancelada.", embed=None, view=None)

@bot.tree.command(name="excluir-time", description="Desativa um time e encerra os contratos ativos.")
@app_commands.describe(time="ID, nome ou sigla do time")
async def excluir_time(interaction: discord.Interaction, time: str):
    if not await require_admin(interaction): return
    async with await db() as con:
        cur = await con.execute("SELECT * FROM teams WHERE guild_id=? AND active=1 AND (CAST(id AS TEXT)=? OR name=? COLLATE NOCASE OR tag=? COLLATE NOCASE) LIMIT 1",
                                (interaction.guild.id,time.strip(),time.strip(),time.strip()))
        team = await cur.fetchone()
    if not team:
        await interaction.response.send_message("Time ativo não encontrado.", ephemeral=True); return
    await interaction.response.send_message(
        f"Confirma desativar **{team['name']} [{team['tag']}]**? Os contratos ativos serão encerrados e o histórico será mantido.",
        view=DeleteTeamConfirm(team["id"],interaction.user.id), ephemeral=True)

# ---------------- Moderação e histórico ----------------
@bot.tree.command(name="ban", description="Bane um membro ou ID do servidor.")
@app_commands.describe(usuario="Membro do servidor", motivo="Motivo", duracao="Opcional: 10m, 2h, 3d, 1w; vazio = permanente")
async def ban(interaction: discord.Interaction, usuario: discord.User, motivo: str, duracao: str = ""):
    if not await require_admin(interaction): return
    if not await check_bot_permission(interaction,"ban_members"): return
    delta = None
    try: delta = parse_duration(duracao or None)
    except ValueError as exc:
        await interaction.response.send_message(str(exc),ephemeral=True); return
    member = interaction.guild.get_member(usuario.id)
    if member and is_higher_than_bot(interaction.guild,member):
        await interaction.response.send_message("Não posso banir alguém com cargo igual ou superior ao meu (nem o dono).",ephemeral=True); return
    reason = f"{motivo} | Responsável: {interaction.user} ({interaction.user.id})"
    try:
        await interaction.guild.ban(usuario, reason=reason, delete_message_seconds=0)
        if delta:
            # Scheduled unban is not reliable across restarts without a worker. Store expiry and explain manual unban.
            expiry = (utcnow()+delta).isoformat(timespec="seconds")
            duration_text = f"{duracao} (expira em {expiry}; desban automático não está habilitado)"
        else:
            duration_text = "Permanente"
        await add_punishment(interaction.guild.id,usuario.id,interaction.user.id,"ban",motivo,duration_text)
        await interaction.response.send_message(f"Ban aplicado a {usuario.mention}. Duração: {duration_text}.",ephemeral=True)
        await log_action(bot,interaction.guild,interaction.user,"Ban",f"Usuário {usuario} ({usuario.id}); motivo: {motivo}; duração: {duration_text}")
    except discord.HTTPException as exc:
        await interaction.response.send_message(f"Falha ao banir: {exc}",ephemeral=True)

@bot.tree.command(name="unban", description="Remove banimento pelo ID do usuário.")
async def unban(interaction: discord.Interaction, usuario_id: str, motivo: str = "Sem motivo informado"):
    if not await require_admin(interaction): return
    if not await check_bot_permission(interaction,"ban_members"): return
    try: uid = int(usuario_id)
    except ValueError:
        await interaction.response.send_message("Informe um ID numérico válido.",ephemeral=True); return
    try:
        await interaction.guild.unban(discord.Object(id=uid), reason=f"{motivo} | por {interaction.user} ({interaction.user.id})")
        await add_punishment(interaction.guild.id,uid,interaction.user.id,"unban",motivo,active=0)
        await interaction.response.send_message(f"Ban removido do ID `{uid}`.",ephemeral=True)
        await log_action(bot,interaction.guild,interaction.user,"Unban",f"ID {uid}; motivo: {motivo}")
    except discord.NotFound:
        await interaction.response.send_message("Esse ID não está banido (ou não foi encontrado).",ephemeral=True)
    except discord.HTTPException as exc:
        await interaction.response.send_message(f"Falha ao remover ban: {exc}",ephemeral=True)

@bot.tree.command(name="expulsar", description="Expulsa um membro do servidor.")
async def expulsar(interaction: discord.Interaction, usuario: discord.Member, motivo: str = "Sem motivo informado"):
    if not await require_admin(interaction): return
    if not await check_bot_permission(interaction,"kick_members"): return
    if is_higher_than_bot(interaction.guild,usuario):
        await interaction.response.send_message("Não posso expulsar alguém com cargo igual ou superior ao meu.",ephemeral=True); return
    try:
        await usuario.kick(reason=f"{motivo} | por {interaction.user} ({interaction.user.id})")
        await add_punishment(interaction.guild.id,usuario.id,interaction.user.id,"kick",motivo,active=0)
        await interaction.response.send_message(f"{usuario} foi expulso.",ephemeral=True)
        await log_action(bot,interaction.guild,interaction.user,"Expulsão",f"{usuario} ({usuario.id}); motivo: {motivo}")
    except discord.HTTPException as exc:
        await interaction.response.send_message(f"Falha ao expulsar: {exc}",ephemeral=True)

@bot.tree.command(name="timeout", description="Aplica timeout temporário (máximo 28 dias).")
async def timeout(interaction: discord.Interaction, usuario: discord.Member, duracao: str, motivo: str = "Sem motivo informado"):
    if not await require_admin(interaction): return
    if not await check_bot_permission(interaction,"moderate_members"): return
    if is_higher_than_bot(interaction.guild,usuario):
        await interaction.response.send_message("Não posso aplicar timeout em alguém com cargo igual ou superior ao meu.",ephemeral=True); return
    try: delta = parse_duration(duracao)
    except ValueError as exc:
        await interaction.response.send_message(str(exc),ephemeral=True); return
    if not delta:
        await interaction.response.send_message("Informe a duração, por exemplo `30m` ou `2d`.",ephemeral=True); return
    try:
        await usuario.timeout(utcnow()+delta,reason=f"{motivo} | por {interaction.user} ({interaction.user.id})")
        await add_punishment(interaction.guild.id,usuario.id,interaction.user.id,"timeout",motivo,duracao)
        await interaction.response.send_message(f"Timeout aplicado a {usuario.mention} por {duracao}.",ephemeral=True)
        await log_action(bot,interaction.guild,interaction.user,"Timeout",f"{usuario} ({usuario.id}); duração {duracao}; motivo: {motivo}")
    except discord.HTTPException as exc:
        await interaction.response.send_message(f"Falha ao aplicar timeout: {exc}",ephemeral=True)

@bot.tree.command(name="removetimeout", description="Remove timeout de um membro.")
async def removetimeout(interaction: discord.Interaction, usuario: discord.Member, motivo: str = "Sem motivo informado"):
    if not await require_admin(interaction): return
    if not await check_bot_permission(interaction,"moderate_members"): return
    if is_higher_than_bot(interaction.guild,usuario):
        await interaction.response.send_message("Não posso modificar timeout de alguém com cargo igual ou superior ao meu.",ephemeral=True); return
    try:
        await usuario.timeout(None,reason=f"{motivo} | por {interaction.user} ({interaction.user.id})")
        await add_punishment(interaction.guild.id,usuario.id,interaction.user.id,"removetimeout",motivo,active=0)
        await interaction.response.send_message(f"Timeout removido de {usuario.mention}.",ephemeral=True)
        await log_action(bot,interaction.guild,interaction.user,"Timeout removido",f"{usuario} ({usuario.id}); motivo: {motivo}")
    except discord.HTTPException as exc:
        await interaction.response.send_message(f"Falha ao remover timeout: {exc}",ephemeral=True)

@bot.tree.command(name="punições", description="Consulta o histórico de punições de um usuário.")
async def punicoes(interaction: discord.Interaction, usuario: discord.User):
    if not await require_admin(interaction): return
    async with await db() as con:
        cur = await con.execute("SELECT * FROM punishments WHERE guild_id=? AND target_id=? ORDER BY id DESC LIMIT 10",
                                (interaction.guild.id,usuario.id))
        rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message(f"Nenhuma punição encontrada para {usuario.mention}.",ephemeral=True); return
    embed = discord.Embed(title=f"Histórico de punições • {usuario}", color=discord.Color.orange())
    for row in rows:
        embed.add_field(name=f"#{row['id']} • {row['kind']} • {row['created_at'][:19]}",
                        value=f"Motivo: {row['reason'][:500]}\nResponsável: <@{row['moderator_id']}>\nDuração: {row['duration'] or '—'}",inline=False)
    await interaction.response.send_message(embed=embed,ephemeral=True)

# ---------------- Consulta de time/contratos ----------------
@bot.tree.command(name="elenco", description="Consulta o elenco ativo de um time.")
@app_commands.describe(time="ID, nome ou sigla do time")
async def elenco(interaction: discord.Interaction, time: str):
    async with await db() as con:
        cur = await con.execute("SELECT * FROM teams WHERE guild_id=? AND active=1 AND (CAST(id AS TEXT)=? OR name=? COLLATE NOCASE OR tag=? COLLATE NOCASE) LIMIT 1",
                                (interaction.guild.id,time.strip(),time.strip(),time.strip()))
        team = await cur.fetchone()
        if not team:
            await interaction.response.send_message("Time ativo não encontrado.",ephemeral=True); return
        cur = await con.execute("""SELECT * FROM contracts WHERE guild_id=? AND team_id=? AND ended_at IS NULL ORDER BY started_at""",
                                (interaction.guild.id,team["id"]))
        rows = await cur.fetchall()
    embed = discord.Embed(title=f"🏆 Elenco • {team['name']} [{team['tag']}]",color=discord.Color.blurple())
    if team["logo_url"]: embed.set_thumbnail(url=team["logo_url"])
    if not rows: embed.description = "Nenhum jogador contratado no momento."
    for row in rows[:25]:
        embed.add_field(name=f"<@{row['player_id']}>",value=f"Posição: {row['position']}\nFunção: {row['role']}\nManager: <@{row['manager_id']}>",inline=True)
    await interaction.response.send_message(embed=embed,ephemeral=True)

@bot.tree.command(name="times", description="Lista os times ativos da liga.")
async def times(interaction: discord.Interaction):
    async with await db() as con:
        cur = await con.execute("SELECT * FROM teams WHERE guild_id=? AND active=1 ORDER BY name", (interaction.guild.id,))
        rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message("Ainda não há times cadastrados.",ephemeral=True); return
    embed = discord.Embed(title="🏆 Times cadastrados",color=discord.Color.blurple())
    for team in rows[:25]:
        embed.add_field(name=f"{team['name']} [{team['tag']}] • ID {team['id']}",value=f"Responsável: <@{team['owner_id']}>",inline=True)
    await interaction.response.send_message(embed=embed,ephemeral=True)

# ---------------- Inicialização ----------------
async def main():
    if not TOKEN or TOKEN == "COLOQUE_SEU_TOKEN_AQUI":
        raise SystemExit("Configure DISCORD_TOKEN nas variáveis de ambiente ou no arquivo .env.")
    await init_db()
    # Views persistentes: os custom_id dos botões são fixos e timeout=None.
    bot.add_view(OpenTicketView())
    bot.add_view(CloseTicketView())
    if GUILD_ID:
        guild_obj = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild_obj)
        async with bot:
            await bot.start(TOKEN)
            # sync is done in on_connect below
    else:
        async with bot:
            await bot.start(TOKEN)

@bot.event
async def on_connect():
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            log.info("Sincronizados %s comandos no servidor de teste %s",len(synced),GUILD_ID)
        else:
            synced = await bot.tree.sync()
            log.info("Sincronizados %s comandos globais",len(synced))
    except Exception:
        log.exception("Falha ao sincronizar comandos")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
