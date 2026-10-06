"""Smoke-Tests, die wirklich laufen.

Der alte run_smoketest (agent_memory_mcp/server.py:813) hat Name, Befehl,
Status und Ausgabe entgegengenommen und in eine JSON geschrieben - alles
davon vom Modell behauptet. Ob der Befehl je gelaufen ist, wusste niemand.
Genau deshalb steht in der alten Ablage ein Test auf "pending", der auf ein
Verzeichnis zeigt, in dem die getestete Datei gar nicht liegt.

Hier wird ausgefuehrt und gemessen. Das Modell liefert den Befehl, nicht das
Ergebnis.

Zu den Rechten: Es gibt bewusst keine Erlaubnisliste. LM Studio hat ueber den
eingetragenen 'terminal'-Server ohnehin ALLOWED_COMMANDS=all auf C:// - eine
Liste hier waere Theater, keine Sicherheit. Was bleibt, ist eine harte
Zeitgrenze und dass jeder Lauf vollstaendig protokolliert wird.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from . import config, records, store


def ausfuehren(
    name: str,
    command: str,
    cwd: str | None = None,
    timeout: int | None = None,
    expect_exit: int | None = 0,
    expect_contains: str | None = None,
    playbook_id: str | None = None,
) -> dict:
    cfg = config.load()["verify"]
    timeout = timeout or cfg["standard_timeout"]
    tail = cfg["ausgabe_tail_zeichen"]
    store.ensure_dirs()

    arbeitsverzeichnis = None
    if cwd:
        p = Path(cwd)
        if not p.is_dir():
            return {
                "passed": False,
                "fehler": f"Arbeitsverzeichnis gibt es nicht: {cwd}",
                "exit_code": None,
            }
        arbeitsverzeichnis = str(p)

    start = time.time()
    abgebrochen = False
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=arbeitsverzeichnis,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        exit_code, stdout, stderr = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        abgebrochen = True
        exit_code = None
        stdout = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = (e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        stderr += f"\n[abgebrochen nach {timeout}s]"
    except OSError as e:
        return {"passed": False, "fehler": f"Start fehlgeschlagen: {e}", "exit_code": None}

    dauer = round(time.time() - start, 2)

    gruende = []
    if abgebrochen:
        gruende.append(f"Zeitgrenze {timeout}s ueberschritten")
    if expect_exit is not None and exit_code != expect_exit:
        gruende.append(f"Rueckgabewert {exit_code}, erwartet {expect_exit}")
    if expect_contains and expect_contains not in (stdout + stderr):
        gruende.append(f"Text {expect_contains!r} kommt in der Ausgabe nicht vor")
    passed = not gruende

    eintrag = {
        "id": f"vf_{store.zeitstempel()}_{store.slugify(name, 40)}",
        "name": name,
        "command": command,
        "cwd": arbeitsverzeichnis,
        "passed": passed,
        "exit_code": exit_code,
        "duration_s": dauer,
        "gruende": gruende,
        "stdout": stdout,
        "stderr": stderr,
        "playbook_id": playbook_id,
        "zeitpunkt": store.jetzt(),
    }
    store.save_json(store.data_dir() / "verifications" / f"{eintrag['id']}.json", eintrag)

    if playbook_id:
        rec = records.laden(playbook_id)
        if rec is not None:
            rec.setdefault("pruefungen", []).append(
                {"id": eintrag["id"], "passed": passed, "zeitpunkt": eintrag["zeitpunkt"]}
            )
            records.speichern(rec)

    # Zurueck geht nur das Ende der Ausgabe - das Vollstaendige liegt in der
    # Datei und waere im Kontext meist reine Verschwendung.
    return {
        "passed": passed,
        "exit_code": exit_code,
        "duration_s": dauer,
        "gruende": gruende,
        "stdout_tail": stdout[-tail:],
        "stderr_tail": stderr[-tail:],
        "protokoll": eintrag["id"],
        "vollstaendig_unter": f"open_record('{eintrag['id']}')",
    }


def laden(vid: str) -> dict | None:
    return store.load_json(store.data_dir() / "verifications" / f"{vid}.json")


def letzte(limit: int = 10) -> list[dict]:
    ordner = store.data_dir() / "verifications"
    if not ordner.exists():
        return []
    dateien = sorted(ordner.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in dateien[:limit]:
        e = store.load_json(p, {})
        out.append(
            {
                "id": e.get("id"),
                "name": e.get("name"),
                "passed": e.get("passed"),
                "zeitpunkt": e.get("zeitpunkt"),
            }
        )
    return out
