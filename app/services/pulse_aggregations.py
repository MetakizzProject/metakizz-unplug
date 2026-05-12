"""Pulse data aggregations.

Centralizes the SQL/computation for every Pulse dashboard so the route
handlers stay thin (HTTP → call helper → render). Each function returns
a JSON-serializable dict shaped exactly for the template that consumes
it. Add a new dashboard panel = add a function here.

Caching strategy: in-process memo with short TTL via functools.lru_cache
patterns is intentionally NOT used yet — Pulse pages will be hit by one
operator (Álvaro) so even uncached queries are fine. Add caching only
when a specific aggregation gets slow.
"""
from __future__ import annotations

# Stubs for each page — filled in during the page-specific iterations.
# Keeping them here so route handlers can already import them, and the
# acquisition/conversion/revenue/activity templates have a single place
# to look for "where does this number come from".


def acquisition_summary() -> dict:
    """KPIs for /admin/pulse/acquisition. Returns:
      {
        "total_leads": int,
        "delta_7d": int,
        "source_breakdown": [{key, label, count, share_pct, sparkline_14d}, ...],
        "funnel_by_source": [{source, signups, watched, deposit, full}, ...],
        "top_referrers": [{name, email, count}, ...],
        "country_distribution": [{country, count}, ...],
        "timeline_30d_by_source": {labels: [...], series: [{source, values}]},
      }
    """
    return {}


def conversion_summary() -> dict:
    """KPIs for /admin/pulse/conversion. Returns:
      {
        "funnel": [{label, count, drop_pct}, ...],
        "temperature_dist": [{bucket, count, pct, color}, ...],
        "avg_time_to_deposit_days": float | None,
        "avg_time_to_full_days":    float | None,
        "queue": {burning_uncontacted: int, hot_uncontacted: int, contacted_today: int},
        "cohorts": {weeks: [...], rows: [{week, signups, day7, day14, day30}]},
      }
    """
    return {}


def revenue_summary() -> dict:
    """KPIs for /admin/pulse/revenue. Returns:
      {
        "cash_collected_net_cents": int,
        "total_billed_cents":       int,
        "deposits_in_cents":        int,
        "full_in_cents":            int,
        "refunds_out_cents":        int,
        "deposit_to_full_pct":      float,
        "revenue_by_program":   [{label, cents}, ...],
        "revenue_by_plan":      [{label, cents}, ...],
        "timeline_30d":         {labels: [...], values: [...]},
      }
    """
    return {}


def activity_summary() -> dict:
    """KPIs for /admin/pulse/activity. Returns:
      {
        "last_24h": {signups: int, deposits: int, full_purchases: int,
                     emails_sent: int, opens: int, clicks: int},
        "outreach_today": {contacted: int, in_queue: int},
        "latest_webinar": {name: str, attendees: int, avg_duration_min: float} | None,
      }
    """
    return {}


def activity_feed(limit: int = 30) -> list:
    """Returns a chronologically-ordered list of recent events for the
    real-time feed on /admin/pulse/activity. Each event:
      {"ts": iso, "type": "signup|deposit|full_purchase|email_open|email_click",
       "actor": "name", "detail": "human-readable"}
    """
    return []
