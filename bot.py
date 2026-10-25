import discord
from discord import app_commands

# 権限の設定
intents = discord.Intents.default()
client = discord.Client(intents=intents)

# スラッシュコマンドを管理するツリーを作成
tree = app_commands.CommandTree(client)

# ボタンの処理を定義
class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # 期限なし

    @discord.ui.button(label="認証する", style=discord.ButtonStyle.green, custom_id="verify_button")
    async def verify_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ボタンが押されたら3秒以内にレスポンスを返す（ephemeral=True で本人だけに表示）
        await interaction.response.send_message("✅ 認証が完了しました！", ephemeral=True)

@client.event
async def on_ready():
    # Discordにスラッシュコマンドを同期（反映）させる
    await tree.sync()
    print(f'✨ ログインしました: {client.user}')
    print('スラッシュコマンドの同期が完了しました！')

# スラッシュコマンド `/verify-panel` の定義
@tree.command(name="verify-panel", description="認証パネルを送信します")
async def verify_panel(interaction: discord.Interaction):
    view = VerifyView()
    await interaction.response.send_message("下のボタンを押して認証を完了してください：", view=view)

# ボットを起動（トークンを入れてください）
import os
from dotenv import load_dotenv

load_dotenv()
client.run(os.getenv('DISCORD_TOKEN'))

