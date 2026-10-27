import discord
from discord.ext import commands
import aiohttp
import re
import asyncio

intents = discord.Intents.default()
intents.message_content = True  

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"⚡ BOT INSTANTÂNEO ONLINE: {bot.user}")

async def checar_codigo_individual(session, codigo):
    url_api = f"https://discord.com{codigo}?with_application=false&with_subscription_plan=true"
    
    try:
        # Timeout curto para responder rápido de qualquer jeito
        async with session.get(url_api, timeout=2) as resposta:
            if resposta.status == 200:
                dados = await resposta.json()
                foi_resgatado = dados.get("consumed", True)
                nome_plano = dados.get("subscription_plan", {}).get("name", "Nitro")
                
                if foi_resgatado:
                    return codigo, f"⚠️ **Já Resgatado** ({nome_plano})"
                return codigo, f"✅ **VÁLIDO! Disponível** ({nome_plano})"
                
            elif resposta.status == 404:
                return codigo, "❌ **Inválido / Falso**"
            elif resposta.status == 429:
                return codigo, "⏳ **IP Bloqueado temporariamente pelo Discord (Rate Limit)**"
            else:
                return codigo, f"⚠️ **Erro na API ({resposta.status})**"
    except Exception:
        return codigo, "⏳ **Sem resposta da API**"

@bot.command()
async def verificar(ctx, *, texto: str):
    # Procura e limpa os códigos enviados
    padrao = r"(?:discord\.gift/|discord\.com/gifts/|discordapp\.com/gifts/)?([a-zA-Z0-9]{16,24})"
    codigos = list(set(re.findall(padrao, texto)))[:10]
    
    if not codigos:
        await ctx.send("❌ Nenhum link de Nitro encontrado.")
        return

    # Consulta tudo de uma vez só no mesmo instante
    async with aiohttp.ClientSession() as session:
        tarefas = [checar_codigo_individual(session, cod) for cod in codigos]
        resultados = await asyncio.gather(*tarefas)

    # Cria e envia o resultado direto, sem enrolação
    embed = discord.Embed(title="⚡ Resultado Instantâneo", color=discord.Color.from_rgb(255, 115, 250))
    for codigo, resultado_texto in resultados:
        embed.add_field(name=f"Código: {codigo}", value=resultado_texto, inline=False)

    await ctx.send(embed=embed)

bot.run(DISCORD_TOKEN)
