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
- Publishes generated posts to Instagram through Meta Graph API using git-hosted image URLs
- Records published files in `instagram-posted.json` to prevent duplicate posts

## Run it

1. Open **Actions** in this repository.
2. Open **Generate Tech News Post V2**.
3. Select **Run workflow**.
4. After the run, open `output/` to find the PNG, caption and metadata committed on `main`.

The generation workflow also runs automatically four times per day and pushes new files to git.

## Enable automatic Instagram publishing through git + Meta

Publishing does **not** upload files from the Actions runner disk. Meta must download each image from a public git URL (`raw.githubusercontent.com`), so the flow is:

1. **Generate Tech News Post V2** creates `output/...png` + `.txt` caption and commits them to `main`.
2. **Publish to Instagram** starts after that successful run (or on schedule / manual dispatch).
3. It reads unpublished PNG + matching caption files from the git checkout.
4. It creates an Instagram media container from the public raw GitHub image URL, then publishes it.
5. It commits `instagram-posted.json` back to git so the same post is never sent twice.

### Account requirements

- An Instagram **Business** or **Creator** account (`news.world.tech`)
- A Facebook Page linked to that Instagram account
- A Meta app with Instagram content publishing permissions
- A long-lived access token with publishing permission

### GitHub repository secrets

Open **Settings → Secrets and variables → Actions → New repository secret**, then add:

- `INSTAGRAM_IG_USER_ID` — numeric Instagram Professional account ID
- `INSTAGRAM_ACCESS_TOKEN` — Meta long-lived access token; never commit this value

Optional repository variable:

- `META_GRAPH_API_VERSION` — Graph API version, such as `v23.0`

### How to get the Meta values

1. Convert `news.world.tech` to a Professional account and link it to a Facebook Page.
2. Create a Meta app at [developers.facebook.com](https://developers.facebook.com/).
3. Add Instagram Graph / content publishing permissions.
4. Generate a Page/User token, then exchange it for a long-lived token.
5. Find the Instagram Business Account ID (IG user id) from the linked Page.
6. Put both values into GitHub Actions secrets.

If Meta setup is blocked, see `LOCAL_INSTAGRAM_WORKER.md` for a local-machine fallback (unofficial API; higher risk).

### Publishing behavior

The **Publish to Instagram** workflow runs fully automatically:

1. After every successful **Generate Tech News Post V2** run, it drains the unpublished queue.
2. It spreads those posts randomly across about **one hour** (random start delay + random gaps).
3. A backup timer runs about **every 30 minutes** and drains leftovers for up to ~50 minutes.
4. Each image uses its matching `.txt` caption and a public git image URL for Meta.
5. Progress is saved to `instagram-posted.json` after each successful post.
6. If Instagram's API publish limit is hit, the run stops cleanly and retries automatically later.

So new daily news is queued briefly, then automatically posted within about an hour — not left sitting for days. Manual runs still work (`max_posts=all` drains within an hour).

## Customize

Edit `config.json` to change the brand name, Instagram handle, RSS sources, posts per run, summary length and hashtags.

## Output

Each generated story produces:

- `output/YYYY-MM-DD/slug.png` — 1080×1080 Instagram image
- `output/YYYY-MM-DD/slug.txt` — ready-to-copy caption
- `output/YYYY-MM-DD/slug.json` — source metadata

## Important

Automatic publishing uses files committed to this public repository so Meta can download each image from a public URL. Review facts, wording, source attribution, and image rights before publishing. Sponsored or compensated content may require Instagram's paid-partnership disclosure.
