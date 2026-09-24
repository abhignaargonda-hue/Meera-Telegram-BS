# Skinstinct content bot

A Telegram bot, hosted on Vercel, that turns Meera Pillai's voice notes and text notes into scored LinkedIn post drafts in her voice. It uses Google Gemini and Google News.

## How a note flows

| Step | Who | What happens |
| --- | --- | --- |
| Trigger | Meera | Sends a voice note or text message to the bot in Telegram. |
| Input | Telegram + Gemini Flash | Telegram forwards it to this app. Voice notes are transcribed and the transcript is sent back. |
| Processing | Gemini Flash (triage) | Scores the note 0 to 10 for publishability. Below `TRIAGE_MIN_SCORE` (default 5), the bot replies with the reason and what's missing, and stops. It also suggests a Google News search. |
| Context | Google News | Searches recent headlines (India edition, last 30 days first) for a timely hook. |
| AI | Gemini Pro (draft) | Writes one post in Meera's voice (`meera_voice.txt`). It uses a headline as a hook only if one genuinely fits. |
| AI | Gemini Pro (scorecard) | Scores the draft on 7 fixed criteria and lists any claims the note doesn't support. |
| Output | Meera (review gate) | Reviews, edits, and publishes on LinkedIn herself. **The bot never posts.** |

Only the Telegram user in `ALLOWED_TELEGRAM_USER_ID` gets replies. Messages from anyone else are ignored.

**Forcing a low-scoring note:** reply to the note, or to its transcript, with `draft anyway`.

### What Meera receives for each note

1. The transcript (voice notes only).
2. The triage score, its reason, and the news search used.
3. The draft, as a message on its own so it's easy to copy.
4. A scorecard:
   - **Draft score** out of 10: the average of the 7 criteria below.
   - **7 criteria**, each scored 0 to 10 with a one-line reason. They're taken from the checklist in the voice guide:
     1. **Opening**: the first sentence is concrete (a number, a dated scene, or the reader's product), not a question.
     2. **Format**: 7 to 8 prose paragraphs, 450 to 600 words, with no bullets, emojis, hashtags, exclamation marks, greeting or sign-off.
     3. **Claim fencing**: at least one "I'm not saying X. I'm saying Y." move.
     4. **Evidence and accuracy**: a mechanism and a specific number for each claim, stated evidence strength, `[VERIFY]` on anything not in the note, and nothing invented.
     5. **Skinstinct honesty**: a cost, limit or mistake rather than a pitch, no named competitors, and blame on systems rather than people.
     6. **Voice and language**: British spelling, no hype or wellness words, and terms like "clean" only in quotes.
     7. **Closing**: tells the reader what to ask for and how; no question to the audience and no call to buy.
   - **"Not in the note, check these"**: any claim about Skinstinct or Meera that the note didn't contain and that isn't marked `[VERIFY]`.
   - **News hook sources**: when a news hook is used, the 2 most relevant articles as clickable headlines, with publisher and date, so Meera can cross-check them.

Word count, paragraph count, exclamation marks, hashtags, list lines and `[VERIFY]` markers are counted in code and given to the scorer, so the format score doesn't depend on the model counting. Scoring runs at temperature 0 on Gemini Pro. In testing, the same draft got the same score on repeated runs.

## Files

| File | What it does |
| --- | --- |
| `app.py` | The Vercel entrypoint (FastAPI). Receives Telegram messages at `/api/telegram` and replies. `/` is a health check. |
| `drafting.py` | The Gemini calls: transcription, triage, drafting and scoring, including all the prompts and the scoring criteria. |
| `news.py` | Google News search for the hook. |
| `meera_voice.txt` | The voice guide. Edit it to change the voice, then redeploy. |
| `scripts/set_webhook.py` | One-off script that tells Telegram where the app lives. |
| `vercel.json` | Allows each request up to 300 seconds (a note usually takes 60 to 90 seconds end to end). |
| `requirements.txt`, `.python-version` | Dependencies and Python 3.12 for Vercel. |
| `.env.example` | The environment variables the app needs. |

## Environment variables

| Name | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Full token from @BotFather, like `123456789:AAG...` |
| `GEMINI_API_KEY` | From https://aistudio.google.com/apikey |
| `ALLOWED_TELEGRAM_USER_ID` | Numeric Telegram ID of the one person allowed to use the bot (@userinfobot tells you yours) |
| `TELEGRAM_WEBHOOK_SECRET` | A long random string. Telegram sends it with every message, so the app knows the request is real. Generate one with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `TRIAGE_MIN_SCORE` (optional) | Notes below this triage score aren't drafted. Defaults to `5` |
| `GEMINI_DRAFT_MODEL` (optional) | Drafting model. Defaults to `gemini-pro-latest` |
| `GEMINI_SCORE_MODEL` (optional) | Scorecard model. Defaults to `gemini-pro-latest` |
| `GEMINI_FAST_MODEL` (optional) | Transcription and triage model. Defaults to `gemini-flash-latest` |

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

- Resolve every `[VERIFY]` before publishing.
- Treat the **"Not in the note, check these"** list seriously. Gemini sometimes adds plausible Skinstinct details, and the scorer flags the ones it can find.
- News hooks are chosen from **headlines only**. Open both sources and confirm the story says what the post claims.
- Google News links go through a Google redirect to the publisher's article.
- The Google News RSS feed is intended for personal feed reading. That fits one person reviewing headlines privately. If the bot is ever opened to more users or used commercially at scale, switch to a licensed news API.
