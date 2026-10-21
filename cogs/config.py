import discord
from discord import app_commands
from discord.ext import commands
from database import get_db

async def check_admin_permission(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator or interaction.user.id == interaction.guild.owner_id:
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

class ConfigSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    async def save_field(self, interaction: discord.Interaction, field: str, value: int, label_msg: str):
        if not await check_admin_permission(interaction):
            return await interaction.response.send_message("❌ Apenas administradores podem modificar as configurações.", ephemeral=True)
            
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO guild_configs (guild_id) VALUES (?)", (interaction.guild.id,))
            cursor.execute(f"UPDATE guild_configs SET {field} = ? WHERE guild_id = ?", (value, interaction.guild.id))
        
        await interaction.response.send_message(f"✅ **{label_msg}** atualizado com sucesso!", ephemeral=True)

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="📌 Canal de Scrims", channel_types=[discord.ChannelType.text])
    async def set_scrims(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self.save_field(interaction, "scrims_channel", select.values[0].id, "Canal de Scrims")

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="📌 Canal de Free Agents", channel_types=[discord.ChannelType.text])
    async def set_freeagents(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self.save_field(interaction, "freeagents_channel", select.values[0].id, "Canal de Free Agents")

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="📌 Canal de Contratos", channel_types=[discord.ChannelType.text])
    async def set_contracts(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self.save_field(interaction, "contracts_channel", select.values[0].id, "Canal de Contratos")

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="📌 Canal de Demissões", channel_types=[discord.ChannelType.text])
    async def set_dismissals(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self.save_field(interaction, "dismissals_channel", select.values[0].id, "Canal de Demissões")

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="📌 Canal de Logs Administrativos", channel_types=[discord.ChannelType.text])
    async def set_logs(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self.save_field(interaction, "logs_channel", select.values[0].id, "Canal de Logs")

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="📌 Categoria de Tickets", channel_types=[discord.ChannelType.category])
    async def set_ticket_category(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await self.save_field(interaction, "ticket_category", select.values[0].id, "Categoria dos Tickets")

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="🛡️ Cargo Administrativo Autorizado")
    async def set_admin_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        await self.save_field(interaction, "admin_role", select.values[0].id, "Cargo Administrativo")

class ConfigCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="configurar", description="Painel principal de configuração da liga.")
    async def configurar(self, interaction: discord.Interaction):
        if not await check_admin_permission(interaction):
            return await interaction.response.send_message("❌ Apenas administradores do servidor podem utilizar este painel.", ephemeral=True)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM guild_configs WHERE guild_id = ?", (interaction.guild.id,))
            cfg = cursor.fetchone()

        embed = discord.Embed(
            title="⚙️ Painel de Configuração da Liga",
            description="Selecione as opções nos menus interativos abaixo para definir a infraestrutura do seu servidor.",
            color=discord.Color.blue()
        )

        def fmt(cid, is_role=False, is_cat=False):
            if not cid:
                return "`Não configurado`"
            if is_role:
                r = interaction.guild.get_role(cid)
                return r.mention if r else "`Removido`"
            elif is_cat:
                c = interaction.guild.get_channel(cid)
                return f"📁 **{c.name}**" if c else "`Removido`"
            else:
                c = interaction.guild.get_channel(cid)
                return c.mention if c else "`Removido`"

        if cfg:
            embed.add_field(name="⚔️ Scrims", value=fmt(cfg["scrims_channel"]), inline=True)
            embed.add_field(name="🏃 Free Agents", value=fmt(cfg["freeagents_channel"]), inline=True)
            embed.add_field(name="📜 Contratos", value=fmt(cfg["contracts_channel"]), inline=True)
            embed.add_field(name="❌ Demissões", value=fmt(cfg["dismissals_channel"]), inline=True)
            embed.add_field(name="📋 Logs", value=fmt(cfg["logs_channel"]), inline=True)
            embed.add_field(name="🎫 Categoria Ticket", value=fmt(cfg["ticket_category"], is_cat=True), inline=True)
            embed.add_field(name="🛡️ Cargo Admin", value=fmt(cfg["admin_role"], is_role=True), inline=True)
        else:
            embed.add_field(name="Status", value="Servidor sem configurações salvas. Utilize os menus para iniciar.", inline=False)

        await interaction.response.send_message(embed=embed, view=ConfigSelectView(), ephemeral=True)

async def setup(bot):
    await bot.add_cog(ConfigCog(bot))
