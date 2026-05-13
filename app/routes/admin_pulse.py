"""Pulse — curated executive dashboard for MetaKizz.

A parallel admin section at /admin/pulse with a clean information
architecture (4 pages: acquisition / conversion / revenue / activity)
that coexists with the existing /admin/* surface. The legacy admin
keeps working untouched; this is the "what an operator actually wants
to see" view, built iteratively with self-evaluation.

═══════════════════════════════════════════════════════════════════════
INFORMATION ARCHITECTURE
═══════════════════════════════════════════════════════════════════════

  /admin/pulse                  → redirects to /acquisition
  /admin/pulse/acquisition      → Where do leads come from
                                  (KPIs · sources · timeline · top
                                   referrers · countries · per-source
                                   funnel)
  /admin/pulse/conversion       → How do leads turn into buyers
                                  (KPIs · launch funnel · temperature
                                   distribution · action queue ·
                                   weekly cohorts)
  /admin/pulse/revenue          → Cash collected & revenue mix
                                  (NET cash · reconciliation · by
                                   program · by payment plan · 30d
                                   timeline)
  /admin/pulse/activity         → Operational pulse (last 24h)
                                  (KPI strip · live event feed with
                                   60s polling)
  /admin/pulse/activity.json    → Live feed JSON polled by activity
                                  page (NOT cached — always fresh)

═══════════════════════════════════════════════════════════════════════
BOUNDARY DISCIPLINE
═══════════════════════════════════════════════════════════════════════

This module imports helpers from existing admin/services code but
NEVER modifies them. Only two files outside the pulse module were
touched at boot time:
  - app/app.py        · 1 line to register admin_pulse_bp
  - admin_base.html   · 1 link added to the sidebar

If a query is missing, add it to app/services/pulse_aggregations.py,
not to existing services. If a CSS rule is needed, put it in
app/static/css/pulse.css scoped under .pulse-shell.

═══════════════════════════════════════════════════════════════════════
HOW TO ADD A NEW PAGE
═══════════════════════════════════════════════════════════════════════

  1. Add an entry to `_pulse_layout_context()` `pulse_pages` list:
       ("retention", "Retention", "📊"),
  2. Define an aggregation function in pulse_aggregations.py:
       @_cached(ttl_seconds=60)
       def retention_summary() -> dict: ...
  3. Define a route here:
       @admin_pulse_bp.route("/retention")
       def retention():
           from app.services.pulse_aggregations import retention_summary
           return render_template(
               "admin_pulse/retention.html",
               active_section="pulse", pulse_active="retention",
               page_title="Pulse · Retention",
               summary=retention_summary(),
               **_pulse_layout_context(),
           )
  4. Create the template `app/templates/admin_pulse/retention.html`:
       {% extends "admin_pulse/_base.html" %}
       {% block pulse_content %} ... {% endblock %}
  5. Reuse existing primitives from pulse.css (.pulse-kpi, .pulse-card,
     .pulse-mix-list, .pulse-funnel-list, etc) before adding new ones.

═══════════════════════════════════════════════════════════════════════
CACHING
═══════════════════════════════════════════════════════════════════════

Each aggregation function is wrapped in `@_cached(ttl_seconds=N)`:
  - acquisition_summary  · 60s
  - conversion_summary   · 60s
  - revenue_summary      · 60s
  - activity_summary     · 15s  (KPIs only; feed JSON stays live)

Cache is per-worker in-process. Multiple Gunicorn workers warm
independently. Call `<func>.cache_clear()` to invalidate on demand.

═══════════════════════════════════════════════════════════════════════
HOW TO ROLL THIS BACK (KILL SWITCH)
═══════════════════════════════════════════════════════════════════════

Remove ONE line in app/app.py:
    app.register_blueprint(admin_pulse_bp)

That's the kill switch. Files in app/routes/admin_pulse.py and
app/templates/admin_pulse/ become dead code but harmless. Legacy
admin keeps working untouched.
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
    from app.services.pulse_aggregations import acquisition_summary
    return render_template(
        "admin_pulse/acquisition.html",
        active_section="pulse",
        pulse_active="acquisition",
        page_title="Pulse · Acquisition",
        summary=acquisition_summary(),
        **_pulse_layout_context(),
    )


@admin_pulse_bp.route("/conversion")
def conversion():
    """Funnel + temperature distribution — how leads turn into buyers."""
    from app.services.pulse_aggregations import conversion_summary
    return render_template(
        "admin_pulse/conversion.html",
        active_section="pulse",
        pulse_active="conversion",
        page_title="Pulse · Conversion",
        summary=conversion_summary(),
        **_pulse_layout_context(),
    )


@admin_pulse_bp.route("/revenue")
def revenue():
    """Cash collected, billed, deposit→full conversion, revenue mix."""
    from app.services.pulse_aggregations import revenue_summary
    return render_template(
        "admin_pulse/revenue.html",
        active_section="pulse",
        pulse_active="revenue",
        page_title="Pulse · Revenue",
        summary=revenue_summary(),
        **_pulse_layout_context(),
    )


@admin_pulse_bp.route("/activity")
def activity():
    """Operational pulse — last 24h events, outreach status, live feed."""
    from app.services.pulse_aggregations import activity_summary, activity_feed
    return render_template(
        "admin_pulse/activity.html",
        active_section="pulse",
        pulse_active="activity",
        page_title="Pulse · Activity",
        summary=activity_summary(),
        feed=activity_feed(limit=30),
        **_pulse_layout_context(),
    )


# ─── JSON polling endpoints ──────────────────────────────────────────

@admin_pulse_bp.route("/activity.json")
def activity_json():
    """Recent activity feed — refreshed every ~60s by the page."""
    from datetime import datetime, timezone
    from app.services.pulse_aggregations import activity_feed
    return jsonify({
        "events": activity_feed(limit=30),
        "ts": datetime.now(timezone.utc).isoformat(),
    })
