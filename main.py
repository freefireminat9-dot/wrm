import discord
from discord import app_commands, ui
from discord.ext import commands
import aiosqlite
import datetime
import re

# --- CONFIGURAÇÃO DO BOT ---
class MPSBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        async with aiosqlite.connect("mps_league.db") as db:
            # Tabela de Configurações
            await db.execute('''CREATE TABLE IF NOT EXISTS config (
                guild_id INTEGER PRIMARY KEY, log_channel INTEGER, fa_channel INTEGER,
                admin_role INTEGER, manager_role INTEGER, player_role INTEGER
            )''')
            # Tabela de Times
            await db.execute('''CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, 
                name TEXT, prefix TEXT, manager_id INTEGER
            )''')
            # Tabela de Jogadores
            await db.execute('''CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY, guild_id INTEGER, 
                team_id INTEGER, status TEXT
            )''')
            await db.commit()
        await self.tree.sync()

bot = MPSBot()

# --- AUXILIARES: LOGS E PERMISSÕES ---
async def logger(guild, title, description, color=discord.Color.blue()):
    async with aiosqlite.connect("mps_league.db") as db:
        async with db.execute("SELECT log_channel FROM config WHERE guild_id = ?", (guild.id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                channel = guild.get_channel(row[0])
                if channel:
                    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.datetime.now())
                    await channel.send(embed=embed)

# --- COMANDOS DE ADMINISTRAÇÃO E CONFIGURAÇÃO ---

@bot.tree.command(name="configurar", description="Configura os canais e cargos da liga")
@app_commands.checks.has_permissions(administrator=True)
async def configurar(interaction: discord.Interaction, log_channel: discord.TextChannel, fa_channel: discord.TextChannel, admin_role: discord.Role, player_role: discord.Role):
    async with aiosqlite.connect("mps_league.db") as db:
        await db.execute("INSERT OR REPLACE INTO config (guild_id, log_channel, fa_channel, admin_role, player_role) VALUES (?, ?, ?, ?, ?)",
                         (interaction.guild_id, log_channel.id, fa_channel.id, admin_role.id, player_role.id))
        await db.commit()
    await interaction.response.send_message("✅ Configurações salvas com sucesso!", ephemeral=True)

# --- GESTÃO DE TIMES ---

@bot.tree.command(name="criar-time", description="Registra um novo time na liga")
async def criar_time(interaction: discord.Interaction, nome: str, prefixo: str, manager: discord.Member):
    async with aiosqlite.connect("mps_league.db") as db:
        await db.execute("INSERT INTO teams (guild_id, name, prefix, manager_id) VALUES (?, ?, ?, ?)",
                         (interaction.guild_id, nome, prefixo.upper(), manager.id))
        await db.commit()
    await interaction.response.send_message(f"Time **{nome}** [{prefixo}] criado com sucesso!")
    await logger(interaction.guild, "Novo Time Criado", f"Time: {nome}\nManager: {manager.mention}", discord.Color.green())

# --- CONTRATOS ---

class ContratoView(ui.View):
    def __init__(self, jogador, time_id, time_nome):
        super().__init__(timeout=None)
        self.jogador = jogador
        self.time_id = time_id
        self.time_nome = time_nome

    @ui.button(label="Aceitar", style=discord.ButtonStyle.success)
    async def aceitar(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.jogador.id:
            return await interaction.response.send_message("Esta proposta não é para você!", ephemeral=True)
        
        async with aiosqlite.connect("mps_league.db") as db:
            await db.execute("INSERT OR REPLACE INTO players (user_id, guild_id, team_id, status) VALUES (?, ?, ?, ?)",
                             (self.jogador.id, interaction.guild_id, self.time_id, 'active'))
            await db.commit()
        
        await interaction.response.edit_message(content=f"✅ {self.jogador.mention} agora faz parte do **{self.time_nome}**!", view=None)
        await logger(interaction.guild, "Contratação Realizada", f"Jogador: {self.jogador.mention}\nTime: {self.time_nome}", discord.Color.gold())

@bot.tree.command(name="contratar", description="Envia uma proposta de contrato")
async def contratar(interaction: discord.Interaction, jogador: discord.Member):
    async with aiosqlite.connect("mps_league.db") as db:
        async with db.execute("SELECT id, name FROM teams WHERE manager_id = ?", (interaction.user.id,)) as cursor:
            time = await cursor.fetchone()
            if not time:
                return await interaction.response.send_message("Apenas Managers podem contratar!", ephemeral=True)
            
            view = ContratoView(jogador, time[0], time[1])
            await interaction.response.send_message(f"{jogador.mention}, você recebeu uma proposta do **{time[1]}**!", view=view)

# --- UTILITÁRIOS: SAY E EMBED ---

@bot.tree.command(name="say", description="Bot envia mensagem normal")
async def say(interaction: discord.Interaction, mensagem: str, canal: discord.TextChannel = None):
    chan = canal or interaction.channel
    await chan.send(mensagem)
    await interaction.response.send_message("Mensagem enviada.", ephemeral=True)

@bot.tree.command(name="embed", description="Cria um embed completo")
async def embed_cmd(interaction: discord.Interaction, titulo: str, descricao: str, cor_hex: str = "3498db"):
    color = int(cor_hex.replace("#", ""), 16)
    embed = discord.Embed(title=titulo, description=descricao, color=color)
    
    view = ui.View()
    btn_send = ui.Button(label="Enviar", style=discord.ButtonStyle.primary)
    
    async def callback(inter):
        await interaction.channel.send(embed=embed)
        await inter.response.edit_message(content="✅ Embed enviado!", view=None)
    
    btn_send.callback = callback
    view.add_item(btn_send)
    
    await interaction.response.send_message("Preview do seu Embed:", embed=embed, view=view, ephemeral=True)

# --- TICKET SYSTEM ---
class TicketView(ui.View):
    @ui.button(label="Abrir Ticket", style=discord.ButtonStyle.grey, emoji="📩")
    async def open_ticket(self, interaction: discord.Interaction, button: ui.Button):
        thread = await interaction.channel.create_thread(name=f"ticket-{interaction.user.name}", type=discord.ChannelType.private_thread)
        await thread.send(f"Olá {interaction.user.mention}, a Staff logo te atenderá.")
        await interaction.response.send_message(f"Ticket aberto em {thread.mention}", ephemeral=True)

@bot.tree.command(name="ticket", description="Envia o painel de tickets")
async def ticket(interaction: discord.Interaction):
    embed = discord.Embed(title="Suporte da Liga", description="Clique no botão abaixo para abrir um chamado.")
    await interaction.response.send_message("Painel enviado.", ephemeral=True)
    await interaction.channel.send(embed=embed, view=TicketView())

import os

bot.run("TOKEN")
