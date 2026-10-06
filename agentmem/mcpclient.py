"""agent-memory als Nutzer anderer MCP-Server.

LM Studio ruft agent-memory auf. agent-memory wiederum startet 'web-search',
'filesystem' usw. als Kindprozesse:

    LM Studio  --ruft auf-->  agent-memory  --startet und nutzt-->  web-search
                                            --startet und nutzt-->  filesystem
                                            --startet und nutzt-->  terminal

Das hat zwei Nutzniesser:

  Subagenten   bekommen Werkzeuge, um selbstaendig zu recherchieren.
  Das Hauptmodell  erreicht ueber tool_find/tool_call alle Server, ohne dass
               LM Studio deren Werkzeugbeschreibungen in den Kontext legen
               muss. 16 Server mit zusammen ueber hundert Werkzeugen kosten
               so nur zwei Werkzeugbeschreibungen statt hundert.

Zur Technik: Die MCP-Client-Bibliothek ist asynchron, der Auftragsablauf
synchron und in Hintergrund-Threads. Deshalb laeuft hier ein eigener
Event-Loop in einem eigenen Thread.

Jeder Server bekommt dabei eine EIGENE, langlebige Aufgabe in diesem Loop.
Das ist keine Kosmetik, sondern noetig: Ein anyio-Kontext muss in derselben
Aufgabe verlassen werden, in der er betreten wurde. Frueher lagen alle
Verbindungen in einem gemeinsamen AsyncExitStack, der beim Aufraeumen aus
einer anderen Aufgabe geschlossen wurde - das warf zuverlaessig
"Attempted to exit cancel scope in a different task" und musste
weggeschluckt werden. Nebenbei loest die Trennung das zweite Problem: Ein
Server, der nicht startet (Blender ist zu, Unity laeuft nicht), reisst die
anderen nicht mehr mit.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import re
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import config, store

TRENNER = "__"

# Ein Server, der so lange zum Starten braucht, ist entweder kaputt oder
# wartet auf eine Anwendung, die gar nicht laeuft.
START_TIMEOUT = 25.0

# Etliche fremde Server schreiben Klartext auf stdout, bevor das Protokoll
# anlaeuft ("Web Search MCP Server starting..."). Der MCP-Leser wirft
# darueber je einen vollen Traceback - bei einem Inventarlauf ueber sechzehn
# Server sind das hunderte Zeilen, die alle nichts bedeuten und im Log von
# LM Studio landen. Ob ein Server wirklich hochkommt, entscheidet ohnehin
# _Verbindung.starten() und wird dort sauber gemeldet.
logging.getLogger("mcp.client.stdio").setLevel(logging.CRITICAL)


class MCPClientFehler(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Hintergrund-Event-Loop
# --------------------------------------------------------------------------


class _Hintergrundloop:
    """Ein asyncio-Loop in einem eigenen Thread, damit synchroner Code
    langlebige asynchrone Verbindungen benutzen kann."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _start(self) -> None:
        with self._lock:
            if self._loop is not None:
                return
            bereit = threading.Event()

            def run() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                bereit.set()
                loop.run_forever()

            self._thread = threading.Thread(target=run, name="agentmem-mcp", daemon=True)
            self._thread.start()
            bereit.wait(timeout=10)

    def submit(self, coro) -> Future:
        self._start()
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run(self, coro, timeout: float = 120.0) -> Any:
        return self.submit(coro).result(timeout=timeout)

    def stop(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)


_loop = _Hintergrundloop()


# --------------------------------------------------------------------------
# Serverkonfiguration aus .lmstudio/mcp.json
# --------------------------------------------------------------------------


def _local_mcp_json() -> dict[str, dict]:
    pfad = store.data_dir() / "system" / "mcp_servers.json"
    if not pfad.exists():
        return {}
    try:
        return json.loads(pfad.read_text(encoding="utf-8")).get("mcpServers", {})
    except json.JSONDecodeError:
        return {}


def _mcp_json() -> dict[str, dict]:
    local = _local_mcp_json()
    pfad = Path(config.load()["pfade"]["lmstudio_mcp_json"])
    if not pfad.exists():
        if local:
            return local
        raise MCPClientFehler(f"mcp.json nicht gefunden: {pfad}")
    try:
        remote = json.loads(pfad.read_text(encoding="utf-8")).get("mcpServers", {})
    except json.JSONDecodeError as e:
        raise MCPClientFehler(f"mcp.json ist kein gueltiges JSON: {e}") from e
    # Was LM Studio startet, hat Vorrang; lokale Vermerke ergaenzen nur.
    return {**local, **remote}


def _gesperrt(name: str) -> bool:
    return any(g in name.lower() for g in config.GESPERRTE_MCP_NAMEN)


def verfuegbare_server() -> list[str]:
    """Server, die ein SUBAGENT nutzen darf: in mcp.json eingetragen UND in
    config.json freigegeben UND nicht gesperrt."""
    eingetragen = set(_mcp_json())
    freigegeben = set(config.load()["erlaubte_mcp_server"])
    return sorted(n for n in eingetragen & freigegeben if not _gesperrt(n))


def gateway_server() -> list[str]:
    """Server, die das HAUPTMODELL ueber tool_call erreichen darf.

    Bewusst weiter gefasst als die Subagenten-Freigabe: Am Chat sitzt ein
    Mensch, der mitliest. Ein Subagent laeuft unbeaufsichtigt, deshalb
    bekommt er nur die enge Liste.
    """
    eingetragen = [n for n in _mcp_json() if not _gesperrt(n)]
    erlaubt = config.load().get("gateway_server", "alle")
    if erlaubt == "alle":
        return sorted(eingetragen)
    return sorted(set(eingetragen) & set(erlaubt))


def _pruefe(name: str, fuer_gateway: bool = False) -> dict:
    if _gesperrt(name):
        raise MCPClientFehler(
            f"'{name}' ist gesperrt - agent-memory darf sich nicht selbst aufrufen."
        )
    erlaubt = gateway_server() if fuer_gateway else verfuegbare_server()
    if name not in erlaubt:
        raise MCPClientFehler(f"'{name}' ist nicht freigegeben. Erlaubt: {erlaubt}")
    server = _mcp_json()
    if name not in server:
        raise MCPClientFehler(f"'{name}' steht nicht in mcp.json.")
    return server[name]


def _sicherer_name(s: str) -> str:
    """OpenAI-Werkzeugnamen duerfen nur [a-zA-Z0-9_-] enthalten."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)


def voller_name(server: str, werkzeug: str) -> str:
    return f"{_sicherer_name(server)}{TRENNER}{_sicherer_name(werkzeug)}"[:64]


# --------------------------------------------------------------------------
# Eine Verbindung, eine Aufgabe
# --------------------------------------------------------------------------


class _Verbindung:
    """Haelt einen Kindprozess offen, betreut von genau einer asyncio-Aufgabe."""

    def __init__(self, name: str, cfg: dict) -> None:
        self.name = name
        self.cfg = cfg
        self.session: ClientSession | None = None
        self.fehler: str | None = None
        self._bereit = asyncio.Event()
        self._stopp = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.aufruf_lock = asyncio.Lock()

    async def _leben(self) -> None:
        params = StdioServerParameters(
            command=self.cfg["command"],
            args=self.cfg.get("args", []),
            env={**self.cfg.get("env", {})} or None,
        )
        try:
            async with stdio_client(params) as (lesen, schreiben):
                async with ClientSession(lesen, schreiben) as session:
                    await session.initialize()
                    self.session = session
                    self._bereit.set()
                    await self._stopp.wait()
        except Exception as e:
            self.fehler = f"{type(e).__name__}: {e}"
        finally:
            self.session = None
            self._bereit.set()  # Wartende nicht haengen lassen

    async def starten(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._leben(), name=f"mcp:{self.name}")
        try:
            await asyncio.wait_for(self._bereit.wait(), timeout=START_TIMEOUT)
        except asyncio.TimeoutError:
            self.fehler = f"Start dauerte laenger als {START_TIMEOUT:.0f}s"
        if self.session is None:
            raise MCPClientFehler(f"'{self.name}' nicht startbar: {self.fehler or 'unbekannt'}")

    async def beenden(self) -> None:
        self._stopp.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()
            self._task = None


# --------------------------------------------------------------------------
# Verbindungspool
# --------------------------------------------------------------------------


class MCPPool:
    def __init__(self) -> None:
        self._verbindungen: dict[str, _Verbindung] = {}
        self._registry_lock = threading.Lock()

    # -- asynchroner Teil ---------------------------------------------------

    async def _hole(self, name: str, fuer_gateway: bool = False) -> _Verbindung:
        v = self._verbindungen.get(name)
        if v is None or (v.session is None and v._task is None):
            v = _Verbindung(name, _pruefe(name, fuer_gateway))
            self._verbindungen[name] = v
        if v.session is None:
            await v.starten()
        return v

    async def _werkzeuge(self, namen: list[str]) -> tuple[list[dict], dict[str, tuple[str, str]]]:
        schemas: list[dict] = []
        zuordnung: dict[str, tuple[str, str]] = {}
        for name in namen:
            v = await self._hole(name)
            for t in (await v.session.list_tools()).tools:  # type: ignore[union-attr]
                voll = voller_name(name, t.name)
                schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": voll,
                            "description": (t.description or "")[:1024],
                            "parameters": t.inputSchema or {"type": "object", "properties": {}},
                        },
                    }
                )
                zuordnung[voll] = (name, t.name)
        return schemas, zuordnung

    async def _katalog(self, name: str) -> dict:
        """Werkzeugliste eines Servers holen. Faellt der Server aus, wird das
        vermerkt statt geworfen - ein toter Blender darf die Suche nicht
        verhindern."""
        neu_geoeffnet = name not in self._verbindungen or self._verbindungen[name].session is None
        try:
            v = await self._hole(name, fuer_gateway=True)
            werkzeuge = [
                {
                    "name": voller_name(name, t.name),
                    "werkzeug": t.name,
                    # Nur der erste Satz. Das ist der Punkt der ganzen Uebung.
                    "beschreibung": _einzeiler(t.description),
                }
                for t in (await v.session.list_tools()).tools  # type: ignore[union-attr]
            ]
            ergebnis = {"werkzeuge": werkzeuge, "fehler": None, "stand": store.jetzt()}
        except Exception as e:
            ergebnis = {"werkzeuge": [], "fehler": f"{type(e).__name__}: {e}", "stand": store.jetzt()}

        if neu_geoeffnet and ergebnis["fehler"] is None:
            # Nur fuers Inventar geoeffnet: wieder zumachen. Sonst stehen nach
            # einem tool_find fuenfzehn Kindprozesse im Speicher.
            await self._schliesse(name)
        return ergebnis

    async def _schliesse(self, name: str) -> None:
        v = self._verbindungen.pop(name, None)
        if v is not None:
            await v.beenden()

    async def _aufrufen(self, server: str, werkzeug: str, args: dict, fuer_gateway: bool) -> tuple[str, bool]:
        v = await self._hole(server, fuer_gateway)
        async with v.aufruf_lock:
            ergebnis = await v.session.call_tool(werkzeug, args)  # type: ignore[union-attr]
        teile = []
        for block in ergebnis.content:
            text = getattr(block, "text", None)
            teile.append(text if text is not None else f"[{getattr(block, 'type', 'inhalt')}]")
        return "\n".join(teile), bool(ergebnis.isError)

    async def _alle_beenden(self) -> None:
        for name in list(self._verbindungen):
            await self._schliesse(name)

    # -- synchrone Oberflaeche ---------------------------------------------

    def werkzeuge(self, namen: list[str], timeout: float = 120.0):
        """Werkzeugbeschreibungen der genannten Server einsammeln.

        Gibt (schemas_im_openai_format, zuordnung) zurueck. Die Zuordnung
        uebersetzt 'web_search__search' zurueck nach ('web-search', 'search').
        """
        return _loop.run(self._werkzeuge(namen), timeout=timeout)

    def katalog(self, name: str, timeout: float = 60.0) -> dict:
        return _loop.run(self._katalog(name), timeout=timeout)

    def aufrufen(self, server: str, werkzeug: str, args: dict, timeout: float = 180.0, fuer_gateway: bool = False):
        return _loop.run(self._aufrufen(server, werkzeug, args, fuer_gateway), timeout=timeout)

    def offen(self) -> list[str]:
        return sorted(n for n, v in self._verbindungen.items() if v.session is not None)

    def beenden(self) -> None:
        """Alle Kindprozesse sauber stoppen."""
        with self._registry_lock:
            try:
                _loop.run(self._alle_beenden(), timeout=45.0)
            except Exception:
                pass  # Beim Herunterfahren nicht am Aufraeumen scheitern.


pool = MCPPool()


def _einzeiler(text: str | None) -> str:
    """Erster Satz einer Werkzeugbeschreibung, hoechstens 160 Zeichen."""
    t = " ".join((text or "").split())
    if not t:
        return ""
    for trenner in (". ", "! ", "? "):
        if trenner in t[:200]:
            t = t.split(trenner, 1)[0] + "."
            break
    return t[:160]


# --------------------------------------------------------------------------
# Werkzeug-Inventar fuer das Hauptmodell
# --------------------------------------------------------------------------


def _index_pfad() -> Path:
    return store.data_dir() / "index" / "werkzeuge.json"


def index_laden() -> dict:
    return store.load_json(_index_pfad(), {}) or {}


def index_aufbauen(nur: list[str] | None = None, neu: bool = False) -> dict:
    """Inventar aller erreichbaren Werkzeuge anlegen oder auffrischen.

    Der teure Teil - jeden Server einmal starten und fragen - passiert genau
    hier. Danach ist tool_find eine reine Textsuche ohne Prozessstart.
    """
    store.ensure_dirs()
    index = index_laden()
    server_stand = index.get("server", {})

    ziele = nur if nur is not None else gateway_server()
    for name in ziele:
        if not neu and name in server_stand and server_stand[name].get("fehler") is None:
            continue
        server_stand[name] = pool.katalog(name)

    # Server, die aus mcp.json verschwunden sind, fliegen raus.
    gueltig = set(gateway_server())
    server_stand = {k: v for k, v in server_stand.items() if k in gueltig}

    index = {"stand": store.jetzt(), "server": server_stand}
    store.save_json(_index_pfad(), index)
    return index


def werkzeuge_finden(query: str = "", server: str | None = None, limit: int = 12) -> dict:
    """Passende Werkzeuge suchen. Gibt Namen und Einzeiler, nie Schemas."""
    index = index_laden()
    if not index.get("server"):
        index = index_aufbauen()

    alle: list[dict] = []
    kaputt: dict[str, str] = {}
    for sname, stand in index.get("server", {}).items():
        if stand.get("fehler"):
            kaputt[sname] = stand["fehler"][:120]
            continue
        if server and sname != server:
            continue
        for w in stand.get("werkzeuge", []):
            alle.append({**w, "server": sname})

    worte = [w.lower() for w in query.split() if len(w) > 2]
    if worte:
        def punkte(w: dict) -> int:
            heu = f"{w['name']} {w['beschreibung']} {w['server']}".lower()
            return sum(3 if wo in w["name"].lower() else 1 for wo in worte if wo in heu)

        bewertet = sorted(((punkte(w), w) for w in alle), key=lambda x: -x[0])
        alle = [w for p, w in bewertet if p > 0]

    return {
        "werkzeuge": [
            {"name": w["name"], "beschreibung": w["beschreibung"], "server": w["server"]}
            for w in alle[:limit]
        ],
        "gefunden": len(alle),
        "server_im_inventar": sorted(index.get("server", {})),
        "nicht_erreichbar": kaputt or None,
    }


def werkzeug_schema(name: str) -> dict | None:
    """Volles Parameterschema eines Werkzeugs.

    Kommt frisch vom Server. Im Inventar steht mit Absicht nur der Einzeiler
    - lauter vollstaendige Schemas waeren genau der Kontext-Berg, den dieses
    Gateway vermeiden soll.
    """
    treffer = aufloesen(name)
    if treffer is None:
        return None
    sname, wname = treffer
    try:
        schemas, _ = _loop.run(_schemas_fuer_gateway(sname), timeout=60.0)
    except Exception as e:
        return {"name": name, "fehler": f"{type(e).__name__}: {e}"}
    for s in schemas:
        if s["function"]["name"] == name:
            return {
                "name": name,
                "server": sname,
                "werkzeug": wname,
                "beschreibung": s["function"]["description"],
                "parameter": s["function"]["parameters"],
            }
    return None


async def _schemas_fuer_gateway(name: str) -> tuple[list[dict], dict]:
    v = await pool._hole(name, fuer_gateway=True)
    schemas: list[dict] = []
    zuordnung: dict[str, tuple[str, str]] = {}
    for t in (await v.session.list_tools()).tools:  # type: ignore[union-attr]
        voll = voller_name(name, t.name)
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": voll,
                    "description": (t.description or "")[:1024],
                    "parameters": t.inputSchema or {"type": "object", "properties": {}},
                },
            }
        )
        zuordnung[voll] = (name, t.name)
    return schemas, zuordnung


def aufloesen(voll: str) -> tuple[str, str] | None:
    """'filesystem__read_file' -> ('filesystem', 'read_file').

    Ueber das Inventar, nicht ueber Zerlegen am Trenner: Servernamen duerfen
    selbst Unterstriche enthalten, dann waere das Zerlegen mehrdeutig.
    """
    index = index_laden()
    for sname, stand in index.get("server", {}).items():
        for w in stand.get("werkzeuge", []):
            if w["name"] == voll:
                return sname, w["werkzeug"]
    return None


@atexit.register
def _aufraeumen() -> None:
    pool.beenden()
    _loop.stop()


# --------------------------------------------------------------------------
# Selbsttest:  uv run python -m agentmem.mcpclient --probe web-search
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if "--inventar" in sys.argv:
        print(f"Gateway-Server: {gateway_server()}")
        idx = index_aufbauen(neu=True)
        for sname, stand in sorted(idx["server"].items()):
            if stand.get("fehler"):
                print(f"  {sname:24} FEHLER: {stand['fehler'][:80]}")
            else:
                print(f"  {sname:24} {len(stand['werkzeuge'])} Werkzeuge")
        gesamt = sum(len(s["werkzeuge"]) for s in idx["server"].values())
        print(f"\nInsgesamt {gesamt} Werkzeuge hinter 2 Werkzeugbeschreibungen.")
        pool.beenden()

    elif "--probe" in sys.argv:
        ziel = sys.argv[sys.argv.index("--probe") + 1]
        print(f"Freigegebene Server (Subagent): {verfuegbare_server()}")
        print(f"Verbinde mit '{ziel}' ...")
        schemas, zuordnung = pool.werkzeuge([ziel])
        print(f"{len(schemas)} Werkzeuge gefunden:")
        for s in schemas:
            f = s["function"]
            print(f"  - {f['name']}: {(f['description'] or '')[:80]}")
        print("\nGesperrt-Test:")
        for name in ("max-effi", "agent-memory", "agentmem"):
            try:
                _pruefe(name)
                print(f"  FEHLER: '{name}' haette gesperrt sein muessen!")
            except MCPClientFehler as e:
                print(f"  OK: {e}")
        pool.beenden()
