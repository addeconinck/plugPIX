"""plugPIX — Admin page.

Password-protected settings: enable/disable booking, manage charge points,
tune refresh/claim limits, change the admin password, and run maintenance.
Default password: admin  (change it below!)
"""

from __future__ import annotations

import streamlit as st

import app  # shared storage + settings helpers live in app.py


def require_login() -> bool:
    """Render the password gate. Returns True once authenticated."""
    if st.session_state.get("admin_ok"):
        return True

    st.title("🔒 Admin login")
    with st.form("admin_login"):
        pw = st.text_input("Password", type="password")
        ok = st.form_submit_button("Log in", type="primary")
    if ok:
        if app.check_admin_password(pw):
            st.session_state["admin_ok"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.caption("Default password is **admin** — change it after logging in.")
    return False


def main() -> None:
    st.set_page_config(page_title="plugPIX · Admin", page_icon="🔧", layout="wide")
    st.markdown(app.MOBILE_CSS, unsafe_allow_html=True)
    app.init_db()

    if not require_login():
        return

    top = st.columns([4, 1])
    top[0].title("🔧 plugPIX — Admin")
    if top[1].button("Log out", use_container_width=True):
        st.session_state["admin_ok"] = False
        st.rerun()

    # --------------------------------------------------------------- Features
    st.subheader("Features")
    booking_on = st.toggle(
        "📅 Booking feature enabled",
        value=app.booking_enabled(),
        help="When off, employees can only claim/release live — no time-slot booking.",
    )
    new_booking = "1" if booking_on else "0"
    if new_booking != app.get_setting("booking_enabled"):
        app.set_setting("booking_enabled", new_booking)
        st.toast("Booking " + ("enabled" if booking_on else "disabled"))

    st.divider()

    # ----------------------------------------------------------- Charge points
    st.subheader("Charge points")
    points = app.list_points()
    for p in points:
        c1, c2, c3 = st.columns([4, 1, 1])
        name = c1.text_input(
            "Name", value=p["name"], label_visibility="collapsed", key=f"pn-{p['id']}"
        )
        if c2.button("Save", key=f"psave-{p['id']}", use_container_width=True):
            if name.strip():
                app.rename_point(p["id"], name)
                st.rerun()
        if c3.button("🗑 Remove", key=f"pdel-{p['id']}", use_container_width=True):
            app.remove_point(p["id"])
            st.rerun()

    with st.form("add_point", clear_on_submit=True):
        cols = st.columns([4, 1])
        pname = cols[0].text_input(
            "New point name", placeholder="e.g. Point 5 / Parking A", label_visibility="collapsed"
        )
        add = cols[1].form_submit_button("➕ Add point", use_container_width=True)
    if add:
        name = pname.strip()
        existing = {p["name"].casefold() for p in points}
        if not name:
            st.error("Enter a name for the new point.")
        elif name.casefold() in existing:
            st.error(f"“{name}” already exists.")
        else:
            app.add_point(name)
            st.rerun()

    st.divider()

    # ---------------------------------------------------------------- People
    st.subheader("People")
    st.caption(
        "Employees can add themselves from the “Your name” dropdown on the board. "
        "Use this to remove or fix entries."
    )
    ppl = app.list_people()
    if not ppl:
        st.info("No people yet.")
    for person in ppl:
        c1, c2 = st.columns([5, 1])
        label = f"**{person['name']}**"
        label += f" — {person['plate']}" if person["plate"] else " — _no plate_"
        c1.markdown(label)
        if c2.button("🗑 Remove", key=f"delp-{person['id']}", use_container_width=True):
            app.remove_person(person["id"])
            st.rerun()

    with st.form("admin_add_person", clear_on_submit=True):
        cols = st.columns([3, 3, 1])
        an = cols[0].text_input("Name", label_visibility="collapsed", placeholder="Name")
        ap = cols[1].text_input(
            "Plate", label_visibility="collapsed", placeholder="1-ABC-123 (optional)"
        )
        addp = cols[2].form_submit_button("➕ Add", use_container_width=True)
    if addp:
        name = an.strip()
        norm = app.normalize_plate(ap)
        if not name:
            st.error("Enter a name.")
        elif name.casefold() in {p["name"].casefold() for p in ppl}:
            st.error(f"“{name}” already exists.")
        elif ap.strip() and norm is None:
            st.error("Plate must look like 1-ABC-123 (Belgian format).")
        else:
            app.add_person(name, norm or "")
            st.rerun()

    st.divider()

    # --------------------------------------------------------------- Settings
    st.subheader("Settings")
    with st.form("settings"):
        refresh = st.number_input(
            "Auto-refresh interval (seconds)",
            min_value=5, max_value=600, step=5, value=app.refresh_seconds(),
        )
        max_hours = st.number_input(
            "Max claim duration on the “I'll be done in…” slider (hours)",
            min_value=1, max_value=24, step=1, value=app.max_claim_hours(),
        )
        lo, hi = app.slot_bounds()
        c1, c2 = st.columns(2)
        start_h = c1.number_input(
            "Booking window — earliest hour", min_value=0, max_value=23, value=lo.hour
        )
        end_h = c2.number_input(
            "Booking window — latest hour", min_value=1, max_value=24, value=hi.hour or 22
        )
        saved = st.form_submit_button("💾 Save settings", type="primary")
    if saved:
        if end_h <= start_h:
            st.error("Latest hour must be after earliest hour.")
        else:
            app.set_setting("refresh_seconds", str(int(refresh)))
            app.set_setting("max_claim_hours", str(int(max_hours)))
            app.set_setting("slot_start_hour", str(int(start_h)))
            app.set_setting("slot_end_hour", str(int(end_h)))
            st.success("Settings saved.")

    st.divider()

    # -------------------------------------------------------------- Password
    st.subheader("Change admin password")
    with st.form("password", clear_on_submit=True):
        p1 = st.text_input("New password", type="password")
        p2 = st.text_input("Confirm new password", type="password")
        change = st.form_submit_button("Update password")
    if change:
        if not p1:
            st.error("Password can't be empty.")
        elif p1 != p2:
            st.error("Passwords don't match.")
        else:
            app.set_admin_password(p1)
            st.success("Password updated.")

    st.divider()

    # ---------------------------------------------------------------- Backup
    st.subheader("Backup")
    if app.get_neon_engine() is None:
        st.info(
            "No backup database configured. Data lives only in local SQLite and "
            "will be lost if this host's storage is wiped. Add a Neon/Postgres "
            "connection string in Secrets to enable durable backups."
        )
    else:
        status = app._backup_status
        if status["restored"]:
            st.caption("↩️ Restored from Neon on startup.")
        if status["last_ok"]:
            st.success(f"Last backup: {status['last_ok'].strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            st.caption("No backup yet this session (auto-backups run on every change).")
        if status["last_error"]:
            st.error(f"Last backup error: {status['last_error']}")
        st.caption(
            "Backups to Neon happen automatically (asynchronously) whenever data "
            "changes, and retry on failure. Use the button to force one now."
        )
        if st.button("💾 Backup now", type="primary", use_container_width=True):
            with st.spinner("Mirroring to Neon…"):
                ok = app.mirror_to_neon()
            if ok:
                st.success("Backup complete.")
            else:
                st.error(f"Backup failed: {app._backup_status['last_error']}")

    st.divider()

    # ------------------------------------------------------------ Maintenance
    st.subheader("Maintenance")
    st.caption("Use if the board gets out of sync with reality.")
    m1, m2 = st.columns(2)
    if m1.button("🔌 Release ALL charge points", use_container_width=True):
        freed = app.release_all_points()
        st.success(f"Released {freed} active session(s).")
    if m2.button("🗑 Clear ALL bookings", use_container_width=True):
        removed = app.clear_all_bookings()
        st.success(f"Removed {removed} booking(s).")


if __name__ == "__main__":
    main()
