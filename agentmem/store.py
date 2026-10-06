"""JSON-Ablage: eine kleine Datei je Eintrag.

Bewusst keine Datenbank. Der Grund steht schon im alten Server
(agent_memory_mcp/server.py:346): eine einzige grosse Log-Datei waechst
unbegrenzt und blaeht den Kontext auf. Eine Datei je Eintrag laesst sich
gezielt oeffnen, versionieren und von Hand lesen.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config

UNTERORDNER = ("records", "jobs", "verifications", "handoffs", "index", "system", "orchestration/tasks", "_archive")


def data_dir() -> Path:
    return config.data_dir()


def ensure_dirs() -> Path:
    root = data_dir()
    for teil in UNTERORDNER:
        (root / teil).mkdir(parents=True, exist_ok=True)
    return root


def slugify(text: str, max_len: int = 50) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_-]+", "_", text).strip("_")
    return text[:max_len] or "eintrag"


def zeitstempel() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def jetzt() -> str:
    return datetime.now().isoformat(timespec="seconds")


def zeit_von_datei(pfad: Path) -> str | None:
    """Aenderungszeitpunkt einer Datei, im selben Format wie jetzt()."""
    try:
        return datetime.fromtimestamp(pfad.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return None


def load_json(pfad: Path, standard: Any = None) -> Any:
    if not pfad.exists():
        return standard
    # Auf Windows kann das Lesen kurz scheitern, waehrend ein anderer Thread
    # gerade die Datei ersetzt. Kurz erneut versuchen statt aufgeben.
    for versuch in range(5):
        try:
            return json.loads(pfad.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return standard
        except OSError:
            if versuch == 4:
                return standard
            time.sleep(0.05 * (versuch + 1))
    return standard


def save_json(pfad: Path, daten: Any) -> None:
    """Erst daneben schreiben, dann umbenennen - ein abgebrochener Schreib-
    vorgang hinterlaesst so keine halbe Datei.

    Zwei Windows-Eigenheiten sind hier beruecksichtigt:
    - os.replace scheitert, solange ein anderer Prozess das Ziel offen hat
      (z.B. ein Poller, der den Auftragsstand liest). Deshalb wiederholen.
    - Die Zwischendatei bekommt einen eindeutigen Namen, damit zwei
      gleichzeitige Schreibvorgaenge sich nicht gegenseitig die Datei
      wegziehen.
    """
    pfad.parent.mkdir(parents=True, exist_ok=True)
    temp = pfad.with_name(f"{pfad.name}.{os.getpid()}_{uuid.uuid4().hex[:8]}.tmp")
    inhalt = json.dumps(daten, indent=2, ensure_ascii=False)

    try:
        temp.write_text(inhalt, encoding="utf-8")
        letzter: OSError | None = None
        for versuch in range(10):
            try:
                os.replace(temp, pfad)
                return
            except OSError as e:
                letzter = e
                time.sleep(0.05 * (versuch + 1))
        raise letzter  # type: ignore[misc]
    finally:
        # Bei Fehlschlag nichts liegen lassen.
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def file_lock(pfad: Path, timeout: int = 300):
    """Dateisperre ueber eine Sentinel-Datei.

    Uebernommen aus agent_memory_mcp/server.py:36-79, inklusive Aufraeumen
    verwaister Sperren - ein abgestuerzter Prozess soll den Ordner nicht
    dauerhaft blockieren.
    """
    sperre = pfad.with_suffix(pfad.suffix + ".lock")
    start = time.time()
    while True:
        try:
            with open(sperre, "x") as f:
                json.dump({"pid": os.getpid(), "locked_at": jetzt()}, f)
            break
        except FileExistsError:
            try:
                alter = time.time() - sperre.stat().st_mtime
                if alter > timeout:
                    sperre.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.time() - start > timeout:
                raise TimeoutError(f"Sperre nicht erhalten: {sperre}")
            time.sleep(0.1)
    try:
        yield
    finally:
        sperre.unlink(missing_ok=True)
