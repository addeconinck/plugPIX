# ⚡ plugPIX — EV charge-point board

A very light [Streamlit](https://streamlit.io) app for a small team to signal
who is using the workplace EV charge points, when they'll be free, and to book a
slot for later. Built to be shared with employees over a browser link.

![status: internal tool](https://img.shields.io/badge/status-internal_tool-blue)

## Features

- **Live status** — each charge point shows 🟢 *Free* or 🔴 *In use*, by whom,
  and roughly when it will free up.
- **Claim / release** — pick your name, claim a point, and drag a slider to say
  when you'll be done (up to a configurable max). Release it when you unplug.
- **Booking** — reserve a time slot for **today** using a draggable time-range
  slider. Overlapping bookings are rejected. A point automatically shows as
  *In use* once its booked slot starts. (Can be disabled by an admin.)
- **"Alert me when any point frees up"** — a sidebar toggle that fires a browser
  notification + beep the moment a point becomes free. The tab must stay open.
- **Mobile-friendly** — big tap targets, drag-based pickers, no iOS zoom-on-focus.
- **People management** — add employees with a name and a Belgian-style plate
  (`1-ABC-123`), shown next to their name.
- **Admin page** — password-protected settings (see below).
- **Shared state** — everything is stored in a single SQLite file, so all users
  and browser tabs see the same board.

## Requirements

- Python 3.10+
- Packages in [`requirements.txt`](requirements.txt) (`streamlit`,
  `streamlit-autorefresh`, `SQLAlchemy`, `psycopg2-binary`)

## Setup & run

```bash
# 1. create a virtual environment
python -m venv venv

# 2. install dependencies
#    Windows (PowerShell):
venv\Scripts\python -m pip install -r requirements.txt
#    macOS / Linux:
#    venv/bin/python -m pip install -r requirements.txt

# 3. run
venv\Scripts\streamlit run app.py     # Windows
# venv/bin/streamlit run app.py       # macOS / Linux
```

Streamlit prints a **Local URL** and a **Network URL**. Share the Network URL
with employees on the same network, or deploy (see below).

## Admin page

The admin settings live at **`<app-url>/Admin`** (e.g. `http://host:8501/Admin`).
It is intentionally **hidden from the sidebar navigation** and reachable by URL
only.

- **Default password: `admin`** — change it on first login
  (Admin → *Change admin password*).
- From the admin page you can:
  - enable/disable the **booking** feature,
  - **add / rename / remove** charge points,
  - set the **auto-refresh interval**, **max claim duration**, and the
    **booking window** (earliest/latest hour),
  - run maintenance: **release all** points, **clear all** bookings.

The password is stored as a SHA-256 hash in the database — not in plaintext.

## Configuration

First-run defaults live at the top of [`app.py`](app.py):

- `TIMEZONE` — defaults to `Europe/Brussels`.
- `DEFAULT_POINTS` — charge points seeded on first launch (later managed in Admin).
- `DEFAULT_PEOPLE` — optional people to seed on first launch.

After the first launch these are stored in the database; change points, people,
and settings from the running app rather than editing the code.

## Data & storage

All runtime state — live claims, bookings, people, charge points, and settings
(including the admin password hash) — is stored via **SQLAlchemy**:

- **Production / cloud:** a persistent **PostgreSQL** database, configured with a
  connection string in Streamlit secrets (see below).
- **Local development:** if no database secret is set, it falls back to a local
  SQLite file `charging.db` next to `app.py`, created automatically.

> ⚠️ **Do not rely on SQLite on Streamlit Community Cloud (or any sleep/restart
> host).** Its filesystem is **ephemeral** — the container is wiped when the app
> sleeps, reboots, or redeploys, so a local `charging.db` is lost (people and
> bookings disappear overnight). Use a hosted Postgres in production.

> `charging.db` and `.streamlit/secrets.toml` are git-ignored on purpose — they
> are per-deployment runtime state / secrets. Don't commit them.

### Setting up a persistent database (Postgres)

1. Create a **free** Postgres database — e.g. [Neon](https://neon.tech) or
   [Supabase](https://supabase.com). Copy its connection string; it looks like:

   ```
   postgresql://user:password@host/dbname?sslmode=require
   ```

2. **On Streamlit Community Cloud:** open your app → **⋮ → Settings → Secrets**
   and paste:

   ```toml
   [database]
   url = "postgresql://user:password@host/dbname?sslmode=require"
   ```

   Save — the app restarts and now reads/writes the Postgres database. Tables
   and default settings are created automatically on first run.

3. **Locally** (optional, to use the same DB in dev): create
   `.streamlit/secrets.toml` with the same content. See
   [`.streamlit/secrets.toml.example`](.streamlit/secrets.toml.example).

That's it — data now survives restarts. (A `postgres://` prefix is accepted too;
it's normalized to `postgresql://` automatically.)

## Deployment

Any host that can run Streamlit works. For a small team:

1. **[Streamlit Community Cloud](https://streamlit.io/cloud)** (recommended) —
   push this repo (without `venv/`, `charging.db`, and `secrets.toml`, all
   git-ignored), deploy `app.py`, and **add the `[database]` secret above** so
   data persists. Serves over HTTPS, which browser notifications require.
2. **One always-on machine** — run the command above and share the Network URL.
   Here the SQLite fallback persists fine (the disk isn't wiped), so a database
   secret is optional.

> Browser notifications only work on a **secure origin** (HTTPS, or
> `localhost`). On a plain `http://` LAN address they are silently disabled.

## Project layout

```
plugPIX/
├── app.py                 # main board (entry point)
├── pages/
│   └── 1_Admin.py         # hidden, password-protected admin page
├── .streamlit/
│   ├── config.toml            # hides the multipage nav
│   ├── secrets.toml.example   # template for the DB connection string
│   └── secrets.toml           # your real secrets (git-ignored; create locally)
├── requirements.txt
├── .gitignore
└── charging.db                # local SQLite fallback, created at runtime (git-ignored)
```
