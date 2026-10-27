import discord
from discord.ext import commands
import aiohttp
import re
import asyncio  # Necessário para rodar as consultas ao mesmo tempo

intents = discord.Intents.default()
intents.message_content = True  

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Bot verificação ultra rápida online: {bot.user}")

async def checar_codigo_individual(session, codigo):
    """Função secundária que consulta um único código na API do Discord"""
    url_api = f"https://discord.com{codigo}?with_application=false&with_subscription_plan=true"
    try:
        async with session.get(url_api, timeout=4) as resposta:
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
                return codigo, "⏳ **Erro: Muitas requisições (Rate Limit)**"
            else:
                return codigo, "⚠️ **Erro na API do Discord**"
    except asyncio.TimeoutError:
        return codigo, "⏳ **Erro: Tempo limite esgotado**"

@bot.command()
async def verificar(ctx, *, texto: str):
    """Comando principal para verificar múltiplos links em paralelo (Ultra Rápido)"""
    
    padrao = r"(?:discord\.gift/|discord\.com/gifts/|discordapp\.com/gifts/)?([a-zA-Z0-9]{16,24})"
    codigos = re.findall(padrao, texto)
    
    if not codigos:
        await ctx.send("❌ Nenhum código ou link de Nitro válido foi encontrado na sua mensagem.")
        return
    
    # Remove duplicados e limita a 10 para evitar ban de IP do bot (Rate Limit do Discord)
    codigos = list(set(codigos))[:10]

    status_msg = await ctx.send(f"⚡ Encontrei {len(codigos)} código(s). Verificando em paralelo...")
    
    embed = discord.Embed(title="🎁 Resultado da Checagem Ultra Rápida", color=discord.Color.blurple())
    
    # Executa todas as consultas de forma simultânea
    async with aiohttp.ClientSession() as session:
        tarefas = [checar_codigo_individual(session, cod) for cod in codigos]
        resultados = await asyncio.gather(*tarefas)

    # Adiciona os resultados que vieram todos juntos no Embed
    for codigo, resultado_texto in resultados:
        embed.add_field(name=f"Code: {codigo}", value=resultado_texto, inline=False)

    # Apaga a mensagem temporária e envia o resultado final
    await status_msg.delete()
    await ctx.send(embed=embed)

# Coloque o Token do seu bot entre as aspas abaixo
bot.run('COLOQUE_SEU_TOKEN_AQUI')
