"""plugPIX — a very light EV charge-point status & booking board.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
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
    "booking_enabled": "1",         # "1"/"0" — show the booking feature
    "refresh_seconds": "30",        # board auto-refresh cadence
    "max_claim_hours": "8",         # upper bound of the "I'll be done in…" slider
    "slot_start_hour": "6",         # earliest bookable hour
    "slot_end_hour": "22",          # latest bookable hour
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
bookings = Table(
    "bookings", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("point", Text, nullable=False),
    Column("person", Text, nullable=False),
    Column("start_at", Text, nullable=False),
    Column("end_at", Text, nullable=False),
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


def _db_url() -> str:
    """Connection URL: Streamlit secret in production, local SQLite in dev.

    Set a persistent database on Streamlit Cloud via app Settings → Secrets:
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
        return f"sqlite:///{DB_PATH}"
    # SQLAlchemy needs the "postgresql://" scheme, not the older "postgres://".
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


_engines: dict[str, Engine] = {}
_inited: set[str] = set()


def get_engine() -> Engine:
    url = _db_url()
    if url not in _engines:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engines[url] = create_engine(
            url,
            connect_args=connect_args,
            pool_pre_ping=True,   # transparently replace connections dropped by the server
            pool_recycle=300,     # recycle connections older than 5 min (serverless idle)
        )
    return _engines[url]


def init_db() -> None:
    """Create tables and seed defaults once per process (idempotent)."""
    url = _db_url()
    if url in _inited:
        return
    engine = get_engine()
    _metadata.create_all(engine)
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
    _inited.add(url)


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
        return conn.execute(stmt).rowcount


# ---- live status ---------------------------------------------------------- #


def active_claim(point: str) -> dict | None:
    return _one(
        select(live)
        .where(live.c.point == point, live.c.released_at.is_(None))
        .order_by(live.c.claimed_at.desc())
        .limit(1)
    )


def claim_point(point: str, person: str, release_eta: dt.datetime | None) -> None:
    _exec(
        insert(live).values(
            point=point,
            person=person,
            claimed_at=iso(now()),
            release_eta=iso(release_eta) if release_eta else None,
        )
    )


def release_point(claim_id: int) -> None:
    _exec(
        update(live)
        .where(live.c.id == claim_id, live.c.released_at.is_(None))
        .values(released_at=iso(now()))
    )


# ---- bookings ------------------------------------------------------------- #


def upcoming_bookings(point: str) -> list[dict]:
    return _all(
        select(bookings)
        .where(bookings.c.point == point, bookings.c.end_at >= iso(now()))
        .order_by(bookings.c.start_at)
    )


def active_booking(point: str) -> dict | None:
    """The booking whose slot covers 'now', if any (start <= now < end)."""
    return _one(
        select(bookings)
        .where(
            bookings.c.point == point,
            bookings.c.start_at <= iso(now()),
            bookings.c.end_at > iso(now()),
        )
        .order_by(bookings.c.start_at)
        .limit(1)
    )


def is_free(point: str) -> bool:
    """A point is free when nobody is charging now and no booking covers now."""
    return active_claim(point) is None and active_booking(point) is None


def booking_conflict(point: str, start: dt.datetime, end: dt.datetime) -> dict | None:
    """Return an overlapping booking on the same point, if any."""
    return _one(
        select(bookings)
        .where(
            bookings.c.point == point,
            bookings.c.start_at < iso(end),
            bookings.c.end_at > iso(start),
        )
        .limit(1)
    )


def add_booking(point: str, person: str, start: dt.datetime, end: dt.datetime) -> None:
    _exec(
        insert(bookings).values(
            point=point, person=person, start_at=iso(start), end_at=iso(end)
        )
    )


def cancel_booking(booking_id: int) -> None:
    _exec(delete(bookings).where(bookings.c.id == booking_id))


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


# ---- bulk board load (one connection, a few queries) ---------------------- #


def load_board() -> dict:
    """Fetch everything the board needs in ONE connection.

    The per-point helpers (active_claim/active_booking/upcoming_bookings) each
    make a separate round-trip; on a remote database that's dozens of trips per
    rerun. This does it in ~5 queries and lets the UI compute status in Python.
    """
    now_iso = iso(now())
    with get_engine().connect() as conn:
        claim_rows = conn.execute(
            select(live)
            .where(live.c.released_at.is_(None))
            .order_by(live.c.claimed_at.desc())
        ).mappings().all()
        bk_rows = conn.execute(
            select(bookings)
            .where(bookings.c.end_at >= now_iso)
            .order_by(bookings.c.start_at)
        ).mappings().all()
        pt_rows = conn.execute(
            select(points).order_by(points.c.position, points.c.id)
        ).mappings().all()
        ppl_rows = conn.execute(select(people)).mappings().all()
        set_rows = conn.execute(select(settings)).mappings().all()

    claims: dict[str, dict] = {}
    for r in claim_rows:
        claims.setdefault(r["point"], dict(r))  # latest active claim per point
    bookings_by_point: dict[str, list[dict]] = {}
    for b in bk_rows:
        bookings_by_point.setdefault(b["point"], []).append(dict(b))

    return {
        "now_iso": now_iso,
        "settings": {r["key"]: r["value"] for r in set_rows},
        "points": [r["name"] for r in pt_rows],
        "people": [dict(r) for r in ppl_rows],
        "claims": claims,
        "bookings_by_point": bookings_by_point,
    }


def active_booking_of(upcoming: list[dict], now_iso: str) -> dict | None:
    """The booking whose slot covers now, from a point's upcoming list."""
    for b in upcoming:
        if b["start_at"] <= now_iso < b["end_at"]:
            return b
    return None


def _setting_int(s: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(s.get(key, default))))
    except (ValueError, TypeError):
        return default


def slot_bounds_from(s: dict) -> tuple[dt.time, dt.time]:
    lo = _setting_int(s, "slot_start_hour", 6, 0, 24)
    hi = _setting_int(s, "slot_end_hour", 22, 0, 24)
    if hi <= lo:
        lo, hi = 6, 22
    return dt.time(lo, 0), dt.time(min(hi, 23), 59 if hi >= 24 else 0)


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


def slot_bounds() -> tuple[dt.time, dt.time]:
    def hour(key: str, fallback: int) -> int:
        try:
            return min(24, max(0, int(get_setting(key, str(fallback)))))
        except ValueError:
            return fallback
    lo = hour("slot_start_hour", 6)
    hi = hour("slot_end_hour", 22)
    if hi <= lo:
        lo, hi = 6, 22
    return dt.time(lo, 0), dt.time(min(hi, 23), 59 if hi >= 24 else 0)


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


def clear_all_bookings() -> int:
    """Delete all bookings. Returns how many were removed."""
    return _exec(delete(bookings))


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


def default_slot(is_today: bool, slot_min: dt.time, slot_max: dt.time) -> tuple[dt.time, dt.time]:
    """Default (start, end) for the booking slider — 'now' rounded up if today."""
    if not is_today:
        start = slot_min
        end = (dt.datetime.combine(now().date(), slot_min) + dt.timedelta(hours=1)).time()
        return start, min(end, slot_max)
    n = now().replace(second=0, microsecond=0)
    rounded = n + dt.timedelta(minutes=(15 - n.minute % 15) % 15)  # up to next quarter
    lo = dt.datetime.combine(n.date(), slot_min, tzinfo=TIMEZONE)
    # leave room for a 1 h slot before the max
    hi = dt.datetime.combine(n.date(), slot_max, tzinfo=TIMEZONE) - dt.timedelta(hours=1)
    start = min(max(rounded, lo), hi)
    end = min(start + dt.timedelta(hours=1),
              dt.datetime.combine(n.date(), slot_max, tzinfo=TIMEZONE))
    return start.time(), end.time()


def fmt_day_time(d: dt.datetime) -> str:
    d = d.astimezone(TIMEZONE)
    today = now().date()
    if d.date() == today:
        return d.strftime("%H:%M")
    if d.date() == today + dt.timedelta(days=1):
        return "tomorrow " + d.strftime("%H:%M")
    return d.strftime("%a %d %b %H:%M")


def render_point(point: str, me: str | None, booking_on: bool,
                 eta_choices: dict[str, int | None], claim: dict | None,
                 booking_now: dict | None, upcoming: list[dict],
                 slot_min: dt.time, slot_max: dt.time) -> None:
    in_use = claim is not None or booking_now is not None
    bookings = upcoming

    with st.container(border=True):
        header = st.columns([3, 2])
        with header[0]:
            if not in_use:
                st.markdown(f"### 🟢 {point}")
                st.caption("Free now")
            elif claim is not None:
                st.markdown(f"### 🔴 {point}")
                eta = parse(claim["release_eta"])
                who = "you" if claim["person"] == me else claim["person"]
                if eta:
                    st.caption(f"In use by **{who}** — free ~{fmt_time(eta)}")
                else:
                    st.caption(f"In use by **{who}**")
            else:  # covered by an active booking
                st.markdown(f"### 🔴 {point}")
                who = "you" if booking_now["person"] == me else booking_now["person"]
                st.caption(
                    f"Booked by **{who}** — free ~{fmt_time(parse(booking_now['end_at']))}"
                )

        can_act = me is not None
        with header[1]:
            if not in_use:
                if not can_act:
                    st.button(
                        "⚡ Claim now",
                        key=f"claim-btn-{point}",
                        use_container_width=True,
                        disabled=True,
                        help="Select your name first.",
                    )
                else:
                    with st.popover("⚡ Claim now", use_container_width=True):
                        st.write(f"Claim **{point}** for **{me}**")
                        choice = st.select_slider(
                            "I'll be done in…",
                            options=list(eta_choices),
                            value="1 h",
                            key=f"eta-{point}",
                        )
                        mins = eta_choices[choice]
                        eta_dt = now() + dt.timedelta(minutes=mins) if mins else None
                        if eta_dt:
                            st.caption(f"Frees up around **{fmt_time(eta_dt)}**")
                        if st.button("Confirm claim", key=f"claim-{point}", type="primary"):
                            claim_point(point, me, eta_dt)
                            st.rerun()
            elif claim is not None:
                if claim["person"] == me:
                    if st.button(
                        "✅ Release", key=f"rel-{point}", use_container_width=True
                    ):
                        release_point(claim["id"])
                        st.rerun()
                else:
                    st.button(
                        "Release",
                        key=f"rel-{point}",
                        use_container_width=True,
                        disabled=True,
                        help=f"Only {claim['person']} can release this.",
                    )
            else:  # in use because of an active booking
                owner = booking_now["person"]
                if owner == me:
                    if st.button(
                        "✅ End early", key=f"rel-{point}", use_container_width=True
                    ):
                        cancel_booking(booking_now["id"])
                        st.rerun()
                else:
                    st.button(
                        "Release",
                        key=f"rel-{point}",
                        use_container_width=True,
                        disabled=True,
                        help=f"Booked by {owner}.",
                    )

        # Bookings
        if bookings:
            st.markdown("**Upcoming bookings**")
            for b in bookings:
                cols = st.columns([5, 1])
                start, end = parse(b["start_at"]), parse(b["end_at"])
                mine = b["person"] == me
                label = "**you**" if mine else b["person"]
                cols[0].write(
                    f"• {fmt_day_time(start)} – {fmt_time(end)} — {label}"
                )
                if mine:
                    if cols[1].button("✕", key=f"cancel-{b['id']}", help="Cancel"):
                        cancel_booking(b["id"])
                        st.rerun()

        if not booking_on:
            return
        if not can_act:
            st.button(
                "📅 Book a slot today",
                key=f"book-btn-{point}",
                use_container_width=True,
                disabled=True,
                help="Select your name first.",
            )
            return
        with st.popover("📅 Book a slot today", use_container_width=True):
            st.write(f"Book **{point}** for **{me}** — today only")
            day = now().date()
            start_t, end_t = st.slider(
                "Drag to set the time slot",
                min_value=slot_min,
                max_value=slot_max,
                value=default_slot(True, slot_min, slot_max),
                step=dt.timedelta(minutes=15),
                format="HH:mm",
                key=f"bslot-{point}",
            )
            if st.button("Add booking", key=f"addbook-{point}", type="primary"):
                start = dt.datetime.combine(day, start_t, tzinfo=TIMEZONE)
                end = dt.datetime.combine(day, end_t, tzinfo=TIMEZONE)
                if end <= start:
                    st.error("End time must be after start time.")
                elif end <= now():
                    st.error("That slot is in the past.")
                else:
                    conflict = booking_conflict(point, start, end)
                    if conflict:
                        st.error(
                            f"Overlaps with {conflict['person']}'s booking "
                            f"({fmt_time(parse(conflict['start_at']))}–"
                            f"{fmt_time(parse(conflict['end_at']))})."
                        )
                    else:
                        add_booking(point, me, start, end)
                        st.rerun()


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

    # One batched load per rerun instead of dozens of per-point queries.
    board = load_board()
    s = board["settings"]
    points = board["points"]
    people = board["people"]
    claims = board["claims"]
    bookings_by_point = board["bookings_by_point"]
    now_iso = board["now_iso"]

    booking_on = s.get("booking_enabled", "1") == "1"
    refresh = _setting_int(s, "refresh_seconds", 30, 5, 3600)
    eta_choices = build_eta_choices(_setting_int(s, "max_claim_hours", 8, 1, 24))
    slot_min, slot_max = slot_bounds_from(s)

    # Per-point state, computed in Python from the batched snapshot.
    states: dict[str, tuple] = {}
    free_now: set[str] = set()
    for p in points:
        claim = claims.get(p)
        upcoming = bookings_by_point.get(p, [])
        booking_now = active_booking_of(upcoming, now_iso) if claim is None else None
        states[p] = (claim, booking_now, upcoming)
        if claim is None and booking_now is None:
            free_now.add(p)

    # Auto-refresh the whole board (keeps session/state, unlike a browser reload).
    st_autorefresh(interval=refresh * 1000, key="autorefresh")

    st.title("⚡ plugPIX — EV charge points")
    n = now()
    st.caption(
        f"🕐 {n.strftime('%A %d %B %Y — %H:%M:%S')} "
        f"· auto-refreshes every {refresh}s"
    )

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
            claim, booking_now, upcoming = states[point]
            with cols[j]:
                render_point(
                    point, me, booking_on, eta_choices,
                    claim, booking_now, upcoming, slot_min, slot_max,
                )


if __name__ == "__main__":
    main()
