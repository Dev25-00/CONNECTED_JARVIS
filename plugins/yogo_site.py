"""
yogo.work — what the public site shows, as figures Jarvis can talk about.

Reads the same public API the yogo.work front end calls for any visitor (no
account, no token): server health, the offer catalogue, client reviews and the
service categories. It never logs in, so it can only report what is public.
New accounts and real revenue sit behind authentication and are listed under
"not_public" in the result, so the model says so instead of inventing a figure.

Also feeds the morning brief (main.py → brief_snapshot()).
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

try:
    import requests
except Exception:          # in requirements.txt; never fatal at import
    requests = None

from memory.config_manager import get_plugin_config

_NS = "yogo_site"
_DEFAULT_BASE = "https://yogo.work/api/"
_TIMEOUT = 10
_CACHE_SECONDS = 60      # one question often asks for several periods in a row
_PAGE = 100
_MAX_PAGES = 50

_PERIODS = (
    "today", "yesterday", "this_week", "last_week", "this_month", "last_month",
    "this_year", "last_7_days", "last_30_days", "custom",
)

PLUGIN = {
    "name": "yogo_site",
    "description": (
        "Live figures of the user's own website yogo.work (Yogo, a marketplace "
        "of craftsmen / artisans), read from its public backend: is the site up, "
        "number of offers (services) published and how many were created in a "
        "period, client requests (demandes) created per period / service / city "
        "and by status, public posts, DIY tutorials, validated orders, client "
        "reviews and ratings, best-selling offers, deployed backend version. Use "
        "it for ANY question about Yogo ('combien de services créés ce mois', "
        "'combien de demandes hier', 'combien de commandes', "
        "'le site est en ligne ?', 'les derniers avis'). Call it once per period "
        "asked about. Do NOT use web_search or browser_control for this."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "period": {
                "type": "STRING",
                "enum": list(_PERIODS),
                "description": "Period for the 'in this period' figures. Use "
                               "'custom' with date_from/date_to for anything else.",
            },
            "date_from": {
                "type": "STRING",
                "description": "Only for period=custom: first day, YYYY-MM-DD.",
            },
            "date_to": {
                "type": "STRING",
                "description": "Only for period=custom: last day, YYYY-MM-DD (inclusive).",
            },
        },
        "required": ["period"],
    },
}


def _test_connection(values: dict):
    cfg = {**get_plugin_config(_NS), **(values or {})}
    try:
        snap = _snapshot(cfg, use_cache=False)
        return True, (f"OK — site {snap['health']}, {len(snap['offers'])} offres, "
                      f"{len(snap['demandes'])} demandes, "
                      f"{snap['reviews_stats'].get('totalCount', '?')} avis")
    except Exception as e:
        return False, str(e)[:200]


PLUGIN_SETTINGS = {
    "namespace": _NS,
    "title": "YOGO.WORK",
    "fields": [
        {"key": "base_url", "label": "API URL", "type": "text", "default": _DEFAULT_BASE},
    ],
    "action": {"label": "TEST", "run": _test_connection},
}


# ── Periods (local time, end-exclusive) ──────────────────────────────────────

def _day(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def _resolve(period: str, date_from: str = "", date_to: str = ""):
    """(label, start, end, prev_start, prev_end). Calendar months and years are
    compared with the previous unit up to the same point."""
    now = datetime.now()
    today = now.date()
    if period == "today":
        s = _day(today)
        return "today", s, now, s - timedelta(days=1), now - timedelta(days=1)
    if period == "yesterday":
        s = _day(today - timedelta(days=1))
        return "yesterday", s, _day(today), s - timedelta(days=1), s
    if period == "this_week":
        s = _day(today - timedelta(days=today.weekday()))
        return "this week (Mon–now)", s, now, s - timedelta(days=7), now - timedelta(days=7)
    if period == "last_week":
        e = _day(today - timedelta(days=today.weekday()))
        s = e - timedelta(days=7)
        return "last week", s, e, s - timedelta(days=7), s
    if period == "this_month":
        s = _day(today.replace(day=1))
        ps = _day(_add_months(today, -1))
        return "this month so far", s, now, ps, min(ps + (now - s), s)
    if period == "last_month":
        e = _day(today.replace(day=1))
        s = _day(_add_months(today, -1))
        return "last month", s, e, _day(_add_months(today, -2)), s
    if period == "this_year":
        s = _day(date(today.year, 1, 1))
        ps = _day(date(today.year - 1, 1, 1))
        return "this year so far", s, now, ps, min(ps + (now - s), s)
    if period in ("last_7_days", "last_30_days"):
        n = 7 if period == "last_7_days" else 30
        s = now - timedelta(days=n)
        return f"last {n} days", s, now, s - timedelta(days=n), s
    if period == "custom":
        d1 = date.fromisoformat((date_from or "").strip())
        d2 = date.fromisoformat((date_to or date_from or "").strip())
        if d2 < d1:
            d1, d2 = d2, d1
        s, e = _day(d1), _day(d2 + timedelta(days=1))
        return f"{d1.isoformat()} to {d2.isoformat()}", s, e, s - (e - s), s
    raise ValueError(f"unknown period '{period}'")


# ── Public API ───────────────────────────────────────────────────────────────

def _get(base: str, path: str, ok_codes=(200,)):
    r = requests.get(base + path, timeout=_TIMEOUT,
                     headers={"Accept": "application/json",
                              "User-Agent": "Jarvis-Yogo/1.0"})
    if r.status_code not in ok_codes:
        raise RuntimeError(f"{path} → HTTP {r.status_code}")
    return r.json()


def _all_offers(base: str) -> list:
    out = []
    for page in range(_MAX_PAGES):
        d = _get(base, f"OFFRES/CATALOGUE?page={page}&size={_PAGE}&sort=recent")
        out.extend(d.get("content") or [])
        if d.get("last", True):
            break
    return out


def _paged(base: str, path_for_page, size: int = _PAGE) -> list:
    """Every item of a Spring-style page envelope ({content, last})."""
    out = []
    for page in range(_MAX_PAGES):
        d = _get(base, path_for_page(page, size))
        items = d.get("content") or []
        out.extend(items)
        if d.get("last", True) or not items:
            break
    return out


def _all_reviews(base: str) -> list:
    # TestimonialsController#feed only knows "offer" (default) and "demande" —
    # any other type silently returns the offer reviews, so both are asked for.
    out = []
    for kind in ("offer", "demande"):
        out.extend(_paged(base, lambda p, s, k=kind: f"TESTIMONIALS/FEED?type={k}&page={p}&size={min(s, 30)}", 30))
    return out


def _all_demandes(base: str) -> list:
    # DEMANDES/GET_ALL/{status} does not filter by the status it is given (every
    # status returns the same rows), so all four are fetched and de-duplicated;
    # each row's own demande_status is what gets counted.
    seen: dict = {}
    for status in (0, 1, 2, 3):
        for page in range(_MAX_PAGES):
            rows = _get(base, f"DEMANDES/GET_ALL/{status}/{page}/{_PAGE}")
            if not isinstance(rows, list) or not rows:
                break
            for r in rows:
                seen[r.get("id")] = r
            if len(rows) < _PAGE:
                break
    return list(seen.values())


def _all_posts(base: str) -> list:
    # Public feed as an anonymous viewer (id 0): PUBLIC posts only.
    return _paged(base, lambda p, s: f"POSTS/FEED/0/{p}/{min(s, 30)}", 30)


def _all_diy(base: str) -> list:
    return _paged(base, lambda p, s: f"DIY_TUTORIALS/FEED?page={p}&size={min(s, 50)}", 50)


def _build_info(base: str) -> dict:
    try:
        return _get(base, "BUILD_INFO")
    except Exception:
        return {}


_cache: dict = {}
_cache_lock = threading.Lock()


def _snapshot(cfg: dict, use_cache: bool = True) -> dict:
    if requests is None:
        raise RuntimeError("the 'requests' package is not installed")
    base = (cfg.get("base_url") or _DEFAULT_BASE).strip()
    if not base.endswith("/"):
        base += "/"
    with _cache_lock:
        hit = _cache.get(base)
        if use_cache and hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
            return hit[1]

    with ThreadPoolExecutor(max_workers=9) as ex:
        f_health   = ex.submit(_get, base, "actuator/health")
        f_build    = ex.submit(_build_info, base)
        f_offers   = ex.submit(_all_offers, base)
        f_rstats   = ex.submit(_get, base, "TESTIMONIALS/STATS")
        f_reviews  = ex.submit(_all_reviews, base)
        f_cats     = ex.submit(_get, base, "SERVICES/GET_ALL")
        f_demandes = ex.submit(_all_demandes, base)
        f_posts    = ex.submit(_all_posts, base)
        f_diy      = ex.submit(_all_diy, base)

        try:
            health = (f_health.result().get("status") or "UNKNOWN")
        except Exception as e:
            health = f"DOWN ({e})"
        snap = {
            "health": health,
            "build": f_build.result(),
            "offers": f_offers.result(),
            "reviews_stats": f_rstats.result(),
            "reviews": f_reviews.result(),
            "categories": f_cats.result(),
            "demandes": f_demandes.result(),
            "posts": f_posts.result(),
            "diy": f_diy.result(),
        }
    with _cache_lock:
        _cache[base] = (time.monotonic(), snap)
    return snap


# ── Figures ──────────────────────────────────────────────────────────────────

def _local(ts) -> datetime | None:
    """ISO string or epoch milliseconds → naive local datetime."""
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().replace(tzinfo=None)
    except Exception:
        return None


def _in(ts, s: datetime, e: datetime) -> bool:
    t = _local(ts)
    return t is not None and s <= t < e


# demandes.demande_status, as used by the backend (see demandesController).
_DEMANDE_STATUS = {0: "open", 1: "in_progress", 2: "status_2", 3: "closed"}


def _count_by(items, key) -> dict:
    out: dict[str, int] = {}
    for it in items:
        k = key(it) or "?"
        out[k] = out.get(k, 0) + 1
    return out


def _period_figures(snap: dict, s: datetime, e: datetime) -> dict:
    new_offers = [o for o in snap["offers"] if _in(o.get("date_creation"), s, e)]
    new_reviews = [r for r in snap["reviews"] if _in(r.get("createdAt"), s, e)]
    new_demandes = [d for d in snap["demandes"] if _in(d.get("date_creation"), s, e)]
    new_posts = [p for p in snap["posts"] if _in(p.get("date_creation"), s, e)]
    new_diy = [t for t in snap["diy"] if _in(t.get("created_at"), s, e)]
    return {
        "offers_created": len(new_offers),
        "offers_created_titles": [o.get("titre") for o in new_offers][:10],
        "client_requests_created": len(new_demandes),
        "client_requests_express": sum(1 for d in new_demandes if d.get("mode_express") == 1),
        "client_requests_by_service": _count_by(new_demandes, lambda d: d.get("service_name")),
        "client_requests_by_city": _count_by(new_demandes, lambda d: d.get("ville")),
        "public_posts": len(new_posts),
        "diy_tutorials_created": len(new_diy),
        "new_reviews": len(new_reviews),
        "new_reviews_avg_rating": (round(sum(r.get("rating") or 0 for r in new_reviews)
                                         / len(new_reviews), 2) if new_reviews else None),
    }


def _overall(snap: dict) -> dict:
    offers = snap["offers"]
    now = datetime.now()
    orders = sum(int(o.get("validated_order_count") or 0) for o in offers)
    est = sum(int(o.get("validated_order_count") or 0) * float(o.get("starting_price") or 0)
              for o in offers)
    by_cat: dict[str, int] = {}
    for o in offers:
        k = o.get("service_name") or "?"
        by_cat[k] = by_cat.get(k, 0) + 1
    top = sorted(offers, key=lambda o: int(o.get("validated_order_count") or 0), reverse=True)[:3]
    artisans = {(o.get("artisan") or {}).get("id") for o in offers} - {None}
    boosted = [o for o in offers
               if (_local(o.get("boost_enddate")) or datetime.min) > now]
    latest_reviews = sorted(snap["reviews"], key=lambda r: r.get("createdAt") or 0,
                            reverse=True)[:3]
    rs = snap["reviews_stats"] or {}
    build = snap.get("build") or {}
    demandes = snap["demandes"]
    return {
        "site_status": snap["health"],
        "backend_build": build.get("build"),
        "server_started_at": build.get("started_at"),
        "client_requests_total": len(demandes),
        "client_requests_by_status": _count_by(
            demandes, lambda d: _DEMANDE_STATUS.get(d.get("demande_status"), str(d.get("demande_status")))),
        "client_requests_boosted_now": sum(
            1 for d in demandes if (_local(d.get("boosted_enddate")) or datetime.min) > now),
        "public_posts_total": len(snap["posts"]),
        "diy_tutorials_published": len(snap["diy"]),
        "offers_published": len(offers),
        "artisans_with_offers": len(artisans),
        "offers_boosted_now": len(boosted),
        "offers_by_category": by_cat,
        "validated_orders_all_time": orders,
        "estimated_order_value_eur_all_time": round(est, 2),
        "estimated_order_value_note": ("ESTIMATE ONLY: validated orders × each offer's "
                                       "starting price, all time. Not real revenue."),
        "best_selling_offers": [{"title": o.get("titre"),
                                 "validated_orders": o.get("validated_order_count"),
                                 "rating": o.get("avg_rating"),
                                 "starting_price_eur": o.get("starting_price")} for o in top],
        "reviews_total": rs.get("totalCount"),
        "reviews_avg_rating": rs.get("avgGlobal"),
        "latest_reviews": [{"rating": r.get("rating"), "text": r.get("text"),
                            "on": r.get("context"),
                            "date": (_local(r.get("createdAt")) or now).strftime("%Y-%m-%d")}
                           for r in latest_reviews],
        "service_categories": len(snap["categories"] or []),
    }


_NOT_PUBLIC = ["new accounts / sign-ups", "real sales revenue in € (PayPal, YoCredits, boosts)",
               "orders per period", "private messages", "followers-only posts"]


def run(parameters: dict, player=None, session_memory=None) -> str:
    period = (parameters.get("period") or "today").strip().lower()
    try:
        label, s, e, ps, pe = _resolve(period, parameters.get("date_from", ""),
                                       parameters.get("date_to", ""))
        snap = _snapshot(get_plugin_config(_NS))
        out = {
            "site": "yogo.work",
            "period": label,
            "from": s.strftime("%Y-%m-%d %H:%M"),
            "to": e.strftime("%Y-%m-%d %H:%M"),
            "in_period": _period_figures(snap, s, e),
            "previous_period": _period_figures(snap, ps, pe),
            "overall": _overall(snap),
            "not_public": _NOT_PUBLIC,
            "instructions": (
                "Answer only what the user asked, in their language, with exact "
                "numbers. If they ask for something listed in not_public, say "
                "plainly it is not available from the public site — never guess. "
                "Present the estimated order value only as an estimate."
            ),
        }
        if player:
            try:
                player.write_log(f"SYS: yogo.work — {label}")
            except Exception:
                pass
        return json.dumps(out, ensure_ascii=False, default=str)
    except Exception as ex:
        print(f"[yogo_site] {ex}")
        return (f"yogo.work could not be reached: {ex}. Tell the user the site's "
                f"backend is not answering.")


def brief_snapshot() -> dict:
    """Figures for the morning brief: yesterday, today so far, and overall.
    Raises if the site cannot be reached — the brief reports that instead."""
    snap = _snapshot(get_plugin_config(_NS), use_cache=False)
    now = datetime.now()
    today = _day(now.date())
    return {
        "site": "yogo.work",
        "yesterday": _period_figures(snap, today - timedelta(days=1), today),
        "today_so_far": _period_figures(snap, today, now),
        "last_7_days": _period_figures(snap, now - timedelta(days=7), now),
        "overall": _overall(snap),
    }
