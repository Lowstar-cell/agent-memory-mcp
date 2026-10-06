"""Kontext-Uebergabe und Modellwechsel.

Ehrlich zur Mechanik, weil sonst falsche Erwartungen entstehen:
Ein Modellwechsel leert den Chat in LM Studio NICHT. Der Verlauf bleibt
stehen. Was den Kontext wirklich leert, ist ein neuer Chat.

Deshalb dieser Ablauf:

    context_checkpoint()  -> verdichtet den Stand in eine Uebergabe-Datei
    [model_cycle()]       -> optional anderes Modell laden
    NEUER CHAT            -> erst das leert den Kontext
    context_resume()      -> Uebergabe als erste Nachricht einfuegen

Das Verdichten uebernimmt ein Modell, nicht ein Algorithmus: Was
weggelassen werden darf, haengt vom Inhalt ab und nicht von der Textlaenge.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from . import config, lmstudio, store

VERDICHTER_PROMPT = """Du verdichtest einen Arbeitsstand zu einer Uebergabe.

Behalte: Ziel, getroffene Entscheidungen samt Begruendung, was fertig ist,
was offen ist, der naechste Schritt, konkrete Pfade, Befehle, Namen, Zahlen.
Wirf weg: Gespraechsverlauf, Hoeflichkeiten, verworfene Zwischenstaende.
Erfinde nichts. Was unklar war, bleibt als offener Punkt stehen.

Antworte in genau diesen Abschnitten, ohne Vorrede:

## Ziel
## Entscheidungen
## Erledigt
## Offen
## Naechster Schritt
## Wichtige Pfade und Befehle
"""


def _ordner() -> Path:
    return store.data_dir() / "handoffs"


def checkpoint(
    goal: str,
    notes: str = "",
    done: list[str] | None = None,
    open_items: list[str] | None = None,
    next_step: str = "",
    files: list[str] | None = None,
    compress: bool = True,
) -> dict:
    store.ensure_dirs()

    roh = f"""# Ziel
{goal}

# Notizen
{notes}

# Erledigt
{chr(10).join('- ' + d for d in (done or [])) or '(nichts angegeben)'}

# Offen
{chr(10).join('- ' + o for o in (open_items or [])) or '(nichts angegeben)'}

# Naechster Schritt
{next_step or '(nicht angegeben)'}

# Pfade
{chr(10).join('- ' + f for f in (files or [])) or '(keine)'}
"""

    verdichtet = roh
    verdichter_status = "uebersprungen (compress=False)"
    if compress:
        try:
            modell, _ = lmstudio.modell_aufloesen()
            antwort = lmstudio.chat(
                modell,
                [
                    {"role": "system", "content": VERDICHTER_PROMPT},
                    {"role": "user", "content": roh},
                ],
                max_tokens=1500,
                temperature=0.2,
            )
            inhalt = (antwort["message"].get("content") or "").strip()
            if inhalt:
                verdichtet = inhalt
                verdichter_status = f"verdichtet mit {modell}"
            else:
                verdichter_status = "Modell lieferte nichts, Rohfassung behalten"
        except lmstudio.LMStudioFehler as e:
            verdichter_status = f"nicht moeglich, Rohfassung behalten ({str(e)[:100]})"

    hid = f"handoff_{store.zeitstempel()}_{store.slugify(goal, 40)}"
    text = f"""---
id: {hid}
ziel: {goal}
erstellt: {store.jetzt()}
verdichter: {verdichter_status}
---

{verdichtet}
"""
    (_ordner() / f"{hid}.md").write_text(text, encoding="utf-8")

    return {
        "handoff_id": hid,
        "verdichter": verdichter_status,
        "hinweis": (
            "Uebergabe gespeichert. Zum Weiterarbeiten mit frischem Kontext: "
            "NEUEN Chat oeffnen, dann context_resume() aufrufen. Ein blosser "
            "Modellwechsel leert den Chatverlauf nicht."
        ),
        "uebergabe": verdichtet,
    }


def resume(handoff_id: str | None = None) -> dict | None:
    ordner = _ordner()
    if not ordner.exists():
        return None
    if handoff_id:
        pfad = ordner / f"{handoff_id}.md"
        if not pfad.exists():
            return None
    else:
        dateien = sorted(ordner.glob("handoff_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not dateien:
            return None
        pfad = dateien[0]
    return {"handoff_id": pfad.stem, "uebergabe": pfad.read_text(encoding="utf-8")}


def auflisten(limit: int = 10) -> list[dict]:
    ordner = _ordner()
    if not ordner.exists():
        return []
    dateien = sorted(ordner.glob("handoff_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"handoff_id": p.stem, "geaendert": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))} for p in dateien[:limit]]


def model_cycle(model: str | None = None, context_length: int | None = None, verzoegerung: float = 2.0) -> dict:
    """Entlaedt das laufende Sprachmodell und laedt eines.

    Der Wechsel passiert verzoegert in einem Hintergrund-Thread. Wuerde man
    sofort entladen, kaeme diese Antwort hier nie beim Aufrufer an - das
    Modell, das auf sie wartet, waere ja gerade weg.
    """
    laufend = lmstudio.laufendes_sprachmodell()
    ziel = model or laufend
    if not ziel:
        return {"ok": False, "fehler": "Kein Modell angegeben und keines geladen."}

    def wechseln() -> None:
        time.sleep(verzoegerung)
        try:
            if laufend:
                lmstudio.unload_model(laufend)
            lmstudio.load_model(ziel, context_length)
        except lmstudio.LMStudioFehler:
            pass  # Der Aufrufer ist zu diesem Zeitpunkt ohnehin nicht mehr da.

    threading.Thread(target=wechseln, name="agentmem-modelcycle", daemon=True).start()

    return {
        "ok": True,
        "entlaedt": laufend,
        "laedt": ziel,
        "in_sekunden": verzoegerung,
        "hinweis": (
            "Der Wechsel laeuft gleich an. ACHTUNG: Das leert den Chatverlauf NICHT. "
            "Fuer frischen Kontext vorher context_checkpoint() aufrufen, danach einen "
            "NEUEN Chat oeffnen und dort context_resume()."
        ),
    }
