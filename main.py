import requests
import re

# Expressão regular para capturar links de gift
GIFT_REGEX = re.compile(r"(?:discord\.gift/|discord\.com/gifts/|discordapp\.com/gifts/)([a-zA-Z0-9]{16,24})")

@bot.event
async def on_message(message):
    # Ignora mensagens do próprio bot para evitar loops
    if message.author == bot.user:
        return

    # Procura por padrões de gift link na mensagem
    match = GIFT_REGEX.search(message.content)
    
    if match:
        code = match.group(1)
        
        # Consulta imediata à API oficial do Discord
        url = f"https://discord.com{code}"
        response = requests.get(url)

        if response.status_code == 200:
            data = response.json()
            
            # Verifica se já foi usado
            if data.get("uses", 0) >= data.get("max_uses", 1):
                await message.reply(f"⚠️ **CÓDIGO JÁ RESGATADO**\nO código `{code}` é real, mas já foi pego por outra pessoa.")
            else:
                sku_name = data.get("store_listing", {}).get("sku", {}).get("name", "Nitro")
                await message.reply(f"🎉 **CÓDIGO VÁLIDO E DISPONÍVEL!**\n🎁 Tipo: **{sku_name}**\n👉 Resgate rápido!")
                
        elif response.status_code == 404:
            await message.reply(f"❌ **CÓDIGO INVÁLIDO**\nO código `{code}` não existe ou é falso.")
            
        elif response.status_code == 429:
            await message.reply("🛑 **RATE LIMIT**\nO Discord bloqueou temporariamente as checagens por excesso de tentativas. Tente de novo em alguns minutos.")
            
        else:
            await message.reply(f"❓ **ERRO DESCONHECIDO**\nA API respondeu com status {response.status_code}.")

    # IMPORTANTE: Mantém os outros comandos normais do seu bot funcionando
    await bot.process_commands(message)
bot.run(DISCORD_TOKEN)
