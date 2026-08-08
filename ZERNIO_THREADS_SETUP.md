# Zernio setup for Threads

Instagram publishing remains on Buffer. Only Threads uses Zernio.

1. Sign in at <https://zernio.com/>.
2. Connect each Threads profile in the Zernio dashboard. Threads profiles must
   be backed by an Instagram Business or Creator account.
3. Create an API key under **Dashboard → API Keys**.
4. Copy the Zernio account ID for each connected Threads profile.
5. Add these GitHub Actions repository secrets:

   - `ZERNIO_API_KEY`: the API key
   - `ZERNIO_THREADS_ACCOUNT_IDS`: comma-separated account IDs, primary first
   - `ZERNIO_THREADS_PRIMARY_ACCOUNT_ID`: primary account ID (optional when it
     is already first in the list)

The workflow schedules today's pending posts FIFO inside the next hour. The
secondary account is scheduled two minutes after the primary. Near Maldives
midnight, it publishes immediately rather than spilling posts into the next
day.
