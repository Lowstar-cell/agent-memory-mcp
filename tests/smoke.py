"""Smoke-Test ueber die ganze Kette.

Arbeitet gegen ein temporaeres Datenverzeichnis. Der Vorgaenger
(agent_memory_mcp/test_client.py) hat in den Produktivspeicher geschrieben -
die drei doppelten Lidar-Eintraege und die drei task_task_*-Leichen dort
stammen genau daher.

    uv run python tests/smoke.py           # ohne Modell-Aufrufe (schnell)
    uv run python tests/smoke.py --voll    # inkl. Subagenten (dauert Minuten)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

WURZEL = Path(__file__).parent.parent
sys.path.insert(0, str(WURZEL))

TMP = Path(tempfile.mkdtemp(prefix="agentmem_smoke_"))
os.environ["AGENTMEM_DATA_DIR"] = str(TMP)

# Eigene Wissensbasis fuer den Test. Sonst laeuft er gegen die echte unter
# Desktop/max-effi, und ob er besteht, haenge davon ab, was dort gerade steht.
WIKI_WURZEL = TMP / "wissensbasis"
(WIKI_WURZEL / "wiki").mkdir(parents=True)

import server  # noqa: E402
from agentmem import config, loops, mcpclient, records, store  # noqa: E402

config.load()["pfade"]["wissensbasis"] = str(WIKI_WURZEL)

VOLL = "--voll" in sys.argv
_ergebnisse: list[tuple[bool, str, str]] = []


def pruefe(bedingung: bool, name: str, detail: str = "") -> bool:
    _ergebnisse.append((bool(bedingung), name, detail))
    print(f"  {'OK  ' if bedingung else 'FEHL'}  {name}" + (f"   {detail}" if detail else ""))
    return bool(bedingung)


def abschnitt(t: str) -> None:
    print(f"\n--- {t} ---")


# --------------------------------------------------------------------------

abschnitt("Ablage und Ueberblick")
s = server.status()
pruefe(isinstance(s, dict) and "lmstudio" in s, "status() liefert Ueberblick")
pruefe("subagenten_duerfen" in s and "fremde_werkzeuge" in s, "status() nennt Werkzeugquellen",
       str(s.get("subagenten_duerfen")))
lm_da = isinstance(s["lmstudio"]["geladen"], list)
pruefe(True, "LM Studio erreichbar" if lm_da else "LM Studio NICHT erreichbar (Modelltests entfallen)")

# Zwei Sprachmodelle gleichzeitig sprengen 24 GB VRAM; LM Studio rechnet dann
# still auf der CPU weiter. Nichts sieht kaputt aus, aber eine Subagenten-
# Runde dauert ploetzlich Minuten. Das muss status() von sich aus sagen.
from agentmem import lmstudio as _lm  # noqa: E402

_echte_instanzen = _lm.geladene_instanzen
_lm.geladene_instanzen = lambda: [
    {"key": "gross-a", "instance_id": "gross-a", "type": "llm", "params": "35B"},
    {"key": "gross-b", "instance_id": "gross-b", "type": "llm", "params": "27B"},
]
try:
    w = server.status()["lmstudio"]["warnung"]
    pruefe(w is not None and "gross-a" in w and "gross-b" in w,
           "status() warnt bei zwei gleichzeitig geladenen Modellen", (w or "")[:60])
    # Gegenprobe ebenfalls gestellt: Ob auf DIESEM Rechner gerade eines oder
    # drei Modelle geladen sind, darf nicht darueber entscheiden, ob der Test
    # besteht.
    _lm.geladene_instanzen = lambda: [
        {"key": "einzeln", "instance_id": "einzeln", "type": "llm", "params": "4B"},
        {"key": "embed", "instance_id": "embed", "type": "embedding", "params": "300M"},
    ]
    pruefe(server.status()["lmstudio"]["warnung"] is None,
           "und schweigt, wenn nur eines laeuft")
finally:
    _lm.geladene_instanzen = _echte_instanzen

abschnitt("Merken und Wiederfinden")
r = server.remember(
    title="Serieller LiDAR liefert nur F0-Bytes",
    trigger="LiDAR antwortet nach dem Einschalten mit F0-Bytes statt Messdaten",
    steps=["Verkabelung pruefen", "AutoBaud abschalten", "Baudrate fest auf 115200"],
    tags=["esp32", "lidar", "uart"],
    outcome="solved",
    verify_cmd="python -c \"print('ok')\"",
)
pruefe(r.get("ok") and r["id"].startswith("pb_"), "remember() legt Playbook an", r.get("id", ""))
pid = r["id"]

woertlich = server.recall("LiDAR F0-Bytes")
pruefe(any(t["id"] == pid for t in woertlich["treffer"]), "recall() findet woertlich")

anders = server.recall("serielle Schnittstelle gibt Muell aus")
gefunden_anders = any(t["id"] == pid for t in anders["treffer"])
pruefe(gefunden_anders or not lm_da,
       "recall() findet auch anders formuliert" if gefunden_anders else "recall(): Bedeutungssuche uebersprungen",
       f"rerank={anders['rerank'][:60]}")

o = server.open_record(pid)
pruefe(o.get("title") and len(o.get("steps", [])) == 3, "open_record() oeffnet Playbook")
pruefe(server.open_record("pb_gibtsnicht").get("ok") is False, "open_record() meldet Unbekanntes sauber")

abschnitt("Eintraege verbinden (Essenz des abgeloesten Knowledge-Graph)")
# Der alte memory-knowledge-graph lag im npx-Cache und war bei einem
# 'npm cache clean' weg. Hier steht die Verbindung IM Eintrag unter
# data/records/ - sie ueberlebt alles, was einen Cache leert.
geraet = server.remember(
    title="LiDAR A1M8 am ESP32",
    trigger="Fragen zum LiDAR-Aufbau an der seriellen Schnittstelle",
    kind="notiz",
    tags=["lidar", "esp32"],
    related=[f"loest: {pid}", "haengt_an: Pi4"],
)
pruefe(geraet.get("ok") and geraet["id"].startswith("nt_"),
       "Notiz mit Verweisen wird angelegt (Praefix nt_ statt pb_)", geraet.get("id", ""))

go = server.open_record(geraet["id"])
arten = {v["art"]: v for v in go.get("verweise", [])}
pruefe("loest" in arten and arten["loest"]["ziel"] == pid,
       "Beschrifteter Verweis auf einen Eintrag", str(list(arten)))
pruefe(arten.get("loest", {}).get("vorhanden") is True and arten["loest"].get("titel"),
       "Vorhandene Ziele bekommen ihren Titel dazu", str(arten.get("loest", {}).get("titel"))[:40])
pruefe(arten.get("haengt_an", {}).get("vorhanden") is False,
       "Ein noch nicht angelegtes Ziel bleibt stehen statt zu verschwinden",
       str(arten.get("haengt_an", {}).get("ziel")))

zurueck = server.open_record(pid).get("rueckverweise", [])
pruefe(any(r["von"] == geraet["id"] and r["art"] == "loest" for r in zurueck),
       "Die Gegenrichtung ist von der anderen Seite aus sichtbar", str(zurueck))

pruefe("Pi4" in records.suchtext(records.laden(geraet["id"])),
       "Klartext-Ziele gehen in die Suche ein")
pruefe(pid not in records.suchtext(records.laden(geraet["id"])),
       "Blosse Kennungen nicht - die waeren nur Rauschen")

abschnitt("Bedeutungssuche heilt sich selbst")
from agentmem import search as _such  # noqa: E402

vorher = _such.abdeckung()
pruefe(vorher["eintraege"] > 0 and "ablage" in vorher, "status() zeigt die Abdeckung an",
       f"{vorher['eingebettet']}/{vorher['eintraege']} eingebettet")

# Der Index ist Zwischenspeicher, kein Bestand: loeschen darf nichts kosten.
index_datei = Path(vorher["ablage"])
if lm_da and index_datei.exists():
    index_datei.unlink()
    pruefe(_such.abdeckung()["eingebettet"] == 0, "Nach dem Loeschen ist der Index leer")
    server.recall("serielle Schnittstelle gibt Muell aus")
    nachher = _such.abdeckung()
    pruefe(nachher["eingebettet"] == nachher["eintraege"] and index_datei.exists(),
           "Ein recall() baut ihn vollstaendig neu auf",
           f"{nachher['eingebettet']}/{nachher['eintraege']}")
    treffer = server.recall("serielle Schnittstelle gibt Muell aus")
    pruefe(any(t["id"] == pid for t in treffer["treffer"]) and treffer["rerank"] == "aktiv",
           "Und die Bedeutungssuche findet wieder wie vorher")
else:
    pruefe(True, "Index-Wiederaufbau uebersprungen (LM Studio nicht erreichbar)")

abschnitt("Skills")
sf = server.skill_find("")
pruefe(len(sf["skills"]) > 0, "skill_find() findet Skills", f"{len(sf['skills'])} Stueck")
pruefe(all("rumpf" not in k and "voll" not in k for k in sf["skills"]),
       "skill_find() gibt KEINEN Inhalt zurueck (Kontext sparen)")
if sf["skills"]:
    name = sf["skills"][0]["name"]
    so = server.open_record(f"skill:{name}")
    pruefe("anleitung" in so and len(so["anleitung"]) > 0, "open_record('skill:..') liefert Anleitung", name)

# Die Wissensbasis hat einen eigenen skills-Ordner. Der war frueher unsichtbar:
# Die Wiki-Artikel wurden gelesen, die Skills daneben nicht - 'knowledge-extractor'
# aus Desktop/max-effi/skills tauchte in skill_find() nie auf.
(WIKI_WURZEL / "skills").mkdir(exist_ok=True)
(WIKI_WURZEL / "skills" / "wissens-sammler.md").write_text(
    "---\nskill_name: wissens-sammler\nstatus: aktiv\n---\n\n"
    "# Wissens-Sammler\n\n**Zweck:** Rohdaten in Wiki-Artikel verwandeln.\n\n"
    "## Ablauf\n\n1. raw/ durchsehen\n",
    encoding="utf-8",
)
(WIKI_WURZEL / "skills" / "_TEMPLATE.md").write_text("---\nname: vorlage\n---\nnicht zeigen\n", encoding="utf-8")
# Namensgleichheit: der ausfuehrbare Skill des Servers muss gewinnen.
(WIKI_WURZEL / "skills" / "code-review.md").write_text(
    "---\nskill_name: code-review\n---\n\n**Zweck:** Doppelgaenger aus der Wissensbasis.\n",
    encoding="utf-8",
)

gefunden = {s["name"]: s for s in server.skill_find("")["skills"]}
pruefe("wissens-sammler" in gefunden, "Skills der Wissensbasis werden mitgefunden",
       str(sorted(gefunden)))
pruefe(gefunden.get("wissens-sammler", {}).get("herkunft") == "wissensbasis",
       "Die Herkunft ist erkennbar")
pruefe(gefunden.get("wissens-sammler", {}).get("description", "").startswith("Rohdaten"),
       "Zweck wird aus dem Text gelesen, wenn kein description-Kopf da ist",
       gefunden.get("wissens-sammler", {}).get("description", "")[:40])
pruefe("vorlage" not in gefunden, "Vorlagen mit _ davor bleiben aussen vor")
pruefe(gefunden.get("code-review", {}).get("herkunft") == "server",
       "Bei gleichem Namen gewinnt der ausfuehrbare Skill des Servers",
       gefunden.get("code-review", {}).get("herkunft", "?"))
ks = server.open_record("skill:wissens-sammler")
pruefe("raw/ durchsehen" in ks.get("anleitung", ""),
       "Auch die Anleitung kommt aus der Wissensbasis")

abschnitt("verify fuehrt wirklich aus")
v_ok = server.verify(name="echo-test", command='python -c "print(42)"', timeout=30)
pruefe(v_ok["passed"] and "42" in v_ok["stdout_tail"], "verify() erkennt Erfolg", f"exit={v_ok['exit_code']}")

v_bad = server.verify(name="soll-scheitern", command='python -c "import sys; sys.exit(3)"', timeout=30)
pruefe(v_bad["passed"] is False and v_bad["exit_code"] == 3,
       "verify() erkennt Fehlschlag", f"exit={v_bad['exit_code']} gruende={v_bad['gruende']}")

v_to = server.verify(name="timeout", command="python -c \"import time; time.sleep(30)\"", timeout=2)
pruefe(v_to["passed"] is False, "verify() bricht bei Zeitgrenze ab", str(v_to["gruende"]))

vo = server.open_record(v_bad["protokoll"])
pruefe(vo.get("art") == "pruefung" and "stdout" in vo, "open_record() oeffnet Pruefprotokoll")

v_pb = server.verify(name="am-playbook", command='python -c "print(1)"', playbook_id=pid, timeout=30)
pruefe(len(server.open_record(pid).get("pruefungen", [])) == 1, "verify() schreibt am Playbook mit")

abschnitt("Aufgabenverwaltung (Altbestand)")
server.create_roadmap("Testplan", "nur fuer den Smoke-Test", ["Ziel A"])
server.add_task("a1", "Erste", "ohne Abhaengigkeit")
server.add_task("a2", "Zweite", "haengt an a1", dependencies=["a1"])
t1 = server.get_next_task("agent_x")
pruefe(t1["aufgabe_gefunden"] and t1["task"]["task_id"] == "a1", "get_next_task() gibt a1")
t2 = server.get_next_task("agent_x")
pruefe(t2["aufgabe_gefunden"] is False, "get_next_task() blockiert a2 wegen Abhaengigkeit")
server.update_task_status("a1", "completed", "fertig")
t3 = server.get_next_task("agent_x")
pruefe(t3["aufgabe_gefunden"] and t3["task"]["task_id"] == "a2", "get_next_task() gibt a2 nach Freigabe")
pruefe(server.get_roadmap_status()["aufgaben_gesamt"] == 2, "get_roadmap_status() zaehlt richtig")

abschnitt("SSH- und MCP-Vermerke")
# 192.0.2.0/24 ist der fuer Dokumentation reservierte Bereich (RFC 5737).
# Hier stand einmal eine echte Adresse aus dem Heimnetz - in einem Test, der
# weitergegeben wird, hat so etwas nichts verloren.
server.ssh_edit("192.0.2.10", "testnutzer", purpose="Beispielrechner")
pruefe(server.ssh_list()["anzahl"] == 1, "ssh_edit() traegt ein")
server.ssh_edit("192.0.2.10", "testnutzer", remove=True)
pruefe(server.ssh_list()["anzahl"] == 0, "ssh_edit(remove=True) entfernt")
pruefe(server.maintenance(dry_run=True)["vorschau"] is True, "maintenance() ist standardmaessig Vorschau")

abschnitt("Kontext-Uebergabe")
cp = server.context_checkpoint(
    goal="Server in LM Studio in Betrieb nehmen",
    notes="Werkzeug-Ablauf laeuft, Umzug erledigt.",
    done=["Gate bestanden"], open_items=["mcp.json umstellen"],
    next_step="Server in LM Studio eintragen",
    files=["server.py"],
    compress=False,
)
pruefe(cp["handoff_id"].startswith("handoff_"), "context_checkpoint() legt Uebergabe an")
res = server.context_resume()
pruefe(cp["handoff_id"] in res["uebergabe"] or cp["handoff_id"] == res["handoff_id"],
       "context_resume() holt sie zurueck")
pruefe(server.open_record(cp["handoff_id"]).get("art") == "uebergabe", "open_record() oeffnet Uebergabe")

# Ein neuer Chat faengt mit context_resume() an. Der Waechter lebt aber im
# Serverprozess weiter - ohne Ruecksetzen meldete er im neuen Gespraech eine
# Schleife, die dort nie stattgefunden hat.
server.recall("etwas aus dem alten chat")
server.recall("etwas aus dem alten chat")
server.context_resume()
danach = server.recall("etwas aus dem alten chat")
pruefe("schleife_erkannt" not in danach, "context_resume() setzt den Waechter zurueck")
loops.haupt().zuruecksetzen()

abschnitt("Wissensbasis: Artikel bleiben lebendig")
# Der Fehler, der hier verhindert wird: open_record() legte den gelesenen
# Wiki-Artikel als JSON in data/records/ ab. Ab da lieferte es fuer immer
# diese Kopie - Aenderungen an der Markdown-Datei waren unsichtbar - und der
# Artikel stand doppelt in der Suchgrundlage.
artikel = WIKI_WURZEL / "wiki" / "lidar-notizen.md"
artikel.write_text(
    "---\ntitle: LiDAR Notizen\ntags: [lidar, uart]\n---\n\n"
    "# LiDAR\n\nERSTE FASSUNG: Sensor braucht feste Baudrate.\n",
    encoding="utf-8",
)
wid = "wk_lidar_notizen"

w1 = server.open_record(wid)
pruefe(w1.get("art") == "wiki" and "ERSTE FASSUNG" in w1.get("inhalt", ""),
       "open_record() oeffnet Wiki-Artikel", w1.get("title", ""))
pruefe(w1.get("title") == "LiDAR Notizen", "Titel kommt aus dem Frontmatter", str(w1.get("title")))

artikel.write_text(
    "---\ntitle: LiDAR Notizen\ntags: [lidar, uart]\n---\n\n"
    "# LiDAR\n\nZWEITE FASSUNG: AutoBaud abschalten.\n",
    encoding="utf-8",
)
w2 = server.open_record(wid)
pruefe("ZWEITE FASSUNG" in w2.get("inhalt", ""),
       "Geaenderter Artikel wird auch geaendert gelesen", w2.get("inhalt", "")[:0] or "kein Einfrieren")

pruefe(not list((TMP / "records").glob("wk_*.json")),
       "Wiki-Artikel wird NICHT nach data/records kopiert")

ids = [r["id"] for r in records.alle()]
pruefe(len(ids) == len(set(ids)), "Kein Eintrag steht doppelt in der Suchgrundlage",
       f"{len(ids)} Eintraege")

pruefe(records.wiki_meta().get(wid, {}).get("genutzt", 0) >= 2,
       "Nutzung wird in der Beiakte gezaehlt, nicht im Artikel",
       str(records.wiki_meta().get(wid, {}).get("genutzt")))

# guidelines.md zeigt ein Beispiel-Frontmatter INNERHALB eines Code-Blocks.
# Wer 'title:' in der ganzen Datei sucht, holt sich von dort den falschen Titel.
falle = WIKI_WURZEL / "wiki" / "schreibregeln.md"
falle.write_text(
    "# Schreibregeln\n\nSo sieht ein Artikelkopf aus:\n\n"
    "```markdown\n---\ntitle: Falscher Titel aus dem Beispiel\n---\n```\n",
    encoding="utf-8",
)
regeln = [r for r in records.wiki_records() if r["id"] == "wk_schreibregeln"][0]
pruefe(regeln["title"] == "schreibregeln",
       "Titel aus einem Code-Block wird nicht uebernommen", regeln["title"])

# Der Trigger ist die EINZIGE Zeile, die spaeter im Suchergebnis steht. Die
# Artikel hier beginnen ihren Fliesstext mit '**Zusammenfassung:** ...' - wird
# das faelschlich fuer eine Aufzaehlung gehalten (fuehrendes '*'), landet
# stattdessen irgendein Listenpunkt im Ergebnis.
zus = WIKI_WURZEL / "wiki" / "mit-zusammenfassung.md"
zus.write_text(
    "---\ntitle: Mit Zusammenfassung\n---\n\n# Ueberschrift\n\n"
    "**Zusammenfassung:** Das ist der Satz, der zaehlt.\n\n"
    "## Punkte\n\n- erster Punkt\n- zweiter Punkt\n",
    encoding="utf-8",
)
mz = [r for r in records.wiki_records() if r["id"] == "wk_mit_zusammenfassung"][0]
pruefe(mz["trigger"] == "Das ist der Satz, der zaehlt.",
       "Zusammenfassung wird nicht fuer eine Aufzaehlung gehalten", repr(mz["trigger"]))

nur_liste = WIKI_WURZEL / "wiki" / "nur-liste.md"
nur_liste.write_text(
    "# Index\n\n1. Erster Schritt: irgendwas tun\n2. Zweiter Schritt\n", encoding="utf-8"
)
nl = [r for r in records.wiki_records() if r["id"] == "wk_nur_liste"][0]
pruefe(nl["trigger"].startswith("1. Erster Schritt"),
       "Ohne Fliesstext wird ein nummerierter Punkt genommen", repr(nl["trigger"][:40]))

abschnitt("Schleifen-Erkennung")
w = loops.Waechter("test")
pruefe(w.pruefe("recall", {"query": "a"}) is None, "Erster Aufruf ist unauffaellig")
b2 = w.pruefe("recall", {"query": "a"})
pruefe(b2 is not None and b2.stufe == "hinweis", "Zweiter gleicher Aufruf: Hinweis",
       b2.stufe if b2 else "-")
b3 = w.pruefe("recall", {"query": "a"})
pruefe(b3 is not None and b3.stufe == "stopp", "Dritter gleicher Aufruf: Stopp")
pruefe(bool(b3 and len(b3.rat) > 40), "Der Stopp nennt einen naechsten Schritt", (b3.rat[:60] + "...") if b3 else "-")

# Der wichtigste Test dieser Datei: aendern, pruefen, aendern, pruefen ist
# ECHTE Arbeit und darf nicht als Schleife abgewuergt werden.
wa = loops.Waechter("arbeit")
stopps = 0
for i in range(4):
    wa.pruefe("tool_call", {"name": "fs__write", "args": {"inhalt": f"Fassung {i}"}})
    bf = wa.pruefe("verify", {"command": "pytest"})
    if bf is not None and bf.stufe == "stopp":
        stopps += 1
pruefe(stopps == 0, "aendern/pruefen im Wechsel wird NICHT abgebrochen", f"{stopps} Stopps")

# Derselbe Wechsel mit unveraenderten Argumenten ist dagegen ein echter Kreis.
wk = loops.Waechter("kreis")
kreis_erkannt = False
for _ in range(4):
    wk.pruefe("tool_call", {"name": "fs__write", "args": {"inhalt": "immer dasselbe"}})
    bf = wk.pruefe("verify", {"command": "pytest"})
    if bf is not None and bf.stufe == "stopp":
        kreis_erkannt = True
        break
pruefe(kreis_erkannt, "Wortgleicher Wechsel wird als Kreis erkannt")

# Suchen, die nie etwas finden: andere Worte, immer null Treffer.
wf = loops.Waechter("fruchtlos")
befund_f = None
for wort in ("alpha", "beta", "gamma"):
    wf.pruefe("recall", {"query": wort})
    befund_f = wf.ergebnis_melden("recall", leer=True)
pruefe(befund_f is not None and befund_f.art == "fruchtlos",
       "Drei erfolglose Suchen: Sackgasse gemeldet")

wg = loops.Waechter("geduld")
letzte = None
for _ in range(5):
    letzte = wg.pruefe("agent_result", {"job_id": "job_1"})
pruefe(letzte is not None and letzte.stufe == "hinweis",
       "Auf einen Subagenten warten wird nie gestoppt", letzte.stufe if letzte else "-")

# Und ueber den echten Werkzeugen: dreimal dieselbe Suche.
loops.haupt().zuruecksetzen()
server.recall("voellig unbekanntes thema xyzzy")
server.recall("voellig unbekanntes thema xyzzy")
gestoppt = server.recall("voellig unbekanntes thema xyzzy")
pruefe(gestoppt.get("schleife_erkannt") is not None,
       "Der Waechter greift auch beim echten recall()", str(gestoppt.get("schleife_erkannt")))
pruefe("treffer" not in gestoppt, "Beim Stopp wird der Aufruf gar nicht ausgefuehrt")
loops.haupt().zuruecksetzen()

abschnitt("Gateway zu fremden Servern")
# Gegen ein erfundenes Inventar, damit dafuer keine 15 Kindprozesse starten.
store.save_json(
    TMP / "index" / "werkzeuge.json",
    {
        "stand": store.jetzt(),
        "server": {
            "filesystem": {
                "stand": store.jetzt(),
                "fehler": None,
                "werkzeuge": [
                    {"name": "filesystem__read_file", "werkzeug": "read_file",
                     "beschreibung": "Liest eine Datei von der Platte."},
                    {"name": "filesystem__write_file", "werkzeug": "write_file",
                     "beschreibung": "Schreibt eine Datei."},
                ],
            },
            "blender": {"stand": store.jetzt(), "fehler": "Blender laeuft nicht", "werkzeuge": []},
        },
    },
)
tf = server.tool_find("datei lesen")
pruefe(any(t["name"] == "filesystem__read_file" for t in tf["werkzeuge"]),
       "tool_find() findet ein fremdes Werkzeug", str([t["name"] for t in tf["werkzeuge"]]))
pruefe(all("parameter" not in t and "inputSchema" not in t for t in tf["werkzeuge"]),
       "tool_find() gibt KEINE Schemas zurueck (Kontext sparen)")
pruefe(tf.get("nicht_erreichbar", {}) and "blender" in tf["nicht_erreichbar"],
       "Ein toter Server wird gemeldet, nicht verschwiegen")
pruefe(mcpclient.aufloesen("filesystem__read_file") == ("filesystem", "read_file"),
       "Voller Name wird richtig aufgeloest")
tc = server.tool_call("gibtsnicht__foo")
pruefe(tc.get("ok") is False and "vielleicht_meinst_du" in tc,
       "tool_call() auf Unbekanntes schlaegt etwas vor")

abschnitt("Subagent bricht seine eigene Schleife ab")
# Mit einem gestellten Modell, das stur immer denselben Werkzeugaufruf
# macht. So laesst sich der Abbruch pruefen, ohne auf ein echtes Modell und
# dessen Tagesform angewiesen zu sein.
from agentmem import agents, lmstudio  # noqa: E402

_echt_chat, _echt_werkzeuge, _echt_aufrufen, _echt_modell = (
    lmstudio.chat, mcpclient.pool.werkzeuge, mcpclient.pool.aufrufen, lmstudio.modell_aufloesen,
)
# Muss VOR dem Stellen gesichert werden. 'agents.mcpclient' ist dasselbe
# Modulobjekt wie 'mcpclient' - wer hinterher agents.mcpclient.x = mcpclient.x
# schreibt, weist dem Stub sich selbst zu und behaelt ihn fuer den Rest des
# Laufs. Genau das ist passiert und hat drei Volltestlaeufe entwertet: Der
# echte Subagent bekam danach 0 Werkzeuge, weil 'filesystem-full' nicht mehr
# in der (gestellten) Freigabeliste stand.
_echt_verfuegbar = mcpclient.verfuegbare_server
_runden = {"n": 0}


def _sturer_chat(modell, verlauf, tools=None, **kw):
    _runden["n"] += 1
    if tools is None:  # Werkzeuge abgeschaltet -> jetzt muss geantwortet werden
        return {"message": {"role": "assistant", "content": "Gut, ich hoere auf."},
                "finish_reason": "stop", "usage": {}}
    return {
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": f"c{_runden['n']}", "type": "function",
                            "function": {"name": "fake__suche",
                                         "arguments": '{"query": "immer dasselbe"}'}}],
        },
        "finish_reason": "tool_calls",
        "usage": {},
    }


lmstudio.chat = _sturer_chat
lmstudio.modell_aufloesen = lambda wunsch=None: ("test-modell", "gestellt")
mcpclient.pool.werkzeuge = lambda namen, **kw: (
    [{"type": "function", "function": {"name": "fake__suche", "description": "tut so",
                                       "parameters": {"type": "object", "properties": {}}}}],
    {"fake__suche": ("fake", "suche")},
)
mcpclient.pool.aufrufen = lambda *a, **kw: ("Irgendein Ergebnis.", False)
agents.mcpclient.verfuegbare_server = lambda: ["fake"]
try:
    job = agents.starten(aufgabe="Such was.", rolle="research", tools=["fake"])
    fertig = agents.ergebnis(job["id"], wait_ms=20000)
    pruefe(fertig is not None and fertig.get("status") == "fertig",
           "Auftrag laeuft trotz Dauerschleife zu Ende", str(fertig and fertig.get("status")))
    pruefe(bool(fertig and fertig.get("schleifen")), "Schleife wurde im Auftrag vermerkt",
           str(fertig and [s["schleife_erkannt"] for s in fertig.get("schleifen", [])]))
    pruefe(bool(fertig and fertig.get("runden", 99) < 8),
           "Abbruch VOR der Rundengrenze", f"{fertig and fertig.get('runden')} statt 8 Runden")
    aufrufe = len(fertig.get("werkzeugaufrufe", [])) if fertig else 0
    pruefe(aufrufe <= 3, "Der wiederholte Aufruf wird nicht immer wieder ausgefuehrt",
           f"{aufrufe} Werkzeugaufrufe")
    # LM Studio entlaedt Modelle nach Leerlauf, und wer im Chat ein anderes
    # Modell waehlt, verdraengt das laufende. Ein Auftrag im Hintergrund darf
    # daran nicht sterben - genau das ist am 09.08.2026 im Volltest passiert.
    _versuche = {"n": 0}

    def _weggeladen(modell, verlauf, tools=None, **kw):
        _versuche["n"] += 1
        if _versuche["n"] == 1:
            raise lmstudio.LMStudioFehler(
                '/v1/chat/completions -> HTTP 400: {"error":"Model unloaded by user or API request."}'
            )
        return {"message": {"role": "assistant", "content": "Bin wieder da, hier das Ergebnis."},
                "finish_reason": "stop", "usage": {}}

    lmstudio.chat = _weggeladen
    job2 = agents.starten(aufgabe="Sag was.", rolle="summarize", tools=[])
    f2 = agents.ergebnis(job2["id"], wait_ms=15000)
    pruefe(f2 is not None and f2.get("status") == "fertig",
           "Auftrag ueberlebt ein weggeladenes Modell", str(f2 and f2.get("status")))
    pruefe(bool(f2 and f2.get("erholungen")), "Der Modellverlust wird vermerkt, nicht verschwiegen",
           str(f2 and len(f2.get("erholungen", []))))

    # Ein zu langsames Modell ist kein Programmfehler. Es soll deshalb auch
    # nicht als einer erscheinen - sondern mit einem Rat, der weiterhilft.
    def _zu_langsam(modell, verlauf, tools=None, **kw):
        raise lmstudio.LMStudioZuLangsam("Keine Antwort binnen 300s von 'riesig-70b'.")

    lmstudio.chat = _zu_langsam
    job3 = agents.starten(aufgabe="Sag was.", rolle="summarize", tools=[])
    f3 = agents.ergebnis(job3["id"], wait_ms=15000)
    pruefe(f3 is not None and f3.get("status") == "gescheitert" and not f3.get("traceback"),
           "Ein zu langsames Modell scheitert ohne Traceback")
    k3 = agents.kurzfassung(f3) if f3 else {}
    pruefe("was_jetzt" in k3 and "kleineres Modell" in k3["was_jetzt"],
           "Und sagt, was dagegen hilft", str(k3.get("was_jetzt", ""))[:60])

    # Kleine Modelle antworten gern aus dem Gedaechtnis, statt das Werkzeug zu
    # benutzen - und klingen dabei genauso sicher wie bei einer echten
    # Auskunft. Genau das ist hier am 10.08.2026 passiert: Ein 4B nannte den
    # Wert einer Konfigurationsdatei, ohne sie je geoeffnet zu haben.
    _antworten = {"n": 0}

    def _raet_einfach(modell, verlauf, tools=None, **kw):
        _antworten["n"] += 1
        if _antworten["n"] == 1:
            return {"message": {"role": "assistant",
                                "content": 'config.json:42 - "subagent_modell": "llama-3-70b"'},
                    "finish_reason": "stop", "usage": {}}
        return {"message": {"role": "assistant", "content": None,
                            "tool_calls": [{"id": "c1", "type": "function",
                                            "function": {"name": "fake__suche", "arguments": "{}"}}]},
                "finish_reason": "tool_calls", "usage": {}}

    lmstudio.chat = _raet_einfach
    job4 = agents.starten(aufgabe="Lies die Datei.", rolle="read", tools=["fake"])
    f4 = agents.ergebnis(job4["id"], wait_ms=15000)
    pruefe(bool(f4 and f4.get("werkzeugaufrufe")),
           "Antwort ohne Werkzeug wird einmal zurueckgewiesen",
           str([w["werkzeug"] for w in (f4 or {}).get("werkzeugaufrufe", [])]))

    # Auch ein Werkzeug, das nur fehlgeschlagen ist, ist kein Beleg. Kleine
    # Modelle bitten dann gern den Benutzer um Wiederholung - hier liest aber
    # niemand mit, also muessen sie es selbst nochmal versuchen.
    _fehl = {"n": 0}

    def _scheitert_dann_bittet(modell, verlauf, tools=None, **kw):
        _fehl["n"] += 1
        if _fehl["n"] == 1:
            return {"message": {"role": "assistant", "content": None,
                                "tool_calls": [{"id": "c1", "type": "function",
                                                "function": {"name": "fake__suche", "arguments": "{}"}}]},
                    "finish_reason": "tool_calls", "usage": {}}
        if _fehl["n"] == 2:
            return {"message": {"role": "assistant",
                                "content": "Fehler beim Lesen. Bitte versuche es erneut."},
                    "finish_reason": "stop", "usage": {}}
        return {"message": {"role": "assistant", "content": None,
                            "tool_calls": [{"id": "c2", "type": "function",
                                            "function": {"name": "fake__suche", "arguments": '{"x": 1}'}}]},
                "finish_reason": "tool_calls", "usage": {}}

    _aufrufe_fehl = {"n": 0}

    def _erst_fehler_dann_ok(*a, **kw):
        _aufrufe_fehl["n"] += 1
        return ("Fehler: unbekanntes Argument", True) if _aufrufe_fehl["n"] == 1 else ("Inhalt der Datei.", False)

    lmstudio.chat = _scheitert_dann_bittet
    mcpclient.pool.aufrufen = _erst_fehler_dann_ok
    job6 = agents.starten(aufgabe="Lies die Datei.", rolle="read", tools=["fake"])
    f6 = agents.ergebnis(job6["id"], wait_ms=15000)
    erfolge = [w for w in (f6 or {}).get("werkzeugaufrufe", []) if not w["fehler"]]
    pruefe(bool(erfolge), "Nach einem fehlgeschlagenen Aufruf wird nachgehakt statt aufgegeben",
           f"{len((f6 or {}).get('werkzeugaufrufe', []))} Aufrufe, davon {len(erfolge)} erfolgreich")
    mcpclient.pool.aufrufen = lambda *a, **kw: ("Irgendein Ergebnis.", False)

    # Und wenn es auch nach dem Nachhaken nicht liest: sagen, nicht kaschieren.
    lmstudio.chat = lambda m, v, tools=None, **kw: {
        "message": {"role": "assistant", "content": "Steht bestimmt so drin."},
        "finish_reason": "stop", "usage": {}}
    job5 = agents.starten(aufgabe="Lies die Datei.", rolle="read", tools=["fake"])
    f5 = agents.ergebnis(job5["id"], wait_ms=15000)
    k5 = agents.kurzfassung(f5) if f5 else {}
    pruefe("warnung" in k5 and "unbelegt" in k5["warnung"],
           "Bleibt es unbelegt, wird das gemeldet", str(k5.get("warnung", ""))[:55])

    # Kleine Modelle schreiben den Werkzeugaufruf gern als JSON in den
    # Antworttext, statt den Werkzeug-Kanal zu benutzen - und verstuemmeln
    # dabei die Trennzeichen im Namen. Beobachtet am 10.08.2026 bei qwen3-4b.
    zuordnung = {"filesystem-full__read_text_file": ("filesystem-full", "read_text_file")}
    t1 = agents._werkzeugwunsch_aus_text(
        '{"name": "filesystem_full_read_text_file", "arguments": {"path": "C:/x.json"}}', zuordnung)
    pruefe(len(t1) == 1 and t1[0]["function"]["name"] == "filesystem-full__read_text_file",
           "Werkzeugaufruf aus dem Fliesstext wird erkannt", str([c["function"]["name"] for c in t1]))
    t2 = agents._werkzeugwunsch_aus_text(
        'Ich lese das mal:\n```json\n<tool_call>{"tool": "read_text_file", '
        '"parameters": {"path": "C:/x.json"}}</tool_call>\n```', zuordnung)
    pruefe(len(t2) == 1, "Auch in Code-Zaeunen und tool_call-Klammern", str(len(t2)))
    pruefe(json.loads(t2[0]["function"]["arguments"]).get("path") == "C:/x.json",
           "Die Argumente kommen mit")
    pruefe(agents._werkzeugwunsch_aus_text("Nur ganz normaler Text ohne alles.", zuordnung) == [],
           "Normaler Text wird nicht als Aufruf missdeutet")
    pruefe(agents._werkzeugwunsch_aus_text('{"name": "gibtsnicht", "arguments": {}}', zuordnung) == [],
           "Unbekannte Namen werden nicht erfunden")
finally:
    lmstudio.chat, mcpclient.pool.werkzeuge = _echt_chat, _echt_werkzeuge
    mcpclient.pool.aufrufen, lmstudio.modell_aufloesen = _echt_aufrufen, _echt_modell
    mcpclient.verfuegbare_server = _echt_verfuegbar

# Bewusst ohne einen bestimmten Servernamen: Welche Server eingetragen sind,
# ist von Rechner zu Rechner verschieden. Bewiesen werden soll nur, dass der
# gestellte Ersatz ("fake") wieder weg ist und die Liste aus mcp.json kommt -
# genau der Fehler, der drei Volltestlaeufe wertlos gemacht hat.
_frei = mcpclient.verfuegbare_server()
pruefe("fake" not in _frei and set(_frei) <= set(mcpclient._mcp_json()),
       "Die echte Freigabeliste ist nach den gestellten Tests wieder da",
       str(_frei))

abschnitt("Selbstaufruf ist gesperrt")
for gesperrt in ("agent-memory", "agentmem", "max-effi"):
    try:
        mcpclient._pruefe(gesperrt, fuer_gateway=True)
        pruefe(False, f"'{gesperrt}' haette gesperrt sein muessen")
    except mcpclient.MCPClientFehler:
        pruefe(True, f"'{gesperrt}' ist gesperrt")

# --------------------------------------------------------------------------

if VOLL and lm_da:
    abschnitt("Subagenten (dauert)")
    sp = server.agent_spawn(
        task=(f"Lies {(WURZEL / 'config.json').as_posix()} und nenne den "
              "Wert von 'subagent_modell'."),
        role="read", tools=["filesystem-full"],
        # Bewusst festgenagelt statt 'auto': Dieser Test soll die Kette
        # pruefen - Auftrag, Werkzeuge, Kindprozess, Antwort - und nicht, was
        # gerade zufaellig im Chat geladen ist. Mit dem grossen Chatmodell
        # scheiterte er zweimal an LM Studios eigener Engine, ohne dass an
        # der Kette etwas falsch war.
        model="qwen/qwen3-4b-2507",
    )
    pruefe("job_id" in sp, "agent_spawn() kehrt sofort zurueck", sp.get("job_id", ""))
    pruefe(not sp.get("nicht_freigegeben"), "Das Werkzeug ist auch freigegeben",
           str(sp.get("nicht_freigegeben")))
    # Grosszuegig: Auf dieser Maschine hat eine einzige Modellrunde mit einem
    # 35B-MoE schon 266s gebraucht, und ein Auftrag braucht mindestens zwei.
    t0 = time.time()
    erg = server.agent_result(sp["job_id"], wait_ms=30000)
    while erg.get("status") not in ("fertig", "gescheitert") and time.time() - t0 < 960:
        erg = server.agent_result(sp["job_id"], wait_ms=30000)
    pruefe(erg.get("status") == "fertig", "Subagent wird fertig",
           f"{int(time.time()-t0)}s" + (f" | {erg.get('fehler','')[:90]}" if erg.get("fehler") else ""))

    voll_job = server.open_record(sp["job_id"])
    pruefe(voll_job.get("werkzeuge_angeboten", 0) > 0,
           "Dem Subagenten wurden Werkzeuge angeboten",
           f"{voll_job.get('werkzeuge_angeboten')} Stueck")
    benutzt = erg.get("werkzeuge_benutzt") or []
    pruefe(any("read" in w for w in benutzt),
           "Subagent hat die Datei WIRKLICH gelesen", str(benutzt))
    pruefe(not erg.get("warnung"), "Antwort ist belegt, nicht geraten",
           str(erg.get("warnung", ""))[:60])

    # Der eigentliche Test: steht die richtige Antwort drin? Eine Laengen-
    # pruefung allein bestuende auch eine 3-Wort-Ausrede.
    #
    # Der Sollwert wird aus derselben Datei gelesen, die der Subagent lesen
    # sollte - nicht fest hineingeschrieben. Ein fester String war hier schon
    # einmal falsch: Er stand noch auf "auto", waehrend in der Datei laengst
    # etwas anderes stand, und meldete eine richtige Antwort als Fehler.
    soll = json.loads((WURZEL / "config.json").read_text(encoding="utf-8"))["subagent_modell"]
    antwort = (erg.get("ergebnis") or "")
    pruefe(soll.lower() in antwort.lower(), "Antwort enthaelt den richtigen Wert",
           f"erwartet {soll!r} in {antwort[:100]!r}")
    woerter = len(antwort.split())
    pruefe(3 < woerter < 400, "Kurzfassung ist knapp, aber nicht leer", f"{woerter} Woerter")
    pruefe(server.open_record(sp["job_id"]).get("art") == "auftrag", "open_record() oeffnet Auftrag")
elif VOLL:
    print("\n  (Subagent-Tests uebersprungen: LM Studio nicht erreichbar)")
else:
    print("\n  (Subagent-Tests uebersprungen: mit --voll einschalten)")

# --------------------------------------------------------------------------

abschnitt("Aufraeumen")
mcpclient.pool.beenden()
pruefe(True, "Kindprozesse gestoppt")

fehl = [n for ok, n, _ in _ergebnisse if not ok]
print(f"\n{'=' * 62}")
print(f"  {len(_ergebnisse) - len(fehl)} von {len(_ergebnisse)} bestanden")
if fehl:
    print("  Fehlgeschlagen:")
    for n in fehl:
        print(f"    - {n}")
print(f"{'=' * 62}")

shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fehl else 0)
