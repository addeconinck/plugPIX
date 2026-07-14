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
  `streamlit-autorefresh`)

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

All runtime state is kept in `charging.db` (SQLite), created automatically next
to `app.py` on first run. It holds live claims, bookings, people, the charge
points, and app settings (including the admin password hash).

> **`charging.db` is git-ignored on purpose** — it is per-deployment runtime
> state and contains the admin password hash. Don't commit it.

## Deployment

Any host that can run Streamlit works. For a small team:

1. **One always-on machine** — run the command above and share the Network URL.
2. **[Streamlit Community Cloud](https://streamlit.io/cloud)** — push this repo
   (without `venv/` and `charging.db`, both git-ignored) and deploy `app.py`.
   Serves over HTTPS, which browser notifications require off-localhost.

> Browser notifications only work on a **secure origin** (HTTPS, or
> `localhost`). On a plain `http://` LAN address they are silently disabled.

## Project layout

```
plugPIX/
├── app.py                 # main board (entry point)
├── pages/
│   └── 1_Admin.py         # hidden, password-protected admin page
├── .streamlit/
│   └── config.toml        # hides the multipage nav
├── requirements.txt
├── .gitignore
└── charging.db            # created at runtime (git-ignored)
```
