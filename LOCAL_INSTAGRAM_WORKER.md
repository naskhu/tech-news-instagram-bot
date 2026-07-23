# Local Instagram publishing worker

Use this when **Meta Content Publishing is unavailable** (“You don’t have access / feature isn't available yet”) and Buffer is not an option.

GitHub Actions still generates images + captions into `output/`. This worker runs on your Mac/PC/VPS, pulls git, and posts to Instagram.

> Important: this uses the unofficial `instagrapi` library. Instagram can request login verification or restrict the account, or break the private API. Prefer Meta Graph API later if/when it becomes available.

## How it works

1. GitHub Actions creates `output/YYYY-MM-DD/name.png` and `name.txt`.
2. The local worker runs `git pull --ff-only`.
3. It skips anything already in `instagram-posted.json` or `.local-instagram-posted.json`.
4. It uploads queued posts randomly across about one hour (default).
5. It records local progress in `.local-instagram-posted.json`.
6. It stores a reusable login session in `.instagram-session.json`.

Never commit `.env`, `.instagram-session.json`, or `.local-instagram-posted.json`.

## One-time setup (macOS)

```bash
cd ~/tech-news-instagram-bot
git pull
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-local-worker.txt
cp .env.example .env
chmod 600 .env
```

Edit `.env` with **only** the username (no password):

```dotenv
INSTAGRAM_USERNAME=news.world.tech
INSTAGRAM_VERIFICATION_CODE=
```

Store the password in **macOS Keychain** (hidden from other Mac users and not written to `.env`):

```bash
python local_instagram_worker.py --store-password
```

You will be prompted; typing is hidden. The password is saved in your login Keychain under service `com.news.world.tech.instagram-worker`.

## Test safely

```bash
source .venv/bin/activate
python local_instagram_worker.py --dry-run --max-posts 3
```

Publish queued posts (randomly within ~55 minutes):

```bash
python local_instagram_worker.py
```

Publish only one:

```bash
python local_instagram_worker.py --max-posts 1 --drain-within-minutes 0
```

If Instagram asks for approval, open the Instagram app, approve the login, then run again.

## Automatic schedule (macOS launchd)

Create `~/Library/LaunchAgents/com.news.world.tech.instagram-worker.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.news.world.tech.instagram-worker</string>
  <key>WorkingDirectory</key>
  <string>/Users/YOUR_USER/tech-news-instagram-bot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOUR_USER/tech-news-instagram-bot/.venv/bin/python</string>
    <string>/Users/YOUR_USER/tech-news-instagram-bot/local_instagram_worker.py</string>
  </array>
  <key>StartInterval</key>
  <integer>1800</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/Users/YOUR_USER/tech-news-instagram-bot/worker.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/YOUR_USER/tech-news-instagram-bot/worker.log</string>
</dict>
</plist>
```

Replace `YOUR_USER`, then:

```bash
launchctl load ~/Library/LaunchAgents/com.news.world.tech.instagram-worker.plist
```

This checks about every **30 minutes**, pulls new generated posts, and drains the queue.

## Automatic schedule (Linux / VPS cron)

On Linux there is no macOS Keychain. Prefer a root-only file and lock permissions:

```bash
install -m 600 /dev/null ~/.config/tech-news-instagram.env
# put INSTAGRAM_USERNAME=... and INSTAGRAM_PASSWORD=... in that file
chmod 600 ~/.config/tech-news-instagram.env
```

Then point the worker at it, or keep using a local `.env` with `chmod 600`.

```cron
*/30 * * * * cd /home/user/tech-news-instagram-bot && /home/user/tech-news-instagram-bot/.venv/bin/python local_instagram_worker.py >> worker.log 2>&1
```

## Security

- On macOS, store the password with `--store-password` (Keychain). Do **not** leave it in `.env`.
- `.env` is chmod `600` (only your user can read it) and is gitignored.
- Other Mac user accounts cannot read your Keychain or your `600` files.
- Enable Instagram 2FA.
- Do not put this password in GitHub Actions secrets.
- Do not paste the password in chat.
- Keep the Mac/VPS awake while the worker should run.
