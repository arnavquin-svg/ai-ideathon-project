# Radar + SquadUp

> Radar brings people in. SquadUp makes each person bring more.

Built for the **X AI Tech Ideathon — Problem Statement 2: AI for Event Reach and Registration.**

First-time student organisers don't have followers, ad budgets or a PR team, so reach stops at their own circle. Radar + SquadUp gets the first 100 registrations without any of that:

- **Radar** scores a directory of student clubs and community groups against your event, explains each match in one line, and drafts a personalised message to each group's admin in English, Hindi or Marathi. Nothing is sent automatically: every draft waits for the organiser to approve it, and only then does its tracked link unlock.
- **SquadUp** is a Telegram bot. People who tap a tracked link register in three taps, get seated in a balanced team of four (Developer, Designer, Domain expert, Presenter), receive a shareable team card and an invite link that gives their friends priority for their team's open seats, and get 7/2/1-day reminders with one-tap confirm or "I can't make it" (which frees the seat).
- The **dashboard** is where the organiser creates events, reviews ranked groups on a radar map, approves and copies outreach, and watches the funnel, sources and squads update live.

```
radar-squadup/
├── app/                  FastAPI backend
│   ├── main.py           REST API, CORS, attribution, stats, demo simulator
│   ├── radar.py          profile building, community ranking, message drafting
│   ├── squad.py          team balancer and team cards
│   ├── db.py             SQLite schema and connection helper (radar.db)
│   ├── schemas.py        request validation (Pydantic)
│   └── seed.py           synthetic community directory + demo event
├── bot/
│   ├── telegram_bot.py   SquadUp bot (python-telegram-bot, inline buttons, job-queue reminders)
│   └── i18n.py           English + Hindi strings and team-card renderer
├── web/
│   └── index.html        organiser dashboard (Tailwind, dark mode, no build step)
├── tests/test_api.py     end-to-end API tests
├── requirements.txt
└── .env.example
```

---

## Run it

You need **Python 3.10+**. The bot additionally needs a Telegram account.

### 1. Install

```bash
git clone <your-repo-url> radar-squadup
cd radar-squadup
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Start the API and dashboard

```bash
uvicorn app.main:app --reload
```

- Dashboard: <http://localhost:8000/>
- Interactive API docs: <http://localhost:8000/docs>

On first start the API creates `radar.db`, loads 32 synthetic community groups and one demo event (*BuildPune AI Hackathon*), so the dashboard has something to show immediately. Set `SEED_DEMO_EVENT=0` to skip the demo event.

The dashboard can also be opened as a plain file or hosted elsewhere (CORS is open by default). If it isn't served by the API, it looks for the API at `http://localhost:8000`; change that in the sidebar or with `?api=https://your-api` in the URL.

### 3. Try the whole flow without Telegram

1. **Radar** → *Run Radar and draft messages*. Groups appear on the radar map and in the ranked list.
2. **Outreach** → open a draft, switch its language if you like, edit it, then *Approve message*. The tracked link unlocks; *Copy message* gives you the text to send to that admin yourself.
3. **Squads** → *Simulate 12 sample sign-ups*. Synthetic participants arrive through your approved links (about 30% through teammate invites) and are seated by the balancer. They're flagged `is_synthetic` in the database.
4. **Overview** shows the funnel, sign-ups per source, skill mix and the latest registrations, refreshing every 5 seconds.

### 4. Connect the Telegram bot

1. In Telegram, message **@BotFather** → `/newbot` → pick a name and a username ending in `bot`. Copy the token.
2. Restart the API with your bot's username so tracked links point at it:

   ```bash
   export TELEGRAM_BOT_USERNAME=YourEventBot          # without the @
   uvicorn app.main:app --reload
   ```

3. In a second terminal (same virtualenv, repo root):

   ```bash
   export TELEGRAM_BOT_TOKEN=123456789:ABC...
   export TELEGRAM_BOT_USERNAME=YourEventBot
   export RADAR_API_URL=http://localhost:8000
   python -m bot.telegram_bot
   ```

4. Copy an approved link from the dashboard (it looks like `https://t.me/YourEventBot?start=o_1a2b3c4d`), open it on your phone and register. You'll see yourself appear in **Squads** and **Overview**.

Bot commands: `/start`, `/team`, `/invite`, `/confirm`, `/lang`, `/help`, and `/demo_reminder`, which sends the 1-day reminder in 10 seconds so you can show drop-off prevention on stage.

On Windows PowerShell use `$env:TELEGRAM_BOT_TOKEN="..."` instead of `export`. You can also copy `.env.example` to `.env` and load it with your shell or IDE.

### 5. Run the tests

```bash
pytest -q
```

---

## How it works

### Radar ranking

For each event Radar builds a profile from the form: topics (tags plus keywords found in the name and description), skill level (beginner-friendly, open, advanced), city, and the languages to draft in (English, Hindi, plus the city's regional language where supported).

Each community in the directory is scored with the formula from the blueprint:

```
Score = 22 × (topics shared with the event) + 25 (if in the same city) + size factor
size factor = min(15, 5 × log10(members))      # 100 members → 10, 1,000+ → 15
```

A group needs at least one shared topic or the same city to qualify. The top 12 are kept, each with a one-line rationale such as *"Shares AI and Hackathons; local to Pune."*

### Honest drafts

Drafts are built only from what the organiser typed: event name, city, date, venue, the first sentence of the description, team size, and a "first-timers welcome" line only if the event itself is beginner-friendly. No invented numbers, deadlines or testimonials. Each draft is written in the language its admin reads (Marathi for a Pune group that lists it, otherwise English or Hindi) and can be switched or edited before approval. Approved drafts are locked; move one back to review to change it.

### Attribution

Every link carries a `start` payload the bot passes to the API:

| Payload | Meaning |
|---|---|
| `o_<code>` | came from an approved community message (counted as a click and a source) |
| `r_<code>` | invited by a participant (referral) |
| `e_<id>` | direct link to an event |

A link from a draft that hasn't been approved yet still lets someone register but isn't credited to that group.

### Team balancing

When someone registers:

1. If they came through a teammate's invite and that team has a free seat, they join it.
2. Otherwise every open team is scored: **+100** if it lacks their role, **−40** for each member who already has their role, **+10** per current member (so near-full teams finish first).
3. The best team wins if its score is positive; otherwise a new team opens.

Withdrawing frees the seat immediately. The team card lists first names only, since it's meant to be forwarded.

### The growth loop

Participants can optionally name their college or club after registering. Unknown names are added to the community directory (marked *Found through SquadUp*), so the next Radar run can reach networks the organiser didn't know about.

### Reminders

The bot schedules reminders 7, 2 and 1 day before the event at 10:00 in `EVENT_TIMEZONE`. People who haven't confirmed get *Confirm* / *I can't make it* buttons; confirmed people only get a friendly note the day before. Scheduled jobs live in memory, so on startup the bot rebuilds them from `GET /api/reminders`.

---

## API reference

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health`, `/api/config` | status, bot username, roles, languages |
| POST / GET | `/api/events` | create / list events |
| GET / DELETE | `/api/events/{id}` | event with its Radar profile / delete |
| GET | `/api/events/{id}/radar` | ranking preview (no drafts) |
| POST | `/api/events/{id}/radar/run` | rank and create drafts for review |
| GET / POST | `/api/communities` | list / add a community group |
| GET | `/api/events/{id}/outreach?status=` | drafts for an event |
| PATCH | `/api/outreach/{id}` | edit text, or switch language (rewrites the draft) |
| POST | `/api/outreach/{id}/approve` · `/reject` · `/reset` | human approval gate |
| GET | `/api/ref/resolve?payload=` | resolve a tracked link |
| POST | `/api/ref/click?payload=` | count a click |
| POST | `/api/register` | register and seat a participant |
| GET | `/api/participants/{id}`, `/api/participants/lookup?telegram_id=` | participant with team card and invite link |
| PATCH | `/api/participants/{id}` | language or college |
| POST | `/api/participants/{id}/confirm` · `/withdraw` | attendance |
| GET | `/api/events/{id}/teams`, `/api/teams/{id}/card` | team cards |
| GET | `/api/events/{id}/stats` | KPIs, funnel, sources, roles, recent sign-ups |
| GET | `/api/reminders` | who the bot should remind |
| POST | `/api/events/{id}/simulate` | add synthetic sign-ups (needs one approved draft) |

## Configuration

| Variable | Used by | Default |
|---|---|---|
| `TELEGRAM_BOT_USERNAME` | API, bot | `RadarSquadUpBot` |
| `TELEGRAM_BOT_TOKEN` | bot | required for the bot |
| `RADAR_API_URL` | bot | `http://localhost:8000` |
| `RADAR_DB_PATH` | API | `./radar.db` |
| `CORS_ORIGINS` | API | `*` (comma-separated list to restrict) |
| `SEED_DEMO_EVENT` | API | `1` |
| `EVENT_TIMEZONE` | bot | `Asia/Kolkata` |
| `REMINDER_HOUR` | bot | `10` |
| `BOT_PERSISTENCE_FILE` | bot | `bot_state.pickle` |

## Guardrails

- **Synthetic data only.** Every community, admin name, event and simulated participant in this repo is made up.
- **Opt-in outreach.** Messages go only to group admins, who choose whether to forward them. The system never sends anything itself; the organiser copies the approved message and sends it.
- **Honest copy.** Drafts contain only facts from the event form.
- **Built for non-technical organisers.** Plain-language labels, a single approval button per message, and clear statuses.

## Deploying

- **API + dashboard:** any host that runs Python (Render, Railway, Fly.io, a VM). Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Put `radar.db` on a persistent disk or set `RADAR_DB_PATH` to one.
- **Bot:** a long-running worker with the same three environment variables. It uses long polling, so no public URL or webhook is needed.
- If the dashboard is hosted separately, set `CORS_ORIGINS` to its origin.
