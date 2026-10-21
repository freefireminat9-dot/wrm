"""Bot de administração de liga (discord.py 2.x) — tudo em um único arquivo.

Requisitos (requirements.txt):  discord.py>=2.3.2,<3  aiosqlite>=0.19.0  python-dotenv>=1.0.0
Variáveis: DISCORD_TOKEN (obrigatória), DEV_GUILD_ID (opcional), DATABASE_PATH (opcional).
Execute com:  python main.py
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional, Sequence, Union
from urllib.parse import urlparse

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("DATABASE_PATH", "data/league.db")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID", "").strip()

log = logging.getLogger("league_bot")


# ==========================================================================
# UTILITÁRIOS
# ==========================================================================

class Colors:
    """Paleta padrão dos embeds."""

    INFO = 0x5865F2
    SUCCESS = 0x57F287
    WARNING = 0xFEE75C
    DANGER = 0xED4245
    GOLD = 0xF1C40F


PERM_LABELS = {
    "view_channel": "Ver canal",
    "send_messages": "Enviar mensagens",
    "embed_links": "Inserir links (embeds)",
    "attach_files": "Anexar arquivos",
    "read_message_history": "Ver histórico de mensagens",
    "mention_everyone": "Mencionar @everyone",
    "manage_channels": "Gerenciar canais",
    "manage_roles": "Gerenciar cargos",
    "ban_members": "Banir membros",
    "kick_members": "Expulsar membros",
    "moderate_members": "Silenciar membros (timeout)",
}


def truncate(text: object, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def now_ts() -> int:
    return int(time.time())


def discord_ts(ts: int, style: str = "F") -> str:
    """Formata um timestamp Unix no formato dinâmico do Discord."""
    return f"<t:{int(ts)}:{style}>"


def missing_perms(perms: discord.Permissions, *names: str) -> list[str]:
    """Retorna os nomes (em português) das permissões que faltam."""
    return [PERM_LABELS.get(n, n) for n in names if not getattr(perms, n, False)]


# --------------------------------------------------------------------- duração
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_PART = re.compile(r"(\d+)([smhdw])")


def parse_duration(text: str) -> int:
    """Converte '30m', '12h', '7d', '1w' ou '1d12h' em segundos. Levanta ValueError se inválido."""
    cleaned = text.strip().lower().replace(" ", "")
    pos = 0
    total = 0
    for match in _PART.finditer(cleaned):
        if match.start() != pos:
            break
        total += int(match.group(1)) * _UNITS[match.group(2)]
        pos = match.end()
    if not cleaned or pos != len(cleaned) or total <= 0:
        raise ValueError("Duração inválida. Use por exemplo: 30m, 12h, 7d, 1w ou 1d12h.")
    return total


def format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "Permanente"
    parts = []
    remaining = int(seconds)
    for label, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        qty, remaining = divmod(remaining, size)
        if qty:
            parts.append(f"{qty}{label}")
    return " ".join(parts) or "0s"


def safe_url(url: Optional[str]) -> Optional[str]:
    """Valida uma URL http(s). Retorna None se for inválida."""
    if not url:
        return None
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or len(url) > 500:
        return None
    return url


# ------------------------------------------------------------ canais/membros
async def get_config_channel(
    interaction: discord.Interaction, key: str, label: str, *extra_perms: str
) -> Optional[discord.abc.GuildChannel]:
    """Obtém o canal configurado e valida as permissões do bot.

    Deve ser chamado DEPOIS de `interaction.response.defer()`; em caso de problema
    responde ao usuário via followup e retorna None.
    """
    guild = interaction.guild
    cfg = await interaction.client.db.get_config(guild.id)
    channel_id = cfg.get(key)
    if not channel_id:
        await interaction.followup.send(
            f"⚠️ O canal de **{label}** ainda não foi configurado. Use `/configurar` primeiro.", ephemeral=True
        )
        return None
    channel = guild.get_channel(channel_id)
    if channel is None:
        await interaction.followup.send(
            f"⚠️ O canal de **{label}** configurado não existe mais. Reconfigure em `/configurar`.", ephemeral=True
        )
        return None
    perms = channel.permissions_for(guild.me)
    missing = missing_perms(perms, "view_channel", "send_messages", "embed_links", *extra_perms)
    if missing:
        await interaction.followup.send(
            f"⚠️ Não tenho permissão em {channel.mention}: **{', '.join(missing)}**.", ephemeral=True
        )
        return None
    return channel


async def fetch_member_safe(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


async def add_role_safe(member: discord.Member, role: Optional[discord.Role], reason: str) -> Optional[str]:
    """Adiciona um cargo. Retorna uma mensagem de erro (str) ou None se deu certo."""
    if role is None:
        return None
    if role >= member.guild.me.top_role:
        return f"O cargo {role.mention} está acima do cargo do bot. Suba o cargo do bot na lista de cargos."
    try:
        await member.add_roles(role, reason=reason)
    except discord.Forbidden:
        return f"Sem permissão para atribuir o cargo {role.mention}."
    except discord.HTTPException as exc:
        return f"Erro ao atribuir o cargo {role.mention}: {exc}"
    return None


async def remove_role_safe(member: discord.Member, role: Optional[discord.Role], reason: str) -> Optional[str]:
    """Remove um cargo. Retorna uma mensagem de erro (str) ou None se deu certo."""
    if role is None or role not in member.roles:
        return None
    try:
        await member.remove_roles(role, reason=reason)
    except discord.Forbidden:
        return f"Sem permissão para remover o cargo {role.mention} de {member.mention}."
    except discord.HTTPException as exc:
        return f"Erro ao remover o cargo de {member.mention}: {exc}"
    return None


async def team_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete de times ativos (por nome ou sigla)."""
    if interaction.guild is None:
        return []
    teams = await interaction.client.db.list_teams(interaction.guild.id)
    needle = current.casefold()
    choices: list[app_commands.Choice[str]] = []
    for team in teams:
        if needle in team["name_key"] or needle in team["tag_key"]:
            choices.append(app_commands.Choice(name=truncate(f"[{team['tag']}] {team['name']}", 100),
                                               value=team["name"][:100]))
        if len(choices) >= 25:
            break
    return choices


# ------------------------------------------------------------------ componentes
class ConfirmView(discord.ui.View):
    """Botões Confirmar/Cancelar. Use `await view.wait()` e depois leia `view.value`.

    value: True (confirmou), False (cancelou) ou None (tempo esgotado).
    """

    def __init__(self, author_id: int, *, confirm_label: str = "Confirmar", timeout: float = 60) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value: Optional[bool] = None
        self.confirm_btn.label = confirm_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Apenas quem iniciou a ação pode confirmar.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.value = True
        await interaction.response.edit_message(content="⏳ Processando...", embed=None, view=None)
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.value = False
        await interaction.response.edit_message(content="❎ Ação cancelada.", embed=None, view=None)
        self.stop()


# ==========================================================================
# BANCO DE DADOS (SQLite)
# ==========================================================================

# Colunas permitidas em guild_config (whitelist usada para montar o UPDATE com segurança).
CONFIG_KEYS = (
    "scrim_channel",
    "freeagent_channel",
    "contract_channel",
    "dismissal_channel",
    "logs_channel",
    "tickets_channel",
    "ticket_category",
    "admin_role",
    "ticket_limit",
)

# Colunas de `teams` que podem ser alteradas via update_team().
TEAM_UPDATE_FIELDS = ("name", "name_key", "tag", "tag_key", "owner_id", "logo_url", "role_id")

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id          INTEGER PRIMARY KEY,
    scrim_channel     INTEGER,
    freeagent_channel INTEGER,
    contract_channel  INTEGER,
    dismissal_channel INTEGER,
    logs_channel      INTEGER,
    tickets_channel   INTEGER,
    ticket_category   INTEGER,
    admin_role        INTEGER,
    ticket_limit      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tickets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL UNIQUE,
    user_id    INTEGER NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'open',   -- open | closing | closed
    opened_at  INTEGER NOT NULL,
    closed_at  INTEGER,
    closed_by  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(guild_id, user_id, status);

CREATE TABLE IF NOT EXISTS teams (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    name_key   TEXT    NOT NULL,                  -- nome em casefold (comparação sem maiúsculas)
    tag        TEXT    NOT NULL,
    tag_key    TEXT    NOT NULL,
    owner_id   INTEGER NOT NULL,
    logo_url   TEXT,
    role_id    INTEGER,
    role_auto  INTEGER NOT NULL DEFAULT 0,        -- 1 = cargo criado pelo bot
    active     INTEGER NOT NULL DEFAULT 1,        -- 0 = excluído/desativado (histórico mantido)
    created_at INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    deleted_at INTEGER,
    deleted_by INTEGER
);
-- Nome e sigla são únicos apenas entre times ATIVOS; times desativados liberam o nome.
CREATE UNIQUE INDEX IF NOT EXISTS ux_team_name ON teams(guild_id, name_key) WHERE active = 1;
CREATE UNIQUE INDEX IF NOT EXISTS ux_team_tag  ON teams(guild_id, tag_key)  WHERE active = 1;

CREATE TABLE IF NOT EXISTS memberships (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    team_id     INTEGER NOT NULL REFERENCES teams(id),
    user_id     INTEGER NOT NULL,
    manager_id  INTEGER,
    position    TEXT,
    role_type   TEXT,                             -- titular | reserva
    joined_at   INTEGER NOT NULL,
    hired_by    INTEGER,
    left_at     INTEGER,
    left_by     INTEGER,
    left_reason TEXT
);
-- Um jogador só pode ter UM vínculo ativo por servidor.
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_member ON memberships(guild_id, user_id) WHERE left_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_members_team ON memberships(team_id, left_at);

CREATE TABLE IF NOT EXISTS punishments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    type         TEXT    NOT NULL,                -- ban | unban | kick | timeout | untimeout
    reason       TEXT,
    created_at   INTEGER NOT NULL,
    duration     INTEGER,                         -- segundos (NULL = permanente/não se aplica)
    expires_at   INTEGER,
    active       INTEGER NOT NULL DEFAULT 0       -- 1 = banimento ainda vigente
);
CREATE INDEX IF NOT EXISTS idx_pun_user   ON punishments(guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_pun_expiry ON punishments(type, active, expires_at);
"""


class Database:
    """Wrapper assíncrono simples sobre uma única conexão aiosqlite."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.conn: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------ infra
    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _run(self, sql: str, params: tuple = ()) -> tuple[int, int]:
        """Executa e confirma (commit). Retorna (lastrowid, rowcount)."""
        try:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise
        result = (cur.lastrowid, cur.rowcount)
        await cur.close()
        return result

    # ----------------------------------------------------------------- config
    async def get_config(self, guild_id: int) -> dict[str, Any]:
        row = await self._fetchone("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))
        if row is None:
            row = {"guild_id": guild_id, **{k: None for k in CONFIG_KEYS}}
            row["ticket_limit"] = 1
        return row

    async def set_config(self, guild_id: int, key: str, value: Optional[int]) -> None:
        if key not in CONFIG_KEYS:
            raise ValueError(f"Chave de configuração inválida: {key}")
        await self._run("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
        await self._run(f"UPDATE guild_config SET {key} = ? WHERE guild_id = ?", (value, guild_id))

    # ---------------------------------------------------------------- tickets
    async def create_ticket(self, guild_id: int, channel_id: int, user_id: int) -> int:
        lastrowid, _ = await self._run(
            "INSERT INTO tickets (guild_id, channel_id, user_id, opened_at) VALUES (?, ?, ?, ?)",
            (guild_id, channel_id, user_id, int(time.time())),
        )
        return lastrowid

    async def list_open_tickets_for_user(self, guild_id: int, user_id: int) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM tickets WHERE guild_id = ? AND user_id = ? AND status = 'open'",
            (guild_id, user_id),
        )

    async def get_ticket_by_channel(self, channel_id: int) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM tickets WHERE channel_id = ?", (channel_id,))

    async def claim_ticket_close(self, channel_id: int) -> bool:
        """Marca o ticket como 'closing' de forma atômica (evita fechar duas vezes)."""
        _, rowcount = await self._run(
            "UPDATE tickets SET status = 'closing' WHERE channel_id = ? AND status = 'open'",
            (channel_id,),
        )
        return rowcount == 1

    async def set_ticket_status(self, channel_id: int, status: str, closed_by: Optional[int] = None) -> None:
        if status == "closed":
            await self._run(
                "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ? WHERE channel_id = ?",
                (int(time.time()), closed_by, channel_id),
            )
        else:
            await self._run("UPDATE tickets SET status = ? WHERE channel_id = ?", (status, channel_id))

    # ------------------------------------------------------------------ times
    async def create_team(
        self,
        guild_id: int,
        name: str,
        tag: str,
        owner_id: int,
        logo_url: Optional[str],
        role_id: Optional[int],
        role_auto: bool,
        created_by: int,
    ) -> int:
        lastrowid, _ = await self._run(
            """INSERT INTO teams (guild_id, name, name_key, tag, tag_key, owner_id, logo_url,
                                  role_id, role_auto, created_at, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, name, name.casefold(), tag, tag.casefold(), owner_id, logo_url,
             role_id, int(role_auto), int(time.time()), created_by),
        )
        return lastrowid

    async def get_team(self, team_id: int) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM teams WHERE id = ?", (team_id,))

    async def find_team(self, guild_id: int, query: str) -> Optional[dict]:
        """Busca um time ATIVO pelo nome ou sigla (sem diferenciar maiúsculas)."""
        key = query.strip().casefold()
        return await self._fetchone(
            "SELECT * FROM teams WHERE guild_id = ? AND active = 1 AND (name_key = ? OR tag_key = ?) LIMIT 1",
            (guild_id, key, key),
        )

    async def list_teams(self, guild_id: int) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM teams WHERE guild_id = ? AND active = 1 ORDER BY name_key", (guild_id,)
        )

    async def list_teams_with_counts(self, guild_id: int) -> list[dict]:
        return await self._fetchall(
            """SELECT t.*, (SELECT COUNT(*) FROM memberships m
                            WHERE m.team_id = t.id AND m.left_at IS NULL) AS players
               FROM teams t WHERE t.guild_id = ? AND t.active = 1 ORDER BY t.name_key""",
            (guild_id,),
        )

    async def list_teams_by_owner(self, guild_id: int, owner_id: int) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM teams WHERE guild_id = ? AND owner_id = ? AND active = 1", (guild_id, owner_id)
        )

    async def team_conflict(
        self, guild_id: int, name_key: str, tag_key: str, exclude_id: Optional[int] = None
    ) -> Optional[str]:
        """Retorna 'nome' ou 'sigla' se já existir time ativo com o mesmo valor."""
        sql = ("SELECT name_key, tag_key FROM teams WHERE guild_id = ? AND active = 1 "
               "AND (name_key = ? OR tag_key = ?)")
        params: tuple = (guild_id, name_key, tag_key)
        if exclude_id is not None:
            sql += " AND id != ?"
            params += (exclude_id,)
        for row in await self._fetchall(sql, params):
            if row["name_key"] == name_key:
                return "nome"
            if row["tag_key"] == tag_key:
                return "sigla"
        return None

    async def role_in_use(self, guild_id: int, role_id: int, exclude_id: Optional[int] = None) -> bool:
        sql = "SELECT 1 FROM teams WHERE guild_id = ? AND role_id = ? AND active = 1"
        params: tuple = (guild_id, role_id)
        if exclude_id is not None:
            sql += " AND id != ?"
            params += (exclude_id,)
        return await self._fetchone(sql, params) is not None

    async def update_team(self, team_id: int, **fields: Any) -> None:
        if not fields:
            return
        for key in fields:
            if key not in TEAM_UPDATE_FIELDS:
                raise ValueError(f"Campo de time inválido: {key}")
        assignments = ", ".join(f"{key} = ?" for key in fields)
        await self._run(f"UPDATE teams SET {assignments} WHERE id = ?", (*fields.values(), team_id))

    async def deactivate_team(self, team_id: int, deleted_by: int) -> None:
        await self._run(
            "UPDATE teams SET active = 0, deleted_at = ?, deleted_by = ? WHERE id = ?",
            (int(time.time()), deleted_by, team_id),
        )

    # ------------------------------------------------------------- vínculos
    async def get_active_membership(self, guild_id: int, user_id: int) -> Optional[dict]:
        return await self._fetchone(
            """SELECT m.*, t.name AS team_name, t.tag AS team_tag, t.role_id AS team_role_id
               FROM memberships m JOIN teams t ON t.id = m.team_id
               WHERE m.guild_id = ? AND m.user_id = ? AND m.left_at IS NULL""",
            (guild_id, user_id),
        )

    async def add_membership(
        self,
        guild_id: int,
        team_id: int,
        user_id: int,
        manager_id: Optional[int],
        position: str,
        role_type: str,
        hired_by: int,
    ) -> int:
        lastrowid, _ = await self._run(
            """INSERT INTO memberships (guild_id, team_id, user_id, manager_id, position,
                                        role_type, joined_at, hired_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, team_id, user_id, manager_id, position, role_type, int(time.time()), hired_by),
        )
        return lastrowid

    async def delete_membership(self, membership_id: int) -> None:
        await self._run("DELETE FROM memberships WHERE id = ?", (membership_id,))

    async def end_membership(self, membership_id: int, left_by: int, reason: str) -> None:
        await self._run(
            "UPDATE memberships SET left_at = ?, left_by = ?, left_reason = ? WHERE id = ? AND left_at IS NULL",
            (int(time.time()), left_by, reason, membership_id),
        )

    async def list_active_members(self, team_id: int) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM memberships WHERE team_id = ? AND left_at IS NULL ORDER BY role_type DESC, joined_at",
            (team_id,),
        )

    async def end_all_memberships(self, team_id: int, left_by: int, reason: str) -> None:
        await self._run(
            "UPDATE memberships SET left_at = ?, left_by = ?, left_reason = ? WHERE team_id = ? AND left_at IS NULL",
            (int(time.time()), left_by, reason, team_id),
        )

    # ------------------------------------------------------------- punições
    async def add_punishment(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        ptype: str,
        reason: Optional[str],
        duration: Optional[int] = None,
        expires_at: Optional[int] = None,
        active: bool = False,
    ) -> int:
        lastrowid, _ = await self._run(
            """INSERT INTO punishments (guild_id, user_id, moderator_id, type, reason, created_at,
                                        duration, expires_at, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, user_id, moderator_id, ptype, reason, int(time.time()), duration, expires_at, int(active)),
        )
        return lastrowid

    async def list_punishments(self, guild_id: int, user_id: int, limit: int = 200) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM punishments WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
            (guild_id, user_id, limit),
        )

    async def deactivate_bans(self, guild_id: int, user_id: int) -> None:
        await self._run(
            "UPDATE punishments SET active = 0 WHERE guild_id = ? AND user_id = ? AND type = 'ban' AND active = 1",
            (guild_id, user_id),
        )

    async def get_expired_bans(self, now: int) -> list[dict]:
        return await self._fetchall(
            """SELECT * FROM punishments
               WHERE type = 'ban' AND active = 1 AND expires_at IS NOT NULL AND expires_at <= ?""",
            (now,),
        )


# ==========================================================================
# SISTEMA DE LOGS
# ==========================================================================

# Cada campo: (nome, valor) ou (nome, valor, inline)
LogField = Union[tuple[str, object], tuple[str, object, bool]]


async def send_log(
    bot,
    guild: Optional[discord.Guild],
    *,
    action: str,
    user: Optional[Union[discord.User, discord.Member]] = None,
    color: int = Colors.INFO,
    description: Optional[str] = None,
    fields: Sequence[LogField] = (),
    file: Optional[discord.File] = None,
) -> bool:
    """Envia um registro ao canal de logs. Retorna True se foi enviado.

    Nunca levanta exceção: falha de log não pode quebrar o comando que o originou.
    """
    if guild is None:
        return False
    try:
        cfg = await bot.db.get_config(guild.id)
        channel_id = cfg.get("logs_channel")
        if not channel_id:
            return False
        channel = guild.get_channel(channel_id)
        if channel is None:
            return False

        needed = ["view_channel", "send_messages", "embed_links"]
        if file is not None:
            needed.append("attach_files")
        if missing_perms(channel.permissions_for(guild.me), *needed):
            log.warning("Sem permissão no canal de logs do servidor %s", guild.id)
            return False

        ts = now_ts()
        embed = discord.Embed(title=f"📋 {action}", description=description, color=color,
                              timestamp=discord.utils.utcnow())
        if user is not None:
            embed.set_author(name=f"{user} ({user.id})", icon_url=user.display_avatar.url)
            embed.add_field(name="👤 Responsável", value=f"{user.mention}\n`{user.id}`", inline=True)
        embed.add_field(name="⚙️ Ação", value=action, inline=True)
        embed.add_field(name="🕒 Data e horário", value=f"{discord_ts(ts, 'F')}\n{discord_ts(ts, 'R')}", inline=True)
        for entry in fields:
            name, value = entry[0], entry[1]
            inline = entry[2] if len(entry) > 2 else True
            embed.add_field(name=name, value=truncate(value, 1024) or "—", inline=inline)
        embed.set_footer(text=guild.name)

        await channel.send(embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none())
        return True
    except Exception:  # noqa: BLE001 - log nunca deve derrubar o fluxo principal
        log.exception("Falha ao enviar log (guild=%s, ação=%s)", getattr(guild, "id", None), action)
        return False


# ==========================================================================
# PERMISSÕES
# ==========================================================================

class NotAuthorized(app_commands.CheckFailure):
    """O usuário não é dono do servidor nem possui o cargo administrativo configurado."""


async def is_authorized(bot, guild: Optional[discord.Guild], member) -> bool:
    """True se for o dono do servidor ou tiver o cargo administrativo configurado."""
    if guild is None or member is None:
        return False
    if member.id == guild.owner_id:
        return True
    cfg = await bot.db.get_config(guild.id)
    role_id = cfg.get("admin_role")
    if not role_id:
        return False
    return any(role.id == role_id for role in getattr(member, "roles", []))


async def log_denied(bot, interaction: discord.Interaction, what: str) -> None:
    """Registra tentativa de uso indevido no canal de logs."""
    await send_log(
        bot,
        interaction.guild,
        action="Tentativa de acesso não autorizado",
        user=interaction.user,
        color=Colors.DANGER,
        fields=[("Tentou usar", what), ("Canal", getattr(interaction.channel, "mention", "—"))],
    )


def admin_only():
    """Decorator de comando slash: exige dono do servidor ou cargo administrativo configurado."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.NoPrivateMessage()
        if await is_authorized(interaction.client, interaction.guild, interaction.user):
            return True
        command = f"/{interaction.command.qualified_name}" if interaction.command else "comando desconhecido"
        await log_denied(interaction.client, interaction, command)
        raise NotAuthorized()

    return app_commands.check(predicate)


# ==========================================================================
# /configurar
# ==========================================================================

# chave -> (emoji, rótulo, tipo, descrição)
SETTINGS: dict[str, tuple[str, str, str, str]] = {
    "scrim_channel": ("⚔️", "Scrims", "text", "Canal das mensagens de desafios e amistosos."),
    "freeagent_channel": ("🆓", "Free Agents", "text", "Canal de anúncios de jogadores disponíveis."),
    "contract_channel": ("📝", "Contratos", "text", "Canal onde contratações são publicadas."),
    "dismissal_channel": ("📤", "Demissões", "text", "Canal de anúncios de jogadores dispensados."),
    "logs_channel": ("📋", "Logs", "text", "Canal de registro das ações administrativas."),
    "tickets_channel": ("🎫", "Tickets (painel)", "text", "Canal onde o painel de tickets é publicado."),
    "ticket_category": ("📁", "Categoria de tickets", "category", "Categoria onde os tickets são criados."),
    "admin_role": ("🛡️", "Cargo administrativo", "role", "Cargo autorizado a usar os comandos do bot."),
    "ticket_limit": ("🔢", "Limite de tickets por usuário", "number", "Tickets abertos ao mesmo tempo por pessoa."),
}
TICKET_LIMIT_OPTIONS = (1, 2, 3, 5, 10)


def format_value(guild: discord.Guild, key: str, value: Optional[int]) -> str:
    """Texto legível para o valor atual de uma configuração."""
    kind = SETTINGS[key][2]
    if value is None:
        return "`Não configurado`"
    if kind == "number":
        return f"`{value}`"
    if kind == "role":
        return f"<@&{value}>" if guild.get_role(value) else f"⚠️ cargo removido (`{value}`)"
    channel = guild.get_channel(value)
    return channel.mention if channel else f"⚠️ canal removido (`{value}`)"


def permission_warning(guild: discord.Guild, key: str, value: Optional[int]) -> str:
    """Avisa se o bot não tem as permissões necessárias no canal/categoria escolhido."""
    if value is None:
        return ""
    kind = SETTINGS[key][2]
    target = guild.get_channel(value)
    if target is None:
        return ""
    perms = target.permissions_for(guild.me)
    if kind == "text":
        need = ["view_channel", "send_messages", "embed_links"]
        if key == "logs_channel":
            need.append("attach_files")
        if key == "scrim_channel":
            need.append("mention_everyone")
    elif kind == "category":
        need = ["view_channel", "manage_channels", "manage_roles"]
    else:
        return ""
    missing = missing_perms(perms, *need)
    return f"\n⚠️ O bot não tem estas permissões em {target.mention}: **{', '.join(missing)}**." if missing else ""


class ConfigView(discord.ui.View):
    """Painel de configuração (efêmero). Reconstruído a cada interação."""

    def __init__(self, bot, origin: discord.Interaction) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.origin = origin
        self.guild: discord.Guild = origin.guild
        self.author_id = origin.user.id
        self.cfg: dict = {}
        self.selected_key: Optional[str] = None
        self.pending: Optional[int] = None
        self.notice = ""

    # ------------------------------------------------------------- segurança
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Este painel pertence a outro usuário.", ephemeral=True)
            return False
        # Revalida no servidor a cada clique (o cargo pode ter sido removido).
        if not await is_authorized(self.bot, interaction.guild, interaction.user):
            await log_denied(self.bot, interaction, "painel /configurar")
            await interaction.response.send_message("⛔ Você não tem mais permissão para configurar o bot.",
                                                    ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.error("Erro no painel de configuração", exc_info=error)
        text = "❌ Ocorreu um erro ao processar sua ação. Tente novamente."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        try:
            await self.origin.edit_original_response(view=self)
        except discord.HTTPException:
            pass

    # ----------------------------------------------------------- construção
    async def load(self) -> None:
        self.cfg = await self.bot.db.get_config(self.guild.id)
        self._build_items()

    def _build_items(self) -> None:
        self.clear_items()

        menu = discord.ui.Select(
            placeholder="Escolha a configuração que deseja alterar...",
            options=[
                discord.SelectOption(label=label, value=key, emoji=emoji, description=truncate(desc, 100),
                                     default=(key == self.selected_key))
                for key, (emoji, label, _kind, desc) in SETTINGS.items()
            ],
            row=0,
        )
        menu.callback = self._on_menu
        self.add_item(menu)

        if self.selected_key is None:
            return

        kind = SETTINGS[self.selected_key][2]
        if kind == "text":
            picker: discord.ui.Item = discord.ui.ChannelSelect(
                placeholder="Selecione o canal...",
                channel_types=[discord.ChannelType.text, discord.ChannelType.news],
                row=1,
            )
        elif kind == "category":
            picker = discord.ui.ChannelSelect(
                placeholder="Selecione a categoria...", channel_types=[discord.ChannelType.category], row=1
            )
        elif kind == "role":
            picker = discord.ui.RoleSelect(placeholder="Selecione o cargo...", row=1)
        else:
            picker = discord.ui.Select(
                placeholder="Selecione o limite...",
                options=[discord.SelectOption(label=str(n), value=str(n)) for n in TICKET_LIMIT_OPTIONS],
                row=1,
            )
        picker.callback = self._on_pick
        self.add_item(picker)

        save = discord.ui.Button(label="Salvar", style=discord.ButtonStyle.success, emoji="💾",
                                 disabled=self.pending is None, row=2)
        clear = discord.ui.Button(label="Limpar", style=discord.ButtonStyle.secondary, emoji="🗑️", row=2)
        back = discord.ui.Button(label="Voltar", style=discord.ButtonStyle.secondary, emoji="↩️", row=2)
        save.callback = self._on_save
        clear.callback = self._on_clear
        back.callback = self._on_back
        for button in (save, clear, back):
            self.add_item(button)

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="⚙️ Painel de Configuração",
            description=self.notice or "Selecione abaixo o que deseja configurar. Cada opção é independente.",
            color=Colors.INFO,
        )
        for key, (emoji, label, _kind, _desc) in SETTINGS.items():
            embed.add_field(name=f"{emoji} {label}", value=format_value(self.guild, key, self.cfg.get(key)),
                            inline=True)
        if self.selected_key:
            emoji, label, _kind, desc = SETTINGS[self.selected_key]
            pending = (format_value(self.guild, self.selected_key, self.pending)
                       if self.pending is not None else "_nenhuma — escolha no seletor abaixo_")
            embed.add_field(
                name=f"✏️ Editando: {emoji} {label}",
                value=f"{desc}\n**Seleção pendente:** {pending}\nClique em **Salvar** para aplicar.",
                inline=False,
            )
        embed.set_footer(text=f"{self.guild.name} • configurações independentes por servidor")
        return embed

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await self.load()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    # ------------------------------------------------------------ callbacks
    async def _on_menu(self, interaction: discord.Interaction) -> None:
        self.selected_key = interaction.data["values"][0]
        self.pending = None
        self.notice = ""
        await self._refresh(interaction)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        self.pending = int(interaction.data["values"][0])
        self.notice = ""
        await self._refresh(interaction)

    async def _apply(self, interaction: discord.Interaction, new_value: Optional[int], cleared: bool) -> None:
        key = self.selected_key
        _emoji, label, kind, _desc = SETTINGS[key]
        old_value = self.cfg.get(key)
        if kind == "number" and new_value is None:
            new_value = 1  # "limpar" volta ao padrão
        await self.bot.db.set_config(self.guild.id, key, new_value)

        verb = "restaurada ao padrão" if (cleared and kind == "number") else ("removida" if cleared else "salva")
        self.notice = (f"✅ **{label}** {verb}: {format_value(self.guild, key, new_value)}"
                       f"{permission_warning(self.guild, key, new_value)}")
        await send_log(
            self.bot, self.guild, action="Configuração alterada", user=interaction.user, color=Colors.WARNING,
            fields=[("Opção", label), ("Antes", format_value(self.guild, key, old_value)),
                    ("Depois", format_value(self.guild, key, new_value))],
        )
        self.selected_key = None
        self.pending = None
        await self._refresh(interaction)

    async def _on_save(self, interaction: discord.Interaction) -> None:
        if self.pending is None:
            await interaction.response.send_message("Selecione um valor antes de salvar.", ephemeral=True)
            return
        await self._apply(interaction, self.pending, cleared=False)

    async def _on_clear(self, interaction: discord.Interaction) -> None:
        await self._apply(interaction, None, cleared=True)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        self.selected_key = None
        self.pending = None
        self.notice = ""
        await self._refresh(interaction)


class ConfigCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="configurar", description="Abre o painel de configuração do bot (somente administradores).")
    @app_commands.guild_only()
    @admin_only()
    async def configurar(self, interaction: discord.Interaction) -> None:
        view = ConfigView(self.bot, interaction)
        await view.load()
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)


# ==========================================================================
# TICKETS
# ==========================================================================

DEFAULT_TITLE = "🎫 Central de Atendimento"
DEFAULT_DESC = (
    "Precisa de ajuda, quer tirar uma dúvida ou fazer uma denúncia?\n\n"
    "Clique no botão abaixo para abrir um **ticket privado** com a nossa equipe."
)
DEFAULT_BUTTON = "Abrir Ticket"

# Trava por usuário: evita abrir dois tickets com cliques simultâneos.
_user_locks: dict[tuple[int, int], asyncio.Lock] = {}


# ============================================================ abrir ticket
async def open_ticket(interaction: discord.Interaction) -> None:
    bot = interaction.client
    guild = interaction.guild
    user = interaction.user
    if guild is None:
        return

    cfg = await bot.db.get_config(guild.id)
    category = guild.get_channel(cfg["ticket_category"]) if cfg.get("ticket_category") else None
    if not isinstance(category, discord.CategoryChannel):
        await interaction.response.send_message(
            "⚠️ O sistema de tickets ainda não foi configurado (categoria ausente). Avise a equipe.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    lock = _user_locks.setdefault((guild.id, user.id), asyncio.Lock())
    async with lock:
        # Limite de tickets simultâneos (ignora/limpa registros de canais que já foram apagados).
        alive: list[discord.abc.GuildChannel] = []
        for row in await bot.db.list_open_tickets_for_user(guild.id, user.id):
            channel = guild.get_channel(row["channel_id"])
            if channel is None:
                await bot.db.set_ticket_status(row["channel_id"], "closed")
            else:
                alive.append(channel)
        limit = cfg.get("ticket_limit") or 1
        if len(alive) >= limit:
            mentions = ", ".join(c.mention for c in alive)
            await interaction.followup.send(
                f"⚠️ Você já atingiu o limite de **{limit}** ticket(s) aberto(s): {mentions}", ephemeral=True
            )
            return

        # Permissões do bot na categoria e limite de canais (50 por categoria).
        missing = missing_perms(category.permissions_for(guild.me), "view_channel", "manage_channels", "manage_roles")
        if missing:
            await interaction.followup.send(
                f"❌ Não consigo criar o ticket: faltam permissões do bot na categoria ({', '.join(missing)}). "
                "Avise a equipe.", ephemeral=True)
            await send_log(bot, guild, action="Erro ao abrir ticket", user=user, color=Colors.DANGER,
                           fields=[("Motivo", f"Bot sem permissão na categoria: {', '.join(missing)}", False)])
            return
        if len(category.channels) >= 50:
            await interaction.followup.send(
                "❌ A categoria de tickets está cheia (limite de 50 canais). Avise a equipe.", ephemeral=True)
            await send_log(bot, guild, action="Erro ao abrir ticket", user=user, color=Colors.DANGER,
                           fields=[("Motivo", "Categoria de tickets cheia (50 canais).", False)])
            return

        staff_role = guild.get_role(cfg["admin_role"]) if cfg.get("admin_role") else None
        member_perms = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: member_perms,
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                embed_links=True, attach_files=True, manage_channels=True),
        }
        if staff_role is not None:
            overwrites[staff_role] = member_perms

        base = re.sub(r"[^a-z0-9]+", "-", user.name.lower()).strip("-") or str(user.id)
        try:
            channel = await guild.create_text_channel(
                name=f"ticket-{base}"[:90],
                category=category,
                overwrites=overwrites,
                topic=f"Ticket de {user} | ID: {user.id}",
                reason=f"Ticket aberto por {user} ({user.id})",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Não tenho permissão para criar o canal do ticket. Avise a equipe.", ephemeral=True)
            await send_log(bot, guild, action="Erro ao abrir ticket", user=user, color=Colors.DANGER,
                           fields=[("Motivo", "Discord recusou a criação do canal (permissões).", False)])
            return
        except discord.HTTPException as exc:
            text = ("❌ O servidor atingiu o limite de canais. Avise a equipe."
                    if exc.code == 30013 else "❌ Não foi possível criar o ticket agora. Tente novamente depois.")
            await interaction.followup.send(text, ephemeral=True)
            await send_log(bot, guild, action="Erro ao abrir ticket", user=user, color=Colors.DANGER,
                           fields=[("Erro", f"{exc.status} / {exc.code}: {truncate(exc.text, 300)}", False)])
            return

        ticket_id = await bot.db.create_ticket(guild.id, channel.id, user.id)

    embed = discord.Embed(
        title=f"🎫 Ticket #{ticket_id}",
        description=(f"Olá {user.mention}, seja bem-vindo(a)!\n\n"
                     "Descreva o seu assunto com o máximo de detalhes. Nossa equipe responderá em breve.\n"
                     "Quando tudo estiver resolvido, clique em **Fechar Ticket**."),
        color=Colors.INFO,
    )
    embed.add_field(name="Aberto por", value=user.mention, inline=True)
    embed.add_field(name="Aberto em", value=discord_ts(now_ts()), inline=True)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=guild.name)
    mention_text = user.mention + (f" {staff_role.mention}" if staff_role else "")
    try:
        await channel.send(
            content=mention_text,
            embed=embed,
            view=TicketControlView(),
            allowed_mentions=discord.AllowedMentions(users=[user], roles=[staff_role] if staff_role else False),
        )
    except discord.HTTPException:
        log.exception("Falha ao enviar mensagem inicial do ticket %s", channel.id)

    await interaction.followup.send(f"✅ Seu ticket foi criado: {channel.mention}", ephemeral=True)
    await send_log(bot, guild, action="Ticket aberto", user=user, color=Colors.SUCCESS,
                   fields=[("Ticket", f"#{ticket_id}"), ("Canal", channel.mention)])


# ========================================================== fechar ticket
async def build_transcript(channel: discord.TextChannel, ticket: dict, opener: str, closer: discord.abc.User) -> str:
    """Gera o transcript em texto puro (TXT)."""
    lines = [
        f"TRANSCRIPT — TICKET #{ticket['id']}",
        f"Servidor : {channel.guild.name}",
        f"Canal    : #{channel.name}",
        f"Aberto por : {opener} (ID {ticket['user_id']})",
        f"Aberto em  : <t:{ticket['opened_at']}> (UTC {discord.utils.utcnow():%Y-%m-%d %H:%M:%S} no fechamento)",
        f"Fechado por: {closer} (ID {closer.id})",
        "=" * 60,
        "",
    ]
    async for message in channel.history(limit=10000, oldest_first=True):
        stamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        content = message.clean_content or ""
        lines.append(f"[{stamp}] {message.author} ({message.author.id}): {content}")
        for attachment in message.attachments:
            lines.append(f"    [Anexo] {attachment.filename} -> {attachment.url}")
        for emb in message.embeds:
            parts = [p for p in (emb.title, emb.description) if p]
            if parts:
                lines.append("    [Embed] " + " | ".join(parts).replace("\n", " "))
    return "\n".join(lines) + "\n"


def _transcript_file(text: str, ticket_id: int) -> discord.File:
    return discord.File(io.BytesIO(text.encode("utf-8")), filename=f"transcript-ticket-{ticket_id}.txt")


async def close_ticket(interaction: discord.Interaction) -> None:
    bot = interaction.client
    guild = interaction.guild
    channel = interaction.channel
    user = interaction.user
    if guild is None or not isinstance(channel, discord.TextChannel):
        return

    ticket = await bot.db.get_ticket_by_channel(channel.id)
    if ticket is None or ticket["status"] != "open":
        await interaction.response.send_message("⚠️ Este canal não é um ticket aberto.", ephemeral=True)
        return

    # Apenas quem abriu ou a equipe autorizada pode fechar.
    if user.id != ticket["user_id"] and not await is_authorized(bot, guild, user):
        await log_denied(bot, interaction, f"fechar o ticket #{ticket['id']}")
        await interaction.response.send_message("⛔ Apenas quem abriu o ticket ou a equipe pode fechá-lo.",
                                                ephemeral=True)
        return

    # Confirmação antes de fechar.
    confirm = ConfirmView(user.id, confirm_label="Fechar ticket")
    await interaction.response.send_message(
        "🔒 Tem certeza que deseja **fechar** este ticket? O canal será excluído e um transcript será salvo.",
        view=confirm, ephemeral=True)
    await confirm.wait()
    if confirm.value is None:
        await interaction.edit_original_response(content="⌛ Tempo esgotado. O ticket continua aberto.", view=None)
        return
    if not confirm.value:
        return

    if not await bot.db.claim_ticket_close(channel.id):
        await interaction.edit_original_response(content="⚠️ Este ticket já está sendo encerrado.")
        return

    # Transcript (precisa de permissão para ler o histórico).
    opener = guild.get_member(ticket["user_id"]) or await _fetch_user(bot, ticket["user_id"])
    opener_name = str(opener) if opener else f"Usuário {ticket['user_id']}"
    try:
        transcript = await build_transcript(channel, ticket, opener_name, user)
    except discord.Forbidden:
        await bot.db.set_ticket_status(channel.id, "open")
        await interaction.edit_original_response(
            content="❌ Não tenho permissão para ler o histórico deste canal. O ticket continua aberto.")
        await send_log(bot, guild, action="Erro ao fechar ticket", user=user, color=Colors.DANGER,
                       fields=[("Ticket", f"#{ticket['id']}"), ("Motivo", "Sem permissão de ler histórico.", False)])
        return
    except discord.HTTPException:
        await bot.db.set_ticket_status(channel.id, "open")
        await interaction.edit_original_response(content="❌ Erro ao gerar o transcript. O ticket continua aberto.")
        return

    try:
        await channel.send(embed=discord.Embed(
            title="🔒 Ticket encerrado",
            description=f"Fechado por {user.mention}. Este canal será excluído em alguns segundos.",
            color=Colors.DANGER))
    except discord.HTTPException:
        pass

    # Log com transcript + aviso ao usuário (DM) de que o ticket foi encerrado.
    await send_log(
        bot, guild, action="Ticket fechado", user=user, color=Colors.DANGER,
        fields=[("Ticket", f"#{ticket['id']}"), ("Aberto por", f"<@{ticket['user_id']}>"),
                ("Fechado por", user.mention), ("Aberto em", discord_ts(ticket["opened_at"]))],
        file=_transcript_file(transcript, ticket["id"]),
    )
    if opener is not None:
        dm_embed = discord.Embed(
            title="🔒 Seu ticket foi encerrado",
            description=f"Seu ticket #{ticket['id']} em **{guild.name}** foi encerrado por **{user}**.\n"
                        "O transcript da conversa está anexado.",
            color=Colors.INFO, timestamp=discord.utils.utcnow())
        try:
            await opener.send(embed=dm_embed, file=_transcript_file(transcript, ticket["id"]))
        except discord.HTTPException:
            pass  # DMs fechadas

    await bot.db.set_ticket_status(channel.id, "closed", closed_by=user.id)
    await asyncio.sleep(5)
    try:
        await channel.delete(reason=f"Ticket #{ticket['id']} fechado por {user} ({user.id})")
    except discord.HTTPException:
        await interaction.edit_original_response(
            content="⚠️ Ticket registrado como fechado, mas não consegui excluir o canal (permissão).")
        return
    try:
        await interaction.edit_original_response(content="✅ Ticket encerrado.")
    except discord.HTTPException:
        pass


async def _fetch_user(bot, user_id: int) -> Optional[discord.User]:
    try:
        return await bot.fetch_user(user_id)
    except discord.HTTPException:
        return None


# ================================================================== views
class TicketPanelView(discord.ui.View):
    """Painel público com o botão de abrir ticket (persistente após reinícios)."""

    def __init__(self, button_label: Optional[str] = None) -> None:
        super().__init__(timeout=None)
        if button_label:
            self.open_btn.label = button_label

    @discord.ui.button(label=DEFAULT_BUTTON, emoji="🎫", style=discord.ButtonStyle.primary, custom_id="ticket:open")
    async def open_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await open_ticket(interaction)


class TicketControlView(discord.ui.View):
    """Botão de fechar dentro do canal do ticket (persistente após reinícios)."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Fechar Ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await close_ticket(interaction)


# =================================================================== cog
class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Registra as views persistentes: os botões continuam funcionando após reiniciar.
        self.bot.add_view(TicketPanelView())
        self.bot.add_view(TicketControlView())

    @app_commands.command(name="ticket", description="Publica o painel de abertura de tickets.")
    @app_commands.describe(
        canal="Canal do painel (padrão: o canal de tickets configurado)",
        titulo="Título do painel",
        descricao="Texto do painel (use \\n para quebrar linha)",
        botao="Texto do botão",
    )
    @app_commands.guild_only()
    @admin_only()
    async def ticket(
        self,
        interaction: discord.Interaction,
        canal: Optional[discord.TextChannel] = None,
        titulo: Optional[str] = None,
        descricao: Optional[str] = None,
        botao: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        cfg = await self.bot.db.get_config(guild.id)

        target = canal
        if target is None and cfg.get("tickets_channel"):
            target = guild.get_channel(cfg["tickets_channel"])
        if not isinstance(target, discord.TextChannel):
            await interaction.followup.send(
                "⚠️ Informe um `canal` ou configure o canal de tickets em `/configurar`.", ephemeral=True)
            return

        missing = missing_perms(target.permissions_for(guild.me), "view_channel", "send_messages", "embed_links")
        if missing:
            await interaction.followup.send(
                f"⚠️ Não tenho permissão em {target.mention}: **{', '.join(missing)}**.", ephemeral=True)
            return
        if not cfg.get("ticket_category"):
            await interaction.followup.send(
                "⚠️ Aviso: a **categoria de tickets** ainda não foi configurada (`/configurar`). "
                "O painel será publicado, mas os tickets só funcionarão depois disso.", ephemeral=True)

        embed = discord.Embed(
            title=truncate(titulo, 256) if titulo else DEFAULT_TITLE,
            description=truncate(descricao.replace("\\n", "\n"), 4000) if descricao else DEFAULT_DESC,
            color=Colors.INFO,
        )
        embed.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else None)
        view = TicketPanelView(truncate(botao, 80) if botao else None)
        try:
            message = await target.send(embed=embed, view=view)
        except discord.HTTPException as exc:
            await interaction.followup.send(f"❌ Não foi possível publicar o painel: {exc}", ephemeral=True)
            return

        await interaction.followup.send(f"✅ Painel publicado em {target.mention}: {message.jump_url}", ephemeral=True)
        await send_log(self.bot, guild, action="Painel de tickets publicado", user=interaction.user,
                       color=Colors.SUCCESS, fields=[("Canal", target.mention), ("Mensagem", message.jump_url, False)])


# ==========================================================================
# SCRIMS E FREE AGENTS
# ==========================================================================

class Announcements(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ scrim
    @app_commands.command(name="scrim", description="Publica um desafio/amistoso no canal de scrims.")
    @app_commands.describe(
        pingar_everyone="Marcar @everyone na mensagem?",
        informacoes="Informações adicionais (horário, formato, requisitos...). Use \\n para quebrar linha.",
    )
    @app_commands.guild_only()
    @admin_only()
    async def scrim(
        self,
        interaction: discord.Interaction,
        pingar_everyone: bool = False,
        informacoes: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        extra = ("mention_everyone",) if pingar_everyone else ()
        channel = await get_config_channel(interaction, "scrim_channel", "Scrims", *extra)
        if channel is None:
            return

        owned = await self.bot.db.list_teams_by_owner(guild.id, interaction.user.id)
        embed = discord.Embed(
            title="⚔️ Desafio de Scrim / Amistoso",
            description=(truncate(informacoes.replace("\\n", "\n"), 3500) if informacoes
                         else "Time procurando adversário! Chame o responsável para combinar."),
            color=Colors.GOLD,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="👤 Responsável", value=interaction.user.mention, inline=True)
        if owned:
            embed.add_field(name="🏆 Time(s)", value="\n".join(f"[{t['tag']}] {t['name']}" for t in owned)[:1024],
                            inline=True)
        embed.add_field(name="🕒 Publicado", value=discord_ts(now_ts(), "R"), inline=True)
        logo = next((t["logo_url"] for t in owned if t["logo_url"]), None)
        embed.set_thumbnail(url=logo or interaction.user.display_avatar.url)
        embed.set_footer(text=guild.name)

        try:
            message = await channel.send(
                content="@everyone" if pingar_everyone else None,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(everyone=pingar_everyone),
            )
        except discord.HTTPException as exc:
            await interaction.followup.send(f"❌ Não foi possível publicar o scrim: {exc}", ephemeral=True)
            return

        await interaction.followup.send(f"✅ Scrim publicado em {channel.mention}: {message.jump_url}", ephemeral=True)
        await send_log(
            self.bot, guild, action="Scrim publicado", user=interaction.user, color=Colors.GOLD,
            fields=[("Canal", channel.mention), ("@everyone", "Sim" if pingar_everyone else "Não"),
                    ("Informações", informacoes or "—", False), ("Mensagem", message.jump_url, False)],
        )

    # ------------------------------------------------------------- free agent
    @app_commands.command(name="freeagent", description="Anuncia um jogador disponível no mercado (Free Agent).")
    @app_commands.describe(
        informacoes="Informações do jogador (posição, experiência, horários...). Use \\n para quebrar linha.",
        jogador="Jogador anunciado (padrão: você mesmo)",
    )
    @app_commands.guild_only()
    @admin_only()
    async def freeagent(
        self,
        interaction: discord.Interaction,
        informacoes: str,
        jogador: Optional[discord.User] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        channel = await get_config_channel(interaction, "freeagent_channel", "Free Agents")
        if channel is None:
            return

        player = jogador or interaction.user
        embed = discord.Embed(
            title="🆓 Free Agent Disponível",
            description=truncate(informacoes.replace("\\n", "\n"), 3500),
            color=Colors.SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="🎮 Jogador", value=player.mention, inline=True)
        embed.add_field(name="📣 Anunciado por", value=interaction.user.mention, inline=True)
        embed.set_thumbnail(url=player.display_avatar.url)
        embed.set_footer(text=guild.name)

        try:
            message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            await interaction.followup.send(f"❌ Não foi possível publicar o anúncio: {exc}", ephemeral=True)
            return

        await interaction.followup.send(f"✅ Anúncio publicado em {channel.mention}: {message.jump_url}",
                                        ephemeral=True)
        await send_log(
            self.bot, guild, action="Free Agent anunciado", user=interaction.user, color=Colors.SUCCESS,
            fields=[("Jogador", player.mention), ("Canal", channel.mention),
                    ("Informações", informacoes, False), ("Mensagem", message.jump_url, False)],
        )


# ==========================================================================
# TIMES
# ==========================================================================

NAME_MIN, NAME_MAX = 2, 40
TAG_RE = re.compile(r"^[A-Za-z0-9]{2,6}$")
CONFLICT_TEXT = {"nome": "o mesmo nome", "sigla": "a mesma sigla"}


def team_embed(team: dict, title: str, color: int, role: Optional[discord.Role] = None) -> discord.Embed:
    embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
    embed.add_field(name="ID", value=f"`#{team['id']}`", inline=True)
    embed.add_field(name="Nome", value=team["name"], inline=True)
    embed.add_field(name="Sigla", value=team["tag"], inline=True)
    embed.add_field(name="Responsável", value=f"<@{team['owner_id']}>", inline=True)
    role_text = role.mention if role else (f"<@&{team['role_id']}>" if team.get("role_id") else "—")
    embed.add_field(name="Cargo", value=role_text, inline=True)
    if team.get("logo_url"):
        embed.set_thumbnail(url=team["logo_url"])
    return embed


class Teams(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------- criar time
    @app_commands.command(name="criar-time", description="Cadastra um novo clube na liga.")
    @app_commands.describe(
        nome="Nome do time",
        sigla="Sigla do time (2 a 6 letras/números)",
        responsavel="Dono ou responsável pelo time",
        logo="Link (URL) da imagem da logo — opcional",
        cargo="Cargo já existente do time — opcional (se vazio, o bot cria um)",
    )
    @app_commands.guild_only()
    @admin_only()
    async def criar_time(
        self,
        interaction: discord.Interaction,
        nome: str,
        sigla: str,
        responsavel: discord.Member,
        logo: Optional[str] = None,
        cargo: Optional[discord.Role] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        db = self.bot.db

        nome = " ".join(nome.split())
        sigla = sigla.strip().upper()
        if not (NAME_MIN <= len(nome) <= NAME_MAX):
            await interaction.followup.send(f"❌ O nome deve ter entre {NAME_MIN} e {NAME_MAX} caracteres.",
                                            ephemeral=True)
            return
        if not TAG_RE.match(sigla):
            await interaction.followup.send("❌ A sigla deve ter de 2 a 6 caracteres (apenas letras e números).",
                                            ephemeral=True)
            return
        logo_url = None
        if logo:
            logo_url = safe_url(logo)
            if logo_url is None:
                await interaction.followup.send("❌ A logo deve ser um link válido começando com http:// ou https://.",
                                                ephemeral=True)
                return
        if responsavel.bot:
            await interaction.followup.send("❌ O responsável não pode ser um bot.", ephemeral=True)
            return

        conflict = await db.team_conflict(guild.id, nome.casefold(), sigla.casefold())
        if conflict:
            await interaction.followup.send(
                f"❌ Já existe um time cadastrado com {CONFLICT_TEXT[conflict]} (maiúsculas/minúsculas são ignoradas).",
                ephemeral=True)
            return

        # Cargo do time: usa o informado ou cria um novo.
        role: Optional[discord.Role] = None
        role_auto = False
        warnings: list[str] = []
        me = guild.me
        if cargo is not None:
            if cargo.is_default() or cargo.managed:
                await interaction.followup.send("❌ Esse cargo não pode ser usado como cargo de time.", ephemeral=True)
                return
            if cargo >= me.top_role:
                await interaction.followup.send(
                    "❌ Esse cargo está acima do cargo do bot. Suba o cargo do bot na lista de cargos.", ephemeral=True)
                return
            if await db.role_in_use(guild.id, cargo.id):
                await interaction.followup.send("❌ Esse cargo já pertence a outro time.", ephemeral=True)
                return
            role = cargo
        elif me.guild_permissions.manage_roles:
            try:
                role = await guild.create_role(name=nome, reason=f"Cargo do time {nome} (por {interaction.user})")
                role_auto = True
            except discord.HTTPException as exc:
                warnings.append(f"Não consegui criar o cargo do time: {exc}")
        else:
            warnings.append("O bot não tem a permissão **Gerenciar Cargos**; o time foi criado sem cargo.")

        try:
            team_id = await db.create_team(
                guild.id, nome, sigla, responsavel.id, logo_url,
                role.id if role else None, role_auto, interaction.user.id)
        except sqlite3.IntegrityError:
            if role_auto and role is not None:
                try:
                    await role.delete(reason="Cadastro de time duplicado")
                except discord.HTTPException:
                    pass
            await interaction.followup.send("❌ Já existe um time com esse nome ou sigla.", ephemeral=True)
            return

        if role is not None:
            error = await add_role_safe(responsavel, role, f"Responsável do time {nome}")
            if error:
                warnings.append(error)

        team = await db.get_team(team_id)
        embed = team_embed(team, "✅ Time cadastrado", Colors.SUCCESS, role)
        if warnings:
            embed.add_field(name="⚠️ Avisos", value="\n".join(warnings)[:1024], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
        await send_log(
            self.bot, guild, action="Time criado", user=interaction.user, color=Colors.SUCCESS,
            fields=[("Time", f"[{sigla}] {nome} (`#{team_id}`)"), ("Responsável", responsavel.mention),
                    ("Cargo", role.mention if role else "—")],
        )

    # ------------------------------------------------------------ excluir time
    @app_commands.command(name="excluir-time", description="Exclui (desativa) um time e dispensa todos os jogadores.")
    @app_commands.describe(time="Time que será excluído")
    @app_commands.autocomplete(time=team_autocomplete)
    @app_commands.guild_only()
    @admin_only()
    async def excluir_time(self, interaction: discord.Interaction, time: str) -> None:
        guild = interaction.guild
        db = self.bot.db
        team = await db.find_team(guild.id, time)
        if team is None:
            await interaction.response.send_message("❌ Time não encontrado (escolha um da lista).", ephemeral=True)
            return

        members = await db.list_active_members(team["id"])
        confirm = ConfirmView(interaction.user.id, confirm_label="Excluir time")
        await interaction.response.send_message(
            f"⚠️ Excluir o time **[{team['tag']}] {team['name']}**?\n"
            f"• **{len(members)}** jogador(es) serão dispensados e perderão o cargo do time.\n"
            "• O histórico do time continua salvo no banco de dados.\n\nEsta ação não pode ser desfeita.",
            view=confirm, ephemeral=True)
        await confirm.wait()
        if confirm.value is None:
            await interaction.edit_original_response(content="⌛ Tempo esgotado. Nada foi excluído.", view=None)
            return
        if not confirm.value:
            return

        # 1) Banco de dados primeiro (fonte da verdade); 2) cargos/anúncios do Discord.
        await db.end_all_memberships(team["id"], interaction.user.id, f"Time {team['name']} excluído")
        await db.deactivate_team(team["id"], interaction.user.id)

        problems: list[str] = []
        role = guild.get_role(team["role_id"]) if team.get("role_id") else None
        reason = f"Time {team['name']} excluído por {interaction.user}"
        if role is not None:
            holder_ids = {m["user_id"] for m in members} | {team["owner_id"]}
            for user_id in holder_ids:
                member = await fetch_member_safe(guild, user_id)
                if member is not None:
                    error = await remove_role_safe(member, role, reason)
                    if error:
                        problems.append(error)
            if team["role_auto"]:
                try:
                    await role.delete(reason=reason)
                except discord.HTTPException as exc:
                    problems.append(f"Não consegui apagar o cargo {role.name}: {exc}")

        # Anúncio consolidado no canal de demissões (se configurado e com permissão).
        announced = False
        cfg = await db.get_config(guild.id)
        channel = guild.get_channel(cfg["dismissal_channel"]) if cfg.get("dismissal_channel") else None
        if members and channel is not None:
            listing = "\n".join(f"• <@{m['user_id']}> — {m['position'] or '—'}" for m in members)
            embed = discord.Embed(
                title="📤 Time excluído — jogadores dispensados",
                description=truncate(f"O time **[{team['tag']}] {team['name']}** foi excluído. "
                                     f"Todos os jogadores abaixo foram dispensados:\n\n{listing}", 4000),
                color=Colors.DANGER, timestamp=discord.utils.utcnow())
            embed.set_footer(text=guild.name)
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                announced = True
            except discord.HTTPException:
                problems.append("Não consegui publicar o anúncio no canal de demissões.")

        summary = (f"✅ Time **[{team['tag']}] {team['name']}** excluído. "
                   f"{len(members)} jogador(es) dispensado(s)"
                   f"{' e anunciado(s)' if announced else ''}.")
        if problems:
            summary += "\n\n⚠️ Avisos:\n" + "\n".join(f"• {p}" for p in problems[:8])
        await interaction.edit_original_response(content=truncate(summary, 1900))

        await send_log(
            self.bot, guild, action="Time excluído", user=interaction.user, color=Colors.DANGER,
            fields=[("Time", f"[{team['tag']}] {team['name']} (`#{team['id']}`)"),
                    ("Jogadores dispensados", len(members)),
                    ("Jogadores", "\n".join(f"<@{m['user_id']}>" for m in members) or "—", False),
                    ("Problemas", "\n".join(problems) or "Nenhum", False)],
        )

    # ------------------------------------------------------------- editar time
    @app_commands.command(name="editar-time", description="Altera nome, sigla, responsável ou logo de um time.")
    @app_commands.describe(
        time="Time que será editado",
        novo_nome="Novo nome",
        nova_sigla="Nova sigla (2 a 6 letras/números)",
        responsavel="Novo dono/responsável",
        logo="Nova URL da logo",
    )
    @app_commands.autocomplete(time=team_autocomplete)
    @app_commands.guild_only()
    @admin_only()
    async def editar_time(
        self,
        interaction: discord.Interaction,
        time: str,
        novo_nome: Optional[str] = None,
        nova_sigla: Optional[str] = None,
        responsavel: Optional[discord.Member] = None,
        logo: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        db = self.bot.db
        team = await db.find_team(guild.id, time)
        if team is None:
            await interaction.followup.send("❌ Time não encontrado (escolha um da lista).", ephemeral=True)
            return
        if not any([novo_nome, nova_sigla, responsavel, logo]):
            await interaction.followup.send("⚠️ Informe pelo menos um campo para alterar.", ephemeral=True)
            return

        changes: dict = {}
        diffs: list[tuple[str, str]] = []
        if novo_nome:
            novo_nome = " ".join(novo_nome.split())
            if not (NAME_MIN <= len(novo_nome) <= NAME_MAX):
                await interaction.followup.send(f"❌ O nome deve ter entre {NAME_MIN} e {NAME_MAX} caracteres.",
                                                ephemeral=True)
                return
            changes.update(name=novo_nome, name_key=novo_nome.casefold())
            diffs.append(("Nome", f"{team['name']} → {novo_nome}"))
        if nova_sigla:
            nova_sigla = nova_sigla.strip().upper()
            if not TAG_RE.match(nova_sigla):
                await interaction.followup.send("❌ A sigla deve ter de 2 a 6 caracteres (letras e números).",
                                                ephemeral=True)
                return
            changes.update(tag=nova_sigla, tag_key=nova_sigla.casefold())
            diffs.append(("Sigla", f"{team['tag']} → {nova_sigla}"))
        if responsavel:
            changes["owner_id"] = responsavel.id
            diffs.append(("Responsável", f"<@{team['owner_id']}> → {responsavel.mention}"))
        if logo:
            url = safe_url(logo)
            if url is None:
                await interaction.followup.send("❌ A logo deve ser um link http:// ou https:// válido.", ephemeral=True)
                return
            changes["logo_url"] = url
            diffs.append(("Logo", "atualizada"))

        new_name = changes.get("name", team["name"])
        new_tag = changes.get("tag", team["tag"])
        conflict = await db.team_conflict(guild.id, new_name.casefold(), new_tag.casefold(), exclude_id=team["id"])
        if conflict:
            await interaction.followup.send(f"❌ Já existe outro time com {CONFLICT_TEXT[conflict]}.", ephemeral=True)
            return

        try:
            # Os vínculos usam o ID do time, então o histórico é preservado automaticamente.
            await db.update_team(team["id"], **changes)
        except sqlite3.IntegrityError:
            await interaction.followup.send("❌ Já existe outro time com esse nome ou sigla.", ephemeral=True)
            return

        warnings: list[str] = []
        role = guild.get_role(team["role_id"]) if team.get("role_id") else None
        if "name" in changes and role is not None and team["role_auto"]:
            try:
                await role.edit(name=new_name, reason=f"Time renomeado por {interaction.user}")
            except discord.HTTPException as exc:
                warnings.append(f"Não consegui renomear o cargo: {exc}")
        if "owner_id" in changes and role is not None:
            old_owner = await fetch_member_safe(guild, team["owner_id"])
            has_contract = old_owner is not None and (
                await db.get_active_membership(guild.id, old_owner.id) is not None)
            if old_owner is not None and not has_contract:
                error = await remove_role_safe(old_owner, role, "Deixou de ser responsável do time")
                if error:
                    warnings.append(error)
            error = await add_role_safe(responsavel, role, "Novo responsável do time")
            if error:
                warnings.append(error)

        updated = await db.get_team(team["id"])
        embed = team_embed(updated, "✏️ Time atualizado", Colors.INFO, role)
        for name, value in diffs:
            embed.add_field(name=f"Alterado: {name}", value=value, inline=False)
        if warnings:
            embed.add_field(name="⚠️ Avisos", value="\n".join(warnings)[:1024], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
        await send_log(
            self.bot, guild, action="Time editado", user=interaction.user, color=Colors.WARNING,
            fields=[("Time", f"`#{team['id']}` [{team['tag']}] {team['name']}")] +
                   [(name, value, False) for name, value in diffs],
        )

    # ------------------------------------------------------- consultas públicas
    @app_commands.command(name="times", description="Lista os times ativos da liga.")
    @app_commands.guild_only()
    async def times(self, interaction: discord.Interaction) -> None:
        teams = await self.bot.db.list_teams_with_counts(interaction.guild.id)
        if not teams:
            await interaction.response.send_message("Nenhum time cadastrado ainda.", ephemeral=True)
            return
        lines = [f"`#{t['id']}` **[{t['tag']}] {t['name']}** — <@{t['owner_id']}> • 👥 {t['players']}" for t in teams]
        embed = discord.Embed(title=f"🏆 Times da liga ({len(teams)})", description=truncate("\n".join(lines), 4000),
                              color=Colors.GOLD)
        embed.set_footer(text=interaction.guild.name)
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="elenco", description="Mostra o elenco atual de um time.")
    @app_commands.describe(time="Time que deseja consultar")
    @app_commands.autocomplete(time=team_autocomplete)
    @app_commands.guild_only()
    async def elenco(self, interaction: discord.Interaction, time: str) -> None:
        team = await self.bot.db.find_team(interaction.guild.id, time)
        if team is None:
            await interaction.response.send_message("❌ Time não encontrado (escolha um da lista).", ephemeral=True)
            return
        members = await self.bot.db.list_active_members(team["id"])
        titulares = [m for m in members if m["role_type"] == "titular"]
        reservas = [m for m in members if m["role_type"] != "titular"]

        def fmt(rows: list[dict]) -> str:
            return "\n".join(f"<@{m['user_id']}> — {m['position'] or '—'}" for m in rows) or "—"

        embed = team_embed(team, f"🏆 [{team['tag']}] {team['name']}", Colors.GOLD)
        embed.add_field(name=f"⭐ Titulares ({len(titulares)})", value=truncate(fmt(titulares), 1024), inline=False)
        embed.add_field(name=f"🪑 Reservas ({len(reservas)})", value=truncate(fmt(reservas), 1024), inline=False)
        embed.add_field(name="Criado em", value=discord_ts(team["created_at"], "D"), inline=True)
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


# ==========================================================================
# CONTRATOS E DEMISSÕES
# ==========================================================================

class Contracts(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # --------------------------------------------------------------- contrato
    @app_commands.command(name="contrato", description="Registra a contratação de um jogador por um clube.")
    @app_commands.describe(
        jogador="Jogador contratado",
        manager="Manager do time",
        clube="Clube contratante",
        posicao="Posição do jogador",
        role="Titular ou reserva",
    )
    @app_commands.choices(role=[
        app_commands.Choice(name="Titular", value="titular"),
        app_commands.Choice(name="Reserva", value="reserva"),
    ])
    @app_commands.autocomplete(clube=team_autocomplete)
    @app_commands.guild_only()
    @admin_only()
    async def contrato(
        self,
        interaction: discord.Interaction,
        jogador: discord.Member,
        manager: discord.Member,
        clube: str,
        posicao: str,
        role: app_commands.Choice[str],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        db = self.bot.db
        posicao = truncate(" ".join(posicao.split()), 60)

        if jogador.bot:
            await interaction.followup.send("❌ Não é possível contratar um bot.", ephemeral=True)
            return
        team = await db.find_team(guild.id, clube)
        if team is None:
            await interaction.followup.send("❌ Clube não encontrado ou desativado (escolha um da lista).",
                                            ephemeral=True)
            return

        # Regra: um jogador não pode estar em dois clubes ao mesmo tempo.
        current = await db.get_active_membership(guild.id, jogador.id)
        if current is not None:
            if current["team_id"] == team["id"]:
                text = f"❌ {jogador.mention} já faz parte de **{team['name']}**."
            else:
                text = (f"❌ {jogador.mention} já está contratado por **[{current['team_tag']}] "
                        f"{current['team_name']}**. Use `/demitir` antes de registrar uma nova contratação.")
            await interaction.followup.send(text, ephemeral=True)
            return

        # Valida o canal ANTES de gravar, para não registrar contrato sem poder anunciá-lo.
        channel = await get_config_channel(interaction, "contract_channel", "Contratos")
        if channel is None:
            return

        try:
            membership_id = await db.add_membership(
                guild.id, team["id"], jogador.id, manager.id, posicao, role.value, interaction.user.id)
        except sqlite3.IntegrityError:
            await interaction.followup.send("❌ Este jogador já possui um vínculo ativo com outro clube.",
                                            ephemeral=True)
            return

        embed = discord.Embed(
            title="📝 Nova Contratação",
            description=f"{jogador.mention} foi contratado(a) por **{team['name']}**!",
            color=Colors.SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=f"{team['name']} [{team['tag']}]", icon_url=team["logo_url"] or None)
        embed.add_field(name="🎮 Jogador", value=jogador.mention, inline=True)
        embed.add_field(name="🏆 Clube", value=f"[{team['tag']}] {team['name']}", inline=True)
        embed.add_field(name="👔 Manager", value=manager.mention, inline=True)
        embed.add_field(name="📌 Posição", value=posicao, inline=True)
        embed.add_field(name="🎖️ Role", value=role.name, inline=True)
        embed.add_field(name="✍️ Registrado por", value=interaction.user.mention, inline=True)
        embed.set_thumbnail(url=jogador.display_avatar.url)
        embed.set_footer(text=guild.name)

        try:
            message = await channel.send(
                content=f"{jogador.mention} {manager.mention}",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=[jogador, manager]),
            )
        except discord.HTTPException as exc:
            await db.delete_membership(membership_id)  # desfaz o registro para manter consistência
            await interaction.followup.send(
                f"❌ Não consegui publicar o contrato ({exc}). Nada foi registrado.", ephemeral=True)
            return

        warnings: list[str] = []
        team_role = guild.get_role(team["role_id"]) if team.get("role_id") else None
        error = await add_role_safe(jogador, team_role, f"Contratado por {team['name']}")
        if error:
            warnings.append(error)

        text = f"✅ Contratação registrada e publicada em {channel.mention}: {message.jump_url}"
        if warnings:
            text += "\n⚠️ " + "\n⚠️ ".join(warnings)
        await interaction.followup.send(text, ephemeral=True)
        await send_log(
            self.bot, guild, action="Contratação registrada", user=interaction.user, color=Colors.SUCCESS,
            fields=[("Jogador", jogador.mention), ("Clube", f"[{team['tag']}] {team['name']} (`#{team['id']}`)"),
                    ("Manager", manager.mention), ("Posição", posicao), ("Role", role.name),
                    ("Cargo do time", "Atribuído" if not error and team_role else "Não atribuído")],
        )

    # ---------------------------------------------------------------- demitir
    @app_commands.command(name="demitir", description="Registra a dispensa de um jogador de um clube.")
    @app_commands.describe(jogador="Jogador dispensado", clube="Clube que está dispensando",
                           motivo="Motivo da dispensa (opcional)")
    @app_commands.autocomplete(clube=team_autocomplete)
    @app_commands.guild_only()
    @admin_only()
    async def demitir(
        self,
        interaction: discord.Interaction,
        jogador: discord.User,
        clube: str,
        motivo: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        db = self.bot.db

        team = await db.find_team(guild.id, clube)
        if team is None:
            await interaction.followup.send("❌ Clube não encontrado ou desativado (escolha um da lista).",
                                            ephemeral=True)
            return
        membership = await db.get_active_membership(guild.id, jogador.id)
        if membership is None or membership["team_id"] != team["id"]:
            extra = (f" Ele(a) está em **[{membership['team_tag']}] {membership['team_name']}**."
                     if membership else "")
            await interaction.followup.send(
                f"❌ {jogador.mention} não possui contrato ativo com **{team['name']}**.{extra}", ephemeral=True)
            return

        channel = await get_config_channel(interaction, "dismissal_channel", "Demissões")
        if channel is None:
            return

        reason = truncate(motivo, 300) if motivo else "Não informado"
        await db.end_membership(membership["id"], interaction.user.id, reason)

        warnings: list[str] = []
        team_role = guild.get_role(team["role_id"]) if team.get("role_id") else None
        member = await fetch_member_safe(guild, jogador.id)
        if member is not None:
            error = await remove_role_safe(member, team_role, f"Dispensado de {team['name']}")
            if error:
                warnings.append(error)

        embed = discord.Embed(
            title="📤 Jogador Dispensado",
            description=f"{jogador.mention} foi dispensado(a) de **{team['name']}**.",
            color=Colors.DANGER,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=f"{team['name']} [{team['tag']}]", icon_url=team["logo_url"] or None)
        embed.add_field(name="🎮 Jogador", value=jogador.mention, inline=True)
        embed.add_field(name="🏆 Clube", value=f"[{team['tag']}] {team['name']}", inline=True)
        embed.add_field(name="📌 Posição", value=membership["position"] or "—", inline=True)
        embed.add_field(name="📝 Motivo", value=reason, inline=False)
        embed.add_field(name="✍️ Registrado por", value=interaction.user.mention, inline=True)
        embed.set_thumbnail(url=jogador.display_avatar.url)
        embed.set_footer(text=guild.name)

        announced = True
        try:
            message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            announced = False
            warnings.append(f"A dispensa foi registrada, mas o anúncio falhou: {exc}")

        text = (f"✅ Dispensa registrada e publicada em {channel.mention}: {message.jump_url}"
                if announced else "⚠️ Dispensa registrada, porém sem anúncio.")
        if warnings:
            text += "\n⚠️ " + "\n⚠️ ".join(warnings)
        await interaction.followup.send(text, ephemeral=True)
        await send_log(
            self.bot, guild, action="Demissão registrada", user=interaction.user, color=Colors.DANGER,
            fields=[("Jogador", jogador.mention), ("Clube", f"[{team['tag']}] {team['name']} (`#{team['id']}`)"),
                    ("Motivo", reason, False)],
        )


# ==========================================================================
# MODERAÇÃO
# ==========================================================================

MAX_BAN_SECONDS = 5 * 365 * 86400
MAX_TIMEOUT_SECONDS = 28 * 86400  # limite do Discord
NO_REASON = "Não informado"

TYPE_INFO = {
    "ban": ("🔨", "Banimento"),
    "unban": ("♻️", "Desbanimento"),
    "kick": ("👢", "Expulsão"),
    "timeout": ("🔇", "Timeout"),
    "untimeout": ("🔊", "Timeout removido"),
}


def hierarchy_error(
    guild: discord.Guild, moderator: discord.Member, target_id: int, member: Optional[discord.Member]
) -> Optional[str]:
    """Retorna o motivo do bloqueio por hierarquia, ou None se a ação é permitida."""
    me = guild.me
    if target_id == guild.owner_id:
        return "Não é possível punir o dono do servidor."
    if target_id == moderator.id:
        return "Você não pode punir a si mesmo."
    if target_id == me.id:
        return "Não posso punir a mim mesmo."
    if member is None:
        return None
    if member.top_role >= me.top_role:
        return ("O alvo tem cargo igual ou superior ao meu. "
                "Suba o cargo do bot na lista de cargos do servidor.")
    if moderator.id != guild.owner_id and member.top_role >= moderator.top_role:
        return "O alvo tem cargo igual ou superior ao seu."
    return None


def audit_reason(moderator: discord.abc.User, reason: str) -> str:
    return truncate(f"{reason} | Responsável: {moderator} ({moderator.id})", 512)


async def dm_user(user: discord.abc.User, guild: discord.Guild, title: str, reason: str,
                  duration: Optional[int] = None, show_duration: bool = False) -> None:
    """Avisa o usuário por DM (melhor esforço: ignora DMs fechadas)."""
    embed = discord.Embed(title=title, description=f"Servidor: **{guild.name}**", color=Colors.DANGER,
                          timestamp=discord.utils.utcnow())
    embed.add_field(name="Motivo", value=truncate(reason, 1024), inline=False)
    if show_duration:
        embed.add_field(name="Duração", value=format_duration(duration), inline=True)
    try:
        await user.send(embed=embed)
    except discord.HTTPException:
        pass


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.expire_bans.start()

    async def cog_unload(self) -> None:
        self.expire_bans.cancel()

    # ------------------------------------------------------ banimento temporário
    @tasks.loop(minutes=1)
    async def expire_bans(self) -> None:
        """Remove banimentos temporários vencidos (também após reinicializações)."""
        try:
            rows = await self.bot.db.get_expired_bans(now_ts())
        except Exception:  # noqa: BLE001
            log.exception("Falha ao consultar banimentos expirados")
            return
        for row in rows:
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                continue  # o bot não está mais (ou ainda não carregou) esse servidor
            error: Optional[str] = None
            try:
                await guild.unban(discord.Object(id=row["user_id"]), reason="Banimento temporário expirado")
            except discord.NotFound:
                pass  # já tinha sido desbanido manualmente
            except discord.HTTPException as exc:
                error = str(exc)
            await self.bot.db.deactivate_bans(guild.id, row["user_id"])
            if error is None:
                await self.bot.db.add_punishment(guild.id, row["user_id"], self.bot.user.id, "unban",
                                                 "Banimento temporário expirado")
            await send_log(
                self.bot, guild,
                action="Desbanimento automático" if error is None else "Erro no desbanimento automático",
                user=self.bot.user, color=Colors.SUCCESS if error is None else Colors.DANGER,
                fields=[("Usuário", f"<@{row['user_id']}> (`{row['user_id']}`)"),
                        ("Banimento nº", f"#{row['id']}"),
                        ("Detalhe", error or "Duração do banimento terminou.", False)],
            )

    @expire_bans.before_loop
    async def _before_expire(self) -> None:
        await self.bot.wait_until_ready()

    # ----------------------------------------------------------------- helpers
    async def _resolve_target(
        self, interaction: discord.Interaction, usuario: Optional[discord.User], id_usuario: Optional[str]
    ) -> Optional[discord.User]:
        if usuario is not None:
            return usuario
        if id_usuario and id_usuario.strip().isdigit():
            try:
                return await self.bot.fetch_user(int(id_usuario.strip()))
            except discord.NotFound:
                await interaction.followup.send("❌ Nenhum usuário encontrado com esse ID.", ephemeral=True)
                return None
            except discord.HTTPException:
                await interaction.followup.send("❌ Não consegui consultar esse ID agora.", ephemeral=True)
                return None
        await interaction.followup.send("❌ Informe o `usuario` ou um `id_usuario` numérico válido.", ephemeral=True)
        return None

    async def _check_bot_perm(self, interaction: discord.Interaction, perm: str) -> bool:
        missing = missing_perms(interaction.guild.me.guild_permissions, perm)
        if missing:
            await interaction.followup.send(f"❌ O bot não tem a permissão **{missing[0]}**.", ephemeral=True)
            return False
        return True

    async def _record(
        self, interaction: discord.Interaction, target: discord.abc.User, ptype: str, reason: str,
        duration: Optional[int] = None, expires_at: Optional[int] = None, active: bool = False,
    ) -> int:
        """Salva no SQLite e envia o registro ao canal de logs."""
        guild = interaction.guild
        pid = await self.bot.db.add_punishment(
            guild.id, target.id, interaction.user.id, ptype, reason, duration, expires_at, active)
        emoji, label = TYPE_INFO[ptype]
        fields = [("Punido", f"{target.mention} (`{target.id}`)"), ("Tipo", f"{emoji} {label}"),
                  ("Registro", f"#{pid}"), ("Motivo", reason, False)]
        if ptype in ("ban", "timeout"):
            fields.append(("Duração", format_duration(duration)))
            if expires_at:
                fields.append(("Expira em", discord_ts(expires_at)))
        await send_log(self.bot, guild, action=label, user=interaction.user, color=Colors.DANGER if ptype in
                       ("ban", "kick", "timeout") else Colors.SUCCESS, fields=fields)
        return pid

    # --------------------------------------------------------------------- ban
    @app_commands.command(name="ban", description="Bane um usuário (permanente ou temporário).")
    @app_commands.describe(
        motivo="Motivo do banimento",
        usuario="Usuário a banir",
        id_usuario="ID do usuário (use se ele não estiver no servidor)",
        duracao="Opcional: 30m, 12h, 7d, 1w... Se vazio, o banimento é permanente",
    )
    @app_commands.guild_only()
    @admin_only()
    async def ban(
        self,
        interaction: discord.Interaction,
        motivo: str,
        usuario: Optional[discord.User] = None,
        id_usuario: Optional[str] = None,
        duracao: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not await self._check_bot_perm(interaction, "ban_members"):
            return
        target = await self._resolve_target(interaction, usuario, id_usuario)
        if target is None:
            return

        seconds: Optional[int] = None
        if duracao:
            try:
                seconds = parse_duration(duracao)
            except ValueError as exc:
                await interaction.followup.send(f"❌ {exc}", ephemeral=True)
                return
            if seconds > MAX_BAN_SECONDS:
                await interaction.followup.send("❌ A duração máxima de um banimento temporário é 5 anos.",
                                                ephemeral=True)
                return

        member = await fetch_member_safe(guild, target.id)
        problem = hierarchy_error(guild, interaction.user, target.id, member)
        if problem:
            await interaction.followup.send(f"❌ {problem}", ephemeral=True)
            return

        try:
            await guild.fetch_ban(target)
        except discord.NotFound:
            pass
        except discord.HTTPException:
            pass
        else:
            await interaction.followup.send("⚠️ Este usuário já está banido.", ephemeral=True)
            return

        reason = truncate(motivo, 500)
        if member is not None:
            await dm_user(target, guild, "🔨 Você foi banido", reason, seconds, show_duration=True)
        try:
            await guild.ban(target, reason=audit_reason(interaction.user, reason))
        except discord.Forbidden:
            await interaction.followup.send("❌ O Discord recusou o banimento (permissões/hierarquia do bot).",
                                            ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"❌ Erro ao banir: {exc}", ephemeral=True)
            return

        expires_at = now_ts() + seconds if seconds else None
        pid = await self._record(interaction, target, "ban", reason, seconds, expires_at, active=True)
        await interaction.followup.send(
            f"✅ **{target}** foi banido ({format_duration(seconds)}). Registro `#{pid}`.", ephemeral=True)

    # ------------------------------------------------------------------- unban
    @app_commands.command(name="unban", description="Remove o banimento de um usuário.")
    @app_commands.describe(id_usuario="ID do usuário banido", motivo="Motivo (opcional)")
    @app_commands.guild_only()
    @admin_only()
    async def unban(self, intera
