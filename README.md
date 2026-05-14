# QuantX All-in-One

Single Render deployment combining:
- 🤖 Telegram Bot (polling mode)
- 🎬 YouTube API (/api/search, /api/download)
- 🌐 Song search website (/)

## Deploy on Render

1. Push this folder to a new GitHub repo
2. Go to render.com → New → Web Service
3. Connect repo, settings:
   - Runtime: Python
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --timeout 120 --workers 1 --threads 4`
4. Add environment variable:
   - `TELEGRAM_BOT_TOKEN` = your bot token
5. Deploy

## Important after deploy

Since bot runs in polling mode, make sure to **delete any existing webhook** first:
```
https://api.telegram.org/bot<TOKEN>/deleteWebhook
```

## Endpoints
- `/` — Website
- `/api/search?q=query` — YouTube search
- `/api/download?id=VIDEO_ID` — YouTube audio download
- `/health` — Health check
