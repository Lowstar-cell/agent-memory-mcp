"""Skills: Ablaufanweisungen, die auch ausgefuehrt werden koennen.

Der entscheidende Unterschied zum alten activate_skill(): Dort wurde der
komplette Skilltext an das aufrufende Modell zurueckgegeben, das ihn dann
selbst abarbeiten musste - der Kontext war danach voll, bevor die Arbeit
begann.

Hier gilt:
  skill_find()  gibt nur Name und Einzeiler zurueck  (~20 Woerter je Skill)
  open_record() gibt den Text, wenn ihn jemand wirklich lesen will
  skill_run()   gibt den Text an einen Subagenten und liefert das Ergebnis

Dafuer stehen 'model' und 'tools' in den Kopfdaten: Sie machen aus einer
Anleitung etwas Ausfuehrbares.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import agents, config


def _skills_ordner() -> list[tuple[str, Path]]:
    """Wo Skills liegen duerfen - mit Herkunft.

    Zwei Orte, und das ist Absicht: Der Server bringt eigene Ablaeufe mit,
    die Wissensbasis auf dem Desktop hat ihre eigenen. Ohne den zweiten Ort
    war 'knowledge-extractor' aus Desktop/max-effi/skills fuer skill_find()
    unsichtbar - die Wissensbasis war nur halb eingebunden: Die Wiki-Artikel
    wurden gelesen, die Skills daneben nicht.
    """
    ordner = [("server", config.SKILLS_DIR)]
    wurzel = config.wissensbasis()
    if wurzel:
        basis = wurzel / "skills"
        if basis.exists() and basis.resolve() != config.SKILLS_DIR.resolve():
            ordner.append(("wissensbasis", basis))
    return ordner


def kopfdaten(text: str) -> tuple[dict, str]:
    """Trennt YAML-Kopf und Rumpf.

    Ersetzt den alten parse_frontmatter (max_effi_server.py:20-31), der nur
    am ersten Doppelpunkt trennte und deshalb keine Listen lesen konnte -
    'tools: [web-search]' wurde dort zum String "[web-search]".
    """
    if not text.startswith("---"):
        return {}, text
    ende = text.find("\n---", 3)
    if ende < 0:
        return {}, text
    try:
        meta = yaml.safe_load(text[3:ende]) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, text[ende + 4 :].lstrip("\n")


def _laden(pfad: Path, herkunft: str = "server") -> dict | None:
    try:
        text = pfad.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    meta, rumpf = kopfdaten(text)
    tools = meta.get("tools") or []
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.strip("[]").split(",") if t.strip()]

    # Die Skills der Wissensbasis benutzen 'skill_name' statt 'name' und haben
    # den Zweck als Fettzeile im Text statt als 'description' im Kopf. Beides
    # mitnehmen, statt sie an einer Formalie scheitern zu lassen.
    beschreibung = (meta.get("description") or "").strip()
    if not beschreibung:
        for zeile in rumpf.splitlines():
            z = zeile.strip()
            if z.lower().startswith("**zweck:**"):
                beschreibung = z.split("**", 2)[-1].lstrip(": ").strip()[:200]
                break

    return {
        "name": meta.get("name") or meta.get("skill_name") or pfad.stem,
        "herkunft": herkunft,
        "description": beschreibung,
        "model": meta.get("model") or "auto",
        "tools": tools,
        "inputs": meta.get("inputs") or [],
        "verify": meta.get("verify"),
        "status": meta.get("status") or "aktiv",
        "datei": pfad.name,
        "rumpf": rumpf,
        "voll": text,
    }


def alle() -> list[dict]:
    """Alle Skills aus beiden Orten, jeder Name genau einmal.

    Bei gleichem Namen gewinnt der Skill des Servers. Er ist derjenige, der
    Kopfdaten mit 'model' und 'tools' hat und damit wirklich ausfuehrbar ist.
    """
    nach_name: dict[str, dict] = {}
    for herkunft, ordner in reversed(_skills_ordner()):
        if not ordner.exists():
            continue
        for p in sorted(ordner.glob("*.md")):
            if p.name.startswith("_"):
                continue
            s = _laden(p, herkunft)
            if s:
                nach_name[s["name"]] = s
    return sorted(nach_name.values(), key=lambda s: s["name"])


def holen(name: str) -> dict | None:
    for s in alle():
        if s["name"] == name or s["datei"] == f"{name}.md":
            return s
    return None


def finden(query: str = "", limit: int = 8) -> list[dict]:
    """Nur Name, Einzeiler und Modell - nie der Inhalt."""
    skills = [s for s in alle() if s["status"] != "veraltet"]
    if query.strip():
        worte = [w.lower() for w in query.split() if len(w) > 2]
        def treffer(s: dict) -> int:
            heu = f"{s['name']} {s['description']} {' '.join(s['tools'])}".lower()
            return sum(1 for w in worte if w in heu)
        bewertet = [(treffer(s), s) for s in skills]
        if any(t for t, _ in bewertet):
            skills = [s for t, s in sorted(bewertet, key=lambda x: -x[0]) if t > 0]
    return [
        {"name": s["name"], "description": s["description"], "model": s["model"],
         "tools": s["tools"], "herkunft": s["herkunft"]}
        for s in skills[:limit]
    ]


def ausfuehren(name: str, inputs: dict[str, Any] | None = None) -> dict:
    """Uebergibt den Skill an einen Subagenten."""
    skill = holen(name)
    if skill is None:
        raise ValueError(f"Skill '{name}' gibt es nicht. Verfuegbar: {[s['name'] for s in alle()]}")

    fehlend = [f for f in skill["inputs"] if f not in (inputs or {})]

    system_prompt = (
        f"Du fuehrst den Skill '{skill['name']}' aus. Die Anleitung steht unten. "
        f"Arbeite sie Schritt fuer Schritt ab und antworte am Ende knapp mit dem "
        f"Ergebnis - hoechstens 250 Woerter, ohne die Anleitung zu wiederholen.\n\n"
        f"{'=' * 50}\n{skill['rumpf']}"
    )

    eingaben = "\n".join(f"- {k}: {v}" for k, v in (inputs or {}).items())
    aufgabe = f"Fuehre den Skill aus.\n\nEingaben:\n{eingaben or '(keine)'}"
    if fehlend:
        aufgabe += (
            f"\n\nHinweis: Fuer {fehlend} wurde nichts uebergeben. "
            f"Arbeite mit dem, was da ist, und sag am Ende, was gefehlt hat."
        )

    job = agents.starten(
        aufgabe=aufgabe,
        rolle="research" if skill["tools"] else "summarize",
        tools=skill["tools"],
        modell=None if skill["model"] == "auto" else skill["model"],
        system_prompt=system_prompt,
        titel=f"skill:{skill['name']}",
    )
    job["fehlende_eingaben"] = fehlend
    return job
