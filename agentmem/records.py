"""Ein gemeinsames Datenmodell fuer alles Gemerkte.

Frueher gab es dafuer drei getrennte Ablagen mit je eigenen Werkzeugen:
problems/ (log_problem, list_problems, get_problem), code_reviews/
(submit_code_review) und das Wiki. Alle drei beantworten dieselbe Frage -
"was wissen wir schon darueber?" - und werden deshalb hier zusammengefuehrt.
Unterschieden wird nur noch ueber 'kind'.

Der wichtigste Teil ist 'trigger': eine einzige Zeile, die sagt, WANN dieser
Eintrag hilft. Nur sie wird bei der Suche zurueckgegeben. Das vollstaendige
Playbook holt sich der Aufrufer erst mit open_record(), wenn er es braucht.

Zwei Sorten Eintraege, und der Unterschied ist wichtig:

  eigene Eintraege (pb_, rv_, nt_)  liegen als JSON in data/records/.
                                    max-effi besitzt sie und schreibt sie.
  Wiki-Artikel     (wk_)            liegen als Markdown in der Wissensbasis.
                                    max-effi liest sie NUR. Die Wahrheit ist
                                    immer die Datei auf der Platte.

Diese Trennung wird hier hart durchgesetzt (siehe _ist_wiki). Wurde sie
verletzt, war die Folge, dass ein einmal geoeffneter Wiki-Artikel als Kopie
in data/records/ landete und ab da eingefroren war: Aenderungen an der
Markdown-Datei waren fuer das Modell unsichtbar, und der Artikel tauchte in
der Suche doppelt auf. Was ein Wiki-Artikel an eigenen Daten mitbringt -
Nutzungszaehler, Pruefergebnisse - steht deshalb in einer Beiakte
(data/index/wiki_meta.json) und nicht im Artikel.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import store

ARTEN = ("playbook", "problem", "review", "wiki", "notiz")


def _ordner() -> Path:
    return store.data_dir() / "records"


def _ist_wiki(record_id: str) -> bool:
    return record_id.startswith("wk_")


def neue_id(titel: str, kind: str = "playbook") -> str:
    praefix = {"playbook": "pb", "problem": "pb", "review": "rv", "wiki": "wk", "notiz": "nt"}.get(kind, "pb")
    return f"{praefix}_{store.zeitstempel()}_{store.slugify(titel, 40)}"


# --------------------------------------------------------------------------
# Beiakte fuer Wiki-Artikel
# --------------------------------------------------------------------------


def _meta_pfad() -> Path:
    return store.data_dir() / "index" / "wiki_meta.json"


def wiki_meta() -> dict[str, dict]:
    return store.load_json(_meta_pfad(), {}) or {}


def wiki_meta_schreiben(record_id: str, aenderung: dict) -> dict:
    """Nutzungszaehler und Pruefungen eines Wiki-Artikels fortschreiben,
    ohne den Artikel selbst anzufassen."""
    alle_meta = wiki_meta()
    eintrag = alle_meta.setdefault(record_id, {"genutzt": 0, "zuletzt_genutzt": None, "pruefungen": []})
    eintrag.update(aenderung)
    store.save_json(_meta_pfad(), alle_meta)
    return eintrag


# --------------------------------------------------------------------------
# Speichern und Laden
# --------------------------------------------------------------------------


def speichern(rec: dict) -> dict:
    """Eigene Eintraege als JSON ablegen.

    Wiki-Artikel gehoeren max-effi nicht. Was an ihnen mitgeschrieben werden
    soll, geht in die Beiakte - sonst entstuende eine Kopie, die den Artikel
    einfriert.
    """
    store.ensure_dirs()
    rec.setdefault("id", neue_id(rec.get("title", "eintrag"), rec.get("kind", "playbook")))
    rec.setdefault("kind", "playbook")

    if _ist_wiki(rec["id"]) or rec.get("kind") == "wiki":
        wiki_meta_schreiben(
            rec["id"],
            {
                "genutzt": rec.get("genutzt", 0),
                "zuletzt_genutzt": rec.get("zuletzt_genutzt"),
                "pruefungen": rec.get("pruefungen", []),
            },
        )
        return rec

    rec.setdefault("erstellt", store.jetzt())
    rec["geaendert"] = store.jetzt()
    store.save_json(_ordner() / f"{rec['id']}.json", rec)
    return rec


def laden(record_id: str) -> dict | None:
    """Wiki-Artikel kommen immer frisch von der Platte, eigene Eintraege aus
    data/records/."""
    if _ist_wiki(record_id):
        for w in wiki_records():
            if w["id"] == record_id:
                try:
                    w["inhalt"] = Path(w["datei"]).read_text(encoding="utf-8", errors="ignore")
                except OSError as e:
                    w["inhalt"] = f"(Datei nicht lesbar: {e})"
                return w
        return None
    return store.load_json(_ordner() / f"{record_id}.json")


def benutzt_markieren(record_id: str) -> None:
    """Zaehlt mit, welche Eintraege tatsaechlich helfen - Grundlage, um
    spaeter Karteileichen zu erkennen."""
    if _ist_wiki(record_id):
        stand = wiki_meta().get(record_id, {})
        wiki_meta_schreiben(
            record_id,
            {"genutzt": stand.get("genutzt", 0) + 1, "zuletzt_genutzt": store.jetzt()},
        )
        return

    rec = laden(record_id)
    if rec:
        rec["genutzt"] = rec.get("genutzt", 0) + 1
        rec["zuletzt_genutzt"] = store.jetzt()
        store.save_json(_ordner() / f"{record_id}.json", rec)


# --------------------------------------------------------------------------
# Wissensbasis (Markdown, nur lesend)
# --------------------------------------------------------------------------


def _wiki_ordner() -> Path | None:
    from . import config

    basis = config.wissensbasis()
    return basis / "wiki" if basis else None


def _kopf_und_rumpf(text: str) -> tuple[list[str], list[str]]:
    """Frontmatter-Zeilen und Rumpf-Zeilen getrennt.

    Bewusst getrennt, weil 'title:' sonst auch aus einem Code-Block im Rumpf
    gelesen wuerde - guidelines.md zeigt genau so ein Beispiel-Frontmatter
    innerhalb von ```markdown.
    """
    zeilen = text.splitlines()
    if not zeilen or zeilen[0].strip() != "---":
        return [], zeilen
    for i, z in enumerate(zeilen[1:], start=1):
        if z.strip() == "---":
            return zeilen[1:i], zeilen[i + 1 :]
    return [], zeilen


def _ist_listenzeichen(z: str) -> bool:
    """'- Punkt' und '* Punkt' sind Listen. '**Zusammenfassung:**' ist es nicht.

    Der Unterschied ist das Leerzeichen dahinter. Ohne diese Unterscheidung
    faellt genau die Zeile weg, die man haben will: Die Wiki-Artikel hier
    beginnen ihren Fliesstext mit '**Zusammenfassung:** ...', und als Trigger
    im Suchergebnis steht dann ein beliebiger Aufzaehlungspunkt.
    """
    return len(z) > 1 and z[0] in "-*+" and z[1] == " "


def _erste_aussagekraeftige_zeile(zeilen: list[str]) -> str:
    """Die erste Zeile, die wirklich etwas aussagt - keine Ueberschrift, kein
    Code-Zaun, keine Aufzaehlung, keine Tabelle."""
    in_codeblock = False
    rueckfall = ""
    for zeile in zeilen:
        z = zeile.strip()
        if z.startswith("```"):
            in_codeblock = not in_codeblock
            continue
        if in_codeblock or not z:
            continue
        if z.startswith(("#", "---", ">", "|")) or _ist_listenzeichen(z):
            continue

        sauber = z.replace("**", "").replace("Zusammenfassung:", "").strip()
        if not sauber:
            continue
        # Nummerierte Punkte ("1. Lokal zuerst: ...") sind besser als nichts,
        # aber schlechter als ein echter Satz. Also merken und weitersuchen.
        if sauber[0].isdigit() and sauber[1:3] in (". ", ") "):
            rueckfall = rueckfall or sauber[:200]
            continue
        return sauber[:200]
    return rueckfall


def wiki_records() -> list[dict]:
    """Die Markdown-Artikel der Wissensbasis mitdurchsuchbar machen, ohne sie
    zu kopieren. Sie bleiben, wo sie sind - Git und Editor arbeiten weiter
    darauf, agent-memory liest nur mit."""
    ordner = _wiki_ordner()
    if ordner is None or not ordner.exists():
        return []

    meta = wiki_meta()
    out = []
    for p in sorted(ordner.glob("*.md")):
        if p.name.startswith("_"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        kopf, rumpf = _kopf_und_rumpf(text)
        titel = p.stem
        schlagworte = ["wiki"]
        status = "aktuell"
        for zeile in kopf:
            z = zeile.strip()
            if z.startswith("title:"):
                titel = z.split(":", 1)[1].strip().strip('"')
            elif z.startswith("tags:"):
                roh = z.split(":", 1)[1].strip().strip("[]")
                schlagworte += [t.strip() for t in roh.split(",") if t.strip()]
            elif z.startswith("status:"):
                status = z.split(":", 1)[1].strip()

        rid = f"wk_{store.slugify(p.stem, 45)}"
        eigen = meta.get(rid, {})
        out.append(
            {
                "id": rid,
                "kind": "wiki",
                "title": titel,
                "trigger": _erste_aussagekraeftige_zeile(rumpf),
                "tags": schlagworte,
                "status": status,
                "context": "\n".join(rumpf)[:400],
                "datei": str(p),
                "geaendert": store.zeit_von_datei(p),
                "genutzt": eigen.get("genutzt", 0),
                "zuletzt_genutzt": eigen.get("zuletzt_genutzt"),
                "pruefungen": eigen.get("pruefungen", []),
            }
        )
    return out


# --------------------------------------------------------------------------
# Auflisten
# --------------------------------------------------------------------------


def verirrte_wiki_kopien() -> list[Path]:
    """Wiki-Artikel, die faelschlich als JSON in data/records/ gelandet sind.

    Vor der Trennung von Besitz und Beiakte konnte open_record() so eine
    Kopie anlegen. Sie ist immer veraltet und immer ein Doppeltreffer.
    """
    ordner = _ordner()
    return sorted(ordner.glob("wk_*.json")) if ordner.exists() else []


def aufraeumen() -> dict:
    """Verirrte Kopien entfernen, ihre Nutzungszahlen aber retten."""
    entfernt = []
    for p in verirrte_wiki_kopien():
        alt = store.load_json(p, {}) or {}
        if alt.get("genutzt") or alt.get("pruefungen"):
            wiki_meta_schreiben(
                p.stem,
                {
                    "genutzt": alt.get("genutzt", 0),
                    "zuletzt_genutzt": alt.get("zuletzt_genutzt"),
                    "pruefungen": alt.get("pruefungen", []),
                },
            )
        p.unlink(missing_ok=True)
        entfernt.append(p.name)
    return {"wiki_kopien_entfernt": entfernt}


def alle(kind: str | None = None) -> list[dict]:
    """Alle Eintraege, jeder genau einmal.

    Die Reihenfolge ist Absicht: erst die eigenen JSON-Eintraege, dann die
    Wiki-Artikel. Trifft eine Kennung doppelt auf, gewinnt der Wiki-Artikel,
    denn er kommt frisch von der Platte.
    """
    nach_id: dict[str, dict] = {}
    ordner = _ordner()
    if ordner.exists():
        for p in ordner.glob("*.json"):
            if _ist_wiki(p.stem):
                continue  # Altlast; gehoert nicht hierher, siehe aufraeumen()
            rec = store.load_json(p)
            if rec and (kind is None or rec.get("kind") == kind):
                nach_id[rec.get("id", p.stem)] = rec
    if kind in (None, "wiki"):
        for w in wiki_records():
            nach_id[w["id"]] = w
    return list(nach_id.values())


# --------------------------------------------------------------------------
# Darstellung
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Verbindungen zwischen Eintraegen
# --------------------------------------------------------------------------


def verweise_normieren(related: list[Any] | None) -> list[dict]:
    """Freie Angaben zu Verweisen vereinheitlichen.

    Das ist der Teil, den der abgeloeste memory-knowledge-graph konnte und
    dieser Server bis dahin nicht: Eintraege aufeinander zeigen lassen. Der
    Graph lag allerdings im npx-Cache und war bei einem 'npm cache clean' weg.
    Hier steht die Verbindung IM Eintrag, in data/records/ - sie ueberlebt
    alles, was einen Cache leert.

    Erlaubt sind:
        "pb_20260809_lidar"              -> art 'verweist'
        "benutzt: pb_20260809_lidar"     -> art 'benutzt'
        {"art": "laeuft_auf", "ziel": "Pi4"}

    Ziele, die es (noch) nicht gibt, bleiben stehen. Sie sind kein Fehler,
    sondern eine Notiz, dass da noch etwas fehlt.
    """
    out: list[dict] = []
    for eintrag in related or []:
        if isinstance(eintrag, dict):
            ziel = str(eintrag.get("ziel") or eintrag.get("target") or "").strip()
            art = str(eintrag.get("art") or eintrag.get("typ") or "verweist").strip()
        else:
            text = str(eintrag).strip()
            if ":" in text and not text.split(":", 1)[0].strip().startswith(("pb_", "rv_", "wk_", "nt_")):
                art, ziel = (t.strip() for t in text.split(":", 1))
            else:
                art, ziel = "verweist", text
        if ziel:
            out.append({"art": art or "verweist", "ziel": ziel})
    return out


def rueckverweise(record_id: str, alle_recs: list[dict] | None = None) -> list[dict]:
    """Wer zeigt auf diesen Eintrag? Wird beim Oeffnen berechnet, nicht
    gespeichert - sonst muesste bei jeder Aenderung die Gegenseite
    mitgepflegt werden, und genau daran verrotten solche Verweise."""
    treffer = []
    for rec in alle_recs if alle_recs is not None else alle():
        if rec.get("id") == record_id:
            continue
        for v in rec.get("verweise", []) or []:
            if v.get("ziel") == record_id:
                treffer.append({"art": v.get("art", "verweist"), "von": rec["id"], "titel": rec.get("title")})
    return treffer


def verweise_aufloesen(rec: dict, alle_recs: list[dict] | None = None) -> list[dict]:
    """Verweise um den Titel des Ziels ergaenzen, damit man beim Lesen weiss,
    wohin sie fuehren - und was noch nicht existiert."""
    nach_id = {r["id"]: r for r in (alle_recs if alle_recs is not None else alle())}
    out = []
    for v in rec.get("verweise", []) or []:
        ziel = nach_id.get(v["ziel"])
        out.append(
            {
                "art": v.get("art", "verweist"),
                "ziel": v["ziel"],
                "titel": ziel.get("title") if ziel else None,
                "vorhanden": ziel is not None,
            }
        )
    return out


def suchtext(rec: dict) -> str:
    """Woraus Stichwort- und Bedeutungssuche ihren Text ziehen.

    Bewusst nicht der ganze Eintrag: Titel, Ausloeser und Schlagwoerter
    tragen die Bedeutung, der Rest verwaessert sie nur.
    """
    # Verweise tragen Bedeutung, aber nur ueber ihre Beschriftung und ueber
    # Ziele, die im Klartext benannt sind. Reine Kennungen wie
    # 'pb_20260809_233_lidar' waeren fuer die Bedeutungssuche nur Rauschen.
    beziehungen = []
    for v in rec.get("verweise", []) or []:
        beziehungen.append(v.get("art", ""))
        ziel = v.get("ziel", "")
        if not ziel.startswith(("pb_", "rv_", "wk_", "nt_", "job_", "vf_")):
            beziehungen.append(ziel)

    teile = [
        rec.get("title", ""),
        rec.get("trigger", ""),
        " ".join(rec.get("tags", []) or []),
        " ".join(t for t in beziehungen if t),
        (rec.get("context") or "")[:400],
    ]
    return "\n".join(t for t in teile if t)


def kurz(rec: dict, score: float | None = None) -> dict:
    """Was bei einer Suche zurueckkommt - rund 40 Woerter statt des ganzen
    Eintrags."""
    out = {
        "id": rec.get("id"),
        "kind": rec.get("kind"),
        "title": rec.get("title"),
        "trigger": rec.get("trigger") or "",
    }
    if rec.get("outcome"):
        out["outcome"] = rec["outcome"]
    if score is not None:
        out["score"] = round(score, 3)
    return out


def playbook_bauen(
    title: str,
    trigger: str,
    steps: list[Any] | None = None,
    tags: list[str] | None = None,
    outcome: str = "solved",
    verify_cmd: str | None = None,
    pitfalls: list[str] | None = None,
    context: str = "",
    kind: str = "playbook",
    quelle: dict | None = None,
    related: list[Any] | None = None,
) -> dict:
    def _schritt(s: Any) -> dict:
        if isinstance(s, dict):
            return {"do": s.get("do", ""), "cmd": s.get("cmd"), "why": s.get("why")}
        return {"do": str(s), "cmd": None, "why": None}

    return {
        "id": neue_id(title, kind),
        "kind": kind,
        "title": title,
        "trigger": trigger,
        "tags": [t.lower() for t in (tags or [])],
        "context": context,
        "steps": [_schritt(s) for s in (steps or [])],
        "verify": {"command": verify_cmd} if verify_cmd else None,
        "pitfalls": pitfalls or [],
        "outcome": outcome,
        "verweise": verweise_normieren(related),
        "quelle": quelle or {},
        "genutzt": 0,
        "zuletzt_genutzt": None,
        "pruefungen": [],
        "erstellt": store.jetzt(),
    }
