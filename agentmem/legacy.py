"""Altbestand aus dem AgentMemory-Server, zusammengefasst.

Von den 21 alten Werkzeugen bleiben hier zehn. Der Rest ist nicht
weggefallen, sondern woanders aufgegangen:

  get_system_state, get_storage_stats     -> status()
  log_problem, list_problems, get_problem -> remember(), recall(), open_record()
  submit_code_review                      -> remember(kind="review")
  run_smoketest                           -> verify()
  get/update_work_instructions            -> die vier SOPs sind jetzt Skills
  cleanup_old_files, archive_completed_*  -> maintenance()

Die Aufgabenverwaltung ist bewusst originalgetreu portiert, inklusive der
doppelten Pruefung beim Sperren (server.py:498-501): Zwischen "Aufgabe sieht
frei aus" und "Aufgabe gehoert jetzt mir" darf kein zweiter Agent
dazwischenkommen.

Der frueher ueberall mitgeschleppte api_key-Parameter ist ersatzlos weg. Er
war Scheinsicherheit: leer konfiguriert, als normaler Werkzeug-Parameter fuer
das Modell sichtbar und damit faelschbar, und lesende Werkzeuge haben ihn nie
geprueft.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

from . import store

# --------------------------------------------------------------------------
# SSH-Verzeichnis
# --------------------------------------------------------------------------


def _ssh_pfad() -> Path:
    return store.data_dir() / "system" / "ssh_connections.json"


def ssh_list() -> dict:
    daten = store.load_json(_ssh_pfad(), {"connections": {}}) or {"connections": {}}
    verbindungen = daten.get("connections", {})
    return {
        "anzahl": len(verbindungen),
        "verbindungen": [
            {
                "id": k,
                "host": v.get("host"),
                "user": v.get("user"),
                "port": v.get("port", 22),
                "zweck": v.get("purpose", ""),
                "programm": v.get("client_program", ""),
            }
            for k, v in verbindungen.items()
        ],
    }


def ssh_edit(
    host: str,
    user: str,
    port: int = 22,
    client_program: str = "OpenSSH",
    purpose: str = "",
    key_type: str = "ed25519",
    remove: bool = False,
) -> dict:
    store.ensure_dirs()
    pfad = _ssh_pfad()
    daten = store.load_json(pfad, {"connections": {}}) or {"connections": {}}
    daten.setdefault("connections", {})
    schluessel = f"{user}@{host}"

    if remove:
        if schluessel not in daten["connections"]:
            return {"ok": False, "fehler": f"'{schluessel}' ist nicht eingetragen."}
        daten["connections"].pop(schluessel)
        store.save_json(pfad, daten)
        return {"ok": True, "entfernt": schluessel}

    daten["connections"][schluessel] = {
        "host": host,
        "user": user,
        "port": port,
        "client_program": client_program,
        "purpose": purpose,
        "key_type": key_type,
        "updated_at": store.jetzt(),
    }
    store.save_json(pfad, daten)
    return {"ok": True, "gespeichert": schluessel}


# --------------------------------------------------------------------------
# MCP-Verzeichnis
# --------------------------------------------------------------------------


def _mcp_pfad() -> Path:
    return store.data_dir() / "system" / "mcp_servers.json"


def mcp_registry_list() -> dict:
    daten = store.load_json(_mcp_pfad(), {"mcpServers": {}}) or {"mcpServers": {}}
    server = daten.get("mcpServers", {})
    return {
        "anzahl": len(server),
        "server": [{"name": k, "command": v.get("command"), "args": v.get("args", [])} for k, v in server.items()],
        "hinweis": (
            "Eigene Vermerke von agent-memory. Sie ERGAENZEN .lmstudio/mcp.json; "
            "bei gleichem Namen gewinnt mcp.json. Das Gateway startet auch diese "
            "hier - womit du wirklich arbeiten kannst, zeigt tool_find()."
        ),
    }


def mcp_registry_set(name: str, command: str, args: list[str], env: dict | None = None, remove: bool = False) -> dict:
    store.ensure_dirs()
    pfad = _mcp_pfad()
    daten = store.load_json(pfad, {"mcpServers": {}}) or {"mcpServers": {}}
    daten.setdefault("mcpServers", {})

    if remove:
        if name not in daten["mcpServers"]:
            return {"ok": False, "fehler": f"'{name}' ist nicht eingetragen."}
        daten["mcpServers"].pop(name)
        store.save_json(pfad, daten)
        return {"ok": True, "entfernt": name}

    daten["mcpServers"][name] = {"command": command, "args": args, "env": env or {}}
    store.save_json(pfad, daten)
    return {"ok": True, "gespeichert": name}


# --------------------------------------------------------------------------
# Roadmap und Aufgaben
# --------------------------------------------------------------------------


def _tasks_dir() -> Path:
    return store.data_dir() / "orchestration" / "tasks"


def _roadmap_pfad() -> Path:
    return store.data_dir() / "orchestration" / "roadmap.json"


def create_roadmap(title: str, description: str, goals: list[str]) -> dict:
    store.ensure_dirs()
    store.save_json(
        _roadmap_pfad(),
        {
            "title": title,
            "description": description,
            "goals": goals,
            "created_at": store.jetzt(),
            "updated_at": store.jetzt(),
        },
    )
    return {"ok": True, "titel": title, "ziele": len(goals)}


def add_task(
    task_id: str,
    title: str,
    description: str,
    assigned_to: str | None = None,
    dependencies: list[str] | None = None,
) -> dict:
    store.ensure_dirs()
    task_id = store.slugify(task_id)
    store.save_json(
        _tasks_dir() / f"task_{task_id}.json",
        {
            "task_id": task_id,
            "title": title,
            "description": description,
            "assigned_to": assigned_to,
            "dependencies": dependencies or [],
            "status": "pending",
            "message": "",
            "result": {},
            "created_at": store.jetzt(),
            "start_time": None,
            "end_time": None,
        },
    )
    return {"ok": True, "task_id": task_id}


def get_next_task(agent_id: str) -> dict:
    """Naechste ausfuehrbare Aufgabe holen und sofort fuer sich sperren."""
    store.ensure_dirs()
    ordner = _tasks_dir()

    with store.file_lock(ordner / "task_index.json"):
        aufgaben = []
        erledigt: set[str] = set()
        for datei in ordner.glob("task_*.json"):
            t = store.load_json(datei)
            if t:
                aufgaben.append((datei, t))
                if t.get("status") == "completed":
                    erledigt.add(t.get("task_id"))

        aufgaben.sort(key=lambda x: x[1].get("created_at", ""))

        for datei, t in aufgaben:
            if t.get("status") != "pending":
                continue
            zustaendig = t.get("assigned_to")
            if zustaendig and zustaendig != agent_id:
                continue
            offene_abhaengigkeiten = [d for d in t.get("dependencies", []) if d not in erledigt]
            if offene_abhaengigkeiten:
                continue

            with store.file_lock(datei, timeout=30):
                # Nochmal lesen: zwischen Auswahl und Sperre kann ein anderer
                # Agent zugegriffen haben.
                aktuell = store.load_json(datei)
                if not aktuell or aktuell.get("status") != "pending":
                    continue
                aktuell["status"] = "in_progress"
                aktuell["assigned_to"] = agent_id
                aktuell["start_time"] = store.jetzt()
                store.save_json(datei, aktuell)
                return {
                    "aufgabe_gefunden": True,
                    "task": {
                        "task_id": aktuell["task_id"],
                        "title": aktuell["title"],
                        "description": aktuell["description"],
                        "dependencies": aktuell["dependencies"],
                    },
                }

    return {"aufgabe_gefunden": False, "hinweis": f"Nichts Bereites fuer '{agent_id}'."}


def update_task_status(task_id: str, status: str, message: str = "", result: dict | None = None) -> dict:
    erlaubt = ("pending", "in_progress", "completed", "failed")
    if status not in erlaubt:
        return {"ok": False, "fehler": f"Status muss einer von {erlaubt} sein."}

    datei = _tasks_dir() / f"task_{store.slugify(task_id)}.json"
    t = store.load_json(datei)
    if not t:
        return {"ok": False, "fehler": f"Aufgabe '{task_id}' gibt es nicht."}

    t["status"] = status
    t["message"] = message
    if result:
        t["result"] = result
    if status in ("completed", "failed"):
        t["end_time"] = store.jetzt()
    store.save_json(datei, t)
    return {"ok": True, "task_id": task_id, "status": status}


def get_roadmap_status() -> dict:
    roadmap = store.load_json(_roadmap_pfad(), {}) or {}
    ordner = _tasks_dir()
    aufgaben = [store.load_json(p, {}) for p in ordner.glob("task_*.json")] if ordner.exists() else []
    aufgaben = [t for t in aufgaben if t]

    nach_status: dict[str, int] = {}
    for t in aufgaben:
        s = t.get("status", "unbekannt")
        nach_status[s] = nach_status.get(s, 0) + 1

    return {
        "roadmap": {"titel": roadmap.get("title"), "ziele": roadmap.get("goals", [])},
        "aufgaben_gesamt": len(aufgaben),
        "nach_status": nach_status,
        "offen": [
            {"task_id": t.get("task_id"), "title": t.get("title"), "status": t.get("status")}
            for t in aufgaben
            if t.get("status") in ("pending", "in_progress")
        ],
    }


# --------------------------------------------------------------------------
# Aufraeumen
# --------------------------------------------------------------------------


def maintenance(retention_days: int = 30, archive_task_days: int = 7, dry_run: bool = True) -> dict:
    """Alte Dateien loeschen und erledigte Aufgaben wegraeumen.

    dry_run ist mit Absicht der Standard - ein Modell, das dieses Werkzeug
    falsch versteht, soll nichts kaputt machen koennen.
    """
    store.ensure_dirs()
    root = store.data_dir()
    grenze = datetime.now() - timedelta(days=retention_days)
    task_grenze = datetime.now() - timedelta(days=archive_task_days)

    zum_loeschen: list[str] = []
    zum_archivieren: list[str] = []

    for unter in ("jobs", "verifications"):
        ordner = root / unter
        if not ordner.exists():
            continue
        for p in ordner.glob("*.json"):
            if datetime.fromtimestamp(p.stat().st_mtime) < grenze:
                zum_loeschen.append(str(p.relative_to(root)))

    ordner = _tasks_dir()
    archiv = root / "_archive" / "tasks"
    if ordner.exists():
        for p in ordner.glob("task_*.json"):
            t = store.load_json(p, {}) or {}
            if t.get("status") != "completed":
                continue
            ende = t.get("end_time") or t.get("created_at")
            try:
                if ende and datetime.fromisoformat(ende) < task_grenze:
                    zum_archivieren.append(str(p.relative_to(root)))
            except ValueError:
                continue

    if not dry_run:
        for rel in zum_loeschen:
            (root / rel).unlink(missing_ok=True)
        archiv.mkdir(parents=True, exist_ok=True)
        for rel in zum_archivieren:
            quelle = root / rel
            shutil.move(str(quelle), str(archiv / quelle.name))

    return {
        "vorschau": dry_run,
        "geloescht": len(zum_loeschen),
        "archiviert": len(zum_archivieren),
        "dateien_geloescht": zum_loeschen[:20],
        "dateien_archiviert": zum_archivieren[:20],
        "hinweis": "Nur Vorschau. Zum Ausfuehren dry_run=False setzen." if dry_run else "Ausgefuehrt.",
    }


def speicher_statistik() -> dict:
    root = store.data_dir()
    if not root.exists():
        return {}
    out = {}
    for unter in ("records", "jobs", "verifications", "handoffs", "orchestration/tasks", "_archive"):
        ordner = root / unter
        if not ordner.exists():
            continue
        dateien = list(ordner.rglob("*")) if unter == "_archive" else list(ordner.glob("*"))
        dateien = [f for f in dateien if f.is_file()]
        out[unter] = {
            "dateien": len(dateien),
            "mb": round(sum(f.stat().st_size for f in dateien) / 1024 / 1024, 2),
        }
    return out
