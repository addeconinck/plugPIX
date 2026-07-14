"""plugPIX — a very light EV charge-point status & booking board.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
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
# Storage
# --------------------------------------------------------------------------- #


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        # A live "claim": someone is charging right now on `point`.
        # released_at IS NULL  => still in use.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                point         TEXT    NOT NULL,
                person        TEXT    NOT NULL,
                claimed_at    TEXT    NOT NULL,
                release_eta   TEXT,
                released_at   TEXT
            )
            """
        )
        # A future booking of a time slot on `point`.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                point     TEXT NOT NULL,
                person    TEXT NOT NULL,
                start_at  TEXT NOT NULL,
                end_at    TEXT NOT NULL
            )
            """
        )
        # The people who may use the app (managed from the sidebar).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS people (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                name  TEXT NOT NULL UNIQUE,
                plate TEXT NOT NULL DEFAULT ''
            )
            """
        )
        empty = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0
        if empty:
            for name, plate in DEFAULT_PEOPLE:
                conn.execute(
                    "INSERT OR IGNORE INTO people (name, plate) VALUES (?, ?)",
                    (name, plate),
                )
        # The charge points (managed from the Admin page).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS points (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT NOT NULL UNIQUE,
                position INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        if conn.execute("SELECT COUNT(*) FROM points").fetchone()[0] == 0:
            for i, name in enumerate(DEFAULT_POINTS):
                conn.execute(
                    "INSERT OR IGNORE INTO points (name, position) VALUES (?, ?)",
                    (name, i),
                )
        # Key/value app settings (managed from the Admin page).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )


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


# ---- live status ---------------------------------------------------------- #


def active_claim(point: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM live WHERE point = ? AND released_at IS NULL "
            "ORDER BY claimed_at DESC LIMIT 1",
            (point,),
        ).fetchone()


def claim_point(point: str, person: str, release_eta: dt.datetime | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO live (point, person, claimed_at, release_eta) "
            "VALUES (?, ?, ?, ?)",
            (point, person, iso(now()), iso(release_eta) if release_eta else None),
        )


def release_point(claim_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE live SET released_at = ? WHERE id = ? AND released_at IS NULL",
            (iso(now()), claim_id),
        )


# ---- bookings ------------------------------------------------------------- #


def upcoming_bookings(point: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM bookings WHERE point = ? AND end_at >= ? "
            "ORDER BY start_at",
            (point, iso(now())),
        ).fetchall()


def active_booking(point: str) -> sqlite3.Row | None:
    """The booking whose slot covers 'now', if any (start <= now < end)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM bookings WHERE point = ? AND start_at <= ? AND end_at > ? "
            "ORDER BY start_at LIMIT 1",
            (point, iso(now()), iso(now())),
        ).fetchone()


def is_free(point: str) -> bool:
    """A point is free when nobody is charging now and no booking covers now."""
    return active_claim(point) is None and active_booking(point) is None


def booking_conflict(point: str, start: dt.datetime, end: dt.datetime) -> sqlite3.Row | None:
    """Return an overlapping booking on the same point, if any."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM bookings WHERE point = ? AND start_at < ? AND end_at > ? "
            "LIMIT 1",
            (point, iso(end), iso(start)),
        ).fetchone()


def add_booking(point: str, person: str, start: dt.datetime, end: dt.datetime) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bookings (point, person, start_at, end_at) VALUES (?, ?, ?, ?)",
            (point, person, iso(start), iso(end)),
        )


def cancel_booking(booking_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))


# ---- people --------------------------------------------------------------- #


def list_people() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM people ORDER BY name COLLATE NOCASE").fetchall()


def add_person(name: str, plate: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO people (name, plate) VALUES (?, ?)", (name.strip(), plate)
        )


def remove_person(person_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM people WHERE id = ?", (person_id,))


def plate_of(name: str) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT plate FROM people WHERE name = ?", (name,)).fetchone()
    return row["plate"] if row else ""


# ---- points --------------------------------------------------------------- #


def list_points() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM points ORDER BY position, id"
        ).fetchall()


def get_points() -> list[str]:
    return [p["name"] for p in list_points()]


def add_point(name: str) -> None:
    with get_conn() as conn:
        pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM points").fetchone()[0]
        conn.execute(
            "INSERT INTO points (name, position) VALUES (?, ?)", (name.strip(), pos)
        )


def rename_point(point_id: int, name: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE points SET name = ? WHERE id = ?", (name.strip(), point_id))


def remove_point(point_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM points WHERE id = ?", (point_id,))


# ---- settings ------------------------------------------------------------- #


def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


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
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE live SET released_at = ? WHERE released_at IS NULL", (iso(now()),)
        )
        return cur.rowcount


def clear_all_bookings() -> int:
    """Delete all bookings. Returns how many were removed."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM bookings")
        return cur.rowcount


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


def default_slot(is_today: bool) -> tuple[dt.time, dt.time]:
    """Default (start, end) for the booking slider — 'now' rounded up if today."""
    slot_min, slot_max = slot_bounds()
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


def render_point(point: str, me: str, booking_on: bool,
                 eta_choices: dict[str, int | None]) -> None:
    claim = active_claim(point)
    booking_now = active_booking(point) if claim is None else None
    in_use = claim is not None or booking_now is not None
    bookings = upcoming_bookings(point)

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
        slot_min, slot_max = slot_bounds()
        with st.popover("📅 Book a slot today", use_container_width=True):
            st.write(f"Book **{point}** for **{me}** — today only")
            day = now().date()
            start_t, end_t = st.slider(
                "Drag to set the time slot",
                min_value=slot_min,
                max_value=slot_max,
                value=default_slot(True),
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

    points = get_points()
    refresh = refresh_seconds()
    booking_on = booking_enabled()
    eta_choices = build_eta_choices(max_claim_hours())

    # Auto-refresh the whole board (keeps session/state, unlike a browser reload).
    st_autorefresh(interval=refresh * 1000, key="autorefresh")

    st.title("⚡ plugPIX — EV charge points")
    n = now()
    st.caption(
        f"🕐 {n.strftime('%A %d %B %Y — %H:%M:%S')} "
        f"· auto-refreshes every {refresh}s"
    )

    # "Alert me when any point frees up" — notify each time a point newly frees.
    free_now = {p for p in points if is_free(p)}
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

    people = list_people()
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
        plate = plate_of(me)
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
            with cols[j]:
                render_point(point, me, booking_on, eta_choices)


if __name__ == "__main__":
    main()
