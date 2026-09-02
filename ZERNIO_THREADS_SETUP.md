# Threads publishing (Buffer)

Instagram and Threads both publish through **Buffer** again.

Zernio was tried briefly but is no longer used (account paused / billing).

## GitHub secrets

- `BUFFER_ACCESS_TOKEN`
- `BUFFER_THREADS_CHANNEL_ID` — comma-separated Buffer Threads channel ids;
  **news.world.tech first**, then `naskhu`

Optional:

- `BUFFER_THREADS_PRIMARY_CHANNEL_ID` — defaults to news.world.tech channel
  `6a64c576e2638b94d7d25d01`

## Find channel ids

```bash
export BUFFER_ACCESS_TOKEN=...
python3 list_buffer_channels.py
```

## Workflow

**Publish to Threads** (`publish-threads.yml`):

- Runs after Generate V2 and every 30 minutes
- Up to **20 posts per run** (gentle on Buffer API limits)
- Rolling **250 / 24h** cap (Buffer Threads limit)
- Primary **news.world.tech** first, **naskhu** ~2 minutes later

Zernio secrets (`ZERNIO_*`) are no longer required and can be removed from
GitHub when convenient.
