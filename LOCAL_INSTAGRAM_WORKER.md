# Local Instagram publishing worker

This option is for cases where Meta's publishing token is unavailable and you do not want to use Buffer. GitHub Actions continues generating the image and caption. A trusted always-on device pulls the repository and publishes the next unpublished post.

> Important: this worker uses the unofficial `instagrapi` library. Instagram can request verification, temporarily restrict the account, or change its private API without notice. The official Meta API remains the lowest-risk method.

## How it works

1. GitHub Actions creates `output/YYYY-MM-DD/name.png` and `name.txt`.
2. The local worker runs `git pull --ff-only`.
3. It finds the oldest image that has a matching caption and has not been posted locally.
4. It uploads one post.
5. It records the image path in `.local-instagram-posted.json`.
6. It stores a reusable login session in `.instagram-session.json`.

Both local files and `.env` are ignored by Git.

## Install on Windows, macOS, Linux, Raspberry Pi, or a VPS

Clone the repository and enter it:

```bash
git clone https://github.com/naskhu/tech-news-instagram-bot.git
cd tech-news-instagram-bot
```

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it.

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS/Linux/Raspberry Pi:

```bash
source .venv/bin/activate
```

Install the worker dependencies:

```bash
pip install -r requirements-local-worker.txt
```

Create the local secrets file:

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set a newly changed private Instagram password:

```dotenv
INSTAGRAM_USERNAME=news.world.tech
INSTAGRAM_PASSWORD=your_private_password
INSTAGRAM_VERIFICATION_CODE=
```

Never commit `.env`, `.instagram-session.json`, or `.local-instagram-posted.json`.

## Test safely

First verify queue detection without uploading:

```bash
python local_instagram_worker.py --dry-run
```

Then publish one post:

```bash
python local_instagram_worker.py
```

If Instagram requests approval, open the Instagram mobile app, approve the login, and run the command again.

## Schedule on Windows

Open **Task Scheduler** and create a basic task.

- Trigger: Daily, for example 09:00 Maldives time
- Program: full path to `.venv\Scripts\python.exe`
- Arguments: full path to `local_instagram_worker.py`
- Start in: repository folder

Example:

```text
Program:
C:\Bots\tech-news-instagram-bot\.venv\Scripts\python.exe

Arguments:
C:\Bots\tech-news-instagram-bot\local_instagram_worker.py

Start in:
C:\Bots\tech-news-instagram-bot
```

## Schedule on Linux, Raspberry Pi, or VPS

Run:

```bash
crontab -e
```

To publish every day at 09:00 Maldives time on a device configured to Maldives time:

```cron
0 9 * * * cd /home/user/tech-news-instagram-bot && /home/user/tech-news-instagram-bot/.venv/bin/python local_instagram_worker.py >> worker.log 2>&1
```

If the device uses UTC, 09:00 Maldives time is 04:00 UTC:

```cron
0 4 * * * cd /home/user/tech-news-instagram-bot && /home/user/tech-news-instagram-bot/.venv/bin/python local_instagram_worker.py >> worker.log 2>&1
```

## Android with Termux

Install Termux from F-Droid, then run:

```bash
pkg update
pkg install python git
termux-wake-lock
git clone https://github.com/naskhu/tech-news-instagram-bot.git
cd tech-news-instagram-bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-local-worker.txt
cp .env.example .env
nano .env
python local_instagram_worker.py --dry-run
```

Android may stop background processes to save battery. Disable battery optimization for Termux and keep the device connected to power.

## Useful options

Do not upload; only inspect the next queue item:

```bash
python local_instagram_worker.py --dry-run
```

Skip `git pull` and use current local files:

```bash
python local_instagram_worker.py --no-pull
```

## Security requirements

- Change the password previously shared in chat before using this worker.
- Enable Instagram two-factor authentication.
- Use a dedicated device account with restricted access.
- Do not store the password in GitHub Actions secrets for this unofficial method.
- Start with one post per day and review account activity regularly.
