"""Pulse — curated executive dashboard for MetaKizz.

A parallel admin section at /admin/pulse with a clean information
architecture (4 pages: acquisition / conversion / revenue / activity)
that coexists with the existing /admin/* surface. The legacy admin
keeps working untouched; this is the "what an operator actually wants
to see" view, built iteratively with self-evaluation.

Boundary: this module imports helpers from existing admin/services
code but never modifies them. If a query is missing, it gets added
to app/services/pulse_aggregations.py, not to existing files.

How to add a page: define the route here, add a template in
app/templates/admin_pulse/, push the data via pulse_aggregations.py.
Then add the nav link in templates/admin_pulse/_base.html.
"""
from flask import Blueprint, render_template, redirect, url_for, session, request, jsonify

admin_pulse_bp = Blueprint(
    "admin_pulse",
    __name__,
    url_prefix="/admin/pulse",
)


@admin_pulse_bp.before_request
def require_admin():
    """Same auth model as the legacy admin blueprint — session-based.

    Static files come through Flask's implicit `static` endpoint and
    must NOT be redirected (otherwise CSS/JS fail to load right after
    login). Mirrors the bypass pattern used by admin.require_admin().
    """
    if request.endpoint and request.endpoint.startswith("static"):
        return
    if not session.get("is_admin"):
        return redirect(url_for("admin.login"))


def _pulse_layout_context():
    """Sidebar context shared across all pulse pages.

    Intentionally imports `_admin_layout_context()` from admin.py — this
    couples Pulse to the legacy layout helper so the sidebar stays in
    sync when nav metadata (countdowns, badges, route enable list)
    changes. If Pulse ever needs to diverge, copy the helper here and
    take ownership of its return shape.
    """
    from app.routes.admin import _admin_layout_context
    ctx = _admin_layout_context()
    ctx["pulse_pages"] = [
        ("acquisition", "Acquisition", "📈"),
        ("conversion",  "Conversion",  "🎯"),
        ("revenue",     "Revenue",     "💎"),
        ("activity",    "Activity",    "⚡"),
    ]
    return ctx


@admin_pulse_bp.route("/")
def index():
    """Pulse landing — redirect to the first page (acquisition)."""
    return redirect(url_for("admin_pulse.acquisition"))


@admin_pulse_bp.route("/acquisition")
def acquisition():
    """Where leads come from — source attribution + per-source funnel."""
    return render_template(
        "admin_pulse/acquisition.html",
        active_section="pulse",
        pulse_active="acquisition",
        page_title="Pulse · Acquisition",
        **_pulse_layout_context(),
    )


@admin_pulse_bp.route("/conversion")
def conversion():
    """Funnel + temperature distribution — how leads turn into buyers."""
    return render_template(
        "admin_pulse/conversion.html",
        active_section="pulse",
        pulse_active="conversion",
        page_title="Pulse · Conversion",
        **_pulse_layout_context(),
    )


@admin_pulse_bp.route("/revenue")
def revenue():
    """Cash collected, billed, deposit→full conversion, revenue mix."""
    return render_template(
        "admin_pulse/revenue.html",
        active_section="pulse",
        pulse_active="revenue",
        page_title="Pulse · Revenue",
        **_pulse_layout_context(),
    )


@admin_pulse_bp.route("/activity")
def activity():
    """Operational pulse — last 24h events, outreach status, live feed."""
    return render_template(
        "admin_pulse/activity.html",
        active_section="pulse",
        pulse_active="activity",
        page_title="Pulse · Activity",
        **_pulse_layout_context(),
    )


# ─── JSON polling endpoints (filled in per-page iterations) ──────────

@admin_pulse_bp.route("/activity.json")
def activity_json():
    """Recent activity feed — refreshed every ~60s by the page."""
    return jsonify({"events": [], "ts": None})
