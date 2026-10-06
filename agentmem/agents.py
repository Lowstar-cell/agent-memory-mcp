"""Subagents: Auftraege im Hintergrund, mit eigenem Werkzeug-Ablauf.

Der Aufrufer bekommt sofort eine Auftragsnummer zurueck und kann weiter-
arbeiten. Der Auftrag laeuft in einem Hintergrund-Thread:

    Anfrage an das Modell (mit Werkzeugen)
      -> Modell will ein Werkzeug benutzen
      -> agent-memory ruft es beim Kindprozess auf
      -> Ergebnis zurueck ins Gespraech
      -> naechste Runde
    ... bis das Modell fertig ist, die Rundengrenze oder die Zeit erreicht ist.

Nur die Kurzfassung geht an den Aufrufer zurueck. Der komplette Verlauf
inklusive aller Werkzeugaufrufe liegt in data/jobs/job_*.json - abrufbar
ueber open_record(), aber eben nur wenn jemand ihn wirklich braucht.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import config, lmstudio, loops, mcpclient, store

# Werkzeugergebnisse koennen riesig sein (ganze Webseiten). Was darueber
# hinausgeht, sieht das Modell nicht - sonst ist der Kontext nach einem
# einzigen Suchtreffer voll.
MAX_WERKZEUG_ZEICHEN = 6000

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(
                max_workers=config.load()["subagent"]["max_gleichzeitig"],
                thread_name_prefix="agentmem-job",
            )
        return _pool


def _job_pfad(job_id: str) -> Path:
    return store.data_dir() / "jobs" / f"{job_id}.json"


def _speichern(job: dict) -> None:
    store.save_json(_job_pfad(job["id"]), job)


def lade_job(job_id: str) -> dict | None:
    return store.load_json(_job_pfad(job_id))


def _nur_buchstaben(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _werkzeug_zuordnen(name: str, zuordnung: dict[str, tuple[str, str]]) -> str | None:
    """Einen krumm geschriebenen Werkzeugnamen dem echten zuordnen.

    Kleine Modelle verstuemmeln gern die Trennzeichen: Aus
    'filesystem-full__read_text_file' wird 'filesystem_full_read_text_file'.
    Auf Zeichenebene ist das ein anderer Name, gemeint ist offensichtlich
    derselbe. Deshalb wird beides auf blosse Buchstaben und Ziffern
    heruntergebrochen und dann verglichen.
    """
    if name in zuordnung:
        return name
    ziel = _nur_buchstaben(name)
    for voll in zuordnung:
        if _nur_buchstaben(voll) == ziel:
            return voll
    # Zweiter Anlauf: nur der Werkzeugteil, ohne Servernamen davor.
    for voll, (_, werkzeug) in zuordnung.items():
        if _nur_buchstaben(werkzeug) == ziel:
            return voll
    return None


def _werkzeugwunsch_aus_text(text: str, zuordnung: dict[str, tuple[str, str]]) -> list[dict]:
    """Werkzeugaufrufe herausholen, die im Antworttext gelandet sind.

    Nicht jedes Modell benutzt den Werkzeug-Kanal der Schnittstelle. Kleine
    schreiben den Aufruf stattdessen als JSON in den Fliesstext:

        {"name": "filesystem_full_read_text_file", "arguments": {"path": "..."}}

    Fuer LM Studio ist das eine gewoehnliche Textantwort, der Auftrag gilt als
    beantwortet - und zurueck kommt statt des Dateiinhalts ein Stueck JSON.
    Genau so ist hier ein 4B-Modell gescheitert, obwohl ihm 14 Werkzeuge
    angeboten wurden und es das richtige gewaehlt hatte.

    Der Wunsch wird deshalb erkannt und ausgefuehrt. Was das Modell falsch
    macht, ist die Verpackung, nicht die Absicht.
    """
    if not text or "{" not in text:
        return []

    roh = re.sub(r"</?tool_call>|</?function_call>|```(?:json)?", " ", text)
    gefunden: list[dict] = []

    # Ausbalancierte JSON-Objekte einsammeln - eine Regex reicht nicht, weil
    # die Argumente selbst geschachtelt sein koennen.
    tiefe = start = 0
    for i, z in enumerate(roh):
        if z == "{":
            if tiefe == 0:
                start = i
            tiefe += 1
        elif z == "}" and tiefe:
            tiefe -= 1
            if tiefe == 0:
                try:
                    daten = json.loads(roh[start : i + 1])
                except json.JSONDecodeError:
                    continue
                if isinstance(daten, dict):
                    gefunden.append(daten)

    aufrufe = []
    for daten in gefunden:
        if "function" in daten and isinstance(daten["function"], dict):
            daten = daten["function"]
        name = daten.get("name") or daten.get("tool") or daten.get("tool_name")
        if not isinstance(name, str):
            continue
        voll = _werkzeug_zuordnen(name, zuordnung)
        if voll is None:
            continue
        args = daten.get("arguments", daten.get("parameters", daten.get("args", {})))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        aufrufe.append(
            {
                "id": f"text_{len(aufrufe)}",
                "type": "function",
                "function": {"name": voll, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        )
    return aufrufe


def _rolle_laden(rolle: str) -> str:
    pfad = config.ROLES_DIR / f"{rolle}.md"
    if not pfad.exists():
        verfuegbar = sorted(p.stem for p in config.ROLES_DIR.glob("*.md"))
        raise ValueError(f"Rolle '{rolle}' gibt es nicht. Verfuegbar: {verfuegbar}")
    return pfad.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Der eigentliche Ablauf
# --------------------------------------------------------------------------


# Wie oft ein Auftrag ein weggeladenes Modell verkraftet, bevor aufgegeben
# wird. Mehr als zwei Anlaeufe heisst: Es raeumt jemand systematisch auf, und
# dann hilft Weiterversuchen auch nicht.
MAX_ERHOLUNGEN = 2

_WEG = ("unload", "not loaded", "no model", "model not found")


def _chat_zaeh(job: dict, modell: str, verlauf: list[dict], **kw) -> tuple[dict, str]:
    """Eine Modellrunde, die einen Modellwechsel unter sich uebersteht.

    Bei lokalen Modellen ist das kein Sonderfall: LM Studio entlaedt nach
    Leerlauf, und wer im Chat ein anderes Modell waehlt, verdraengt das
    laufende. Der Auftrag im Hintergrund bekommt dann mitten im Satz
    HTTP 400 "Model unloaded by user or API request" - und war frueher
    verloren, obwohl er nichts falsch gemacht hat.

    Gibt (ergebnis, benutztes_modell) zurueck; das Modell kann sich dabei
    geaendert haben.
    """
    try:
        return lmstudio.chat(modell, verlauf, **kw), modell
    except lmstudio.LMStudioFehler as e:
        text = str(e).lower()
        if not any(w in text for w in _WEG):
            raise
        if len(job.get("erholungen", [])) >= MAX_ERHOLUNGEN:
            raise
        job.setdefault("erholungen", []).append(str(e)[:200])

        neu, grund = lmstudio.modell_aufloesen(job.get("modell_wunsch"))
        job["modell"] = neu
        job["modell_begruendung"] = f"{grund} - neu aufgeloest, altes Modell war weg"
        _speichern(job)
        return lmstudio.chat(neu, verlauf, **kw), neu


def _ausfuehren(job: dict) -> None:
    try:
        cfg = config.load()["subagent"]
        frist = time.time() + cfg["timeout_sekunden"]

        job["status"] = "laeuft"
        job["gestartet"] = store.jetzt()
        _speichern(job)

        modell, begruendung = lmstudio.modell_aufloesen(job.get("modell_wunsch"))
        job["modell"] = modell
        job["modell_begruendung"] = begruendung
        _speichern(job)

        schemas: list[dict] = []
        zuordnung: dict[str, tuple[str, str]] = {}
        if job["tools"]:
            schemas, zuordnung = mcpclient.pool.werkzeuge(job["tools"])
        # Mitschreiben, wieviele Werkzeuge tatsaechlich angeboten wurden. Ohne
        # diese Zahl laesst sich hinterher nicht unterscheiden, ob ein Modell
        # das Werkzeug verschmaeht hat oder ob ihm nie eines angeboten wurde -
        # von aussen sehen beide Faelle gleich aus.
        job["werkzeuge_angeboten"] = len(schemas)
        _speichern(job)

        verlauf: list[dict] = [
            {"role": "system", "content": job["system_prompt"]},
            {"role": "user", "content": job["aufgabe"]},
        ]

        waechter = loops.fuer_auftrag(job["id"])
        antworttext = ""
        stillstand = 0
        nachgehakt = False
        # Ist eine Schleife gebrochen worden, bekommt das Modell noch genau
        # eine Runde - aber ohne Werkzeuge. Dann MUSS es antworten, statt den
        # naechsten Aufruf zu versuchen.
        werkzeuge_aus = False

        for runde in range(1, cfg["max_runden"] + 1):
            if time.time() > frist:
                job["abbruchgrund"] = f"Zeit ueberschritten nach Runde {runde - 1}"
                break

            ergebnis, modell = _chat_zaeh(
                job,
                modell,
                verlauf,
                tools=None if werkzeuge_aus else (schemas or None),
                max_tokens=cfg["max_antwort_tokens"],
                timeout=max(30.0, frist - time.time()),
            )
            nachricht = ergebnis["message"]
            verlauf.append(nachricht)
            job["runden"] = runde

            aufrufe = nachricht.get("tool_calls") or []
            if not aufrufe and not werkzeuge_aus and zuordnung:
                # Der Aufruf steckt womoeglich im Text statt im Werkzeug-Kanal.
                aus_text = _werkzeugwunsch_aus_text(nachricht.get("content") or "", zuordnung)
                if aus_text:
                    aufrufe = aus_text
                    job["werkzeuge_aus_text"] = job.get("werkzeuge_aus_text", 0) + len(aus_text)
                    # Im Verlauf so hinterlegen, als waere es ein regulaerer
                    # Aufruf gewesen. Sonst folgt gleich eine tool-Nachricht
                    # auf eine Assistenten-Nachricht ohne tool_calls, und die
                    # Schnittstelle weist die naechste Runde ab.
                    nachricht["tool_calls"] = aus_text
                    nachricht["content"] = None

            if not aufrufe:
                antworttext = (nachricht.get("content") or "").strip()
                if antworttext:
                    # Antwort ohne Beleg: Es standen Werkzeuge bereit, benutzt
                    # wurde keines - und trotzdem kommt eine konkrete Auskunft.
                    # Bei kleinen Modellen ist das keine Seltenheit, sondern
                    # ein Standardfehler. Hier hat ein 4B den Wert einer
                    # Konfigurationsdatei frei erfunden, ohne sie zu oeffnen,
                    # und es klang genauso ueberzeugend wie die echte Antwort.
                    # Einmal nachhaken kostet eine Runde und faengt das ab.
                    geklappt = any(not w["fehler"] for w in job.get("werkzeugaufrufe", []))
                    if schemas and not geklappt and not nachgehakt and not werkzeuge_aus:
                        nachgehakt = True
                        antworttext = ""
                        versucht = bool(job.get("werkzeugaufrufe"))
                        verlauf.append(
                            {
                                "role": "user",
                                "content": (
                                    # Zwei verschiedene Fehler, zwei verschiedene Saetze.
                                    "Der Werkzeugaufruf ist fehlgeschlagen. Niemand liest "
                                    "hier mit und wiederholt etwas fuer dich - korrigiere "
                                    "die Argumente selbst und ruf das Werkzeug nochmal auf. "
                                    "Erst danach antwortest du."
                                    if versucht else
                                    "Du hast geantwortet, ohne ein einziges Werkzeug zu "
                                    "benutzen. Aus dem Gedaechtnis antworten gilt hier "
                                    "nicht - du kannst den Inhalt nicht kennen. Ruf jetzt "
                                    "das passende Werkzeug auf und antworte erst mit dem, "
                                    "was es zurueckgibt."
                                ),
                            }
                        )
                        continue
                    if schemas and not geklappt:
                        job["ohne_beleg"] = True
                    break
                # Weder Text noch Werkzeug: eine leere Runde. Zweimal
                # hintereinander ist kein Zufall mehr.
                stillstand += 1
                if stillstand >= 2:
                    b = waechter.stillstand(runde)
                    job["abbruchgrund"] = b.rat
                    job.setdefault("schleifen", []).append(b.als_dict())
                    break
                verlauf.append(
                    {
                        "role": "user",
                        "content": "Du hast nichts geliefert. Antworte jetzt mit dem "
                                   "Ergebnis, das du bisher hast - in ganzen Saetzen.",
                    }
                )
                continue

            if werkzeuge_aus:
                # Kann vorkommen, wenn das Modell Werkzeugaufrufe halluziniert,
                # obwohl gar keine angeboten wurden.
                antworttext = (nachricht.get("content") or "").strip()
                break

            stillstand = 0
            for aufruf in aufrufe:
                fn = aufruf.get("function", {})
                voll = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}

                befund = waechter.pruefe(voll, args)
                if befund is not None and befund.stufe == "stopp":
                    # Bewusst NICHT ausfuehren: der Aufruf ist identisch zu
                    # einem frueheren, das Ergebnis waere es auch.
                    inhalt = f"ABGEBROCHEN ({befund.art}). {befund.rat}"
                    fehler = True
                    werkzeuge_aus = True
                    job.setdefault("schleifen", []).append(befund.als_dict())
                    job["abbruchgrund"] = f"Schleife erkannt ({befund.art}) bei {voll}"
                elif voll not in zuordnung:
                    inhalt = (
                        f"FEHLER: Werkzeug '{voll}' gibt es nicht. "
                        f"Verfuegbar: {sorted(zuordnung)[:12]}"
                    )
                    fehler = True
                else:
                    server, werkzeug = zuordnung[voll]
                    try:
                        inhalt, fehler = mcpclient.pool.aufrufen(server, werkzeug, args)
                    except Exception as e:  # Kindprozess weg, Timeout, kaputte Antwort
                        inhalt, fehler = f"FEHLER beim Aufruf: {e}", True

                    leer = not inhalt.strip() or fehler
                    nachbefund = waechter.ergebnis_melden(voll, leer)
                    if nachbefund is not None:
                        inhalt = f"{inhalt}\n\n[{nachbefund.rat}]"
                        werkzeuge_aus = True
                        job.setdefault("schleifen", []).append(nachbefund.als_dict())
                        job["abbruchgrund"] = f"Schleife erkannt (fruchtlos) bei {voll}"
                    elif befund is not None:
                        inhalt = f"{inhalt}\n\n[Hinweis: {befund.rat}]"

                job.setdefault("werkzeugaufrufe", []).append(
                    {
                        "runde": runde,
                        "werkzeug": voll,
                        "argumente": args,
                        "fehler": fehler,
                        "zeichen": len(inhalt),
                        "ergebnis": inhalt[:MAX_WERKZEUG_ZEICHEN],
                    }
                )
                verlauf.append(
                    {
                        "role": "tool",
                        "tool_call_id": aufruf.get("id", voll),
                        "content": inhalt[:MAX_WERKZEUG_ZEICHEN],
                    }
                )
            _speichern(job)
        else:
            job["abbruchgrund"] = f"Rundengrenze ({cfg['max_runden']}) erreicht"

        if not antworttext:
            # Rundengrenze oder Zeit erreicht: letzte Modellaeusserung nehmen.
            for n in reversed(verlauf):
                if n.get("role") == "assistant" and (n.get("content") or "").strip():
                    antworttext = n["content"].strip()
                    break

        job["kurzfassung"] = antworttext or "(keine Antwort erzeugt)"
        job["verlauf"] = verlauf
        job["status"] = "fertig"

    except lmstudio.LMStudioZuLangsam as e:
        # Kein Programmfehler, sondern eine Aussage ueber die Maschine. Ein
        # Traceback waere hier irrefuehrend - er legt nahe, es sei etwas
        # kaputt. Es ist aber nur zu langsam, und das laesst sich beheben.
        job["status"] = "gescheitert"
        job["fehler"] = str(e)
        job["rat"] = (
            "Entweder ein kleineres Modell fuer Subagenten setzen "
            "(config.json: subagent_modell), Speicher freimachen, oder "
            "config.json: subagent.timeout_sekunden hochsetzen."
        )
        if job.get("runden"):
            job["kurzfassung"] = "(Zeit reichte nicht fuer eine vollstaendige Antwort)"

    except Exception as e:
        job["status"] = "gescheitert"
        job["fehler"] = f"{type(e).__name__}: {e}"
        job["traceback"] = traceback.format_exc()

    job["beendet"] = store.jetzt()
    try:
        _speichern(job)
    except OSError as e:
        # Letzter Ausweg: Wenn nicht einmal der Fehlerstand geschrieben werden
        # kann, darf der Auftrag nicht stumm auf "wartet" stehen bleiben.
        print(f"[agent-memory] Auftrag {job.get('id')} nicht speicherbar: {e}", file=sys.stderr)


# --------------------------------------------------------------------------
# Oberflaeche
# --------------------------------------------------------------------------


def starten(
    aufgabe: str,
    rolle: str = "research",
    tools: list[str] | None = None,
    modell: str | None = None,
    system_prompt: str | None = None,
    titel: str | None = None,
) -> dict:
    """Startet einen Auftrag und kehrt sofort zurueck."""
    store.ensure_dirs()

    if tools is None:
        # Rollen, die von sich aus Werkzeuge brauchen, bekommen sinnvolle
        # Voreinstellungen; 'summarize' arbeitet bewusst ohne.
        tools = {"research": ["web-search"], "read": ["filesystem"], "verify": []}.get(rolle, [])

    erlaubt = mcpclient.verfuegbare_server()
    abgelehnt = [t for t in tools if t not in erlaubt]
    tools = [t for t in tools if t in erlaubt]

    job = {
        "id": f"job_{store.zeitstempel()}_{uuid.uuid4().hex[:6]}",
        "titel": titel or aufgabe[:80],
        "aufgabe": aufgabe,
        "rolle": rolle,
        "tools": tools,
        "abgelehnte_tools": abgelehnt,
        "modell_wunsch": modell,
        "system_prompt": system_prompt or _rolle_laden(rolle),
        "status": "wartet",
        "runden": 0,
        "erstellt": store.jetzt(),
    }
    _speichern(job)
    _executor().submit(_ausfuehren, job)
    return job


def ergebnis(job_id: str, wait_ms: int = 0) -> dict | None:
    """Stand eines Auftrags. wait_ms wartet hoechstens 30 Sekunden - laenger
    haelt die Werkzeugantwort an LM Studio nicht durch."""
    frist = time.time() + min(wait_ms, 30_000) / 1000
    while True:
        job = lade_job(job_id)
        if job is None:
            return None
        if job["status"] in ("fertig", "gescheitert") or time.time() >= frist:
            return job
        time.sleep(0.25)


def auflisten(limit: int = 20) -> list[dict]:
    ordner = store.data_dir() / "jobs"
    if not ordner.exists():
        return []
    dateien = sorted(ordner.glob("job_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in dateien[:limit]:
        job = store.load_json(p, {})
        out.append(
            {
                "id": job.get("id"),
                "titel": job.get("titel"),
                "rolle": job.get("rolle"),
                "status": job.get("status"),
                "runden": job.get("runden"),
                "erstellt": job.get("erstellt"),
            }
        )
    return out


def kurzfassung(job: dict) -> dict:
    """Was der Aufrufer sieht - bewusst ohne Verlauf und Werkzeugausgaben."""
    knapp = {
        "job_id": job.get("id"),
        "status": job.get("status"),
        "rolle": job.get("rolle"),
        "runden": job.get("runden"),
        "modell": job.get("modell"),
    }
    if job.get("status") == "fertig":
        knapp["ergebnis"] = job.get("kurzfassung")
        knapp["werkzeuge_benutzt"] = [w["werkzeug"] for w in job.get("werkzeugaufrufe", [])]
        if job.get("abbruchgrund"):
            knapp["hinweis"] = job["abbruchgrund"]
        # Nicht verschweigen: ein Ergebnis, das aus einer abgebrochenen
        # Schleife stammt, ist weniger wert als eines aus sauberer Arbeit.
        if job.get("schleifen"):
            knapp["schleifen"] = job["schleifen"]
        if job.get("erholungen"):
            knapp["modell_war_zwischendurch_weg"] = len(job["erholungen"])
        if job.get("ohne_beleg"):
            knapp["warnung"] = (
                "Der Subagent hat kein einziges Werkzeug benutzt, obwohl welche "
                "bereitstanden. Diese Antwort ist unbelegt - behandle sie als "
                "Vermutung, nicht als Auskunft."
            )
    elif job.get("status") == "gescheitert":
        knapp["fehler"] = job.get("fehler")
        if job.get("rat"):
            knapp["was_jetzt"] = job["rat"]
    if job.get("abgelehnte_tools"):
        knapp["nicht_freigegeben"] = job["abgelehnte_tools"]
    knapp["volles_protokoll"] = f"open_record('{job.get('id')}')"
    return knapp
