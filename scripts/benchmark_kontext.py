"""Misst, was Werkzeugbeschreibungen im Kontextfenster wirklich kosten.

Die Frage ist einfach: Wie viele Token gehen bei JEDER Nachricht drauf,
nur damit das Modell weiss, welche Werkzeuge es gibt?

Gemessen wird nicht geschaetzt, sondern gezaehlt - von LM Studio selbst:
Derselbe Auftrag wird zweimal geschickt, einmal ohne und einmal mit
Werkzeugen. Die Differenz der 'prompt_tokens' ist der Preis.

Mit --volllaufen kommt der Gegentest dazu: Das Modell wird mit einem
festen, kleinen Kontextfenster geladen und soll dann schreiben, bis nichts
mehr hineingeht. Was dabei herauskommt, ist der Platz, der nach den
Werkzeugbeschreibungen zum Arbeiten uebrig bleibt - keine Rechnung,
sondern das, was das Modell tatsaechlich noch unterbekommt.

    uv run python scripts/benchmark_kontext.py

    --ohne-modell     nur Zeichen zaehlen, LM Studio nicht anfassen
    --modell NAME     anderes Modell zum Zaehlen (Standard: subagent_modell)
    --volllaufen      zusaetzlich schreiben lassen, bis der Kontext voll ist
    --kontext N       Fenstergroesse fuer diesen Test (Standard: 16384)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

WURZEL = Path(__file__).parent.parent
sys.path.insert(0, str(WURZEL))

import httpx  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from agentmem import config  # noqa: E402

# Fremde Server schreiben gern Klartext auf stdout, bevor das Protokoll
# anlaeuft. Der MCP-Leser wirft darueber je einen vollen Traceback.
logging.getLogger("mcp.client.stdio").setLevel(logging.CRITICAL)

START_TIMEOUT = 30.0
SELBST = "agent-memory"


# --------------------------------------------------------------------------
# Werkzeuge einsammeln
# --------------------------------------------------------------------------


def _ist_selbst(name: str) -> bool:
    return any(g in name.lower() for g in config.GESPERRTE_MCP_NAMEN)


async def _hole_werkzeuge(name: str, cfg: dict) -> tuple[list[dict], str | None]:
    """Startet einen Server, holt die vollen Werkzeugdefinitionen, faehrt ihn
    wieder herunter. Rueckgabe: (Werkzeuge, Fehler)."""
    params = StdioServerParameters(
        command=cfg["command"],
        args=cfg.get("args", []),
        env={**cfg.get("env", {})} or None,
    )
    try:
        async with asyncio.timeout(START_TIMEOUT):
            async with stdio_client(params) as (lesen, schreiben):
                async with ClientSession(lesen, schreiben) as sitzung:
                    await sitzung.initialize()
                    werkzeuge = (await sitzung.list_tools()).tools
                    return [
                        {
                            "type": "function",
                            "function": {
                                "name": f"{name}__{t.name}"[:64],
                                "description": t.description or "",
                                "parameters": t.inputSchema or {"type": "object"},
                            },
                        }
                        for t in werkzeuge
                    ], None
    except TimeoutError:
        return [], f"kein Start binnen {START_TIMEOUT:.0f}s"
    except Exception as e:  # noqa: BLE001 - jeder Serverfehler ist hier nur eine Zeile
        kurz = str(e).split("\n")[0][:70]
        return [], f"{type(e).__name__}: {kurz}"


async def sammle_alle() -> tuple[dict[str, list[dict]], dict[str, str]]:
    pfad = Path(config.load()["pfade"]["lmstudio_mcp_json"])
    if not pfad.exists():
        print(f"mcp.json nicht gefunden: {pfad}")
        sys.exit(1)
    server = json.loads(pfad.read_text(encoding="utf-8")).get("mcpServers", {})

    werkzeuge: dict[str, list[dict]] = {}
    fehler: dict[str, str] = {}
    print(f"\n--- {len(server)} Server aus mcp.json starten und abfragen ---")
    for name, cfg in sorted(server.items()):
        t0 = time.time()
        w, f = await _hole_werkzeuge(name, cfg)
        dauer = time.time() - t0
        if f:
            fehler[name] = f
            print(f"  {name:<20} --      {f}   ({dauer:.1f}s)")
        else:
            werkzeuge[name] = w
            print(f"  {name:<20} {len(w):>3} Werkzeuge   ({dauer:.1f}s)")
    return werkzeuge, fehler


# --------------------------------------------------------------------------
# Token zaehlen lassen
# --------------------------------------------------------------------------

_NACHRICHTEN = [
    {"role": "system", "content": "Du bist ein hilfreicher Assistent."},
    {"role": "user", "content": "Was hast du fuer Werkzeuge?"},
]


def zaehle_tokens(basis_url: str, modell: str, tools: list[dict] | None) -> int:
    """Fragt LM Studio, wie viele Prompt-Token dieser Aufruf kostet."""
    nutzlast: dict = {
        "model": modell,
        "messages": _NACHRICHTEN,
        "max_tokens": 1,
        "temperature": 0,
    }
    if tools:
        nutzlast["tools"] = tools
    antwort = httpx.post(
        f"{basis_url}/v1/chat/completions", json=nutzlast, timeout=600.0
    )
    antwort.raise_for_status()
    return antwort.json()["usage"]["prompt_tokens"]


def zeichen(tools: list[dict]) -> int:
    return len(json.dumps(tools, ensure_ascii=False))


# --------------------------------------------------------------------------
# Den Kontext wirklich volllaufen lassen
# --------------------------------------------------------------------------

# Eine Aufgabe ohne natuerliches Ende. Alles andere waere unbrauchbar: Ein
# Modell, das nach drei Absaetzen fertig ist, sagt nichts darueber aus,
# wieviel Platz es gehabt haette.
_SCHREIBAUFTRAG = [
    {"role": "system", "content": "Du bist ein hilfreicher Assistent."},
    {
        "role": "user",
        "content": (
            "Zaehle von 1 aufwaerts. Schreibe je Zeile die Zahl, einen "
            "Doppelpunkt und einen kurzen Satz ueber Werkzeuge, Gedaechtnis "
            "oder Kontextfenster. Hoere nicht von selbst auf - schreibe "
            "weiter, bis kein Platz mehr da ist."
        ),
    },
]


def lade_modell(modell: str, kontext: int) -> bool:
    """Laedt das Modell mit einer festen Fenstergroesse ueber die lms-CLI.

    Ohne festes Fenster misst der Test nichts Vergleichbares: LM Studio
    nimmt sonst irgendeine Standardgroesse, und die kann zwischen zwei
    Laeufen eine andere sein.
    """
    import shutil
    import subprocess

    lms = shutil.which("lms") or str(Path.home() / ".lmstudio" / "bin" / "lms.exe")
    if not Path(lms).exists():
        print(f"  lms-CLI nicht gefunden ({lms}) - nehme das Modell, wie es geladen ist.")
        return False
    # Erst alles entladen. Zwei Modelle gleichzeitig teilen sich den Speicher,
    # und was dann gemessen wird, ist die Auslagerung auf die CPU - nicht das
    # Modell. Auf dieser Maschine hat das eine Runde von Sekunden auf 266s
    # gebracht, ohne jede Fehlermeldung.
    # errors='replace': Die lms-CLI malt Fortschrittsbalken mit Zeichen, die
    # Windows' Standardkodierung nicht kennt. Ohne das stirbt der Lesethread
    # mit einem UnicodeDecodeError - mitten in einer Messung, die Minuten
    # gelaufen ist.
    lauf = dict(capture_output=True, text=True, encoding="utf-8", errors="replace")
    subprocess.run([lms, "unload", "--all"], timeout=120, **lauf)
    print(f"  {modell} mit {kontext} Token Kontext laden (alles andere entladen) ...")
    erg = subprocess.run(
        [lms, "load", modell, "-c", str(kontext), "-y"], timeout=600, **lauf
    )
    if erg.returncode != 0:
        print(f"  Laden fehlgeschlagen: {(erg.stderr or erg.stdout).strip()[:200]}")
        return False
    return True


def volllaufen(basis_url: str, modell: str, tools: list[dict] | None, kontext: int) -> dict:
    """Runde um Runde weiterschreiben lassen, bis das Fenster voll ist.

    Eine einzelne Antwort reicht dafuer nicht: Ein Modell hoert von selbst
    auf, lange bevor der Platz alle ist - im ersten Versuch nach 649 von
    11.076 freien Token. Erst der fortgesetzte Dialog fuellt das Fenster
    wirklich, und genau so wird ein Kontextfenster im Betrieb auch voll:
    Nachricht fuer Nachricht.
    """
    verlauf = list(_SCHREIBAUFTRAG)
    t0 = time.time()
    runden = 0
    geschrieben = 0
    letzter_prompt = 0
    ende = "?"
    probe = ""
    werkzeugversuch = ""

    while True:
        nutzlast: dict = {
            "model": modell,
            "messages": verlauf,
            "max_tokens": -1,  # bis das Fenster voll ist
            "temperature": 0.7,
        }
        if tools:
            nutzlast["tools"] = tools
        try:
            antwort = httpx.post(
                f"{basis_url}/v1/chat/completions", json=nutzlast, timeout=1800.0
            )
        except httpx.HTTPError as e:
            return {"fehler": f"{type(e).__name__}: {str(e)[:120]}",
                    "runden": runden, "geschrieben": geschrieben,
                    "prompt_tokens": letzter_prompt, "dauer": time.time() - t0}

        if antwort.status_code != 200:
            # Der Fall, auf den es ankommt: Es passt nichts mehr hinein.
            try:
                grund = antwort.json().get("error")
                if isinstance(grund, dict):
                    grund = grund.get("message", "")
            except ValueError:
                grund = antwort.text
            return {"voll_weil": str(grund)[:160], "runden": runden,
                    "geschrieben": geschrieben, "prompt_tokens": letzter_prompt,
                    "dauer": time.time() - t0, "probe": probe,
                    "werkzeugversuch": werkzeugversuch}

        d = antwort.json()
        wahl = (d.get("choices") or [{}])[0]
        nutzung = d.get("usage", {})
        letzter_prompt = nutzung.get("prompt_tokens", 0)
        neu = nutzung.get("completion_tokens", 0)
        geschrieben += neu
        runden += 1
        ende = wahl.get("finish_reason", "?")
        inhalt = (wahl.get("message", {}).get("content") or "").strip()
        if inhalt:
            probe = inhalt[-200:]

        # Statt zu schreiben ein Werkzeug aufrufen heisst: Das Modell hat den
        # Auftrag nicht mehr vor Augen. Genau das passiert, wenn der Prompt
        # nicht mehr ins Fenster passt und der Anfang still herausfaellt.
        rufe = wahl.get("message", {}).get("tool_calls") or []
        if rufe and not inhalt:
            werkzeugversuch = (rufe[0].get("function", {}) or {}).get("name", "?")
            return {"voll_weil": f"Modell rief '{werkzeugversuch}' auf statt zu schreiben "
                                 f"- der Auftrag war aus dem Fenster gefallen",
                    "runden": runden, "geschrieben": geschrieben,
                    "prompt_tokens": letzter_prompt, "dauer": time.time() - t0,
                    "probe": probe, "werkzeugversuch": werkzeugversuch}

        # Passt die naechste Runde noch? Ein bisschen Luft lassen, sonst
        # bricht der Aufruf mittendrin ab statt sauber zu enden.
        if letzter_prompt + neu >= kontext - 200:
            return {"voll_weil": "Fenster voll", "runden": runden,
                    "geschrieben": geschrieben, "prompt_tokens": letzter_prompt + neu,
                    "dauer": time.time() - t0, "probe": probe, "ende": ende}

        verlauf = verlauf + [
            {"role": "assistant", "content": inhalt},
            {"role": "user", "content": "Weiter. Nicht wiederholen, an der letzten Zahl anknuepfen."},
        ]
        if runden > 60:  # Reissleine
            return {"voll_weil": "60 Runden erreicht", "runden": runden,
                    "geschrieben": geschrieben, "prompt_tokens": letzter_prompt,
                    "dauer": time.time() - t0, "probe": probe, "ende": ende}


# --------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--ohne-modell", action="store_true", help="nur Zeichen zaehlen")
    p.add_argument("--modell", default=None, help="Modell zum Token-Zaehlen")
    p.add_argument("--volllaufen", action="store_true", help="Kontext volllaufen lassen")
    p.add_argument("--kontext", type=int, default=16384, help="Fenstergroesse dafuer")
    args = p.parse_args()

    cfg = config.load()
    alle, fehler = asyncio.run(sammle_alle())

    eigene = {n: w for n, w in alle.items() if _ist_selbst(n)}
    fremde = {n: w for n, w in alle.items() if not _ist_selbst(n)}

    if not eigene:
        print(f"\n'{SELBST}' steht nicht in mcp.json oder startet nicht - Vergleich unmoeglich.")
        sys.exit(1)

    eigene_tools = [t for w in eigene.values() for t in w]
    fremde_tools = [t for w in fremde.values() for t in w]
    alle_tools = eigene_tools + fremde_tools

    zeilen = [
        ("alle Server zusammen", len(alle), alle_tools),
        (f"nur {SELBST}", len(eigene), eigene_tools),
    ]

    tokens: dict[str, int] = {}
    if not args.ohne_modell:
        modell = (args.modell or cfg["subagent_modell"]).split(",")[0].strip()
        if modell == "auto":
            print("\nsubagent_modell steht auf 'auto' - bitte --modell NAME angeben.")
            sys.exit(1)
        basis = cfg["lmstudio_base_url"]
        print(f"\n--- Token zaehlen lassen von {modell} ---")
        try:
            leer = zaehle_tokens(basis, modell, None)
            print(f"  Grundlast ohne Werkzeuge: {leer} Token")
            for name, _, tools in zeilen:
                t = zaehle_tokens(basis, modell, tools) - leer
                tokens[name] = t
                print(f"  {name:<24} {t:>7} Token")
        except httpx.HTTPError as e:
            print(f"  LM Studio antwortet nicht ({e}) - zaehle nur Zeichen.")
            tokens = {}

    print(f"\n{'=' * 74}")
    print(f"  {'in mcp.json eingetragen':<24} {'Werkzeuge':>10} {'Zeichen':>10} {'Token':>10}")
    print(f"  {'-' * 70}")
    for name, _, tools in zeilen:
        tok = f"{tokens[name]:>10,}" if name in tokens else f"{'~' + format(zeichen(tools) // 4, ','):>10}"
        print(f"  {name:<24} {len(tools):>10} {zeichen(tools):>10,} {tok}")

    if len(zeilen) == 2 and tokens:
        voll, schlank = tokens[zeilen[0][0]], tokens[zeilen[1][0]]
        if voll:
            print(f"  {'-' * 70}")
            print(f"  Ersparnis: {voll - schlank:,} Token je Nachricht "
                  f"({(voll - schlank) / voll * 100:.0f} %)")
            for fenster in (8192, 16384, 32768):
                print(f"    bei {fenster // 1024:>2}k Kontext: "
                      f"{voll / fenster * 100:>5.0f} % belegt  ->  {schlank / fenster * 100:>4.0f} %")
    print(f"{'=' * 74}")

    if args.volllaufen and not args.ohne_modell:
        basis = cfg["lmstudio_base_url"]
        modelle = [m.strip() for m in (args.modell or cfg["subagent_modell"]).split(",")]
        for modell in modelle:
            print(f"\n{'=' * 74}")
            print(f"  Schreiben lassen, bis {args.kontext:,} Token voll sind - {modell}")
            print(f"{'=' * 74}")
            lade_modell(modell, args.kontext)
            for name, _, tools in zeilen:
                print(f"\n  {name} ({len(tools)} Werkzeuge):")
                e = volllaufen(basis, modell, tools, args.kontext)
                if e.get("fehler"):
                    print(f"    Abbruch: {e['fehler']}")
                    continue
                frei = args.kontext - e["prompt_tokens"] if e["prompt_tokens"] < args.kontext else 0
                print(f"    Werkzeuge belegen   : {zeichen(tools) // 4:>7,} Token (geschaetzt)")
                print(f"    Runden bis voll     : {e['runden']:>7}")
                print(f"    wirklich geschrieben: {e['geschrieben']:>7,} Token "
                      f"in {e['dauer']:.0f}s")
                print(f"    Schluss             : {e.get('voll_weil', '?')}")
                if e.get("probe"):
                    print(f"    zuletzt geschrieben : ...{e['probe'][-80:]}")

    if fehler:
        print("\n  Nicht gestartet (nicht mitgezaehlt):")
        for name, f in fehler.items():
            print(f"    {name:<20} {f}")

    print(f"\n  Gemessen am {time.strftime('%d.%m.%Y %H:%M')}, "
          f"{len(alle)} von {len(alle) + len(fehler)} Servern erreichbar.")


if __name__ == "__main__":
    main()
