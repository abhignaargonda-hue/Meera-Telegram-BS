# Skinstinct content bot

A Telegram bot, hosted on Vercel, that turns Meera Pillai's voice notes and text notes into LinkedIn post drafts in her voice. It uses Google Gemini for transcription and drafting.

1. Meera sends the bot a voice note or a text message.
2. Telegram forwards the message to this app on Vercel (a webhook).
3. Gemini transcribes voice notes. The bot sends the transcript back so she can check it.
4. Gemini writes one LinkedIn post, using `meera_voice.txt` as its instructions.
5. The bot replies in the same chat with the draft.

The bot **never posts to LinkedIn**. Meera reviews and publishes drafts herself. Only the Telegram user in `ALLOWED_TELEGRAM_USER_ID` gets replies. Messages from anyone else are ignored.

## Files

| File | What it does |
| --- | --- |
| `app.py` | The Vercel entrypoint (FastAPI). Receives Telegram messages at `/api/telegram` and replies. `/` is a health check. |
| `drafting.py` | The Gemini calls: transcription and drafting, including the drafting instructions. |
| `meera_voice.txt` | The voice guide. Edit it to change the voice, then redeploy. |
| `scripts/set_webhook.py` | One-off script that tells Telegram where the app lives. |
| `vercel.json` | Allows each request up to 300 seconds (a draft usually takes 30 to 60). |
| `requirements.txt`, `.python-version` | Dependencies and Python 3.12 for Vercel. |
| `.env.example` | The environment variables the app needs. |

## Environment variables

| Name | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Full token from @BotFather, like `123456789:AAG...` |
| `GEMINI_API_KEY` | From https://aistudio.google.com/apikey |
| `ALLOWED_TELEGRAM_USER_ID` | Numeric Telegram ID of the one person allowed to use the bot (@userinfobot tells you yours) |
| `TELEGRAM_WEBHOOK_SECRET` | A long random string. Telegram sends it with every message, so the app knows the request is real. Generate one with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `GEMINI_DRAFT_MODEL` (optional) | Defaults to `gemini-pro-latest` |
| `GEMINI_TRANSCRIBE_MODEL` (optional) | Defaults to `gemini-flash-latest` |

## Deploy

### 1. Put the code on GitHub

Create an **empty private** repository on GitHub. Don't add a README or a .gitignore. Then, in this folder:

```bash
git remote add origin https://github.com/YOUR-NAME/skinstinct-content-bot.git
git push -u origin main
```

`.env` is in `.gitignore`, so your keys stay on your machine.

### 2. Import it into Vercel

1. Go to https://vercel.com/new, sign in with GitHub, and import the repository.
2. Leave the framework preset as detected (FastAPI or Other) and leave the build settings alone.
3. Open **Environment Variables** and add the four required variables above. Copy the values from your local `.env`.
4. Click **Deploy**.
5. When it finishes, open the production URL (for example `https://skinstinct-content-bot.vercel.app`). You should see `"status": "ok"`, with all four `..._set` values `true`.

### 3. Connect Telegram to the app

Run this once from this folder, using your Vercel URL:

```bash
python3 scripts/set_webhook.py https://skinstinct-content-bot.vercel.app
```

It should print `Webhook was set`. Send the bot a message in Telegram and the draft arrives within about a minute.

**Only one copy of the bot can receive messages at a time.** Once the webhook is set, don't run a local polling copy of the bot. It would delete the webhook.

## Making changes

- Push to `main` and Vercel redeploys automatically.
- If you change an environment variable in Vercel, redeploy (Deployments, then Redeploy on the latest one) for it to take effect.
- If you change `TELEGRAM_WEBHOOK_SECRET`, the bot token or the Vercel URL, run `scripts/set_webhook.py` again.

## Troubleshooting

- **No reply at all:** open the health check URL. Then run `scripts/set_webhook.py` again. It prints Telegram's last delivery error, if there is one. Also check that `ALLOWED_TELEGRAM_USER_ID` is your ID.
- **A reply saying what failed:** the message names the step (transcription, drafting or download) and the reason, for example a rejected Gemini key or a rate limit.
- **Details:** Vercel dashboard, then your project, then **Logs**.

## Reviewing drafts

Anything not stated in the note is marked `[VERIFY]` or left as a `[VERIFY: ...]` placeholder. Resolve every one before publishing. Gemini can still slip in a plausible detail that wasn't in the note, so read each draft against the original note.
