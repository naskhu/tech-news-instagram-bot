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
- Publishes generated posts to an Instagram Professional account through Meta's supported Graph API
- Records published files in `instagram-posted.json` to prevent duplicate posts

## Run it

1. Open **Actions** in this repository.
2. Open **Generate Tech News Post**.
3. Select **Run workflow**.
4. After the run, open `output/` to find the PNG, caption and metadata.

The generation workflow also runs automatically four times per day.

## Enable automatic Instagram publishing

### Account requirements

- An Instagram **Business** or **Creator** account
- A Facebook Page linked to that Instagram account
- A Meta app with Instagram API access
- An access token with at least `instagram_basic`, `instagram_content_publish`, `pages_show_list`, and `pages_read_engagement`

### GitHub repository secrets

Open **Settings → Secrets and variables → Actions → New repository secret**, then add:

- `INSTAGRAM_IG_USER_ID` — the numeric Instagram Professional account ID
- `INSTAGRAM_ACCESS_TOKEN` — the Meta access token; never commit this value into the repository

Optional repository variable:

- `META_GRAPH_API_VERSION` — Graph API version, such as `v23.0`; update this when Meta retires an older version

### Publishing behavior

The **Publish to Instagram** workflow:

1. Starts after a successful **Generate Tech News Post** workflow.
2. Finds the oldest generated PNG that is not listed in `instagram-posted.json`.
3. Uses the matching `.txt` file as its Instagram caption.
4. Publishes one post through Meta's `/media` and `/media_publish` endpoints.
5. Commits the resulting Instagram media ID to `instagram-posted.json`.

A backup schedule also checks for an unpublished post at **09:17, 15:17, and 21:17 Maldives time**. You can manually publish from **Actions → Publish to Instagram → Run workflow** and choose how many queued posts to publish.

## Customize

Edit `config.json` to change the brand name, Instagram handle, RSS sources, posts per run, summary length and hashtags.

## Output

Each generated story produces:

- `output/YYYY-MM-DD/slug.png` — 1080×1080 Instagram image
- `output/YYYY-MM-DD/slug.txt` — ready-to-copy caption
- `output/YYYY-MM-DD/slug.json` — source metadata

## Important

Automatic publishing uses files committed to this public repository so Meta can download each image from a public URL. Review facts, wording, source attribution, and image rights before publishing. Sponsored or compensated content may require Instagram's paid-partnership disclosure.
