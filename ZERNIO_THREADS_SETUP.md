# Zernio setup for Threads

Instagram publishing remains on Buffer. Only Threads uses Zernio.

Each Threads profile can use its **own** Zernio API key (for example
`news.world.tech` and `naskhu` on separate Zernio accounts).

1. Sign in at <https://zernio.com/> for each Zernio account.
2. Connect the Threads profile in that dashboard. Threads profiles must be
   backed by an Instagram Business or Creator account.
3. Create an API key under **Dashboard → API Keys**.
4. Copy the Zernio account ID for the connected Threads profile.
5. Add these GitHub Actions repository secrets:

   - `ZERNIO_THREADS_ACCOUNT_IDS`: comma-separated account IDs, primary first
     (example: `news_world_tech_id,naskhu_id`)
   - `ZERNIO_THREADS_PRIMARY_ACCOUNT_ID`: primary account ID (`news.world.tech`)
   - `ZERNIO_NEWS_WORLD_TECH_API_KEY`: API key for the `news.world.tech` Zernio
     account
   - `ZERNIO_API_KEY`: API key for the `naskhu` Zernio account
   - Optional override: `ZERNIO_THREADS_API_KEYS` as comma-separated keys in the
     same order as `ZERNIO_THREADS_ACCOUNT_IDS` (skips the two named keys above)

The workflow schedules up to **20 posts per run** (under Zernio's 25/hour
account limit), FIFO inside the next hour, and runs about every 30 minutes so
leftovers keep draining. The secondary account is scheduled two minutes after
the primary. Near Maldives midnight, it publishes immediately rather than
spilling posts into the next day.
