# notify_bot

Telegram reminder bot that sends scheduled notifications – including **circle video notes** (video messages in a bubble) as well as text, photos, videos, voice, audio, and documents.

The bot lets you capture a piece of content once, attach a schedule (one-time or recurring), and then delivers it back to you on time.

---

## Features

### Multi-format alerts

When creating an alert you can choose what will be sent back:

- Text message
- Photo (with optional caption)
- Video
- Voice message
- Audio file
- Document
- **Video note (circle video)** – ideal for short, visual reminders

The content is stored in the database with its original Telegram `file_id`, so sending alerts is fast and doesn’t re-upload media.

### One-time and recurring schedules

For every alert you configure:

- One-shot reminder at a specific date and time
- Recurring schedules:
  - Daily
  - Weekly
  - Monthly
  - Custom **CRON** expression (`m h dom mon dow`)

Internally the bot uses APScheduler to trigger alerts and supports:

- Timezone-aware scheduling per alert
- Automatic rescheduling after restart (alerts are rebuilt from the DB on startup)

### Smart time parsing

The bot parses human-friendly date/time phrases (RU / EN) into concrete datetimes and timezones, including things like:

- “сегодня в 18:30”, “завтра в 9”, “понедельник в 07:45”
- “today at 18:30”, “tomorrow at 9”, “Friday 07:45”
- Explicit dates like `2025-08-16 09:30` or `16.08.2025 09:30`
- Timezones like `Europe/Moscow`, `Europe/London`, or `UTC+3`

There’s also an AI-based interval parser that converts natural language like:

> “каждый будний день в 9 и 18” / “every weekday at 9:00 and 18:00”

into a structured plan and one or more CRON expressions. This is powered by a DeepSeek-compatible API key configured via environment variables.

### Profiles and localization

- RU / EN localization for all system texts
- Per-user profile stored in the DB:
  - Telegram user id / username
  - Preferred language
  - Basic flags (premium, bot)
  - Balance placeholder for XTR (Telegram Stars) if you extend billing later
- Main flows are driven by a reply keyboard:
  - “Create alert”
  - “My alerts”
  - “Profile”
  - Back / Delete / Cancel / Skip actions

### Alert management

From the chat UI you can:

- Create a new alert (title → content → schedule)
- List active alerts with a compact summary (title, next run, type, timezone)
- Open alert details
- Delete an alert

Each alert record tracks:

- Owner (Telegram user id)
- Content type and JSON payload
- Schedule type (`one` vs `cron`)
- Next run info and last run timestamp
- Timezone and enabled flag

---

## Tech stack & architecture

**Core stack**

- Python **3.11**
- [aiogram v3](https://docs.aiogram.dev/) – async Telegram bot framework
- [APScheduler](https://apscheduler.readthedocs.io/) – job scheduler for alerts
- [SQLAlchemy (async)](https://docs.sqlalchemy.org/) – SQLite database access
- [Redis](https://redis.io/) – FSM storage (with automatic in-memory fallback)
- [loguru](https://github.com/Delgan/loguru) – structured logging
- [python-dotenv](https://github.com/theskumar/python-dotenv) – `.env` loading
- [OpenAI Python client](https://github.com/openai/openai-python) – used against a DeepSeek-compatible API for interval parsing

**Key modules**

- `main.py` – entrypoint:
  - Loads settings from ENV / `.env`
  - Spins up DB, FSM storage, logging, and APScheduler
  - Registers bot commands and routers
  - Rebuilds alert schedule from DB and starts polling
- `handlers.py` – Telegram handlers:
  - `/start`, `/help`, `/profile`
  - “Create alert” / “My alerts” keyboards
  - Alert creation FSM (title → content → schedule → cron)
- `db.py` – async SQLAlchemy models + `Database` wrapper:
  - `User` – Telegram profile
  - `Alert` – scheduled alert with content and schedule config
- `alerts.py` – alert scheduler:
  - APScheduler integration
  - `AlertScheduler` class for scheduling and executing alert jobs
  - Sends stored content (including `video_note`) back to the user
- `time_parse.py` – natural language date/time parsing (RU/EN)
- `ai_interval.py` – AI interval parser → normalized plan → CRON lines
- `fsm.py` – FSM storage factory (Redis or memory) + alert creation state machine
- `text.py` – phrase catalog for RU/EN texts and keyboards
- `setup_log.py` – loguru setup + optional Telegram alerts sink

**Data store**

- Default DB: SQLite (`sqlite+aiosqlite:///./data/app.db`) with:
  - WAL mode
  - Foreign keys enabled
- DB and logs live under `./data` and `./logs` (mounted in Docker)

---

## Getting started

### 1. Prerequisites

- Python **3.11+**
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- `redis` instance (local or via Docker) – optional but recommended
- (Optional) DeepSeek-compatible API key if you want AI interval parsing

### 2. Clone the repo

```bash
git clone https://github.com/mchomak/notify_bot.git
cd notify_bot
