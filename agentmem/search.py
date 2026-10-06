"""Suche: Stichwoerter plus Bedeutung.

Stichwortsuche (BM25) findet, was woertlich uebereinstimmt - schnell,
nachvollziehbar, ohne Modell. Sie versagt aber, sobald jemand dieselbe Sache
anders nennt ("serielle Verbindung bricht ab" vs. "UART Timeout").

Deshalb zusaetzlich eine Bedeutungssuche ueber ein kleines Embedding-Modell.
Faellt die aus - Modell nicht ladbar, LM Studio aus - laeuft die
Stichwortsuche allein weiter und die Antwort sagt das dazu. Nie ein harter
Fehler, denn Suchen darf nicht davon abhaengen, ob gerade ein Modell passt.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

from . import config, lmstudio, records, store

_WORT = re.compile(r"[a-zA-Z0-9_äöüÄÖÜß]{2,}")


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORT.findall(text or "")]


# --------------------------------------------------------------------------
# Stichwortsuche (BM25)
# --------------------------------------------------------------------------


def bm25(anfrage: str, dokumente: list[tuple[str, str]], k1: float = 1.5, b: float = 0.75) -> dict[str, float]:
    """dokumente: [(id, text)] -> {id: score}."""
    if not dokumente:
        return {}
    tok_docs = {doc_id: _tokens(text) for doc_id, text in dokumente}
    laengen = {i: len(t) for i, t in tok_docs.items()}
    avg = (sum(laengen.values()) / len(laengen)) or 1.0
    N = len(tok_docs)

    df = Counter()
    for t in tok_docs.values():
        df.update(set(t))

    frage = _tokens(anfrage)
    scores: dict[str, float] = {}
    for doc_id, toks in tok_docs.items():
        tf = Counter(toks)
        s = 0.0
        for w in frage:
            if w not in tf:
                continue
            idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
            zaehler = tf[w] * (k1 + 1)
            nenner = tf[w] + k1 * (1 - b + b * laengen[doc_id] / avg)
            s += idf * zaehler / nenner
        if s > 0:
            scores[doc_id] = s
    return scores


# --------------------------------------------------------------------------
# Bedeutungssuche
# --------------------------------------------------------------------------


def _index_pfad() -> Path:
    return store.data_dir() / "index" / "embeddings.jsonl"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def index_laden() -> dict[str, dict]:
    pfad = _index_pfad()
    if not pfad.exists():
        return {}
    out = {}
    for zeile in pfad.read_text(encoding="utf-8").splitlines():
        if not zeile.strip():
            continue
        try:
            eintrag = json.loads(zeile)
            out[eintrag["id"]] = eintrag
        except (json.JSONDecodeError, KeyError):
            continue  # kaputte Zeile ueberspringen statt Index verwerfen
    return out


def index_schreiben(index: dict[str, dict]) -> None:
    pfad = _index_pfad()
    pfad.parent.mkdir(parents=True, exist_ok=True)
    zeilen = "\n".join(json.dumps(e, ensure_ascii=False) for e in index.values())
    tmp = pfad.with_name(pfad.name + ".tmp")
    tmp.write_text(zeilen + ("\n" if zeilen else ""), encoding="utf-8")
    tmp.replace(pfad)


def index_auffrischen(
    recs: list[dict], index: dict[str, dict] | None = None, aufraeumen: bool = False
) -> tuple[dict, str | None]:
    """Fehlende oder veraltete Eintraege nachtragen. Gibt (index, fehler) zurueck.

    aufraeumen=True wirft ausserdem Vektoren zu Eintraegen weg, die es nicht
    mehr gibt. Nur beim vollstaendigen Durchlauf sinnvoll - beim Nachtragen
    eines einzelnen neuen Eintrags waere 'recs' ja nicht die ganze Welt und
    der Index waere anschliessend leer.
    """
    index = index if index is not None else index_laden()

    offen = []
    for rec in recs:
        text = records.suchtext(rec)
        h = _hash(text)
        vorhanden = index.get(rec["id"])
        if not vorhanden or vorhanden.get("text_hash") != h:
            offen.append((rec["id"], rec.get("kind", "playbook"), text, h))

    verwaist: list[str] = []
    if aufraeumen:
        lebende = {r["id"] for r in recs}
        verwaist = [k for k in index if k not in lebende]
        for k in verwaist:
            del index[k]

    if not offen:
        if verwaist:
            index_schreiben(index)
        return index, None

    try:
        vektoren = lmstudio.embed([t for _, _, t, _ in offen])
    except lmstudio.LMStudioFehler as e:
        return index, str(e)

    for (rid, kind, _, h), vec in zip(offen, vektoren):
        index[rid] = {"id": rid, "kind": kind, "text_hash": h, "vector": vec, "updated_at": store.jetzt()}
    index_schreiben(index)
    return index, None


def abdeckung() -> dict:
    """Wieviel des Gedaechtnisses ist wirklich ueber die Bedeutung auffindbar?

    Die Zahl gehoert nach aussen, weil die Bedeutungssuche still ausfallen
    kann: Ist das Embedding-Modell nicht da, laeuft alles weiter - nur eben
    mit Stichwortsuche allein. Ohne diese Anzeige merkt man das erst, wenn
    ein Eintrag nicht gefunden wird, den es laengst gibt.

    Verlorengehen kann dabei nichts: data/index/embeddings.jsonl ist ein
    Zwischenspeicher, kein Bestand. Wird er geloescht, legt ihn der naechste
    recall() aus den Eintraegen neu an.
    """
    recs = records.alle()
    index = index_laden()
    aktuell = sum(
        1 for r in recs
        if (e := index.get(r["id"])) and e.get("text_hash") == _hash(records.suchtext(r))
    )
    return {
        "eintraege": len(recs),
        "eingebettet": aktuell,
        "fehlend": len(recs) - aktuell,
        "modell": config.load()["embed_modell"],
        "ablage": str(_index_pfad()),
        "hinweis": None if aktuell == len(recs) else
                   "Wird beim naechsten recall() nachgetragen, sofern LM Studio erreichbar ist.",
    }


def _kosinus(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    punkt = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return punkt / (na * nb) if na and nb else 0.0


# --------------------------------------------------------------------------
# Zusammenfuehrung
# --------------------------------------------------------------------------


def _normieren(scores: dict[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    hoch = max(scores.values())
    return {k: v / hoch for k, v in scores.items()} if hoch > 0 else {}


def suchen(anfrage: str, kind: str | None = None, limit: int | None = None) -> dict:
    cfg = config.load()["recall"]
    limit = limit or cfg["top_k"]

    recs = records.alle(kind)
    if not recs:
        return {"treffer": [], "durchsucht": 0, "rerank": "keine Eintraege"}

    stichwort = _normieren(bm25(anfrage, [(r["id"], records.suchtext(r)) for r in recs]))

    bedeutung: dict[str, float] = {}
    rerank_status = "aktiv"
    # Nur beim ungefilterten Durchlauf aufraeumen. Mit kind-Filter ist 'recs'
    # eine Teilmenge - dann waeren alle anderen Arten "verwaist" und flogen
    # aus dem Index.
    index, fehler = index_auffrischen(recs, aufraeumen=kind is None)
    if fehler:
        rerank_status = f"nicht verfuegbar ({fehler[:120]})"
    else:
        try:
            frage_vec = lmstudio.embed([anfrage])[0]
            for rec in recs:
                eintrag = index.get(rec["id"])
                if eintrag:
                    bedeutung[rec["id"]] = _kosinus(frage_vec, eintrag["vector"])
        except lmstudio.LMStudioFehler as e:
            rerank_status = f"nicht verfuegbar ({str(e)[:120]})"

    if bedeutung:
        gesamt = {
            r["id"]: cfg["stichwort_gewicht"] * stichwort.get(r["id"], 0.0)
            + cfg["bedeutung_gewicht"] * max(0.0, bedeutung.get(r["id"], 0.0))
            for r in recs
        }
    else:
        gesamt = stichwort

    nach_id = {r["id"]: r for r in recs}
    beste = sorted(gesamt.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    treffer = [records.kurz(nach_id[i], s) for i, s in beste if s > 0.01]

    return {"treffer": treffer, "durchsucht": len(recs), "rerank": rerank_status}
