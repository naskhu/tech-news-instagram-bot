# Tech News Instagram Bot

A fully automated, no-API-key pipeline that collects technology news from RSS feeds, creates a concise extractive summary locally, and renders a post-ready 1080×1080 Instagram image plus caption.

## Features

- No Gemini, OpenAI, or other AI API key required
- Reads multiple public RSS/Atom feeds
- Removes HTML and ranks sentences locally using word-frequency scoring
- Avoids duplicate stories using `state.json`
- Generates a branded square PNG using Pillow
- Generates a matching Instagram caption and source link
- Runs automatically with GitHub Actions
- Commits generated posts to `output/`
- Uploads each run as a downloadable GitHub Actions artifact
- Publishes generated posts to Instagram through Meta Graph API when available, or via the local worker when Meta Content Publishing is unavailable
- Records published files in `instagram-posted.json` (Actions) or `.local-instagram-posted.json` (local worker)

## Run it

1. Open **Actions** in this repository.
2. Open **Generate Tech News Post V2**.
3. Select **Run workflow**.
4. After the run, open `output/` to find the PNG, caption and metadata committed on `main`.

The generation workflow also runs automatically four times per day and pushes new files to git.

## Publishing options

### A) Meta Graph API (preferred when available)

Use this only if Meta Content Publishing works for your app. If Meta shows **“You don’t have access / This feature isn't available to you yet”**, use option B.

Secrets required:

- `INSTAGRAM_IG_USER_ID`
- `INSTAGRAM_ACCESS_TOKEN`

### B) Local Instagram worker (recommended fallback now)

See **[LOCAL_INSTAGRAM_WORKER.md](LOCAL_INSTAGRAM_WORKER.md)**.

GitHub Actions keeps generating posts into git. On your Mac:

```bash
cd ~/tech-news-instagram-bot
source .venv/bin/activate
python local_instagram_worker.py --store-password   # once; saves to Keychain (not .env)
python local_instagram_worker.py
```

That pulls new files and posts queued images + captions to Instagram automatically (spread randomly within about an hour). Schedule it every 30 minutes with launchd/cron.

### Publishing behavior (Meta Actions path)

When Meta secrets are configured, **Publish to Instagram** runs automatically after Generate and on a backup schedule, using public git image URLs. If Meta secrets are missing, the Actions publish job skips cleanly and generation continues.

## Customize

Edit `config.json` to change the brand name, Instagram handle, RSS sources, posts per run, summary length and hashtags.

## Output

Each generated story produces:

- `output/YYYY-MM-DD/slug.png` — 1080×1080 Instagram image
- `output/YYYY-MM-DD/slug.txt` — ready-to-copy caption
- `output/YYYY-MM-DD/slug.json` — source metadata

## Important

Automatic publishing uses files committed to this public repository so Meta can download each image from a public URL. Review facts, wording, source attribution, and image rights before publishing. Sponsored or compensated content may require Instagram's paid-partnership disclosure.
