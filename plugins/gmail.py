"""
Gmail plugin — lire, envoyer et chercher des emails via l'API Gmail.

Premier lancement : un navigateur s'ouvre pour l'autorisation OAuth (une seule fois).
Le token est sauvegardé dans config/gmail_token.json pour les lancements suivants.
Fonctionne avec la double authentification activée.
"""
import base64
import email as _email_lib
import json
import sys
from email.mime.text import MIMEText
from pathlib import Path

PLUGIN = {
    "name": "gmail",
    "description": (
        "Lire, envoyer et chercher des emails Gmail. "
        "Actions disponibles : read_inbox (lire les N derniers emails), "
        "send (envoyer un email), search (chercher par texte), "
        "read_email (lire le contenu d'un email par son ID)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "read_inbox | send | search | read_email",
            },
            "to": {
                "type": "STRING",
                "description": "Destinataire (pour send).",
            },
            "subject": {
                "type": "STRING",
                "description": "Sujet (pour send).",
            },
            "body": {
                "type": "STRING",
                "description": "Corps du message (pour send).",
            },
            "query": {
                "type": "STRING",
                "description": "Requête de recherche Gmail (pour search), ex: 'from:alice@example.com'.",
            },
            "message_id": {
                "type": "STRING",
                "description": "ID d'un message Gmail (pour read_email).",
            },
            "max_results": {
                "type": "NUMBER",
                "description": "Nombre maximum d'emails à retourner (défaut 5).",
            },
        },
        "required": ["action"],
    },
}

_BASE = Path(__file__).resolve().parent.parent
_CREDS_FILE = _BASE / "config" / "gmail_credentials.json"
_TOKEN_FILE = _BASE / "config" / "gmail_token.json"
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def _connection_status() -> str:
    """Returns a status string for the settings UI."""
    if not _CREDS_FILE.exists():
        return "credentials.json manquant"
    if not _TOKEN_FILE.exists():
        return "Non connecté — cliquer Autoriser Gmail"
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)
        if creds.valid:
            import json as _j
            tok = _j.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
            email = tok.get("token", {})
            return f"Connecté ✓"
        if creds.expired and creds.refresh_token:
            return "Token expiré — sera renouvelé automatiquement"
        return "Token invalide — Autoriser à nouveau"
    except Exception as e:
        return f"Erreur token : {e}"


def _action_authorize(values: dict) -> tuple[bool, str]:
    """Called by the settings UI — writes credentials.json from UI fields then runs OAuth."""
    client_id     = (values.get("client_id") or "").strip()
    client_secret = (values.get("client_secret") or "").strip()
    # If the user filled in new values, write them to credentials.json first
    if client_id and client_secret:
        _CREDS_FILE.write_text(json.dumps({
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            }
        }, indent=2), encoding="utf-8")
    if not _CREDS_FILE.exists():
        return False, ("Client ID et Secret manquants. Renseignez-les ci-dessus "
                       "ou déposez gmail_credentials.json dans config/.")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        flow = InstalledAppFlow.from_client_secrets_file(str(_CREDS_FILE), _SCOPES)
        creds = flow.run_local_server(port=0)
        _TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        svc = build("gmail", "v1", credentials=creds)
        profile = svc.users().getProfile(userId="me").execute()
        return True, f"Connecté en tant que {profile.get('emailAddress', '?')} ✓"
    except Exception as e:
        return False, f"Échec d'autorisation : {e}"


def _action_disconnect(values: dict) -> tuple[bool, str]:
    """Revoke and delete the stored token."""
    if _TOKEN_FILE.exists():
        _TOKEN_FILE.unlink()
        return True, "Déconnecté — token supprimé."
    return True, "Déjà déconnecté."


def _saved_client_id() -> str:
    try:
        return json.loads(_CREDS_FILE.read_text(encoding="utf-8"))["installed"]["client_id"]
    except Exception:
        return ""

def _saved_client_secret() -> str:
    try:
        return json.loads(_CREDS_FILE.read_text(encoding="utf-8"))["installed"]["client_secret"]
    except Exception:
        return ""


PLUGIN_SETTINGS = {
    "namespace": "gmail",
    "title":     "📧  Gmail",
    "fields": [
        {
            "key":         "client_id",
            "label":       "Client ID (Google Cloud Console)",
            "type":        "text",
            "default":     _saved_client_id(),
            "placeholder": "276765872117-…apps.googleusercontent.com",
        },
        {
            "key":         "client_secret",
            "label":       "Client Secret",
            "type":        "password",
            "default":     _saved_client_secret(),
            "placeholder": "GOCSPX-…",
        },
        {
            "key":         "status",
            "label":       "État de la connexion",
            "type":        "readonly",
            "default":     _connection_status(),
        },
    ],
    "action": {
        "label": "🔗  Autoriser Gmail (navigateur)",
        "run":   _action_authorize,
    },
    "action2": {
        "label": "✕  Déconnecter",
        "run":   _action_disconnect,
    },
}


def _get_service():
    """Return an authenticated Gmail service. Opens browser on first run."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise RuntimeError(
            "Paquets manquants. Lancez : pip install google-auth-oauthlib google-api-python-client"
        )

    creds = None
    if _TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not _CREDS_FILE.exists():
                raise RuntimeError(
                    f"Fichier credentials introuvable : {_CREDS_FILE}. "
                    "Téléchargez-le depuis Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(_CREDS_FILE), _SCOPES)
            creds = flow.run_local_server(port=0)
        _TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds)


def _snippet(msg: dict) -> str:
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    frm  = headers.get("From", "?")
    subj = headers.get("Subject", "(sans objet)")
    date = headers.get("Date", "")[:16]
    snip = msg.get("snippet", "")[:120]
    return f"De: {frm}\nSujet: {subj}\nDate: {date}\n{snip}"


def _full_body(msg: dict) -> str:
    payload = msg.get("payload", {})
    parts = payload.get("parts", [payload])
    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")[:2000]
    return msg.get("snippet", "(corps non disponible)")


def run(parameters: dict) -> str:
    action = (parameters.get("action") or "").strip().lower()
    try:
        svc = _get_service()
    except RuntimeError as e:
        return f"Gmail: {e}"
    except Exception as e:
        return f"Gmail: échec d'authentification — {e}"

    try:
        if action == "read_inbox":
            n = int(parameters.get("max_results") or 5)
            results = svc.users().messages().list(
                userId="me", labelIds=["INBOX"], maxResults=n
            ).execute()
            messages = results.get("messages", [])
            if not messages:
                return "Boîte de réception vide."
            lines = []
            for m in messages:
                full = svc.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["From", "Subject", "Date"]
                ).execute()
                lines.append(_snippet(full))
            return f"Derniers {len(lines)} emails :\n\n" + "\n\n---\n\n".join(lines)

        elif action == "send":
            to      = parameters.get("to", "")
            subject = parameters.get("subject", "(sans objet)")
            body    = parameters.get("body", "")
            if not to:
                return "Gmail: destinataire manquant."
            mime = MIMEText(body)
            mime["to"]      = to
            mime["subject"] = subject
            raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
            svc.users().messages().send(userId="me", body={"raw": raw}).execute()
            return f"Email envoyé à {to}."

        elif action == "search":
            q = parameters.get("query", "")
            n = int(parameters.get("max_results") or 5)
            if not q:
                return "Gmail: requête de recherche manquante."
            results = svc.users().messages().list(
                userId="me", q=q, maxResults=n
            ).execute()
            messages = results.get("messages", [])
            if not messages:
                return f"Aucun email trouvé pour : {q}"
            lines = []
            for m in messages:
                full = svc.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["From", "Subject", "Date"]
                ).execute()
                lines.append(f"[ID: {m['id']}]\n{_snippet(full)}")
            return f"{len(lines)} résultat(s) pour '{q}' :\n\n" + "\n\n---\n\n".join(lines)

        elif action == "read_email":
            mid = parameters.get("message_id", "")
            if not mid:
                return "Gmail: message_id manquant."
            full = svc.users().messages().get(
                userId="me", id=mid, format="full"
            ).execute()
            headers = {h["name"]: h["value"]
                       for h in full.get("payload", {}).get("headers", [])}
            return (
                f"De: {headers.get('From','?')}\n"
                f"À: {headers.get('To','?')}\n"
                f"Sujet: {headers.get('Subject','?')}\n"
                f"Date: {headers.get('Date','?')}\n\n"
                + _full_body(full)
            )

        else:
            return (f"Gmail: action inconnue '{action}'. "
                    "Utilisez : read_inbox, send, search, read_email.")

    except Exception as e:
        return f"Gmail: erreur — {e}"
