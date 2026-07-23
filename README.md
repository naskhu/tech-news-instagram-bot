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
- Publishes generated posts to Instagram through Buffer using git-hosted image URLs
- Records published files in `instagram-posted.json` to prevent duplicate posts

## Run it

1. Open **Actions** in this repository.
2. Open **Generate Tech News Post V2**.
3. Select **Run workflow**.
4. After the run, open `output/` to find the PNG, caption and metadata committed on `main`.

The generation workflow also runs automatically four times per day and pushes new files to git.

## Enable automatic Instagram publishing through git + Buffer

Publishing does **not** upload files from the Actions runner disk. Buffer must download each image from a public git URL (`raw.githubusercontent.com`), so the flow is:

1. **Generate Tech News Post V2** creates `output/...png` + `.txt` caption and commits them to `main`.
2. **Publish to Instagram** starts after that successful run (or on schedule / manual dispatch).
3. It reads the oldest unpublished PNG + matching caption from the git checkout.
4. It tells Buffer to fetch the image from the public raw GitHub URL and share it now to the connected Instagram channel.
5. It commits `instagram-posted.json` back to git so the same post is never sent twice.

### Account requirements

- A Buffer account with Instagram connected (Business or Creator)
- A Buffer API key from **Buffer → Settings → API**
- The Buffer channel ID for the Instagram account (from the Buffer channels API / dashboard)

### GitHub repository secrets

Open **Settings → Secrets and variables → Actions → New repository secret**, then add:

- `BUFFER_ACCESS_TOKEN` — Buffer API key / bearer token; never commit this value into the repository
- `BUFFER_CHANNEL_ID` — Buffer channel ID for the Instagram account (for example `news.world.tech`)

### Publishing behavior

The **Publish to Instagram** workflow runs fully automatically:

1. After every successful **Generate Tech News Post V2** run, it drains the unpublished queue.
2. It spreads those posts randomly across about **one hour** (random start delay + random gaps).
3. A backup timer runs **every 10 minutes** and continues draining anything still queued.
4. Each image uses its matching `.txt` caption and a public git image URL for Buffer.
5. Progress is saved to `instagram-posted.json` after each successful post.

So new daily news is queued briefly, then automatically posted within about an hour — not left sitting for days. Manual runs still work (`max_posts=all` drains within an hour).

## Customize

Edit `config.json` to change the brand name, Instagram handle, RSS sources, posts per run, summary length and hashtags.

## Output

Each generated story produces:

- `output/YYYY-MM-DD/slug.png` — 1080×1080 Instagram image
- `output/YYYY-MM-DD/slug.txt` — ready-to-copy caption
- `output/YYYY-MM-DD/slug.json` — source metadata

## Important

Automatic publishing uses files committed to this public repository so Buffer can download each image from a public URL. Review facts, wording, source attribution, and image rights before publishing. Sponsored or compensated content may require Instagram's paid-partnership disclosure.
