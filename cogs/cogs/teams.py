import discord
from discord import app_commands
from discord.ext import commands
from database import get_db
from cogs.config import check_admin_permission

class ConfirmDeleteTeamView(discord.ui.View):
    def __init__(self, team_name: str):
        super().__init__(timeout=60)
        self.team_name = team_name

    @discord.ui.button(label="Confirmar Exclusão de Time", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        guild = interaction.guild

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM teams WHERE guild_id = ? AND lower(name) = ?", (guild.id, self.team_name.lower()))
            team = cursor.fetchone()

            if not team:
                return await interaction.followup.send("❌ Time não encontrado.", ephemeral=True)

            if team["role_id"]:
                role = guild.get_role(team["role_id"])
                if role:
                    try:
                        await role.delete(reason=f"Exclusão do clube {self.team_name}")
                    except discord.HTTPException:
                        pass

            cursor.execute("DELETE FROM contracts WHERE guild_id = ? AND team_id = ?", (guild.id, team["id"]))
            cursor.execute("DELETE FROM teams WHERE id = ?", (team["id"],))

        await interaction.followup.send(f"✅ O time **{self.team_name.upper()}** foi excluído e os contratos foram cancelados.", ephemeral=True)

class TeamsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="criar-time", description="Cadastra um novo time na liga.")
    @app_commands.describe(nome="Nome do Clube", sigla="Sigla oficial", responsavel="Dono ou Líder", logo_url="Link de Imagem (URL)")
    async def criar_time(self, interaction: discord.Interaction, nome: str, sigla: str, responsavel: discord.Member, logo_url: str = None):
        if not await check_admin_permission(interaction):
            return await interaction.response.send_message("❌ Permissão negada.", ephemeral=True)

        try:
            role = await interaction.guild.create_role(name=f"[{sigla.upper()}] {nome.title()}", mentionable=True)
        except discord.Forbidden:
            return await interaction.response.send_message("❌ O bot não tem permissão para criar cargos.", ephemeral=True)

        try:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO teams (guild_id, name, acronym, owner_id, logo_url, role_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (interaction.guild.id, nome.lower(), sigla.upper(), responsavel.id, logo_url, role.id)
                )
            
            await responsavel.add_roles(role)
            embed = discord.Embed(title="🛡️ Clube Cadastrado", color=discord.Color.green())
            embed.add_field(name="Time", value=nome.title(), inline=True)
            embed.add_field(name="Sigla", value=sigla.upper(), inline=True)
            embed.add_field(name="Responsável", value=responsavel.mention, inline=True)
            embed.add_field(name="Cargo Criado", value=role.mention, inline=False)
            if logo_url:
                embed.set_thumbnail(url=logo_url)

            await interaction.response.send_message(embed=embed)
        except Exception:
            await role.delete()
            await interaction.response.send_message("❌ Erro ao criar o time! Sigla ou Nome já cadastrados.", ephemeral=True)

    @app_commands.command(name="excluir-time", description="Exclui um clube da liga.")
    @app_commands.describe(nome="Nome do clube")
    async def excluir_time(self, interaction: discord.Interaction, nome: str):
        if not await check_admin_permission(interaction):
            return await interaction.response.send_message("❌ Permissão negada.", ephemeral=True)

        view = ConfirmDeleteTeamView(team_name=nome)
        await interaction.response.send_message(f"⚠️ Deseja excluir o clube **{nome.upper()}** e todos os seus vínculos?", view=view, ephemeral=True)

    @app_commands.command(name="contrato", description="Registra uma contratação.")
    @app_commands.describe(jogador="Atleta", time_nome="Nome do time", posicao="Posição", role="Titular ou Reserva")
    @app_commands.choices(role=[
        app_commands.Choice(name="Titular", value="Titular"),
        app_commands.Choice(name="Reserva", value="Reserva")
    ])
    async def contrato(self, interaction: discord.Interaction, jogador: discord.Member, time_nome: str, posicao: str, role: str):
        if not await check_admin_permission(interaction):
            return await interaction.response.send_message("❌ Permissão negada.", ephemeral=True)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM teams WHERE guild_id = ? AND lower(name) = ?", (interaction.guild.id, time_nome.lower()))
            team = cursor.fetchone()

            if not team:
                return await interaction.response.send_message("❌ Time não encontrado.", ephemeral=True)

            cursor.execute("SELECT * FROM contracts WHERE guild_id = ? AND player_id = ? AND status = 'active'", (interaction.guild.id, jogador.id))
            if cursor.fetchone():
                return await interaction.response.send_message("❌ O atleta já possui um contrato ativo!", ephemeral=True)

            cursor.execute("INSERT INTO contracts (guild_id, team_id, player_id, role, position) VALUES (?, ?, ?, ?, ?)",
                           (interaction.guild.id, team["id"], jogador.id, role, posicao))

            cursor.execute("SELECT contracts_channel FROM guild_configs WHERE guild_id = ?", (interaction.guild.id,))
            cfg = cursor.fetchone()

        if team["role_id"]:
            t_role = interaction.guild.get_role(team["role_id"])
            if t_role:
                try:
                    await jogador.add_roles(t_role)
                except discord.HTTPException:
                    pass

        embed = discord.Embed(title="📝 Novo Contrato Registrado", color=discord.Color.green())
        embed.add_field(name="Atleta", value=jogador.mention, inline=True)
        embed.add_field(name="Clube", value=team["name"].title(), inline=True)
        embed.add_field(name="Condição", value=f"{posicao} ({role})", inline=True)
        if team["logo_url"]:
            embed.set_thumbnail(url=team["logo_url"])

        if cfg and cfg["contracts_channel"]:
            chan = interaction.guild.get_channel(cfg["contracts_channel"])
            if chan:
                await chan.send(embed=embed)

        await interaction.response.send_message(f"✅ Contrato registrado para {jogador.mention}!", ephemeral=True)

    @app_commands.command(name="demitir", description="Demite um jogador.")
    @app_commands.describe(jogador="Atleta")
    async def demitir(self, interaction: discord.Interaction, jogador: discord.Member):
        if not await check_admin_permission(interaction):
            return await interaction.response.send_message("❌ Permissão negada.", ephemeral=True)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT contracts.id, teams.role_id, teams.name FROM contracts
                JOIN teams ON contracts.team_id = teams.id
                WHERE contracts.guild_id = ? AND contracts.player_id = ? AND contracts.status = 'active'
            """, (interaction.guild.id, jogador.id))
            contract = cursor.fetchone()

            if not contract:
                return await interaction.response.send_message("❌ Atleta sem contrato ativo.", ephemeral=True)

            cursor.execute("DELETE FROM contracts WHERE id = ?", (contract["id"],))
            cursor.execute("SELECT dismissals_channel FROM guild_configs WHERE guild_id = ?", (interaction.guild.id,))
            cfg = cursor.fetchone()

        if contract["role_id"]:
            r = interaction.guild.get_role(contract["role_id"])
            if r:
                try:
                    await jogador.remove_roles(r)
                except discord.HTTPException:
                    pass

        embed = discord.Embed(title="❌ Rescisão Contratual", color=discord.Color.red())
        embed.add_field(name="Atleta", value=jogador.mention, inline=True)
        embed.add_field(name="Ex-Clube", value=contract["name"].title(), inline=True)

        if cfg and cfg["dismissals_channel"]:
            chan = interaction.guild.get_channel(cfg["dismissals_channel"])
            if chan:
                await chan.send(embed=embed)

        await interaction.response.send_message(f"✅ Demissão efetuada para {jogador.mention}.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(TeamsCog(bot))
