# agent-memory

*English version: [README.md](README.md)*

Ein MCP-Server, der lokalen Modellen in LM Studio den Rücken freihält:
Gedächtnis, Subagenten, Skills, Smoke-Tests, Kontext-Übergabe — und ein
Durchgang zu allen anderen MCP-Servern, ohne dass deren
Werkzeugbeschreibungen den Kontext auffressen.

Er gibt nicht bloß Anweisungen zurück (*"du bist der Ausführende!"*),
sondern **rechnet selbst**: startet Subagenten, führt Befehle aus,
verdichtet Kontext, reicht fremde Werkzeuge durch.

---

## Das Problem in einer Tabelle

Jeder Server in `mcp.json` legt seine Werkzeugbeschreibungen in **jede
einzelne Nachricht**. Nachgemessen am 10.08.2026 auf einem Rechner mit
13 eingetragenen Servern — nicht geschätzt, sondern von LM Studio selbst
gezählt:

| in `mcp.json` eingetragen | Werkzeuge | Zeichen | Token |
|---|---:|---:|---:|
| alle Server zusammen | 160 | 140.247 | **35.630** |
| nur `agent-memory` | 26 | 18.266 | **5.219** |

**30.411 Token weniger, bei jeder Nachricht — 85 %.**

Was das für ein Kontextfenster bedeutet:

| Fenster | alle Server | nur agent-memory |
|---|---:|---:|
| 8k | 435 % belegt — unmöglich | 64 % |
| 16k | 217 % belegt — unmöglich | 32 % |
| 32k | 109 % belegt — unmöglich | 16 % |

Ein Modell mit 16k Kontext kann mit allen Servern gleichzeitig schlicht
nicht arbeiten, egal wie gut es ist.

### Das ist keine Rechnung, das ist der Praxistest

Beide Modelle wurden mit einem festen 16k-Fenster geladen (alles andere
entladen) und sollten schreiben, bis nichts mehr hineingeht:

| 16k-Fenster | alle 160 Werkzeuge | nur agent-memory |
|---|---|---|
| `qwen3-4b-2507` | **kein einziges Token** | 11.049 Token in 100 s |
| `qwen3.6-35b-a3b` | **kein einziges Token** | 11.156 Token in 1.463 s |

Links steht kein Zahlenspiel, sondern eine abgelehnte Anfrage:

```
400: request (35719 tokens) exceeds the available context size (16384 tokens)
```

Das Modell kommt gar nicht erst zum Zug. Rechts bleiben rund 11.000 Token
zum Arbeiten — nachweislich beschreibbar, nicht bloß rechnerisch frei.

Nebenbefund, der die Wahl des Subagenten-Modells stützt: Für dieselben
~11.000 Token brauchte das 4B **100 Sekunden**, das 35B-A3B **1.463**.
Faktor 15, bei allein geladenem Modell.

Nachmessen auf deinem eigenen Rechner:

```powershell
uv run python scripts/benchmark_kontext.py
uv run python scripts/benchmark_kontext.py --volllaufen   # dauert Minuten
```

Ehrlich dazugesagt: Die 26 eigenen Werkzeuge kosten immer noch ~5.200
Token. Ihre Beschreibungen sind absichtlich ausführlich, weil sie dem
kleinen Modell beibringen, *wann* es was nimmt — das ist der Teil, der
sich bezahlt macht.

### Der Kontext ist nur die halbe Rechnung

Gesparte Token sind das eine. Der eigentliche Gewinn kommt beim **zweiten
Mal**: wenn eine mühsame Aufgabe wiederkommt und `recall()` die fertige
Lösung schon hat, statt dass ein Modell sie noch einmal erarbeitet.

Dieselbe Frage, zwei Wege — gemessen mit den beiliegenden
Beispiel-Einträgen (`scripts/benchmark_gedaechtnis.py`):

> *"Ein Auftrag im Hintergrund kommt nicht vom Fleck, obwohl die
> Grafikkarte 24 GB hat. Woran liegt das und was hilft?"*

| Weg | Token | Zeit | Modellrunden |
|---|---:|---:|---:|
| **erinnern** — `recall` + `open_record` | 925 | 2,2 s | 0 |
| **erarbeiten** — Subagent liest die Dateien | 1.733 | 18,1 s | 2 |

Die Frage teilt kaum ein Wort mit dem gespeicherten Auslöser
("Zeitüberschreitung", "dauert Minuten"). Gefunden wird sie trotzdem, mit
Güte **0,756** — das ist die Bedeutungssuche, nicht Stichwortglück.

Zwei Dinge dazu, die man nicht verschweigen sollte:

- Der Token-Faktor ist mit 1,9x klein, **weil die Aufgabe klein ist**: eine
  Datei lesen. Der Eintrag, der hier gefunden wird, ist ursprünglich aus
  einer Fehlersuche entstanden, die einen halben Tag gedauert hat. Genau da
  liegt der Gewinn, und der lässt sich nicht in zwanzig Sekunden nachstellen.
- Die erinnerte Antwort war auch die **bessere**: Ursache plus vier Schritte.
  Der Subagent lieferte eine Liste von Fundstellen aus `config.json`.

Und das gilt ab dem zweiten Mal — den ersten Weg muss jemand einmal gehen,
damit `remember()` etwas abzulegen hat.

---

## Einrichten

**1. Abhängigkeiten holen** (braucht [uv](https://docs.astral.sh/uv/)):

```powershell
cd agent-memory-mcp
uv sync
```

**2. Prüfen, dass der Server startet** — genau so, wie LM Studio ihn
startet, als Kindprozess über stdio:

```powershell
uv run python tests/stdio_check.py
```

Neun Prüfungen, keine davon braucht ein Modell. Erst wenn die grün sind,
lohnt sich Schritt 3.

**3. In LM Studio eintragen.** Rechts in der Seitenleiste unter
*Program → Install → Edit mcp.json*, oder direkt die Datei bearbeiten:

| System | Ort |
|---|---|
| Windows | `%USERPROFILE%\.lmstudio\mcp.json` |
| macOS / Linux | `~/.lmstudio/mcp.json` |

Der Eintrag — Pfade anpassen, sonst nichts:

```json
{
  "mcpServers": {
    "agent-memory": {
      "command": "C:/Users/DEIN-NAME/AppData/Local/Programs/Python/Python311/Scripts/uv.exe",
      "args": [
        "--directory",
        "C:/Users/DEIN-NAME/MCP-Server/agent-memory-mcp",
        "run",
        "server.py"
      ],
      "env": {}
    }
  }
}
```

Dieselbe Datei liegt als `beispiel-mcp.json` bei. Auf macOS und Linux:

```json
{
  "mcpServers": {
    "agent-memory": {
      "command": "/Users/dein-name/.local/bin/uv",
      "args": ["--directory", "/Users/dein-name/agent-memory-mcp", "run", "server.py"],
      "env": {}
    }
  }
}
```

Vier Dinge, an denen es erfahrungsgemäß scheitert:

- **`uv` mit vollem Pfad angeben.** Der PATH einer GUI-Anwendung ist oft
  kürzer als der im Terminal. `where uv` bzw. `which uv` verrät ihn.
- **Vorwärts-Schrägstriche benutzen**, auch auf Windows. Rückwärts-Schräg­
  striche müssten in JSON verdoppelt werden (`C:\\Users\\...`).
- **Kein `uv`?** Dann `command` auf dein Python zeigen lassen und
  `args: ["C:/…/agent-memory-mcp/server.py"]` — vorher einmal
  `pip install "mcp>=1.28,<2" httpx psutil pyyaml`.
- **Nach dem Eintragen** den Server in LM Studio aus- und wieder
  einschalten. Bei einem Fehlstart sagt LM Studio nichts Lautes, der
  Eintrag verschwindet einfach aus der Werkzeugliste.

**4. Einstellungen ansehen.** `config.json` ist durchkommentiert; drei
Stellen sind mit `ANPASSEN` markiert. Ohne jede Änderung läuft es auch.

---

## Die drei Leitgedanken

### 1. Suchen gibt Verweise. Öffnen gibt Inhalt.

```
recall("serielle Verbindung bricht ab")
  -> [{id: "pb_...", trigger: "LiDAR antwortet mit F0-Bytes", score: 0.83}]   ~40 Wörter

open_record("pb_...")
  -> das vollständige Playbook                                              ~600 Wörter
```

Dasselbe bei Skills (`skill_find` zeigt nur den Einzeiler), bei Subagenten
(nur die Kurzfassung, nie der Verlauf), bei `verify` (nur das Ende der
Ausgabe) und bei fremden Werkzeugen (`tool_find` zeigt kein Schema).

### 2. Ein Server statt sechzehn

```
LM Studio ──▶ agent-memory ──▶ filesystem · terminal · web-search · blender
                              unity · unreal · image-gen · ...
```

`tool_find(query)` sucht in **allen** anderen MCP-Servern, `tool_call(name,
args)` führt aus, `open_record('tool:NAME')` zeigt das Parameterschema. Der
erste `tool_find`-Aufruf baut das Inventar auf (startet jeden Server einmal,
~1 Minute), danach kommt es aus der Datei.

Umstellen — mit Vorschau und Rückweg:

```powershell
uv run python scripts/schlank.py            # zeigt nur, was passieren würde
uv run python scripts/schlank.py --apply    # mcp.json auf einen Server eindampfen
uv run python scripts/schlank.py --zurueck  # rückgängig
```

Die anderen Server werden dabei **nicht gelöscht**, sondern ziehen nach
`data/system/mcp_servers.json` um. Würdest du sie einfach aus `mcp.json`
streichen, wären sie auch für das Gateway weg — es liest dieselbe Datei.

### 3. Wer sich im Kreis dreht, wird gestoppt

Kleine Modelle hängen sich fest. Nicht aus Dummheit, sondern weil ihnen an
einer Stelle nichts Besseres einfällt: Der Aufruf hat nichts gebracht, also
machen sie ihn nochmal. Und nochmal.

| | |
|---|---|
| **Wiederholung** | Dreimal derselbe Aufruf mit denselben Argumenten |
| **Kreis** | `A → B → A → B`, jedes Mal wortgleich |
| **Fruchtlos** | Drei Suchen hintereinander, jede ohne Treffer |
| **Stillstand** | Ein Subagent liefert weder Text noch Werkzeugaufruf |

Beim Stopp wird der Aufruf **gar nicht erst ausgeführt** — das Ergebnis wäre
dasselbe wie beim letzten Mal. Zurück geht statt der Antwort ein konkreter
nächster Schritt:

> *"Die Suche hat dir schon dieselbe Antwort gegeben. Öffne einen der Treffer
> mit `open_record()`, oder such mit ANDEREN Wörtern. Findest du nichts, gibt
> es dazu nichts Gemerktes: arbeite ohne, und leg das Ergebnis danach mit
> `remember()` ab."*

Ein bloßes *"Schleife erkannt, Stopp"* bringt nichts — das Modell wüsste
danach immer noch nicht weiter.

**Was ausdrücklich keine Schleife ist:** `verify` → Code ändern → `verify`.
Deshalb zählen nur *ununterbrochene* Wiederholungen hart, und ein Kreis
verlangt gleiche Argumente in jedem Durchgang. Auf einen Subagenten zu
warten (`agent_result`) wird nie gestoppt.

---

## Werkzeuge (27)

**Überblick** — `status`

**Gedächtnis** — `recall` · `open_record` · `remember`

**Fremde Server** — `tool_find` · `tool_call` · `tool_refresh`

**Skills** — `skill_find` · `skill_run`

**Subagenten** — `agent_spawn` · `agent_result` · `agent_cancel`

**Prüfen** — `verify`

**Kontext** — `context_checkpoint` · `context_resume` · `model_cycle`

**Sonstiges** — `current_datetime` · `ssh_list` · `ssh_edit` ·
`mcp_registry_list` · `mcp_registry_set` · `create_roadmap` · `add_task` ·
`get_next_task` · `update_task_status` · `get_roadmap_status` · `maintenance`

---

## Was der Betrieb mit echten Modellen gelehrt hat

Die schnellen Tests liefen längst grün, als der erste Lauf gegen ein echtes
Modell **fünfmal hintereinander** scheiterte — jedes Mal aus einem anderen
Grund. Vier davon waren keine Programmfehler, sondern Verhalten von LM Studio
und kleinen Modellen. Repariert sind sie trotzdem alle. Für ein System, das
lokale Modelle stützen soll, zählt nicht, wer schuld ist, sondern ob es am
Ende funktioniert.

**Das Modell wird unter dem Auftrag weggeladen.** `HTTP 400: Model unloaded
by user or API request` nach 142 Sekunden. Bei lokalen Modellen ist das der
Normalfall: Leerlauf-Timeout, oder jemand wählt im Chat ein anderes Modell.
Ein Auftrag im Hintergrund löst das Modell jetzt neu auf und nimmt die Runde
nochmal — höchstens zweimal. Dass es passiert ist, steht in der Antwort
(`modell_war_zwischendurch_weg`) und wird nicht verschwiegen.

**Zwei Modelle teilen sich den Speicher.** Eine einzige Modellrunde dauerte
266 Sekunden — 129 Token, rund 0,5 Token/s für ein 35B-A3B auf einer
7900 XTX. Der Grund war nicht das Modell, sondern zwei gleichzeitig geladene
Modelle, die zusammen nicht in 24 GB passen; LM Studio rechnet dann einen
Teil auf der CPU. Von außen sieht das nach nichts aus. Deshalb warnt
`status()` von sich aus, wenn mehr als ein Sprachmodell geladen ist, und
eine Zeitüberschreitung liefert keinen Traceback mehr, sondern den Satz, der
weiterhilft.

**Antwort ohne Beleg.** Ein Lauf lieferte `config.json:42 - "subagent_modell":
"llama-3-70b"` — frei erfunden, das Modell hatte die Datei nie geöffnet.
Kommt eine fertige Antwort, obwohl Werkzeuge bereitstanden und **keines**
benutzt wurde, hakt der Server einmal nach. Liest das Modell danach immer
noch nicht, wird die Antwort trotzdem geliefert, aber mit einer Warnung:

> *"Der Subagent hat kein einziges Werkzeug benutzt, obwohl welche
> bereitstanden. Diese Antwort ist unbelegt — behandle sie als Vermutung,
> nicht als Auskunft."*

Lieber eine als unsicher gekennzeichnete Antwort als eine erfundene, die wie
eine sichere aussieht.

**Der Werkzeugaufruf steht im Antworttext.** Manche Modelle schreiben
`{"name": "filesystem_read_text_file", "arguments": {...}}` als Fließtext,
statt den Werkzeug-Kanal zu benutzen, und verschlucken dabei die Trennzeichen
im Namen. Falsch ist daran die Verpackung, nicht die Absicht: Der Server
holt solche Aufrufe aus dem Text heraus — auch aus ```-Zäunen und
`<tool_call>`-Klammern — und ordnet den verstümmelten Namen dem echten zu.
Erfunden wird dabei nichts: Was sich keinem angebotenen Werkzeug zuordnen
lässt, bleibt Text.

---

## Einstellungen (`config.json`)

```json
"subagent_modell": "auto",
"erlaubte_mcp_server": ["web-search", "filesystem", "terminal"],
"gateway_server": "alle",
"pfade": { "wissensbasis": "" }
```

- **`subagent_modell`** — `"auto"` nimmt das Modell, das ohnehin läuft, und
  lädt nie etwas nach. Das funktioniert immer, erbt aber die Geschwindigkeit
  deines Chatmodells. Gemessen:

  | Modell | Ergebnis |
  |---|---|
  | `auto` → 35B-A3B (Chatmodell) | 2 Versuche, beide gescheitert; eine Runde 266 s, dann Engine-Abbruch |
  | `qwen3-4b-2507` | 71 s, 4 Runden, 3 echte Werkzeugaufrufe, richtige Antwort |

  Subagenten sind viel Lesen und wenig Denken — dafür ist ein 4B das
  passende Werkzeug, und dein Chatmodell bleibt unangetastet.
- **`erlaubte_mcp_server`** — was ein **Subagent** unbeaufsichtigt benutzen
  darf. Bewusst eng: hier liest kein Mensch mit.
- **`gateway_server`** — was das **Hauptmodell** über `tool_call` erreichen
  darf. Weiter gefasst, denn am Chat sitzt jemand, der mitliest.
- **`pfade.wissensbasis`** — optional. Ein Ordner mit `wiki/*.md` und
  `skills/*.md` darin wird mitdurchsucht (`recall`, `skill_find`), aber
  **nie beschrieben**. Die Dateien bleiben, wo sie sind; Git und Editor
  arbeiten weiter darauf.

**Rückruf-Sperre:** Namen, die `agent-memory` oder `agentmem` enthalten,
werden hart herausgefiltert. Der Server kann sich nicht selbst aufrufen, ein
Subagent keine Subagenten starten.

### Was du wissen solltest, bevor du Werkzeuge freigibst

Ein Subagent läuft **unbeaufsichtigt** in einem Hintergrund-Thread. Was in
`erlaubte_mcp_server` steht, darf ein kleines Modell ohne Rückfrage
benutzen — bis zu acht Runden lang, während du etwas anderes tust.

Deshalb steht `terminal` **nicht** in der Standardliste, obwohl es
naheliegt. Übliche terminal-MCP-Server laufen mit `ALLOWED_COMMANDS=all` auf
dem ganzen Laufwerk; ein 4B-Modell dürfte damit beliebige Befehle absetzen —
auch solche, die es missversteht. Das Hauptmodell erreicht denselben Server
weiterhin über `tool_call`, und dort sitzt jemand davor, der mitliest. Genau
dafür sind die beiden Listen getrennt.

Ebenso: `verify` führt den Befehl aus, den du ihm gibst, und `gateway_server:
"alle"` heißt wirklich alle Server aus deiner `mcp.json`. Das ist gewollt,
aber es sollte eine Entscheidung sein und keine Überraschung.

---

## Ablage

Eine kleine JSON-Datei je Eintrag unter `data/` — keine Datenbank, lesbar,
versionierbar.

```
data/records/         eigene Einträge: Playbooks, Probleme, Reviews, Notizen
data/jobs/            Subagent-Aufträge mit vollem Verlauf
data/verifications/   Smoke-Test-Protokolle
data/handoffs/        Kontext-Übergaben
data/index/           Embeddings, Werkzeug-Inventar
data/system/          Hardware, SSH, eigene MCP-Server-Vermerke
```

Drei Beispiel-Einträge liegen bei, damit `recall()` vom ersten Aufruf an
etwas findet und das Format sichtbar ist. Sie sind zum Löschen gedacht,
sobald du eigene hast.

**Einträge verbinden:**

```
remember(..., related=["loest: pb_20260809_lidar", "haengt_an: Pi4"])
open_record("nt_…")  ->  verweise:      loest -> "Serieller LiDAR liefert F0-Bytes" (vorhanden)
                                        haengt_an -> "Pi4" (noch nicht angelegt)
                         rueckverweise: wer von anderswo hierher zeigt
```

Die Verbindung steht *im* Eintrag, nicht in einer separaten Graph-Datei im
Cache-Verzeichnis eines Paketmanagers. Rückverweise werden beim Öffnen
**berechnet**, nicht gespeichert — gespeicherte Gegenrichtungen verrotten
beim ersten Umbenennen. Ein Ziel, das es noch nicht gibt, verschwindet
nicht: Es bleibt stehen und zeigt, was noch fehlt.

`data/index/embeddings.jsonl` ist Zwischenspeicher, kein Bestand. Löscht man
ihn, legt ihn der nächste `recall()` aus den Einträgen neu an. `status()`
zeigt die Abdeckung (`gedaechtnis: eingebettet/einträge`), damit ein stiller
Ausfall des Embedding-Modells nicht erst auffällt, wenn etwas nicht gefunden
wird.

---

## Selbst prüfen

```powershell
uv run python tests/stdio_check.py     # Handshake wie von LM Studio, 9 Prüfungen
uv run python tests/smoke.py           # ~91 Prüfungen, ohne Modell, ~60 s
uv run python tests/smoke.py --voll    # zusätzlich mit echtem Subagenten
uv run python -m agentmem.mcpclient --inventar   # Werkzeug-Inventar neu aufbauen
```

Alle Tests arbeiten gegen ein temporäres Datenverzeichnis und fassen deine
Einträge nicht an.

---

## Zwei ehrliche Hinweise

**`model_cycle` leert den Chatverlauf nicht.** Was den Kontext wirklich
leert, ist ein neuer Chat:

```
context_checkpoint()   →  Stand verdichten und ablegen
[model_cycle()]        →  optional anderes Modell
NEUEN Chat öffnen      →  erst das leert den Kontext
context_resume()       →  Übergabe als erste Nachricht
```

**`mcp` ist bewusst auf 1.x festgehalten.** In `mcp 2.0` heißt `FastMCP`
jetzt `MCPServer` unter `mcp.server`, und `httpx` wurde durch `httpx2`
ersetzt — beides bricht diesen Server sofort. Die 1.x-Reihe ist in Wartung
und bekommt weiter Sicherheitskorrekturen. Dass das kein theoretisches
Risiko ist, zeigt der Alltag: Ein Eintrag `git-versioning` startete nicht
mehr, weil `uvx` für `mcp-server-git` die 2.x-Reihe zog —
`AttributeError: 'Server' object has no attribute 'list_tools'`. Genau der
Bruch, den die Sperre hier verhindert.

---

## Erprobt auf

Windows 11, AMD Radeon RX 7900 XTX, LM Studio, Python 3.11+. **Nie gelaufen
auf macOS oder Linux** — die Pfade sind plattformneutral geschrieben, aber
geschrieben ist nicht dasselbe wie ausprobiert. Gesprochen wird nur mit LM
Studio; Ollama und llama.cpp nutzen dieselbe Schnittstelle, das Nachladen von
Modellen und die Modellliste sind aber LM-Studio-eigen und ungetestet.

---

## Voraussetzungen

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/) (empfohlen) oder pip
- LM Studio mit aktiviertem lokalem Server (Standard: `http://localhost:1234`)
- Ein werkzeugfähiges Modell. Für Subagenten reicht ein 4B; fürs Chatten
  nimm, was du magst.
