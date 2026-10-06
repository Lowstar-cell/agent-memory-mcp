"""Prueft den Server so, wie LM Studio ihn benutzt: als Kindprozess ueber stdio.

Die Werkzeugliste innerhalb desselben Python-Prozesses abzufragen beweist
wenig - sie sagt nichts darueber, ob der Prozess sauber startet, ob der
Handshake klappt und ob ein Aufruf ueber die Leitung ein Ergebnis
zurueckbringt. Genau das passiert hier.

    uv run python tests/stdio_check.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

WURZEL = Path(__file__).parent.parent
_ergebnisse: list[tuple[bool, str]] = []


def pruefe(ok: bool, name: str, detail: str = "") -> None:
    _ergebnisse.append((bool(ok), name))
    print(f"  {'OK  ' if ok else 'FEHL'}  {name}" + (f"   {detail}" if detail else ""))


async def main() -> None:
    umgebung = {
        **os.environ,
        "AGENTMEM_DATA_DIR": tempfile.mkdtemp(prefix="agentmem_stdio_"),
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(WURZEL / "server.py")],
        env=umgebung,
        cwd=str(WURZEL),
    )

    print("\n--- Start als Kindprozess (wie LM Studio) ---")
    async with stdio_client(params) as (lesen, schreiben):
        async with ClientSession(lesen, schreiben) as sitzung:
            init = await sitzung.initialize()
            pruefe(True, "Handshake steht", f"{init.serverInfo.name} / Protokoll {init.protocolVersion}")
            pruefe(init.serverInfo.name == "agent-memory", "Server meldet sich als agent-memory",
                   init.serverInfo.name)

            werkzeuge = (await sitzung.list_tools()).tools
            namen = {t.name for t in werkzeuge}
            pruefe(len(werkzeuge) >= 26, "Werkzeuge werden angeboten", f"{len(werkzeuge)} Stueck")
            pruefe({"tool_find", "tool_call", "recall", "remember", "status"} <= namen,
                   "Die wichtigsten sind dabei")

            ohne_beschreibung = [t.name for t in werkzeuge if not (t.description or "").strip()]
            pruefe(not ohne_beschreibung, "Jedes Werkzeug hat eine Beschreibung", str(ohne_beschreibung))

            print("\n--- Ein echter Aufruf ueber die Leitung ---")
            antwort = await sitzung.call_tool("current_datetime", {})
            text = "".join(getattr(b, "text", "") for b in antwort.content)
            pruefe(not antwort.isError and "date" in text, "current_datetime() antwortet", text[:70])

            r = await sitzung.call_tool(
                "remember",
                {"title": "stdio-Probe", "trigger": "Nur ein Testeintrag ueber stdio",
                 "steps": ["nichts tun"], "outcome": "solved"},
            )
            rtext = "".join(getattr(b, "text", "") for b in r.content)
            pruefe(not r.isError and "pb_" in rtext, "remember() ueber stdio legt an", rtext[:70])

            s = await sitzung.call_tool("recall", {"query": "stdio-Probe"})
            stext = "".join(getattr(b, "text", "") for b in s.content)
            pruefe(not s.isError and "stdio-Probe" in stext, "recall() findet ihn wieder")

            print("\n--- Schleifenwaechter ueber die Leitung ---")
            for _ in range(2):
                await sitzung.call_tool("recall", {"query": "voellig unbekanntes thema qqxz"})
            dritte = await sitzung.call_tool("recall", {"query": "voellig unbekanntes thema qqxz"})
            dtext = "".join(getattr(b, "text", "") for b in dritte.content)
            pruefe("schleife_erkannt" in dtext, "Dritter gleicher Aufruf wird gestoppt", dtext[:90])

    fehl = [n for ok, n in _ergebnisse if not ok]
    print(f"\n{'=' * 62}")
    print(f"  {len(_ergebnisse) - len(fehl)} von {len(_ergebnisse)} bestanden")
    for n in fehl:
        print(f"    - {n}")
    print(f"{'=' * 62}")
    sys.exit(1 if fehl else 0)


if __name__ == "__main__":
    asyncio.run(main())
