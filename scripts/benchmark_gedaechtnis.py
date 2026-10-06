"""Misst, was ein Treffer im Gedaechtnis spart.

Das andere Benchmark (benchmark_kontext.py) zaehlt, was Werkzeug-
beschreibungen kosten. Das ist aber nur die halbe Rechnung. Der eigentliche
Gewinn kommt beim ZWEITEN Mal: wenn eine aehnliche, muehsame Aufgabe
wiederkommt und recall() die fertige Loesung schon hat, statt dass ein
Modell sie noch einmal erarbeitet.

Genau das wird hier gegenuebergestellt - dieselbe Frage, zwei Wege:

    erarbeiten   Ein Subagent bekommt Werkzeuge und muss die Antwort aus
                 den Dateien holen. Kostet Runden, Sekunden und Token.
    erinnern     recall() sucht, open_record() oeffnet den einen Treffer.

Gemessen wird beides gleich: alle Token, die dabei durch das
Kontextfenster gehen, und die Zeit von der Frage bis zur Antwort.

    uv run python scripts/benchmark_gedaechtnis.py
    uv run python scripts/benchmark_gedaechtnis.py --modell qwen/qwen3-4b-2507
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

WURZEL = Path(__file__).parent.parent
sys.path.insert(0, str(WURZEL))

import httpx  # noqa: E402

import server  # noqa: E402
from agentmem import config  # noqa: E402

# Die Frage ist mit Bedacht gewaehlt, in zwei Richtungen:
#
# 1. Sie laesst sich auf BEIDEN Wegen beantworten - aus den Dateien (die
#    Begruendung steht als Kommentar in config.json) und aus dem Gedaechtnis
#    (ein Playbook haelt genau das fest). Waere sie nur auf einem Weg
#    loesbar, waere der Vergleich wertlos.
# 2. Sie benutzt ANDERE Woerter als der gespeicherte Ausloeser. Sonst wuerde
#    schon die blosse Stichwortsuche treffen und die Bedeutungssuche haette
#    nichts zu tun - der Test wuerde bestehen, ohne etwas zu zeigen.
FRAGE = (
    "Ein Auftrag im Hintergrund kommt nicht vom Fleck, obwohl die "
    "Grafikkarte 24 GB hat. Woran liegt das und was hilft?"
)


def tokens_von(basis_url: str, modell: str, text: str) -> int:
    """Laesst LM Studio zaehlen, wie viele Token ein Text kostet."""
    try:
        antwort = httpx.post(
            f"{basis_url}/v1/chat/completions",
            json={
                "model": modell,
                "messages": [{"role": "user", "content": text}],
                "max_tokens": 1,
                "temperature": 0,
            },
            timeout=600.0,
        )
        antwort.raise_for_status()
        # Die paar Token der Rollen-Verpackung abziehen, damit nur der Inhalt zaehlt.
        return max(0, antwort.json()["usage"]["prompt_tokens"] - 8)
    except httpx.HTTPError:
        return 0


def weg_erinnern(basis_url: str, modell: str) -> dict:
    """recall() -> open_record(). Was davon geht ins Kontextfenster?"""
    t0 = time.time()
    treffer = server.recall(FRAGE)
    liste = treffer.get("treffer", []) if isinstance(treffer, dict) else treffer
    if not liste:
        return {"fehler": "recall() findet nichts - liegt ueberhaupt ein passender Eintrag vor?"}
    erster = liste[0]
    voll = server.open_record(erster["id"])
    dauer = time.time() - t0

    text_suche = json.dumps(liste, ensure_ascii=False)
    text_eintrag = json.dumps(voll, ensure_ascii=False)
    return {
        "dauer": dauer,
        "treffer": erster.get("title") or erster.get("id"),
        "guete": erster.get("score"),
        "tokens_suche": tokens_von(basis_url, modell, text_suche),
        "tokens_eintrag": tokens_von(basis_url, modell, text_eintrag),
        "modellrunden": 0,
    }


def weg_erarbeiten(basis_url: str, modell: str, werkzeuge: list[str]) -> dict:
    """Ein Subagent muss die Antwort selbst zusammensuchen."""
    t0 = time.time()
    # Bewusst fuehrend formuliert: Datei und Vorgehen stehen drin. Sonst
    # misst der Vergleich, ob ein 4B das richtige Werkzeug erraet - im
    # ersten Versuch wendete es ein Verzeichnis-Auflisten auf eine Datei an
    # und kam mit ENOTDIR zurueck. Auf dem Erinnern-Weg wird ja auch gesagt,
    # was zu tun ist (suchen, dann oeffnen); alles andere waere unfair.
    sp = server.agent_spawn(
        task=(f"{FRAGE} Die Begruendung steht als Kommentar in der Datei "
              f"{(WURZEL / 'config.json').as_posix()}. Lies diese Datei mit "
              f"einem Werkzeug zum Datei-LESEN (nicht auflisten) und "
              f"beantworte damit die Frage."),
        role="read",
        tools=werkzeuge,
        model=modell,
    )
    if not sp.get("job_id"):
        return {"fehler": f"kein Auftrag gestartet: {sp}"}

    erg = server.agent_result(sp["job_id"], wait_ms=30000)
    while erg.get("status") not in ("fertig", "gescheitert") and time.time() - t0 < 900:
        erg = server.agent_result(sp["job_id"], wait_ms=30000)
    dauer = time.time() - t0

    # Der volle Verlauf kommt aus der Auftragsdatei, nicht aus open_record():
    # das gibt bei Auftraegen absichtlich nur die Kurzfassung zurueck (genau
    # der Leitgedanke des Servers). Fuer diese Messung brauchen wir aber, was
    # wirklich durch das Kontextfenster ging.
    from agentmem import agents

    job = agents.lade_job(sp["job_id"]) or {}
    verlauf = job.get("verlauf", [])
    text = json.dumps(verlauf, ensure_ascii=False)
    runden = sum(1 for n in verlauf if n.get("role") == "assistant")
    return {
        "dauer": dauer,
        "status": erg.get("status"),
        "modellrunden": runden,
        "werkzeugaufrufe": len(erg.get("werkzeuge_benutzt") or []),
        "werkzeuge_angeboten": job.get("werkzeuge_angeboten", 0),
        "tokens_verlauf": tokens_von(basis_url, modell, text),
        "antwort": (erg.get("ergebnis") or "")[:200],
        "warnung": erg.get("warnung"),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--modell", default=None)
    p.add_argument("--werkzeuge", default="filesystem-full,filesystem,terminal",
                   help="Kommaliste; genommen wird der erste freigegebene")
    args = p.parse_args()

    cfg = config.load()
    basis = cfg["lmstudio_base_url"]
    modell = args.modell or cfg["subagent_modell"]

    from agentmem import mcpclient
    frei = set(mcpclient.verfuegbare_server())
    werkzeuge = [w for w in args.werkzeuge.split(",") if w.strip() in frei][:1]

    print(f"\nFrage: {FRAGE}")
    print(f"Modell: {modell}   Werkzeuge fuer den muehsamen Weg: {werkzeuge or 'KEINE'}")

    print("\n--- Weg 1: erinnern (recall + open_record) ---")
    b = weg_erinnern(basis, modell)
    if b.get("fehler"):
        print(f"  {b['fehler']}")
        sys.exit(1)
    b_gesamt = b["tokens_suche"] + b["tokens_eintrag"]
    print(f"  Treffer            : {b['treffer']}  (Guete {b['guete']})")
    print(f"  Trefferliste       : {b['tokens_suche']:>6,} Token")
    print(f"  voller Eintrag     : {b['tokens_eintrag']:>6,} Token")
    print(f"  zusammen           : {b_gesamt:>6,} Token in {b['dauer']:.1f}s, 0 Modellrunden")

    print("\n--- Weg 2: erarbeiten (Subagent mit Werkzeugen) ---")
    if not werkzeuge:
        print("  Kein freigegebener Server - uebersprungen.")
        return
    a = weg_erarbeiten(basis, modell, werkzeuge)
    if a.get("fehler"):
        print(f"  {a['fehler']}")
        return
    print(f"  Ergebnis           : {a['status']}, {a['modellrunden']} Modellrunden, "
          f"{a['werkzeugaufrufe']} Werkzeugaufrufe")
    print(f"  Verlauf im Fenster : {a['tokens_verlauf']:>6,} Token")
    print(f"  Dauer              : {a['dauer']:>6.1f}s")
    if a.get("warnung"):
        print(f"  Warnung            : {a['warnung'][:100]}")
    print(f"  Antwort            : {a['antwort'][:150]}")

    print(f"\n{'=' * 70}")
    if a["tokens_verlauf"] and b_gesamt:
        print(f"  erinnern   : {b_gesamt:>7,} Token   {b['dauer']:>6.1f}s   0 Modellrunden")
        print(f"  erarbeiten : {a['tokens_verlauf']:>7,} Token   {a['dauer']:>6.1f}s   "
              f"{a['modellrunden']} Modellrunden")
        print(f"  Verhaeltnis: {a['tokens_verlauf'] / b_gesamt:>6.1f}x Token   "
              f"{a['dauer'] / max(b['dauer'], 0.01):>5.1f}x Zeit")
    print("  Und das gilt ab dem ZWEITEN Mal - der erste Weg musste einmal")
    print("  gegangen werden, damit remember() etwas abzulegen hatte.")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
