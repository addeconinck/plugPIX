"""plugPIX — a very light EV charge-point status & booking board.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    delete,
    event,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine
from streamlit_autorefresh import st_autorefresh

# --------------------------------------------------------------------------- #
# Configuration  — edit these to match your workplace
# --------------------------------------------------------------------------- #
TIMEZONE = ZoneInfo("Europe/Brussels")

# Charge points to seed the first time the DB is created. After that, manage
# them from the Admin page.
DEFAULT_POINTS = ["Point 1", "Point 2", "Point 3", "Point 4"]

# People to seed the very first time the database is created. After that,
# manage people from the sidebar ("Manage people"). (name, plate)
DEFAULT_PEOPLE: list[tuple[str, str]] = []

DB_PATH = Path(__file__).with_name("charging.db")


def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


# App settings, editable from the Admin page. Values are stored as text.
DEFAULT_SETTINGS: dict[str, str] = {
    "booking_enabled": "1",         # "1"/"0" — enable the "take next slot" waiting list
    "refresh_seconds": "30",        # board auto-refresh cadence
    "max_claim_hours": "8",         # upper bound of the duration sliders
    "admin_password": _hash_pw("admin"),
}

# Belgian standard plate since 2010: one digit, three letters, three digits,
# e.g. "1-ABC-123". We accept any separators / casing on input and normalize.
_PLATE_RE = re.compile(r"^([1-9])([A-Z]{3})([0-9]{3})$")


def normalize_plate(raw: str) -> str | None:
    """Return a canonical '1-ABC-123' plate, or None if it isn't valid."""
    compact = re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()
    m = _PLATE_RE.match(compact)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

# --------------------------------------------------------------------------- #
# Storage  (SQLAlchemy — Postgres in production, local SQLite as a fallback)
# --------------------------------------------------------------------------- #

_metadata = MetaData()

live = Table(
    "live", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("point", Text, nullable=False),
    Column("person", Text, nullable=False),
    Column("claimed_at", Text, nullable=False),
    Column("release_eta", Text),
    Column("released_at", Text),
)
# The waiting list ("take next slot"): people queued for a point that is in use
# right now. FIFO by created_at. Front of the queue is alerted when it frees.
queue = Table(
    "queue", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("point", Text, nullable=False),
    Column("person", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("minutes", Integer),  # how long they'll need it (None = not sure)
)
people = Table(
    "people", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", Text, nullable=False, unique=True),
    Column("plate", Text, nullable=False, server_default=""),
)
points = Table(
    "points", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", Text, nullable=False, unique=True),
    Column("position", Integer, nullable=False, server_default="0"),
)
settings = Table(
    "settings", _metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text),
)


# The app ALWAYS runs against this fast, local SQLite file. Neon (if configured)
# is only a durable backup: restored into SQLite on boot, and mirrored back to
# asynchronously whenever data changes (with automatic retry, plus a manual
# button on the Admin page).
LOCAL_URL = f"sqlite:///{DB_PATH}"

_TABLES = [live, queue, people, points, settings]


def _neon_url() -> str | None:
    """Backup database URL from Streamlit secrets, or None if not configured.

    Set on Streamlit Cloud via app Settings → Secrets:
        [database]
        url = "postgresql://user:pass@host/dbname?sslmode=require"
    """
    try:
        secrets = st.secrets
        if "database" in secrets and "url" in secrets["database"]:
            url = str(secrets["database"]["url"])
        elif "DB_URL" in secrets:
            url = str(secrets["DB_URL"])
        else:
            url = ""
    except Exception:
        url = ""
    if not url:
        return None
    # SQLAlchemy needs the "postgresql://" scheme, not the older "postgres://".
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def _make_engine(url: str) -> Engine:
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(
        url, connect_args=connect_args, pool_pre_ping=True, pool_recycle=300,
    )
    if url.startswith("sqlite"):
        # WAL lets readers (e.g. the background mirror worker) run without
        # blocking a writer; busy_timeout makes brief contention wait rather
        # than fail with "database is locked".
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _rec):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
    return engine


# IMPORTANT: Streamlit re-executes this whole script on every rerun, so ordinary
# module-level globals are reset each time. Anything that must survive across
# reruns (engines, the "already initialised" flag, the mirror worker + its
# events) lives in st.cache_resource, which persists for the life of the process.
@st.cache_resource(show_spinner=False)
def get_engine() -> Engine:
    """The app always talks to the fast, local SQLite database."""
    return _make_engine(LOCAL_URL)


@st.cache_resource(show_spinner=False)
def get_neon_engine() -> Engine | None:
    """The durable backup database (Postgres/Neon), or None if not configured."""
    url = _neon_url()
    return _make_engine(url) if url else None


@st.cache_resource(show_spinner=False)
def _shared() -> dict:
    """Cross-rerun, cross-thread state: mirror signals + backup status."""
    return {
        "event": threading.Event(),   # a change happened → mirror soon
        "dirty": threading.Event(),   # local has changes not yet safe in Neon
        "status": {"last_ok": None, "last_error": None, "restored": False},
    }


def backup_status() -> dict:
    return _shared()["status"]


def _copy_all(src: Engine, dst: Engine) -> None:
    """Replace every table in dst with the rows from src (full snapshot copy)."""
    data: dict[str, list[dict]] = {}
    with src.connect() as sconn:
        for t in _TABLES:
            data[t.name] = [dict(r) for r in sconn.execute(select(t)).mappings().all()]
    with dst.begin() as dconn:
        for t in _TABLES:
            dconn.execute(delete(t))
            if data[t.name]:
                dconn.execute(insert(t), data[t.name])


def _engine_has_data(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            return (conn.execute(select(func.count()).select_from(settings)).scalar() or 0) > 0
    except Exception:
        return False


def _seed_defaults(engine: Engine) -> None:
    with engine.begin() as conn:
        if conn.execute(select(func.count()).select_from(people)).scalar() == 0:
            for name, plate in DEFAULT_PEOPLE:
                conn.execute(insert(people).values(name=name, plate=plate))
        if conn.execute(select(func.count()).select_from(points)).scalar() == 0:
            for i, name in enumerate(DEFAULT_POINTS):
                conn.execute(insert(points).values(name=name, position=i))
        have = {r[0] for r in conn.execute(select(settings.c.key))}
        for key, value in DEFAULT_SETTINGS.items():
            if key not in have:
                conn.execute(insert(settings).values(key=key, value=value))


_RETRY_INTERVAL = 120  # seconds; retry a failed/pending mirror


@st.cache_resource(show_spinner=False)
def _bootstrap() -> bool:
    """Run ONCE per process: create schema, restore from Neon, start the worker.

    Cached via st.cache_resource so it does NOT re-run (and therefore does not
    re-restore over live changes) on every Streamlit rerun.
    """
    local = get_engine()
    _metadata.create_all(local)

    neon = get_neon_engine()
    status = _shared()["status"]
    if neon is not None:
        try:
            _metadata.create_all(neon)
            # Neon is the durable source of truth: restore it into local SQLite.
            if _engine_has_data(neon):
                _copy_all(src=neon, dst=local)
                status["restored"] = True
        except Exception as e:  # Neon unreachable → keep whatever local we have
            status["last_error"] = f"restore failed: {e}"

    _seed_defaults(local)

    if neon is not None:
        sh = _shared()
        threading.Thread(
            target=_mirror_worker, args=(sh, local, neon),
            name="plugpix-mirror", daemon=True,
        ).start()
    return True


def init_db() -> None:
    _bootstrap()


# ---- backup / mirror (SQLite → Neon) -------------------------------------- #


def mirror_to_neon() -> bool:
    """Copy local SQLite → Neon. Returns True on success (no-op if unconfigured)."""
    neon = get_neon_engine()
    if neon is None:
        return False
    sh = _shared()
    try:
        _metadata.create_all(neon)
        _copy_all(src=get_engine(), dst=neon)
        sh["status"]["last_ok"] = now()
        sh["status"]["last_error"] = None
        sh["dirty"].clear()
        return True
    except Exception as e:
        sh["status"]["last_error"] = str(e)
        return False


def _mirror_worker(sh: dict, local: Engine, neon: Engine) -> None:
    # Uses only the objects passed in — never touches st.cache_resource from this
    # background thread.
    event_, dirty, status = sh["event"], sh["dirty"], sh["status"]
    while True:
        # Wake on a change, else every _RETRY_INTERVAL to retry a pending mirror.
        event_.wait(timeout=_RETRY_INTERVAL)
        event_.clear()
        if not dirty.is_set():
            continue
        time.sleep(2)  # debounce: coalesce a burst of quick changes
        event_.clear()
        try:
            _metadata.create_all(neon)
            _copy_all(src=local, dst=neon)
            status["last_ok"] = now()
            status["last_error"] = None
            dirty.clear()
        except Exception as e:
            status["last_error"] = str(e)


def request_backup() -> None:
    """Mark data changed and wake the worker to mirror to Neon (non-blocking)."""
    if get_neon_engine() is not None:
        sh = _shared()
        sh["dirty"].set()
        sh["event"].set()


def now() -> dt.datetime:
    return dt.datetime.now(TIMEZONE)


def iso(d: dt.datetime) -> str:
    return d.isoformat()


def parse(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=TIMEZONE)
    return d


def _one(stmt) -> dict | None:
    with get_engine().connect() as conn:
        row = conn.execute(stmt).mappings().first()
    return dict(row) if row else None


def _all(stmt) -> list[dict]:
    with get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings().all()]


def _exec(stmt) -> int:
    with get_engine().begin() as conn:
        rc = conn.execute(stmt).rowcount
    request_backup()  # any write triggers an async mirror to Neon
    return rc


# ---- live status ---------------------------------------------------------- #


def active_claim(point: str) -> dict | None:
    return _one(
        select(live)
        .where(live.c.point == point, live.c.released_at.is_(None))
        .order_by(live.c.claimed_at.desc())
        .limit(1)
    )


def claim_point(point: str, person: str, release_eta: dt.datetime | None) -> None:
    with get_engine().begin() as conn:
        conn.execute(
            insert(live).values(
                point=point,
                person=person,
                claimed_at=iso(now()),
                release_eta=iso(release_eta) if release_eta else None,
            )
        )
        # Claiming a point removes you from its waiting list.
        conn.execute(delete(queue).where(queue.c.point == point, queue.c.person == person))
    request_backup()


def release_point(claim_id: int) -> None:
    _exec(
        update(live)
        .where(live.c.id == claim_id, live.c.released_at.is_(None))
        .values(released_at=iso(now()))
    )


def is_free(point: str) -> bool:
    """A point is free when nobody is charging on it right now."""
    return active_claim(point) is None


# ---- waiting list ("take next slot") -------------------------------------- #


def queue_for(point: str) -> list[dict]:
    return _all(select(queue).where(queue.c.point == point).order_by(queue.c.created_at))


def join_queue(point: str, person: str, minutes: int | None) -> None:
    """Add `person` to the point's waiting list (updates duration if already queued)."""
    with get_engine().begin() as conn:
        row = conn.execute(
            select(queue.c.id).where(queue.c.point == point, queue.c.person == person)
        ).first()
        if row:
            conn.execute(update(queue).where(queue.c.id == row[0]).values(minutes=minutes))
        else:
            conn.execute(
                insert(queue).values(
                    point=point, person=person, created_at=iso(now()), minutes=minutes
                )
            )
    request_backup()


def leave_queue(point: str, person: str) -> None:
    _exec(delete(queue).where(queue.c.point == point, queue.c.person == person))


# ---- people --------------------------------------------------------------- #


def list_people() -> list[dict]:
    # Case-insensitive sort done in Python so it's portable across DBs.
    ppl = _all(select(people))
    return sorted(ppl, key=lambda p: p["name"].casefold())


def add_person(name: str, plate: str) -> None:
    _exec(insert(people).values(name=name.strip(), plate=plate))


def remove_person(person_id: int) -> None:
    _exec(delete(people).where(people.c.id == person_id))


def plate_of(name: str) -> str:
    row = _one(select(people.c.plate).where(people.c.name == name))
    return row["plate"] if row else ""


# ---- points --------------------------------------------------------------- #


def list_points() -> list[dict]:
    return _all(select(points).order_by(points.c.position, points.c.id))


def get_points() -> list[str]:
    return [p["name"] for p in list_points()]


def add_point(name: str) -> None:
    with get_engine().begin() as conn:
        pos = conn.execute(
            select(func.coalesce(func.max(points.c.position), -1) + 1)
        ).scalar()
        conn.execute(insert(points).values(name=name.strip(), position=pos))
    request_backup()


def rename_point(point_id: int, name: str) -> None:
    _exec(update(points).where(points.c.id == point_id).values(name=name.strip()))


def remove_point(point_id: int) -> None:
    _exec(delete(points).where(points.c.id == point_id))


# ---- settings ------------------------------------------------------------- #


def get_setting(key: str, default: str = "") -> str:
    row = _one(select(settings.c.value).where(settings.c.key == key))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    # Portable upsert: try UPDATE, INSERT if the row didn't exist yet.
    with get_engine().begin() as conn:
        updated = conn.execute(
            update(settings).where(settings.c.key == key).values(value=value)
        ).rowcount
        if not updated:
            conn.execute(insert(settings).values(key=key, value=value))
    request_backup()


# ---- bulk board load (one connection, a few queries) ---------------------- #


def load_board() -> dict:
    """Fetch everything the board needs in ONE connection.

    Doing this in one connection (~5 queries) instead of per-point helpers
    avoids dozens of round-trips per rerun and lets the UI compute status in
    Python.
    """
    now_iso = iso(now())
    with get_engine().connect() as conn:
        claim_rows = conn.execute(
            select(live)
            .where(live.c.released_at.is_(None))
            .order_by(live.c.claimed_at.desc())
        ).mappings().all()
        q_rows = conn.execute(
            select(queue).order_by(queue.c.created_at)
        ).mappings().all()
        pt_rows = conn.execute(
            select(points).order_by(points.c.position, points.c.id)
        ).mappings().all()
        ppl_rows = conn.execute(select(people)).mappings().all()
        set_rows = conn.execute(select(settings)).mappings().all()

    claims: dict[str, dict] = {}
    for r in claim_rows:
        claims.setdefault(r["point"], dict(r))  # latest active claim per point
    queue_by_point: dict[str, list[dict]] = {}
    for r in q_rows:
        queue_by_point.setdefault(r["point"], []).append(dict(r))

    return {
        "now_iso": now_iso,
        "settings": {r["key"]: r["value"] for r in set_rows},
        "points": [r["name"] for r in pt_rows],
        "people": [dict(r) for r in ppl_rows],
        "claims": claims,
        "queue_by_point": queue_by_point,
    }


def _setting_int(s: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(s.get(key, default))))
    except (ValueError, TypeError):
        return default


def minutes_label(mins: int | None) -> str:
    if not mins:
        return "not sure"
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h} h {m:02d}"
    if h:
        return f"{h} h"
    return f"{m} min"


def booking_enabled() -> bool:
    return get_setting("booking_enabled", "1") == "1"


def refresh_seconds() -> int:
    try:
        return max(5, int(get_setting("refresh_seconds", "30")))
    except ValueError:
        return 30


def max_claim_hours() -> int:
    try:
        return min(24, max(1, int(get_setting("max_claim_hours", "8"))))
    except ValueError:
        return 8


def check_admin_password(pw: str) -> bool:
    return _hash_pw(pw) == get_setting("admin_password", _hash_pw("admin"))


def set_admin_password(pw: str) -> None:
    set_setting("admin_password", _hash_pw(pw))


# ---- maintenance ---------------------------------------------------------- #


def release_all_points() -> int:
    """End every active live claim. Returns how many were released."""
    return _exec(
        update(live).where(live.c.released_at.is_(None)).values(released_at=iso(now()))
    )


def clear_all_queue() -> int:
    """Empty every point's waiting list. Returns how many entries were removed."""
    return _exec(delete(queue))


# --------------------------------------------------------------------------- #
# UI helpers
# --------------------------------------------------------------------------- #

MOBILE_CSS = """
<style>
/* Big, thumb-friendly tap targets everywhere */
.stButton > button,
.stFormSubmitButton > button,
[data-testid="stPopover"] > button {
    min-height: 46px;
    font-size: 1rem;
    border-radius: 12px;
}
/* Fatter slider handles are far easier to drag on a touch screen */
[data-testid="stSlider"] [role="slider"] { height: 26px; width: 26px; }
/* iOS zooms the page when it focuses an input < 16px — force 16px to stop it */
input, textarea, select { font-size: 16px !important; }
/* Give each charge-point card some breathing room + a tappable feel */
[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 14px; }
/* Tighter margins so cards use the full width of a phone */
@media (max-width: 640px) {
    .block-container { padding: 0.75rem 0.6rem 3rem; }
    h1 { font-size: 1.5rem; }
}
/* Timeline segment: let its ✕ overlay sit inside the coloured box (top-right) */
[class*="st-key-seg-"] { position: relative; }
[class*="st-key-qx-"] {
    position: absolute; top: 3px; right: 3px; width: 22px; margin: 0 !important; z-index: 5;
}
[class*="st-key-qx-"] button {
    min-height: 0 !important; height: 20px !important; width: 22px !important;
    padding: 0 !important; line-height: 1; border-radius: 6px;
    background: rgba(0,0,0,0.30); color: #fff; border: none;
}
[class*="st-key-qx-"] button:hover { background: rgba(0,0,0,0.55); color: #fff; }
</style>
"""

def build_eta_choices(max_hours: int) -> dict[str, int | None]:
    """Quick "I'll be done in…" choices → minutes (None = unknown)."""
    choices: dict[str, int | None] = {"Not sure": None, "30 min": 30, "1 h": 60, "1 h 30": 90}
    for h in range(2, max_hours + 1):
        choices[f"{h} h"] = h * 60
    return choices


def request_notify_permission() -> None:
    """Ask the browser for notification permission (no-op if already decided)."""
    st.iframe(
        """<script>
        if (window.Notification && Notification.permission === "default") {
            Notification.requestPermission();
        }
        </script>""",
        height=1,
    )


def fire_browser_notification(points: list[str]) -> None:
    """Pop a browser notification + short beep that the given points are free."""
    body = json.dumps(", ".join(points) + " now available")
    html = """<script>
    (function () {
        var body = __BODY__;
        try {
            if (window.Notification && Notification.permission === "granted") {
                new Notification("plugPIX ⚡ charge point free", { body: body });
            } else if (window.Notification && Notification.permission !== "denied") {
                Notification.requestPermission().then(function (p) {
                    if (p === "granted") new Notification("plugPIX ⚡ charge point free", { body: body });
                });
            }
        } catch (e) {}
        try {
            var ctx = new (window.AudioContext || window.webkitAudioContext)();
            var o = ctx.createOscillator(), g = ctx.createGain();
            o.connect(g); g.connect(ctx.destination);
            o.frequency.value = 880; o.start();
            g.gain.setValueAtTime(0.2, ctx.currentTime);
            g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.5);
            o.stop(ctx.currentTime + 0.5);
        } catch (e) {}
    })();
    </script>""".replace("__BODY__", body)
    st.iframe(html, height=1)


def fmt_time(d: dt.datetime) -> str:
    return d.astimezone(TIMEZONE).strftime("%H:%M")


# Timeline colours: muted grey for the current in-use block; the validated
# categorical palette (fixed order) for each person waiting in line.
_TL_BUSY = "#8a8a8a"
_TL_COLORS = [
    "#2a78d6", "#1baf7a", "#eda100", "#008300",
    "#4a3aa7", "#e34948", "#e87ba4", "#eb6834",
]
_DEFAULT_MIN = 60  # assumed duration when someone didn't say ("not sure")


def schedule_segments(claim: dict | None, wait_list: list[dict]) -> list[dict]:
    """Estimated back-to-back schedule: the current use, then each queued person."""
    n = now()
    segs: list[dict] = []
    cursor = n
    if claim is not None:
        end = parse(claim["release_eta"])
        known = bool(end and end > n)
        occ_end = end if known else n + dt.timedelta(minutes=_DEFAULT_MIN)
        segs.append({
            "person": claim["person"], "start": n, "end": occ_end,
            "minutes": max(1, (occ_end - n).total_seconds() / 60),
            "kind": "busy", "approx": not known, "booked": None,
            "color": _TL_BUSY, "id": None,
        })
        cursor = occ_end
    for idx, q in enumerate(wait_list):
        m = q["minutes"] or _DEFAULT_MIN
        start = cursor
        end = start + dt.timedelta(minutes=m)
        segs.append({
            "person": q["person"], "start": start, "end": end, "minutes": m,
            "kind": "queue", "approx": q["minutes"] is None, "booked": q["minutes"],
            "color": _TL_COLORS[idx % len(_TL_COLORS)], "id": q["id"],
        })
        cursor = end
    return segs


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _text_on(hex_color: str) -> str:
    """Pick black or white text for best contrast on the given fill colour."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lin = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    lum = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    contrast_white = 1.05 / (lum + 0.05)
    contrast_black = (lum + 0.05) / 0.05
    return "#ffffff" if contrast_white >= contrast_black else "#000000"


def segment_box_html(sg: dict, me: str | None) -> str:
    """One timeline segment: a colored box with the person's name and start–end."""
    txt = _text_on(sg["color"])
    is_me = sg["person"] == me
    name = "you" if is_me else sg["person"]
    ring = "box-shadow:inset 0 0 0 2px rgba(255,255,255,0.9);" if is_me else ""
    # Leave room for the overlaid ✕ on your own queued segment.
    pad = "4px 24px 4px 6px" if (is_me and sg["kind"] == "queue") else "4px 6px"
    approx = "~" if sg["approx"] else ""
    interval = f'{approx}{fmt_time(sg["start"])}–{fmt_time(sg["end"])}'
    return (
        f'<div title="{_esc(name)} · {interval}" style="background:{sg["color"]};'
        f'color:{txt};border-radius:6px;padding:{pad};{ring}text-align:center;'
        f'overflow:hidden;box-sizing:border-box;">'
        f'<div style="font-size:11px;font-weight:600;white-space:nowrap;overflow:hidden;'
        f'text-overflow:ellipsis;">{_esc(name)}</div>'
        f'<div style="font-size:10px;opacity:.9;white-space:nowrap;overflow:hidden;'
        f'text-overflow:ellipsis;">{interval}</div></div>'
    )


def render_point(point: str, me: str | None, booking_on: bool,
                 eta_choices: dict[str, int | None], claim: dict | None,
                 wait_list: list[dict]) -> None:
    in_use = claim is not None
    can_act = me is not None
    my_entry = next((q for q in wait_list if q["person"] == me), None)

    with st.container(border=True):
        header = st.columns([3, 2])
        with header[0]:
            if not in_use:
                st.markdown(f"### 🟢 {point}")
                st.caption("Free now")
            else:
                st.markdown(f"### 🔴 {point}")
                eta = parse(claim["release_eta"])
                who = "you" if claim["person"] == me else claim["person"]
                if eta:
                    st.caption(f"In use by **{who}** — free ~{fmt_time(eta)}")
                else:
                    st.caption(f"In use by **{who}**")

        with header[1]:
            if not in_use:
                # Free → just claim it (no booking on a free point).
                if not can_act:
                    st.button("⚡ Claim now", key=f"claim-btn-{point}",
                              use_container_width=True, disabled=True,
                              help="Select your name first.")
                else:
                    with st.popover("⚡ Claim now", use_container_width=True):
                        st.write(f"Claim **{point}** for **{me}**")
                        default = _eta_label_for(eta_choices, my_entry)
                        choice = st.select_slider(
                            "I'll be done in…", options=list(eta_choices),
                            value=default, key=f"eta-{point}")
                        mins = eta_choices[choice]
                        eta_dt = now() + dt.timedelta(minutes=mins) if mins else None
                        if eta_dt:
                            st.caption(f"Frees up around **{fmt_time(eta_dt)}**")
                        if st.button("Confirm claim", key=f"claim-{point}", type="primary"):
                            claim_point(point, me, eta_dt)
                            st.rerun()
            else:
                # In use → owner can release; others can queue for the next slot.
                if claim["person"] == me:
                    if st.button("✅ Release", key=f"rel-{point}", use_container_width=True):
                        release_point(claim["id"])
                        st.rerun()
                elif not booking_on:
                    st.button("Release", key=f"rel-{point}", use_container_width=True,
                              disabled=True, help=f"Only {claim['person']} can release this.")
                elif not can_act:
                    st.button("🎟️ Book next slot", key=f"book-btn-{point}",
                              use_container_width=True, disabled=True,
                              help="Select your name first.")
                elif my_entry is not None:
                    if st.button("🎟️ Leave the line", key=f"leave-{point}",
                                 use_container_width=True):
                        leave_queue(point, me)
                        st.rerun()
                else:
                    with st.popover("🎟️ Book next slot", use_container_width=True):
                        st.write(f"Get in line for **{point}** — **{me}**")
                        choice = st.select_slider(
                            "I'll need it for…", options=list(eta_choices),
                            value="1 h", key=f"q-{point}")
                        if st.button("Join the line", key=f"join-{point}", type="primary"):
                            join_queue(point, me, eta_choices[choice])
                            st.rerun()

        # Timeline: a Gantt-style row — current use, then each person in line, with
        # start–end on every segment and a ✕ on your own to leave the line.
        if booking_on and (in_use or wait_list):
            segs = schedule_segments(claim, wait_list)
            total = sum(sg["minutes"] for sg in segs) or 1
            # Floor each width so a short slot still fits its label and the ✕.
            weights = [max(sg["minutes"], total * 0.14) for sg in segs]
            cols = st.columns(weights, gap="small")
            pslug = re.sub(r"\W+", "_", point)
            for idx, (col, sg) in enumerate(zip(cols, segs)):
                with col.container(key=f"seg-{pslug}-{idx}"):
                    st.markdown(segment_box_html(sg, me), unsafe_allow_html=True)
                    if sg["kind"] == "queue" and sg["person"] == me:
                        # CSS overlays this ✕ into the segment's top-right corner.
                        if st.button("✕", key=f"qx-{sg['id']}", help="Leave the line"):
                            leave_queue(point, me)
                            st.rerun()


def _eta_label_for(eta_choices: dict[str, int | None], my_entry: dict | None) -> str:
    """Pre-select the duration a queued person booked, when they finally claim."""
    if my_entry and my_entry.get("minutes"):
        for label, mins in eta_choices.items():
            if mins == my_entry["minutes"]:
                return label
    return "1 h"


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


def main() -> None:
    st.set_page_config(
        page_title="plugPIX · EV charging",
        page_icon="⚡",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(MOBILE_CSS, unsafe_allow_html=True)
    init_db()

    # One batched load per rerun from the local SQLite database (fast).
    _t0 = time.perf_counter()
    board = load_board()
    _load_ms = (time.perf_counter() - _t0) * 1000.0
    s = board["settings"]
    points = board["points"]
    people = board["people"]
    claims = board["claims"]
    queue_by_point = board["queue_by_point"]

    booking_on = s.get("booking_enabled", "1") == "1"
    refresh = _setting_int(s, "refresh_seconds", 30, 5, 3600)
    eta_choices = build_eta_choices(_setting_int(s, "max_claim_hours", 8, 1, 24))

    # Per-point state from the batched snapshot.
    free_now = {p for p in points if claims.get(p) is None}

    # Auto-refresh the whole board (keeps session/state, unlike a browser reload).
    st_autorefresh(interval=refresh * 1000, key="autorefresh")

    st.title("⚡ plugPIX — EV charge points")
    n = now()
    st.caption(
        f"🕐 {n.strftime('%A %d %B %Y — %H:%M:%S')} "
        f"· auto-refreshes every {refresh}s"
    )
    # Add ?debug=1 to the URL to see where time goes (local load + backup state).
    if st.query_params.get("debug"):
        bkp = "off"
        if get_neon_engine() is not None:
            status = backup_status()
            last = status["last_ok"]
            bkp = f"ok@{fmt_time(last)}" if last else "pending"
            if status["last_error"]:
                bkp = "ERROR"
        st.caption(f"⏱ local load_board={_load_ms:.0f} ms · neon backup={bkp}")

    # "Alert me when any point frees up" — notify each time a point newly frees.
    if st.session_state.get("watch_any"):
        prev_free = st.session_state.get("prev_free")
        # prev_free is None on the run we enable it => set a baseline, don't alert yet.
        newly_free = (free_now - prev_free) if prev_free is not None else set()
        st.session_state["prev_free"] = free_now
        for p in sorted(newly_free):
            st.toast(f"🔌 {p} is now free!", icon="🔔")
        if newly_free:
            fire_browser_notification(sorted(newly_free))
        request_notify_permission()
    else:
        st.session_state["prev_free"] = None

    people = sorted(people, key=lambda p: p["name"].casefold())
    names = [p["name"] for p in people]
    plate_by_name = {p["name"]: p["plate"] for p in people}

    def name_label(name: str) -> str:
        plate = plate_by_name.get(name)
        return f"{name} — {plate}" if plate else name

    # --- Who are you? (on the main page) ---
    # Apply a name selection queued after adding a new person (must happen
    # before the selectbox widget is created).
    if "pending_me" in st.session_state:
        st.session_state["me"] = st.session_state.pop("pending_me")

    ADD_NEW = "➕ Add a new person…"
    choice = st.selectbox(
        "Your name",
        names + [ADD_NEW],
        index=None,
        placeholder="Select your name…",
        format_func=lambda o: o if o == ADD_NEW else name_label(o),
        key="me",
    )

    me = None  # stays None (claim/book locked) until a known name is chosen
    if choice == ADD_NEW:
        with st.form("add_me"):
            nm = st.text_input("Your name")
            pl = st.text_input("Number plate (optional)", placeholder="1-ABC-123")
            added = st.form_submit_button("➕ Add & select", type="primary")
        if added:
            name = nm.strip()
            norm = normalize_plate(pl)
            if not name:
                st.error("Please enter a name.")
            elif name.casefold() in {n.casefold() for n in names}:
                st.error(f"“{name}” already exists — pick it from the list.")
            elif pl.strip() and norm is None:
                st.error("Plate must look like 1-ABC-123 (Belgian format).")
            else:
                add_person(name, norm or "")
                st.session_state["pending_me"] = name  # auto-select after rerun
                st.rerun()
    elif choice:
        me = choice
        plate = plate_by_name.get(me, "")
        st.caption(f"🚗 {plate}" if plate else "No plate on file.")
    else:
        st.info("👆 Select your name (or add yourself) to claim or book a charge point.")

    # --- "It's your turn" — you're first in line and the point just freed ---
    your_turn = []
    if me and booking_on:
        for p in points:
            q = queue_by_point.get(p, [])
            if claims.get(p) is None and q and q[0]["person"] == me:
                your_turn.append(p)
    if your_turn:
        st.success(
            "🎟️ **Your turn:** " + ", ".join(your_turn)
            + " — claim it before someone else does!"
        )
    prev_turn = st.session_state.get("turn_prev", set())
    newly_turn = set(your_turn) - prev_turn
    st.session_state["turn_prev"] = set(your_turn)
    for p in sorted(newly_turn):
        st.toast(f"🎟️ {p} is free — you're up!", icon="🎟️")
    if newly_turn:
        fire_browser_notification([f"{p} — your turn" for p in sorted(newly_turn)])
        request_notify_permission()

    # --- Controls ---
    c1, c2 = st.columns([1, 1])
    c1.metric("Free right now", f"{len(free_now)} / {len(points)}")
    with c2:
        st.checkbox("🔔 Alert me when any point frees up", key="watch_any")
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()

    st.divider()

    # --- The board: two per row so mobile stacks them in order (1, 2, 3, 4) ---
    if not points:
        st.warning("No charge points configured. Add some on the Admin page.")
    for i in range(0, len(points), 2):
        cols = st.columns(2)
        for j, point in enumerate(points[i:i + 2]):
            with cols[j]:
                render_point(
                    point, me, booking_on, eta_choices,
                    claims.get(point), queue_by_point.get(point, []),
                )


if __name__ == "__main__":
    main()
