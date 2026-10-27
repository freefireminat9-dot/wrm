import discord
from discord.ext import commands
import aiohttp
import re

intents = discord.Intents.default()
intents.message_content = True  

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Bot verificador em massa online como: {bot.user}")

@bot.command()
async def verificar(ctx, *, texto: str):
    """Comando para verificar múltiplos links de Nitro de uma vez"""
    
    # Expressão regular para achar TODOS os códigos de 16 a 24 caracteres alfanuméricos
    padrao = r"(?:discord\.gift/|discord\.com/gifts/|discordapp\.com/gifts/)?([a-zA-Z0-9]{16,24})"
    codigos = re.findall(padrao, texto)
    
    if not codigos:
        await ctx.send("❌ Nenhum código ou link de Nitro válido foi encontrado na sua mensagem.")
        return
    
    # Remove códigos duplicados enviados na mesma mensagem
    codigos = list(set(codigos))
    
    # Limite de segurança para não tomar bloqueio (Rate Limit) da API do Discord
    if len(codigos) > 10:
        await ctx.send("⚠️ Por segurança, envie no máximo **10 códigos** por vez para evitar que o bot seja bloqueado.")
        return

    status_msg = await ctx.send(f"🔎 Encontrei {len(codigos)} código(s). Verificando na API do Discord...")
    
    embed = discord.Embed(title="🎁 Resultado da Checagem em Massa", color=discord.Color.blurple())
    
    async with aiohttp.ClientSession() as session:
        for codigo in codigos:
            url_api = f"https://discord.com{codigo}?with_application=false&with_subscription_plan=true"
            
            async with session.get(url_api) as resposta:
                if resposta.status == 200:
                    dados = await resposta.json()
                    foi_resgatado = dados.get("consumed", True)
                    nome_plano = dados.get("subscription_plan", {}).get("name", "Nitro")
                    
                    if foi_resgatado:
                        embed.add_field(name=f"Code: {codigo}", value=f"⚠️ **Já Resgatado** ({nome_plano})", inline=False)
                    else:
                        embed.add_field(name=f"Code: {codigo}", value=f"✅ **VÁLIDO! Disponível** ({nome_plano})", inline=False)
                        
                elif resposta.status == 404:
                    embed.add_field(name=f"Code: {codigo}", value="❌ **Inválido / Falso**", inline=False)
                elif resposta.status == 429:
                    embed.add_field(name=f"Code: {codigo}", value="⏳ **Erro: Muitas requisições (Rate Limit)**", inline=False)
                else:
                    embed.add_field(name=f"Code: {codigo}", value="⚠️ **Erro interno da API**", inline=False)

    # Apaga a mensagem de "Aguarde" e envia o resultado final
    await status_msg.delete()
    await ctx.send(embed=embed)

# Coloque o Token do seu bot entre as aspas abaixo
bot.run('COLOQUE_SEU_TOKEN_AQUI')
