"""
SSH plugin — exécuter des commandes sur des machines distantes configurées via l'UI SSH de JARVIS.

Deux types de connexions (gérés dans l'UI "SSH CONNECTIONS") :
  - config   : alias déjà présent dans ~/.ssh/config  → ssh <alias> <cmd>
  - password : host + user + password                 → sshpass -p <pwd> ssh user@host <cmd>
"""
import subprocess
import shlex
import platform

from memory.config_manager import get_ssh_connections

PLUGIN = {
    "name": "ssh_remote",
    "description": (
        "Exécuter une commande shell sur une machine distante via SSH. "
        "Utilise les connexions sauvegardées dans l'UI SSH de JARVIS. "
        "Actions : list_connections (lister les connexions disponibles), "
        "run_command (exécuter une commande sur une connexion nommée). "
        "IMPORTANT — chaque appel run_command est une session SSH indépendante : "
        "un 'cd' ne persiste pas d'un appel à l'autre. Pour naviguer, combiner "
        "les commandes en une seule : ex. 'cd /chemin && ls' ou 'ls /chemin/absolu'. "
        "Pour explorer une machine, commencer par 'ls ~' puis cibler le chemin voulu. "
        "Toujours appeler list_connections d'abord si le nom exact de la connexion "
        "n'est pas connu. "
        "Utiliser ce plugin quand l'utilisateur parle d'un serveur distant, "
        "d'une machine remote, ou cite explicitement un nom de connexion SSH."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "list_connections | run_command",
            },
            "connection_name": {
                "type": "STRING",
                "description": "Nom de la connexion SSH (champ 'name' dans l'UI SSH). Requis pour run_command.",
            },
            "command": {
                "type": "STRING",
                "description": "Commande shell à exécuter sur la machine distante. Requis pour run_command.",
            },
            "timeout": {
                "type": "NUMBER",
                "description": "Timeout en secondes (défaut 30).",
            },
        },
        "required": ["action"],
    },
}


def _find_connection(name: str) -> dict | None:
    name_l = name.lower()
    for conn in get_ssh_connections():
        if conn.get("name", "").lower() == name_l:
            return conn
        if conn.get("alias", "").lower() == name_l:
            return conn
    return None


def _run_ssh(conn: dict, command: str, timeout: int) -> str:
    conn_type = conn.get("type", "config")

    if conn_type == "config":
        alias = conn.get("alias") or conn.get("name")
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", alias, command]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            out = result.stdout.strip()
            err = result.stderr.strip()
            if result.returncode != 0:
                return f"Erreur (code {result.returncode}): {err or out}"
            return out if out else "(commande exécutée avec succès — aucune sortie, répertoire peut-être vide)"
        except subprocess.TimeoutExpired:
            return f"Timeout après {timeout}s."
        except FileNotFoundError:
            return "SSH n'est pas installé sur cette machine."

    elif conn_type == "password":
        host = conn.get("host", "")
        user = conn.get("user", "")
        password = conn.get("password", "")
        if not host or not user:
            return "Connexion incomplète : host ou user manquant."

        # Essai avec sshpass si disponible, sinon via paramiko si dispo
        import shutil
        if shutil.which("sshpass"):
            cmd = [
                "sshpass", "-p", password,
                "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
                f"{user}@{host}", command,
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                out = result.stdout.strip()
                err = result.stderr.strip()
                if result.returncode != 0:
                    return f"Erreur (code {result.returncode}): {err or out}"
                return out if out else "(commande exécutée avec succès — aucune sortie, répertoire peut-être vide)"
            except subprocess.TimeoutExpired:
                return f"Timeout après {timeout}s."
            except FileNotFoundError:
                pass

        try:
            import paramiko
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(host, username=user, password=password, timeout=10)
            _, stdout, stderr = client.exec_command(command, timeout=timeout)
            out = stdout.read().decode().strip()
            err = stderr.read().decode().strip()
            client.close()
            if err and not out:
                return f"Erreur: {err}"
            return out if out else "(commande exécutée avec succès — aucune sortie, répertoire peut-être vide)"
        except ImportError:
            return (
                "Connexion par mot de passe nécessite 'sshpass' (Linux/Mac) "
                "ou le paquet Python 'paramiko' (pip install paramiko). "
                "Aucun n'est disponible."
            )
        except Exception as e:
            return f"Erreur SSH: {e}"

    return f"Type de connexion inconnu : {conn_type!r}"


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "")

    if action == "list_connections":
        conns = get_ssh_connections()
        if not conns:
            return "Aucune connexion SSH configurée. Utilisez l'UI SSH CONNECTIONS pour en ajouter."
        lines = []
        for c in conns:
            t = c.get("type", "?")
            name = c.get("name", "?")
            detail = c.get("alias") or c.get("host") or ""
            lines.append(f"- {name} ({t}){': ' + detail if detail else ''}")
        return "Connexions SSH disponibles :\n" + "\n".join(lines)

    if action == "run_command":
        name = parameters.get("connection_name", "").strip()
        command = parameters.get("command", "").strip()
        timeout = int(parameters.get("timeout") or 30)

        if not name:
            return "Précisez le nom de la connexion SSH (connection_name)."
        if not command:
            return "Précisez la commande à exécuter (command)."

        conn = _find_connection(name)
        if conn is None:
            available = [c.get("name", "") for c in get_ssh_connections()]
            avail_str = ", ".join(available) if available else "aucune"
            return f"Connexion '{name}' introuvable. Disponibles : {avail_str}."

        if player:
            try:
                player.write_log(f"JARVIS: SSH → {name} : {command}")
            except Exception:
                pass

        result = _run_ssh(conn, command, timeout)
        return f"[{name}] {result}"

    return f"Action inconnue : {action!r}. Valeurs valides : list_connections, run_command."
