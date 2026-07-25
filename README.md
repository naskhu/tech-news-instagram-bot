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
- Publishes generated posts to Instagram through **Buffer** (recommended), or Meta Graph API / local worker as fallbacks
- Records published files in `instagram-posted.json` (Actions) or `.local-instagram-posted.json` (local worker)

## Run it

1. Open **Actions** in this repository.
2. Open **Generate Tech News Post V2**.
3. Select **Run workflow**.
4. After the run, open `output/` to find the PNG, caption and metadata committed on `main`.

The generation workflow also runs automatically about every **20 minutes** (Generate V2) and pushes new files to git.

## Publishing options

### A) Buffer (recommended)

Uses GitHub Actions + Buffer so Instagram/Threads see normal Buffer publishing (lower automation risk than unofficial local login).

Secrets required:

- `BUFFER_ACCESS_TOKEN`
- `BUFFER_CHANNEL_ID` (Instagram channel in Buffer)
- `BUFFER_THREADS_CHANNEL_ID` (Threads channel id(s) in Buffer — comma-separated; **news.world.tech first**, then `naskhu`)

Requires an active Buffer plan that can post to Instagram and/or Threads. Connect Threads in Buffer first (Threads must be linked to Instagram and public).

To find the Threads channel id:

```bash
export BUFFER_ACCESS_TOKEN=...
python3 list_buffer_channels.py
```

### B) Meta Graph API

Only if Meta Content Publishing is available for your app.

Secrets required:

- `INSTAGRAM_IG_USER_ID`
- `INSTAGRAM_ACCESS_TOKEN`

### C) Local Instagram worker (last resort)

See **[LOCAL_INSTAGRAM_WORKER.md](LOCAL_INSTAGRAM_WORKER.md)**. Instagram often flags unofficial API posting — keep the Mac schedule off unless Buffer/Meta are unavailable.

### Publishing behavior (Actions)

**Publish to Instagram** runs after Generate and on a backup schedule (~every 30 minutes), using public git image URLs for Buffer. If Buffer secrets are missing, the job skips cleanly and generation continues.

Limits (Maldives local time, `Indian/Maldives`):

- **Calendar-day cap:** at most **50** posts between local midnight and next midnight
- **Start hour:** automatic posting starts at **08:00** local (configurable via `POSTING_START_HOUR`)
- **Cool-down:** publishing stays paused until `PUBLISH_RESUME_DATE` (currently `2026-07-26`) while Instagram action-blocks cool down
- **Fresh daily queue:** generate workflows delete previous `output/YYYY-MM-DD/` folders after local midnight so the next day builds new images

Keep the local Mac `instagrapi` worker **off** while Instagram shows “Try Again Later”.

### Threads publishing (Actions)

**Publish to Threads** is a separate workflow (`publish-threads.yml`) that posts the same `output/` images through Buffer’s Threads channel. It does **not** use the Instagram cool-down.

- **Rolling 24h soft cap:** at most **240** posts (under Buffer’s Threads hard limit of 250)
- **Generate:** every **20 minutes** (V2) creates up to **2** new posts when fresh news exists
- **Threads check:** every **20 minutes** (and right after Generate) enqueues up to **4** posts
- **Primary release:** same story posts to **news.world.tech** first, then **naskhu** (~2 minutes later)
- **Random Buffer schedule:** each post gets a random `dueAt` inside the next **20 minutes** (`customScheduled`), so they appear in Buffer’s queue and go out at staggered times
- **Captions:** trimmed to Threads’ **500** character limit with a single topic (`#TechNews`)
- **State:** `threads-posted.json` (independent from `instagram-posted.json`)

## Customize

Edit `config.json` to change the brand name, Instagram handle, RSS sources, posts per run, summary length and hashtags.

## Output

Each generated story produces:

- `output/YYYY-MM-DD/slug.png` — 1080×1080 Instagram image
- `output/YYYY-MM-DD/slug.txt` — ready-to-copy caption
- `output/YYYY-MM-DD/slug.json` — source metadata

## Important

Automatic publishing uses files committed to this public repository so Buffer can download each image from a public URL. Review facts, wording, source attribution, and image rights before publishing. Sponsored or compensated content may require Instagram's paid-partnership disclosure.
