"""
yogo.work — back-office admin, driven by voice, for the SITE OWNER only.

This never logs in and never touches a password. The owner's account uses
Google sign-in, so there is no password to hold: the owner authenticates
themselves in their browser on yogo.work, and this plugin reuses only the
resulting session token (JWT), which the owner pastes once per session into
⚙ SETUP → "YOGO — ADMIN". Every request is sent as that owner, with their own
rights — this plugin grants no access the owner does not already have.

Two safety properties for every state-changing action:

  1. VERBAL TWO-STEP. An action is never executed on the first call. The model
     must first call it with phase="preview", which returns a spoken recap and
     arms a short-lived pending action. Only a second call with phase="execute"
     — after the user has agreed out loud — actually runs it, and only if it
     matches the pending preview within _CONFIRM_WINDOW seconds. execute without
     a matching preview does nothing.

  2. OWNER-ONLY. Without a valid admin token every call refuses. The token is
     validated against an admin-only read endpoint before the first action.

Read-only operations (listing pending items, searching users) run directly,
with no confirmation — they change nothing.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime, timezone

try:
    import requests
except Exception:          # requests is in requirements.txt; never fatal at import
    requests = None

from memory.config_manager import get_plugin_config

_NS = "yogo_admin"
_DEFAULT_BASE = "https://yogo.work/api/"
_TIMEOUT = 15
_CONFIRM_WINDOW = 180          # seconds a preview stays valid for an execute


# ── Operation catalogue ───────────────────────────────────────────────────────
# Each operation declares: whether it only reads, the HTTP method, a path built
# from the params, the JSON body built from the params, the params it requires,
# and a function that turns the params into a one-line human recap. Nothing
# outside this table can be called — the model cannot reach an arbitrary URL.

def _s(p, k, default=""):
    v = p.get(k)
    return default if v is None else str(v).strip()


def _i(p, k):
    try:
        return int(float(str(p.get(k)).strip()))
    except Exception:
        return None


_OPS = {
    # ---- read-only ----
    "pending_disputes":  dict(read=True, method="GET", path=lambda p: "DISPUTES/ADMIN_PENDING"),
    "bug_reports":       dict(read=True, method="GET", path=lambda p: "BUG_REPORTS/ADMIN_ALL"),
    "pending_documents": dict(read=True, method="GET", path=lambda p: "USER_DOCUMENTS/ADMIN_PENDING"),
    "pending_tutorials": dict(read=True, method="GET", path=lambda p: "DIY_TUTORIALS/ADMIN_PENDING"),
    "pending_payments":  dict(read=True, method="GET", path=lambda p: "PAYMENT_METHODS/ADMIN_PENDING_REQUESTS"),
    "reported_users":    dict(read=True, method="GET", path=lambda p: "SIGNALEMENT/GET_ALL"),
    "search_users":      dict(read=True, method="GET",
                              path=lambda p: f"USERS/ADMIN_LIST_USERS?q={_s(p,'query')}"
                                             f"&page={_i(p,'page') or 0}&size=15"
                                             + (f"&status={_i(p,'account_status')}"
                                                if _i(p, 'account_status') is not None else "")),

    # ---- state-changing (verbal two-step) ----
    "ban_user": dict(
        method="POST", path=lambda p: "USERS/BAN_USER",
        body=lambda p: {"id_user": _i(p, "user_id"), "reason": _s(p, "reason")},
        need=["user_id"],
        recap=lambda p: f"bannir l'utilisateur #{_i(p,'user_id')}"
                        + (f" (motif : {_s(p,'reason')})" if _s(p, 'reason') else "")),
    "unban_user": dict(
        method="POST", path=lambda p: "USERS/UNBAN_USER",
        body=lambda p: {"id_user": _i(p, "user_id")},
        need=["user_id"],
        recap=lambda p: f"réactiver l'utilisateur #{_i(p,'user_id')}"),
    "approve_dispute": dict(
        method="POST", path=lambda p: f"DISPUTES/ADMIN_APPROVE/{_i(p,'target_id')}",
        body=lambda p: {"refund_amount": _i(p, "amount"), "admin_note": _s(p, "admin_note")},
        need=["target_id", "amount"],
        recap=lambda p: f"approuver le litige #{_i(p,'target_id')} et rembourser "
                        f"{_i(p,'amount')} €"),
    "reject_dispute": dict(
        method="POST", path=lambda p: f"DISPUTES/ADMIN_REJECT/{_i(p,'target_id')}",
        body=lambda p: {"admin_note": _s(p, "admin_note")},
        need=["target_id", "admin_note"],
        recap=lambda p: f"rejeter le litige #{_i(p,'target_id')} "
                        f"(motif : {_s(p,'admin_note')})"),
    "review_document": dict(
        method="POST", path=lambda p: f"USER_DOCUMENTS/ADMIN_REVIEW/{_i(p,'target_id')}",
        body=lambda p: {"status": _s(p, "status"), "admin_note": _s(p, "admin_note")},
        need=["target_id", "status"],
        recap=lambda p: f"marquer le document #{_i(p,'target_id')} comme "
                        f"« {_s(p,'status')} »"),
    "set_badge": dict(
        method="POST", path=lambda p: f"USER_DOCUMENTS/ADMIN_SET_BADGE/{_i(p,'user_id')}",
        body=lambda p: {"badge_tier": _s(p, "badge_tier") or None},
        need=["user_id"],
        recap=lambda p: f"attribuer le badge « {_s(p,'badge_tier') or 'aucun'} » "
                        f"à l'utilisateur #{_i(p,'user_id')}"),
    "review_tutorial": dict(
        method="POST", path=lambda p: f"DIY_TUTORIALS/ADMIN_REVIEW/{_i(p,'target_id')}",
        body=lambda p: {"status": _s(p, "status"), "admin_note": _s(p, "admin_note")},
        need=["target_id", "status"],
        recap=lambda p: f"passer le tutoriel #{_i(p,'target_id')} en "
                        f"« {_s(p,'status')} »"),
    "update_bug_status": dict(
        method="POST", path=lambda p: f"BUG_REPORTS/ADMIN_UPDATE_STATUS/{_i(p,'target_id')}",
        body=lambda p: {"status": _s(p, "status"), "admin_note": _s(p, "admin_note")},
        need=["target_id", "status"],
        recap=lambda p: f"passer le signalement de bug #{_i(p,'target_id')} en "
                        f"« {_s(p,'status')} »"),
    "approve_payment": dict(
        method="POST", path=lambda p: f"PAYMENT_METHODS/ADMIN_APPROVE_REQUEST/{_i(p,'target_id')}",
        body=lambda p: {},
        need=["target_id"],
        recap=lambda p: f"approuver le paiement manuel #{_i(p,'target_id')}"),
    "reject_payment": dict(
        method="POST", path=lambda p: f"PAYMENT_METHODS/ADMIN_REJECT_REQUEST/{_i(p,'target_id')}",
        body=lambda p: {},
        need=["target_id"],
        recap=lambda p: f"rejeter le paiement manuel #{_i(p,'target_id')}"),
    # Irréversible. Le backend exige en plus l'e-mail exact du compte (confirm_email),
    # qu'il revérifie lui-même — un simple "oui" verbal ne suffit donc jamais.
    "delete_account": dict(
        method="POST", path=lambda p: "USERS/ADMIN_DELETE_ACCOUNT",
        body=lambda p: {"id": _i(p, "user_id"), "confirm_email": _s(p, "confirm_email")},
        need=["user_id", "confirm_email"], danger=True,
        recap=lambda p: f"SUPPRIMER DÉFINITIVEMENT le compte #{_i(p,'user_id')} "
                        f"et toutes ses données (e-mail de confirmation : "
                        f"{_s(p,'confirm_email')})"),
}

_VALIDATE_PATH = "DISPUTES/ADMIN_PENDING"   # 200 for an admin token, 403 otherwise

PLUGIN = {
    "name": "yogo_admin",
    "description": (
        "ADMIN back-office of the owner's website yogo.work (ban/reactivate a "
        "user, approve or reject a dispute / manual payment / verification "
        "document / DIY tutorial, set a trust badge, update a bug report, delete "
        "an account), plus read-only listings of everything pending moderation "
        "and user search. ONLY for the site owner. Workflow you MUST follow for "
        "any change: call with phase='preview' FIRST, read the returned recap "
        "out loud to the user, wait for a clear spoken 'yes', THEN call again "
        "with phase='execute' and the SAME arguments. Read-only operations "
        "(pending_*, reported_users, search_users) run directly. If it answers "
        "that authentication is required, tell the user to paste their yogo.work "
        "session token in Settings and stop."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "operation": {"type": "STRING", "enum": list(_OPS.keys()),
                          "description": "Which admin operation."},
            "phase": {"type": "STRING", "enum": ["preview", "execute"],
                      "description": "preview = recap only (default). execute = run "
                                     "it, only after the user agreed out loud."},
            "user_id": {"type": "INTEGER", "description": "Target user id (ban, unban, set_badge, delete_account)."},
            "target_id": {"type": "INTEGER", "description": "Target row id (dispute, document, tutorial, bug, payment)."},
            "amount": {"type": "INTEGER", "description": "Refund amount in euros (approve_dispute)."},
            "status": {"type": "STRING", "description": "New status: approved/rejected, or nouveau/en_cours/resolu/ferme for a bug."},
            "badge_tier": {"type": "STRING", "description": "cin_certifie / professionnel / expert_approuve, or empty to remove."},
            "admin_note": {"type": "STRING", "description": "Note / reason shown to the user."},
            "reason": {"type": "STRING", "description": "Ban reason."},
            "confirm_email": {"type": "STRING", "description": "Exact email of the account to delete (delete_account only)."},
            "query": {"type": "STRING", "description": "Search text (search_users)."},
            "account_status": {"type": "INTEGER", "description": "Filter: 0 active, 1 banned (search_users)."},
            "page": {"type": "INTEGER", "description": "Page number for listings (default 0)."},
        },
        "required": ["operation"],
    },
}


# ── Session token ──────────────────────────────────────────────────────────────

def _token() -> str:
    return (get_plugin_config(_NS).get("jwt") or "").strip()


def _token_expiry(tok: str):
    """exp claim of the JWT as a naive-UTC datetime, or None. No signature check
    — this is only to warn the user before the server would reject it."""
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return datetime.fromtimestamp(exp, tz=timezone.utc).replace(tzinfo=None) if exp else None
    except Exception:
        return None


def _base() -> str:
    b = (get_plugin_config(_NS).get("base_url") or _DEFAULT_BASE).strip()
    return b if b.endswith("/") else b + "/"


def _api(method: str, path: str, body=None):
    """Returns (status_code, parsed_json_or_text). Raises only on no token / no requests."""
    if requests is None:
        raise RuntimeError("the 'requests' package is not installed")
    tok = _token()
    if not tok:
        raise PermissionError("no session token configured")
    headers = {"Authorization": f"Bearer {tok}", "Accept": "application/json",
               "User-Agent": "Jarvis-Yogo-Admin/1.0"}
    r = requests.request(method, _base() + path, headers=headers,
                         json=(body if body is not None else None), timeout=_TIMEOUT)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, (r.text or "")


def _auth_hint() -> str:
    exp = _token_expiry(_token())
    if not _token():
        return ("Authentication required: open yogo.work, sign in with Google, then "
                "paste your session token into Settings → YOGO — ADMIN. I did nothing.")
    if exp and exp < datetime.utcnow():
        return ("Your yogo.work session has expired. Sign in again on yogo.work and "
                "paste a fresh token into Settings → YOGO — ADMIN. I did nothing.")
    return ("yogo.work refused the token (not an admin session, or expired). Paste a "
            "fresh admin token into Settings → YOGO — ADMIN. I did nothing.")


def _test_connection(values):
    cfg = {**get_plugin_config(_NS), **(values or {})}
    tok = (cfg.get("jwt") or "").strip()
    if requests is None:
        return False, "requests not installed"
    if not tok:
        return False, "No token — sign in on yogo.work and paste the session token."
    try:
        base = (cfg.get("base_url") or _DEFAULT_BASE).strip()
        if not base.endswith("/"):
            base += "/"
        r = requests.get(base + _VALIDATE_PATH,
                         headers={"Authorization": f"Bearer {tok}",
                                  "User-Agent": "Jarvis-Yogo-Admin/1.0"}, timeout=_TIMEOUT)
        if r.status_code == 200:
            exp = _token_expiry(tok)
            return True, ("OK — admin session valide"
                          + (f", expire le {exp:%Y-%m-%d %H:%M} UTC" if exp else ""))
        if r.status_code in (401, 403):
            return False, "Jeton refusé : ce n'est pas une session admin, ou elle a expiré."
        return False, f"Réponse inattendue du serveur (HTTP {r.status_code})."
    except Exception as e:
        return False, str(e)[:200]


PLUGIN_SETTINGS = {
    "namespace": _NS,
    "title": "YOGO — ADMIN",
    "fields": [
        {"key": "base_url", "label": "API URL", "type": "text", "default": _DEFAULT_BASE},
        {"key": "jwt", "label": "Session token (after Google sign-in on yogo.work)",
         "type": "password"},
    ],
    "action": {"label": "VÉRIFIER LA SESSION", "run": _test_connection},
}


# ── Verbal two-step state ────────────────────────────────────────────────────

_pending: dict = {}            # signature -> (recap, timestamp)
_pending_lock = threading.Lock()


def _signature(operation: str, params: dict) -> str:
    keep = ("user_id", "target_id", "amount", "status", "badge_tier",
            "admin_note", "reason", "confirm_email")
    norm = {k: str(params.get(k)) for k in keep if params.get(k) is not None}
    return operation + "|" + json.dumps(norm, sort_keys=True, ensure_ascii=False)


def _arm(sig: str, recap: str):
    now = time.monotonic()
    with _pending_lock:
        for s, (_, t) in list(_pending.items()):      # drop stale previews
            if now - t > _CONFIRM_WINDOW:
                _pending.pop(s, None)
        _pending[sig] = (recap, now)


def _consume(sig: str) -> bool:
    now = time.monotonic()
    with _pending_lock:
        hit = _pending.pop(sig, None)
    return bool(hit and now - hit[1] <= _CONFIRM_WINDOW)


# ── Result shaping ───────────────────────────────────────────────────────────

def _summarize_read(operation: str, data) -> dict:
    rows = data if isinstance(data, list) else (data.get("content") if isinstance(data, dict) else None)
    out = {"operation": operation}
    if isinstance(rows, list):
        out["count"] = len(rows)
        out["items"] = rows[:15]
        if isinstance(data, dict) and "totalElements" in data:
            out["total"] = data.get("totalElements")
    else:
        out["data"] = data
    out["instructions"] = ("Summarize for the owner in their language: how many, and "
                           "the few most relevant, with their ids so they can act. Do "
                           "not invent rows.")
    return out


# ── Entry point ──────────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    operation = _s(parameters, "operation")
    spec = _OPS.get(operation)
    if spec is None:
        return f"Unknown admin operation '{operation}'."

    if not _token():
        return _auth_hint()

    # ---- read-only: run straight away ----
    if spec.get("read"):
        try:
            code, data = _api(spec["method"], spec["path"](parameters))
        except PermissionError:
            return _auth_hint()
        except Exception as e:
            print(f"[yogo_admin] {operation}: {e}")
            return f"Could not reach yogo.work: {e}"
        if code in (401, 403):
            return _auth_hint()
        if code >= 400:
            return f"yogo.work returned HTTP {code} for {operation}: {str(data)[:200]}"
        return json.dumps(_summarize_read(operation, data), ensure_ascii=False, default=str)

    # ---- state-changing: validate required params ----
    missing = [k for k in spec.get("need", []) if parameters.get(k) in (None, "")]
    if missing:
        return (f"To {operation} I still need: {', '.join(missing)}. Ask the user, "
                f"then preview again.")

    recap = spec["recap"](parameters)
    sig = _signature(operation, parameters)
    phase = _s(parameters, "phase", "preview").lower()

    # ---- preview: arm and ask out loud ----
    if phase != "execute":
        _arm(sig, recap)
        danger = " C'est IRRÉVERSIBLE." if spec.get("danger") else ""
        if player:
            try:
                player.write_log(f"SYS: Yogo admin — aperçu : {recap}")
            except Exception:
                pass
        return (f"[CONFIRMATION_REQUISE] Action demandée : {recap}.{danger} "
                f"Dis à l'utilisateur, dans sa langue, ce que tu vas faire, en UNE phrase, "
                f"et demande-lui de confirmer clairement à voix haute. N'exécute ("
                f"phase='execute') que s'il accepte explicitement. Ne prétends pas que "
                f"c'est fait.")

    # ---- execute: only if a matching preview was armed ----
    if not _consume(sig):
        _arm(sig, recap)
        return ("[CONFIRMATION_REQUISE] No confirmed preview for this exact action "
                f"(or it expired). Recap again: {recap}. Ask the user to confirm out "
                f"loud, then execute.")

    try:
        code, data = _api(spec["method"], spec["path"](parameters), spec["body"](parameters))
    except PermissionError:
        return _auth_hint()
    except Exception as e:
        print(f"[yogo_admin] {operation} execute: {e}")
        return f"The action failed to reach yogo.work: {e}. Nothing is confirmed done."
    if code in (401, 403):
        return _auth_hint()
    if code >= 400:
        return (f"yogo.work refused the action (HTTP {code}): {str(data)[:200]}. "
                f"It was NOT done.")
    if player:
        try:
            player.write_log(f"SYS: Yogo admin — exécuté : {recap}")
        except Exception:
            pass
    return json.dumps({"operation": operation, "status": "done", "recap": recap,
                       "response": data,
                       "instructions": "Tell the owner it is done, in one short sentence."},
                      ensure_ascii=False, default=str)
