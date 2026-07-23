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
2. Open **Generate Tech News Post V2**.
3. Select **Run workflow**.
4. After the run, open `output/` to find the PNG, caption and metadata committed on `main`.

The generation workflow also runs automatically four times per day and pushes new files to git.

## Enable automatic Instagram publishing through git

Publishing does **not** upload files from the Actions runner disk. Meta must download each image from a public git URL (`raw.githubusercontent.com`), so the flow is:

1. **Generate Tech News Post V2** creates `output/...png` + `.txt` caption and commits them to `main`.
2. **Publish to Instagram** starts after that successful run (or on schedule / manual dispatch).
3. It reads the oldest unpublished PNG + matching caption from the git checkout.
4. It tells Meta to fetch the image from the public raw GitHub URL and publish with that caption.
5. It commits `instagram-posted.json` back to git so the same post is never sent twice.

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

1. Starts after a successful **Generate Tech News Post V2** (or **Generate Tech News Posts**) workflow.
2. Syncs the latest `main` commit that contains generated files under `output/`.
3. Finds the oldest generated PNG that is not listed in `instagram-posted.json`.
4. Uses the matching `.txt` file as its Instagram caption.
5. Waits until the git-hosted image URL is publicly reachable, then publishes through Meta's `/media` and `/media_publish` endpoints.
6. Commits the resulting Instagram media ID to `instagram-posted.json`.

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
