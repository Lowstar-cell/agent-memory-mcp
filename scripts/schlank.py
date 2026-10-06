"""LM Studio auf einen einzigen MCP-Server umstellen.

Der Sinn der Uebung: LM Studio legt die Werkzeugbeschreibungen JEDES
eingetragenen Servers in den Kontext, bei jeder Nachricht, ob gebraucht oder
nicht. Auf diesem Rechner sind das rund 170 Beschreibungen. Ein 4B-Modell
hat davon sein halbes Arbeitsgedaechtnis voll, bevor die erste Frage gestellt
ist.

Danach steht in mcp.json nur noch 'agent-memory'. Die uebrigen Server sind
nicht weg - sie ziehen in agent-memorys eigenes Verzeichnis um und werden
von dort bei Bedarf gestartet. Erreichbar ueber tool_find/tool_call, also
ueber zwei Beschreibungen statt hundertsiebzig.

    uv run python scripts/schlank.py            # Vorschau
    uv run python scripts/schlank.py --apply    # umstellen
    uv run python scripts/schlank.py --zurueck  # rueckgaengig

Rueckgaengig geht immer: vor jeder Aenderung wird mcp.json gesichert.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agentmem import config, mcpclient, store  # noqa: E402

EIGENER_NAME = "agent-memory"


def _mcp_json_pfad() -> Path:
    return Path(config.load()["pfade"]["lmstudio_mcp_json"])


def _registry_pfad() -> Path:
    return store.data_dir() / "system" / "mcp_servers.json"


def _sicherungen() -> list[Path]:
    p = _mcp_json_pfad()
    return sorted(p.parent.glob(f"{p.name}.schlank-*"), reverse=True)


def zurueck() -> None:
    sicherungen = _sicherungen()
    if not sicherungen:
        print("Keine Sicherung von diesem Skript gefunden - nichts umzustellen.")
        return
    neueste = sicherungen[0]
    shutil.copy2(neueste, _mcp_json_pfad())
    print(f"mcp.json wiederhergestellt aus:\n  {neueste}")


def main() -> None:
    apply = "--apply" in sys.argv
    pfad = _mcp_json_pfad()

    if not pfad.exists():
        print(f"mcp.json nicht gefunden: {pfad}")
        sys.exit(1)

    mcp_json = json.loads(pfad.read_text(encoding="utf-8"))
    server = mcp_json.get("mcpServers", {})

    eigener = {k: v for k, v in server.items() if k == EIGENER_NAME}
    if not eigener:
        print(f"'{EIGENER_NAME}' steht nicht in mcp.json. Erst eintragen, dann umstellen.")
        sys.exit(1)

    umzuziehen = {k: v for k, v in server.items() if k != EIGENER_NAME}
    registry = store.load_json(_registry_pfad(), {}) or {}
    bestand = registry.get("mcpServers", {})

    # Was agent-memory schon selbst kennt, nicht ueberschreiben.
    neu = {k: v for k, v in umzuziehen.items() if k not in bestand}

    print(f"\n{'=' * 66}")
    print(f"  LM Studio verschlanken   [{'AUSGEFUEHRT' if apply else 'VORSCHAU'}]")
    print(f"{'=' * 66}\n")
    print(f"  mcp.json:  {pfad}\n")
    print(f"  Bleibt in LM Studio eingetragen:")
    print(f"    - {EIGENER_NAME}\n")
    print(f"  Zieht um nach agent-memory ({len(umzuziehen)} Server):")

    inventar = mcpclient.index_laden().get("server", {})
    for name in sorted(umzuziehen):
        stand = inventar.get(name, {})
        if stand.get("fehler"):
            wie = "nicht erreichbar - bleibt erreichbar, sobald der Dienst laeuft"
        elif stand.get("werkzeuge"):
            wie = f"{len(stand['werkzeuge'])} Werkzeuge"
        else:
            wie = "noch nicht im Inventar"
        schon = "" if name in neu else "  (kannte agent-memory schon)"
        print(f"    - {name:24} {wie}{schon}")

    gesamt = sum(len(s.get("werkzeuge", [])) for s in inventar.values())
    print(f"\n  Werkzeugbeschreibungen im Kontext:  {gesamt + 26} -> 26")
    print(f"  Die {gesamt} fremden bleiben nutzbar ueber tool_find() und tool_call().")

    if not apply:
        print(f"\n  Nichts geaendert. Zum Umstellen:")
        print(f"    uv run python scripts/schlank.py --apply")
        print(f"  Danach LM Studio neu starten.\n")
        return

    stempel = datetime.now().strftime("%Y%m%d_%H%M%S")
    sicherung = pfad.with_name(f"{pfad.name}.schlank-{stempel}")
    shutil.copy2(pfad, sicherung)

    registry["mcpServers"] = {**bestand, **neu}
    registry.setdefault(
        "//",
        "Server, die agent-memory selbst startet. Aus mcp.json hierher umgezogen, "
        "damit LM Studio nur noch einen Server laden muss.",
    )
    store.save_json(_registry_pfad(), registry)

    mcp_json["mcpServers"] = eigener
    pfad.write_text(json.dumps(mcp_json, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n  Gesichert nach:  {sicherung}")
    print(f"  Umgezogen nach:  {_registry_pfad()}")
    print(f"\n  LM Studio neu starten, dann pruefen:  status()")
    print(f"  Rueckgaengig:    uv run python scripts/schlank.py --zurueck\n")


if __name__ == "__main__":
    if "--zurueck" in sys.argv:
        zurueck()
    else:
        main()
