# Telegram alerts

1. In Telegram, message **@BotFather**, send `/newbot`, follow the prompts, copy the bot token.
2. Open a chat with your new bot and send it any message.
3. Get your chat id: open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and read
   `"chat":{"id": ...}` from the JSON.
4. In `.env` on the server:
   `MEV_ALERT_WEBHOOK=https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>`
5. Restart the agent: `docker compose -f docker-compose.app.yml restart agent`.

Slack (incoming webhook URL) and Discord (channel webhook URL) work the same way: paste the URL.
