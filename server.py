"""agent-memory MCP Server.

Ein Server, der selbst rechnet. Der Urahn (agent_memory_mcp) gab nur
Anweisungen zurueck - "du bist der Ausfuehrende!" - und das lokale Modell
musste alles allein stemmen. Hier delegiert der Server: an Subagenten, an
die Shell, an ein Verdichtermodell, an andere MCP-Server.

Drei Leitgedanken ziehen sich durch alle Werkzeuge:

1. Suchen gibt Verweise, Oeffnen gibt Inhalt.
   Ein Suchergebnis kostet rund 40 Woerter. Was davon wirklich gebraucht
   wird, holt sich der Aufrufer gezielt nach. Genau das ist der Unterschied
   zwischen einem Modell, das denken kann, und einem, dessen Kontext schon
   voll ist, bevor die Arbeit anfaengt.

2. Ein Server statt sechzehn.
   tool_find und tool_call reichen an alle anderen MCP-Server durch. Deren
   ueber hundert Werkzeugbeschreibungen muessen dadurch nicht mehr im
   Kontext liegen - zwei Beschreibungen genuegen.

3. Wer sich im Kreis dreht, wird gestoppt.
   Jeder Werkzeugaufruf laeuft durch einen Schleifenwaechter. Wiederholt
   sich etwas wortgleich, kommt statt derselben Antwort ein konkreter
   naechster Schritt zurueck. Siehe agentmem/loops.py.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any

from mcp.server.fastmcp import FastMCP

from agentmem import (
    agents,
    config,
    context as ctx,
    legacy,
    lmstudio,
    loops,
    mcpclient,
    records,
    search,
    skills,
    store,
    verify as verify_mod,
)

mcp = FastMCP("agent-memory")


# ==========================================================================
#  Schleifenwaechter fuer jedes Werkzeug
# ==========================================================================

# Woran man sieht, dass ein Aufruf nichts erbracht hat. Ohne das laesst sich
# die fruchtlose Suche nicht erkennen - die, bei der das Modell immer neue
# Wortlaute probiert und jedes Mal null Treffer bekommt.
_LEER_FELDER = ("treffer", "werkzeuge", "skills", "verbindungen", "auftraege")


def _ist_leer(ergebnis: Any) -> bool:
    if not isinstance(ergebnis, dict):
        return not bool(ergebnis)
    if ergebnis.get("ok") is False:
        return True
    for feld in _LEER_FELDER:
        if feld in ergebnis:
            return not ergebnis[feld]
    return False


def werkzeug(fn):
    """Registriert ein Werkzeug und legt den Schleifenwaechter davor.

    Bewusst ueber alle Werkzeuge und nicht nur ueber die verdaechtigen: Wo
    ein Modell haengenbleibt, laesst sich vorher nicht sagen, und ein
    vergessener Einzelfall faellt niemandem auf.
    """

    # Damit derselbe Aufruf dieselbe Signatur ergibt, egal ob er als
    # recall("x") oder recall(query="x") ankommt - sonst faende der Waechter
    # keine Wiederholung, weil sie anders geschrieben ist.
    namen = list(inspect.signature(fn).parameters)

    @functools.wraps(fn)
    def huelle(*args, **kwargs):
        vereinheitlicht = {**dict(zip(namen, args)), **kwargs}
        wache = loops.haupt()
        befund = wache.pruefe(fn.__name__, vereinheitlicht)
        if befund is not None and befund.stufe == "stopp":
            # Den Aufruf gar nicht erst ausfuehren. Das Ergebnis waere
            # dasselbe wie beim letzten Mal - und genau das ist das Problem.
            return {
                "ok": False,
                "ergebnis_unveraendert": True,
                **befund.als_dict(),
            }

        ergebnis = fn(*args, **kwargs)

        nachbefund = wache.ergebnis_melden(fn.__name__, _ist_leer(ergebnis))
        if isinstance(ergebnis, dict):
            if nachbefund is not None:
                return {**ergebnis, **nachbefund.als_dict()}
            if befund is not None:
                return {**ergebnis, "schleifen_hinweis": befund.rat}
        return ergebnis

    return mcp.tool()(huelle)


# ==========================================================================
#  Ueberblick
# ==========================================================================


@werkzeug
def status() -> dict:
    """Kompakter Gesamtueberblick: laufendes Modell, offene Auftraege, letzte
    Uebergabe, Anzahl Skills und Eintraege, Speicherbelegung.

    Erster Aufruf, wenn du wissen willst, wo du stehst.
    """
    store.ensure_dirs()
    cfg = config.load()

    modelle: Any
    warnung = None
    try:
        geladen = lmstudio.geladene_instanzen()
        modelle = [{"modell": i["key"], "art": i["type"], "groesse": i["params"]} for i in geladen]
        # Zwei Sprachmodelle gleichzeitig sprengen auf einer 24-GB-Karte den
        # Speicher; LM Studio rechnet dann teilweise auf der CPU. Das sieht
        # nicht nach einem Fehler aus, macht Subagenten aber unbrauchbar
        # langsam - hier ist eine Runde deshalb schon auf 266s gekommen.
        sprachmodelle = [i["key"] for i in geladen if i["type"] == "llm"]
        if len(sprachmodelle) > 1:
            warnung = (
                f"{len(sprachmodelle)} Sprachmodelle gleichzeitig geladen "
                f"({', '.join(sprachmodelle)}). Reicht der VRAM nicht, rechnet ein "
                f"Teil auf der CPU und Subagenten werden sehr langsam. Ueberfluessiges "
                f"in LM Studio entladen oder mit model_cycle() wechseln."
            )
    except lmstudio.LMStudioFehler as e:
        modelle = f"LM Studio nicht erreichbar: {e}"

    jobs = agents.auflisten(limit=10)
    letzte_uebergabe = ctx.auflisten(limit=1)

    inventar = mcpclient.index_laden().get("server", {})

    return {
        "lmstudio": {"url": cfg["lmstudio_base_url"], "geladen": modelle, "warnung": warnung},
        "subagent_modell": cfg["subagent_modell"],
        "auftraege": {
            "laufend": [j for j in jobs if j["status"] in ("wartet", "laeuft")],
            "letzte_fertig": [j for j in jobs if j["status"] == "fertig"][:3],
        },
        "letzte_uebergabe": letzte_uebergabe[0] if letzte_uebergabe else None,
        "skills": len(skills.alle()),
        "gedaechtnis": search.abdeckung(),
        "letzte_pruefungen": verify_mod.letzte(limit=3),
        "speicher": legacy.speicher_statistik(),
        "fremde_werkzeuge": {
            "server_im_inventar": len(inventar),
            "werkzeuge": sum(len(s.get("werkzeuge", [])) for s in inventar.values()),
            "offen": mcpclient.pool.offen(),
            "hinweis": "tool_find(...) sucht darin, tool_call(...) fuehrt aus."
            if inventar
            else "Noch kein Inventar. tool_find() baut es beim ersten Aufruf.",
        },
        "subagenten_duerfen": mcpclient.verfuegbare_server(),
        "schleifen_zuletzt": loops.letzte_befunde(3) or None,
    }


# ==========================================================================
#  Fremde Werkzeuge (das Gateway)
# ==========================================================================


@werkzeug
def tool_find(query: str = "", server: str | None = None, limit: int = 12) -> dict:
    """Sucht Werkzeuge in ALLEN anderen MCP-Servern - Dateien, Shell, Web,
    Blender, Unity, Unreal, Bildgenerierung, was eingetragen ist.

    Gibt nur Name und Einzeiler zurueck. Das Parameterschema holst du danach
    mit open_record('tool:NAME'), ausgefuehrt wird mit tool_call().

    Deshalb muessen diese Server in LM Studio nicht mehr einzeln eingetragen
    sein: Ihre Werkzeugbeschreibungen wuerden sonst alle gleichzeitig im
    Kontext liegen, auch die, die du in diesem Gespraech nie brauchst.

    Der allererste Aufruf dauert etwas - dabei wird jeder Server einmal
    gestartet und nach seinen Werkzeugen gefragt. Danach kommt es aus dem
    Inventar.
    """
    return mcpclient.werkzeuge_finden(query, server=server, limit=limit)


@werkzeug
def tool_call(name: str, args: dict | None = None) -> dict:
    """Fuehrt ein Werkzeug eines anderen MCP-Servers aus.

    name ist der volle Name aus tool_find, z.B. 'filesystem__read_file'.
    Wenn du die Argumente nicht kennst: open_record('tool:filesystem__read_file')
    zeigt das Schema.
    """
    ziel = mcpclient.aufloesen(name)
    if ziel is None:
        vorschlag = mcpclient.werkzeuge_finden(name.replace("__", " ").replace("_", " "), limit=5)
        return {
            "ok": False,
            "fehler": f"Werkzeug '{name}' steht nicht im Inventar.",
            "vielleicht_meinst_du": vorschlag["werkzeuge"],
        }

    server, wname = ziel
    try:
        inhalt, fehler = mcpclient.pool.aufrufen(server, wname, args or {}, fuer_gateway=True)
    except Exception as e:
        return {"ok": False, "fehler": f"{type(e).__name__}: {e}", "server": server}

    return {
        "ok": not fehler,
        "server": server,
        "werkzeug": wname,
        "ergebnis": inhalt[:12000],
        "gekuerzt": len(inhalt) > 12000,
    }


@werkzeug
def tool_refresh(server: str | None = None) -> dict:
    """Baut das Werkzeug-Inventar neu auf. Noetig, wenn du in
    .lmstudio/mcp.json etwas geaendert hast oder ein Server damals nicht
    lief (Blender zu, Unity nicht gestartet)."""
    idx = mcpclient.index_aufbauen(nur=[server] if server else None, neu=True)
    return {
        "ok": True,
        "stand": idx["stand"],
        "server": {
            k: (v["fehler"] if v.get("fehler") else f"{len(v['werkzeuge'])} Werkzeuge")
            for k, v in sorted(idx["server"].items())
        },
    }


# ==========================================================================
#  Skills
# ==========================================================================


@werkzeug
def skill_find(query: str = "") -> dict:
    """Findet passende Skills. Gibt NUR Name und Einzeiler zurueck, nie den
    Inhalt - damit kostet das Suchen fast nichts.

    Den Inhalt holst du danach mit open_record('skill:NAME'), oder du laesst
    den Skill mit skill_run() gleich von einem Subagenten ausfuehren.
    """
    gefunden = skills.finden(query)
    return {
        "skills": gefunden,
        "hinweis": "open_record('skill:NAME') zeigt die Anleitung, skill_run('NAME', {...}) fuehrt sie aus.",
    }


@werkzeug
def skill_run(name: str, inputs: dict | None = None, wait_ms: int = 0) -> dict:
    """Laesst einen Skill von einem Subagenten ausfuehren.

    Kehrt sofort mit einer Auftragsnummer zurueck - arbeite weiter und hole
    das Ergebnis spaeter mit agent_result(). Mit wait_ms wartest du bis zu
    30 Sekunden direkt hier.

    Welches Modell und welche Werkzeuge benutzt werden, steht in den
    Kopfdaten des Skills.
    """
    try:
        job = skills.ausfuehren(name, inputs or {})
    except ValueError as e:
        return {"ok": False, "fehler": str(e)}

    if wait_ms > 0:
        fertig = agents.ergebnis(job["id"], wait_ms=wait_ms)
        if fertig:
            return agents.kurzfassung(fertig)

    return {
        "job_id": job["id"],
        "status": job["status"],
        "skill": name,
        "werkzeuge": job["tools"],
        "fehlende_eingaben": job.get("fehlende_eingaben") or [],
        "hinweis": f"Laeuft im Hintergrund. Ergebnis: agent_result('{job['id']}')",
    }


@werkzeug
def current_datetime() -> dict:
    """Gibt das aktuelle Datum und die aktuelle Uhrzeit zurueck."""
    from datetime import datetime

    now = datetime.now()
    return {
        "success": True,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "iso": now.isoformat(),
        "timestamp": now.timestamp(),
    }


# ==========================================================================
#  Subagenten
# ==========================================================================


@werkzeug
def agent_spawn(
    task: str,
    role: str = "research",
    tools: list[str] | None = None,
    model: str | None = None,
    wait_ms: int = 0,
) -> dict:
    """Startet einen Subagenten und kehrt SOFORT zurueck, damit du
    weiterarbeiten kannst.

    role: 'research' (sucht im Web), 'read' (liest lokale Dateien),
          'verify' (prueft), 'summarize' (verdichtet, ohne Werkzeuge).
    tools: leer lassen - die Rolle bringt sinnvolle Voreinstellungen mit.
    model: leer lassen - dann laeuft der Auftrag auf dem Modell, das ohnehin
           schon geladen ist, und es wird nichts nachgeladen.

    Nutze das fuer alles, was viel Lesen und wenig Denken ist: Recherche,
    Dateien durchsehen, lange Ausgaben verdichten. Du behaeltst deinen
    Kontext, der Subagent verbrennt seinen.

    Der Subagent hat einen eigenen Schleifenwaechter: Wiederholt er sich,
    wird der Auftrag abgebrochen statt bis zur Rundengrenze weiterzulaufen.
    """
    try:
        job = agents.starten(aufgabe=task, rolle=role, tools=tools, modell=model)
    except ValueError as e:
        return {"ok": False, "fehler": str(e)}

    if wait_ms > 0:
        fertig = agents.ergebnis(job["id"], wait_ms=wait_ms)
        if fertig:
            return agents.kurzfassung(fertig)

    return {
        "job_id": job["id"],
        "status": job["status"],
        "rolle": role,
        "werkzeuge": job["tools"],
        "nicht_freigegeben": job.get("abgelehnte_tools") or [],
        "hinweis": f"Laeuft im Hintergrund. Ergebnis: agent_result('{job['id']}')",
    }


@werkzeug
def agent_result(job_id: str | None = None, wait_ms: int = 0) -> dict:
    """Holt das Ergebnis eines Subagenten.

    Ohne job_id: Liste aller Auftraege mit Status.
    Mit job_id: Stand plus Kurzfassung. wait_ms wartet hoechstens 30
    Sekunden - laenger haelt die Verbindung nicht durch, dann lieber
    weiterarbeiten und spaeter nochmal fragen.
    """
    if job_id is None:
        return {"auftraege": agents.auflisten()}

    job = agents.ergebnis(job_id, wait_ms=wait_ms)
    if job is None:
        return {"ok": False, "fehler": f"Auftrag '{job_id}' gibt es nicht."}
    return agents.kurzfassung(job)


@werkzeug
def agent_cancel(job_id: str) -> dict:
    """Bricht einen Subagent-Auftrag ab (status 'gescheitert').

    Fuer Auftraege, die festhaengen oder die man nicht mehr braucht. Der
    Hintergrund-Thread beendet sich an der naechsten Rundengrenze; haengt
    er in einem Modellruf, uebernimmt der Watchdog die Schadensbegrenzung.
    Ohne Rueckwirkung auf fertige Auftraege.
    """
    job = agents.abbrechen(job_id)
    if job is None:
        return {"ok": False, "fehler": f"Auftrag '{job_id}' gibt es nicht."}
    return {
        "ok": True,
        "job_id": job_id,
        "status": job.get("status"),
        "hinweis": "abgebrochen" if job.get("abgebrochen")
                   else "war schon beendet",
    }


# ==========================================================================
#  Gedaechtnis
# ==========================================================================


@werkzeug
def recall(query: str, kind: str | None = None, limit: int = 5) -> dict:
    """Sucht in allem Gemerkten: Playbooks, geloeste und gescheiterte
    Probleme, Reviews, Wiki-Artikel.

    Sucht zweigleisig - woertlich UND nach Bedeutung. Deshalb findet
    "serielle Verbindung bricht ab" auch einen Eintrag, der "UART Timeout"
    heisst.

    Gibt nur Verweise zurueck. Das Vollstaendige holst du mit open_record().
    kind: 'playbook', 'problem', 'review', 'wiki'.
    """
    return search.suchen(query, kind=kind, limit=limit)


@werkzeug
def open_record(record_id: str) -> dict:
    """Oeffnet einen Eintrag vollstaendig. Ein Weg fuer alles - die Art
    steckt im Praefix der Kennung:

      pb_...       Playbook oder Problem
      rv_...       Code-Review
      wk_...       Wiki-Artikel (kommt immer frisch von der Platte)
      job_...      Subagent-Auftrag, mit komplettem Verlauf
      vf_...       Pruefprotokoll, mit voller Ausgabe
      handoff_...  Kontext-Uebergabe
      skill:NAME   Skill-Anleitung
      tool:NAME    Parameterschema eines fremden Werkzeugs
    """
    if record_id.startswith("tool:"):
        name = record_id.split(":", 1)[1]
        schema = mcpclient.werkzeug_schema(name)
        if schema is None:
            return {"ok": False, "fehler": f"Werkzeug '{name}' steht nicht im Inventar. Nutze tool_find()."}
        return {"art": "werkzeug", **schema}

    if record_id.startswith("skill:"):
        s = skills.holen(record_id.split(":", 1)[1])
        if s is None:
            return {"ok": False, "fehler": f"Skill gibt es nicht. Verfuegbar: {[x['name'] for x in skills.alle()]}"}
        return {"art": "skill", "name": s["name"], "model": s["model"], "tools": s["tools"],
                "inputs": s["inputs"], "anleitung": s["rumpf"]}

    if record_id.startswith("job_"):
        job = agents.lade_job(record_id)
        if job is None:
            return {"ok": False, "fehler": f"Auftrag '{record_id}' gibt es nicht."}
        return {
            "art": "auftrag",
            "id": job["id"],
            "status": job.get("status"),
            "aufgabe": job.get("aufgabe"),
            "modell": job.get("modell"),
            "modell_begruendung": job.get("modell_begruendung"),
            "runden": job.get("runden"),
            "ergebnis": job.get("kurzfassung"),
            "werkzeuge_angeboten": job.get("werkzeuge_angeboten"),
            "werkzeuge_aus_text": job.get("werkzeuge_aus_text"),
            "werkzeugaufrufe": job.get("werkzeugaufrufe", []),
            "schleifen": job.get("schleifen", []),
            "abbruchgrund": job.get("abbruchgrund"),
            "fehler": job.get("fehler"),
        }

    if record_id.startswith("vf_") or record_id.startswith("migr_"):
        v = verify_mod.laden(record_id)
        if v is None:
            return {"ok": False, "fehler": f"Pruefprotokoll '{record_id}' gibt es nicht."}
        return {"art": "pruefung", **v}

    if record_id.startswith("handoff_"):
        h = ctx.resume(record_id)
        if h is None:
            return {"ok": False, "fehler": f"Uebergabe '{record_id}' gibt es nicht."}
        return {"art": "uebergabe", **h}

    rec = records.laden(record_id)
    if rec is None:
        return {"ok": False, "fehler": f"'{record_id}' gibt es nicht. Nutze recall(), um zu suchen."}
    records.benutzt_markieren(record_id)

    # Beide Richtungen zeigen: wohin dieser Eintrag zeigt, und wer auf ihn
    # zeigt. Die Rueckrichtung wird berechnet und nicht gespeichert - sonst
    # muesste bei jeder Aenderung die Gegenseite mitgepflegt werden.
    alle_recs = records.alle()
    return {
        "art": rec.get("kind", "eintrag"),
        **rec,
        "verweise": records.verweise_aufloesen(rec, alle_recs),
        "rueckverweise": records.rueckverweise(record_id, alle_recs),
    }


@werkzeug
def remember(
    title: str,
    trigger: str,
    steps: list[str] | None = None,
    tags: list[str] | None = None,
    outcome: str = "solved",
    verify_cmd: str | None = None,
    pitfalls: list[str] | None = None,
    context: str = "",
    kind: str = "playbook",
    related: list[str] | None = None,
) -> dict:
    """Merkt sich eine geloeste Aufgabe als wiederverwendbares Playbook.

    Ruf das auf, wenn etwas Aufwendiges endlich funktioniert hat - dann
    muss es beim naechsten Mal niemand mehr herleiten.

    trigger ist das Wichtigste: EINE Zeile, die sagt, WANN dieses Playbook
    hilft. Nur sie taucht spaeter in Suchergebnissen auf, sie entscheidet
    also darueber, ob der Eintrag je wiedergefunden wird.
    Gut:     "LiDAR antwortet nach dem Einschalten mit F0-Bytes statt Daten"
    Nutzlos: "Lidar-Problem"

    outcome: 'solved' (hat funktioniert) oder 'failed' (Sackgasse - auch
    wertvoll, damit sie niemand nochmal laeuft).

    kind: 'playbook' (Ablauf), 'problem', 'review' oder 'notiz' fuer einen
    blossen Merksatz ueber ein Ding - ein Geraet, einen Rechner, ein Bauteil.

    related verbindet diesen Eintrag mit anderen. Erlaubt sind Kennungen
    ("pb_20260809_lidar"), beschriftete Verweise ("laeuft_auf: Pi4") oder
    einfach Namen, die es noch nicht als Eintrag gibt - die bleiben stehen
    und zeigen, was noch fehlt.
    """
    if outcome not in ("solved", "failed", "open"):
        return {"ok": False, "fehler": "outcome muss 'solved', 'failed' oder 'open' sein."}

    rec = records.playbook_bauen(
        title=title, trigger=trigger, steps=steps, tags=tags, outcome=outcome,
        verify_cmd=verify_cmd, pitfalls=pitfalls, context=context, kind=kind,
        related=related,
    )
    records.speichern(rec)

    # Sofort einbetten, damit der Eintrag ab jetzt auch ueber die Bedeutung
    # gefunden wird und nicht erst beim naechsten recall().
    _, fehler = search.index_auffrischen([rec])

    return {
        "ok": True,
        "id": rec["id"],
        "bedeutungssuche": "eingetragen" if not fehler else f"spaeter nachtragen ({fehler[:80]})",
        "hinweis": f"Wiederfinden mit recall(), oeffnen mit open_record('{rec['id']}').",
    }


# ==========================================================================
#  Pruefen
# ==========================================================================


@werkzeug
def verify(
    name: str,
    command: str,
    cwd: str | None = None,
    timeout: int = 120,
    expect_exit: int | None = 0,
    expect_contains: str | None = None,
    playbook_id: str | None = None,
) -> dict:
    """Fuehrt einen Smoke-Test WIRKLICH aus und meldet, was passiert ist.

    Behaupte nie selbst, dass etwas funktioniert - lass es hier laufen. Du
    lieferst den Befehl, das Ergebnis kommt von der Maschine.

    Zurueck kommt nur das Ende der Ausgabe. Das Vollstaendige liegt im
    Protokoll und ist ueber open_record() erreichbar, falls du es brauchst.

    Mit playbook_id wird das Ergebnis an einem Playbook mitgeschrieben - so
    ist spaeter sichtbar, ob dessen Schritte noch funktionieren.
    """
    return verify_mod.ausfuehren(
        name=name, command=command, cwd=cwd, timeout=timeout,
        expect_exit=expect_exit, expect_contains=expect_contains, playbook_id=playbook_id,
    )


# ==========================================================================
#  Kontext
# ==========================================================================


@werkzeug
def context_checkpoint(
    goal: str,
    notes: str = "",
    done: list[str] | None = None,
    open_items: list[str] | None = None,
    next_step: str = "",
    files: list[str] | None = None,
    compress: bool = True,
) -> dict:
    """Verdichtet den Arbeitsstand zu einer Uebergabe, bevor der Kontext voll
    wird.

    Ruf das auf, wenn das Gespraech lang geworden ist. Danach oeffnest du
    einen NEUEN Chat und holst dort mit context_resume() weiter - erst der
    neue Chat leert den Kontext, ein Modellwechsel allein tut das nicht.
    """
    return ctx.checkpoint(
        goal=goal, notes=notes, done=done, open_items=open_items,
        next_step=next_step, files=files, compress=compress,
    )


@werkzeug
def context_resume(handoff_id: str | None = None) -> dict:
    """Holt die letzte (oder eine bestimmte) Kontext-Uebergabe zurueck.
    Erster Aufruf in einem frischen Chat."""
    # Genau hier faengt ein neues Gespraech an. Der Schleifenwaechter lebt im
    # Serverprozess weiter und wuerde sonst Aufrufe aus dem alten Chat
    # mitzaehlen - und im neuen sofort etwas melden, das dort nie passiert ist.
    loops.haupt().zuruecksetzen()
    h = ctx.resume(handoff_id)
    if h is None:
        return {"ok": False, "fehler": "Keine Uebergabe vorhanden.", "vorhanden": ctx.auflisten()}
    return h


@werkzeug
def model_cycle(model: str | None = None, context_length: int | None = None) -> dict:
    """Wechselt das geladene Modell in LM Studio.

    ACHTUNG: Das leert den Chatverlauf NICHT. Es setzt nur den Modellzustand
    zurueck und laedt gegebenenfalls ein anderes Modell. Fuer frischen
    Kontext: context_checkpoint(), dann NEUER Chat, dann context_resume().

    Der Wechsel laeuft mit kurzer Verzoegerung an, damit diese Antwort noch
    ankommt.
    """
    return ctx.model_cycle(model=model, context_length=context_length)


# ==========================================================================
#  Altbestand aus agent_memory v1
# ==========================================================================


@werkzeug
def ssh_list() -> dict:
    """Listet die vermerkten SSH-Verbindungen."""
    return legacy.ssh_list()


@werkzeug
def ssh_edit(
    host: str,
    user: str,
    port: int = 22,
    client_program: str = "OpenSSH",
    purpose: str = "",
    key_type: str = "ed25519",
    remove: bool = False,
) -> dict:
    """Traegt eine SSH-Verbindung ein oder entfernt sie (remove=True)."""
    return legacy.ssh_edit(host, user, port, client_program, purpose, key_type, remove)


@werkzeug
def mcp_registry_list() -> dict:
    """Listet die hier vermerkten MCP-Server. Das ist eine Notiz, nicht die
    Wahrheit - was LM Studio wirklich startet, steht in .lmstudio/mcp.json.
    Womit du wirklich arbeiten kannst, zeigt tool_find()."""
    return legacy.mcp_registry_list()


@werkzeug
def mcp_registry_set(
    name: str, command: str, args: list[str], env: dict | None = None, remove: bool = False
) -> dict:
    """Vermerkt einen MCP-Server oder entfernt den Vermerk (remove=True)."""
    return legacy.mcp_registry_set(name, command, args, env, remove)


@werkzeug
def create_roadmap(title: str, description: str, goals: list[str]) -> dict:
    """Legt die Roadmap an oder aktualisiert sie."""
    return legacy.create_roadmap(title, description, goals)


@werkzeug
def add_task(
    task_id: str,
    title: str,
    description: str,
    assigned_to: str | None = None,
    dependencies: list[str] | None = None,
) -> dict:
    """Fuegt eine Aufgabe hinzu. dependencies nennt task_ids, die vorher
    fertig sein muessen."""
    return legacy.add_task(task_id, title, description, assigned_to, dependencies)


@werkzeug
def get_next_task(agent_id: str) -> dict:
    """Holt die naechste ausfuehrbare Aufgabe und sperrt sie fuer diesen
    Agenten. Beruecksichtigt Abhaengigkeiten."""
    return legacy.get_next_task(agent_id)


@werkzeug
def update_task_status(task_id: str, status: str, message: str = "", result: dict | None = None) -> dict:
    """Setzt den Status einer Aufgabe: pending, in_progress, completed, failed."""
    return legacy.update_task_status(task_id, status, message, result)


@werkzeug
def get_roadmap_status() -> dict:
    """Kompakter Stand von Roadmap und Aufgaben."""
    return legacy.get_roadmap_status()


@werkzeug
def maintenance(retention_days: int = 30, archive_task_days: int = 7, dry_run: bool = True) -> dict:
    """Raeumt alte Auftraege und Pruefprotokolle weg, archiviert erledigte
    Aufgaben und repariert die Wissensbasis. Zeigt standardmaessig nur eine
    Vorschau - zum Ausfuehren dry_run=False."""
    ergebnis = legacy.maintenance(retention_days, archive_task_days, dry_run)

    # Verirrte Wiki-Kopien: Sie entstanden, solange open_record() einen
    # gelesenen Artikel als JSON zurueckschrieb. Jede davon ist veraltet und
    # ein Doppeltreffer in der Suche.
    verirrt = [p.name for p in records.verirrte_wiki_kopien()]
    if verirrt:
        ergebnis["wiki_kopien"] = verirrt
        if not dry_run:
            ergebnis.update(records.aufraeumen())
    return ergebnis


if __name__ == "__main__":
    store.ensure_dirs()
    mcp.run()
