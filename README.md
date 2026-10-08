# Creatical video worker

Builds one vertical video (Gemini voice + free photos + animated captions) per job and sends it to Telegram.
Triggered by an n8n workflow through the GitHub `repository_dispatch` API (event type `creatical-video`).

Required repository secrets: `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
No secret is stored in the code.
