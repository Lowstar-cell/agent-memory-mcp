"""Zugriff auf LM Studio.

Zwei Schnittstellen werden benutzt, jede fuer das, was sie am besten kann:

  /api/v1/*            Modellverwaltung: auflisten, laden, entladen.
                       Nur hier gibt es Instanz-Kennungen und Ladezeiten.
  /v1/chat/completions Antworten mit Werkzeugen (OpenAI-kompatibel).
                       Der native /api/v1/chat koennte zwar auch Werkzeuge,
                       verlangt dafuer aber aktivierte Authentifizierung in
                       LM Studio ("Permission denied to use plugin ...").
  /v1/embeddings       Text in Zahlen umwandeln fuer die Bedeutungssuche.
"""

from __future__ import annotations

import logging

import httpx

from . import config

# httpx protokolliert jede Anfrage auf INFO. Als stdio-Server landet das im
# Fehlerkanal von LM Studio und macht die Logs unlesbar.
logging.getLogger("httpx").setLevel(logging.WARNING)


class LMStudioFehler(RuntimeError):
    """LM Studio ist nicht erreichbar oder hat die Anfrage abgelehnt."""


def _base() -> str:
    return config.load()["lmstudio_base_url"]


class LMStudioZuLangsam(LMStudioFehler):
    """Das Modell hat geantwortet - nur nicht rechtzeitig.

    Eigene Klasse, weil das etwas voellig anderes ist als "nicht erreichbar"
    und einen anderen Rat verdient: Nicht LM Studio ist kaputt, sondern das
    geladene Modell ist fuer diese Aufgabe zu gross oder teilt sich den
    Speicher mit einem zweiten.
    """


def _post(pfad: str, payload: dict, timeout: float = 120.0) -> dict:
    url = f"{_base()}{pfad}"
    try:
        r = httpx.post(url, json=payload, timeout=timeout)
    except httpx.TimeoutException as e:
        raise LMStudioZuLangsam(
            f"Keine Antwort binnen {timeout:.0f}s von '{payload.get('model', '?')}'. "
            f"Meist liegt das daran, dass mehrere Modelle gleichzeitig geladen sind "
            f"und der Speicher nicht reicht - dann rechnet ein Teil auf der CPU. "
            f"Pruefe status(), entlade Ueberfluessiges, oder nimm ein kleineres "
            f"Modell fuer Subagenten (config.json: subagent_modell)."
        ) from e
    except httpx.RequestError as e:
        raise LMStudioFehler(f"LM Studio nicht erreichbar ({url}): {e}") from e
    if r.status_code >= 400:
        # LM Studio gibt HTTP 400 zurueck, wenn seine EIGENE Engine die
        # Vorhersage abbricht ("Engine protocol predict request failed:
        # fetch failed"). Das kam hier nach gut 300s bei einem 35B-Modell -
        # nachgestellt mit einem rohen HTTP-Aufruf ohne diesen Server, also
        # nichts, was er verursacht oder verhindern koennte. Es als
        # gewoehnlichen Protokollfehler zu melden waere irrefuehrend: Es ist
        # dieselbe Sache wie eine Zeitueberschreitung und braucht denselben Rat.
        text = r.text[:400]
        if any(w in text.lower() for w in ("engine protocol", "predict request", "fetch failed")):
            raise LMStudioZuLangsam(
                f"LM Studio hat die Vorhersage von '{payload.get('model', '?')}' selbst "
                f"abgebrochen: {text} - das passiert bei sehr langen Antworten. Nimm fuer "
                f"Subagenten ein kleines, werkzeugfaehiges Modell (config.json: "
                f"subagent_modell, z.B. 'qwen/qwen3-4b-2507'), statt den Auftrag auf einem "
                f"grossen Chatmodell laufen zu lassen."
            )
        raise LMStudioFehler(f"{pfad} -> HTTP {r.status_code}: {text}")
    return r.json()


def _get(pfad: str, timeout: float = 30.0) -> dict:
    url = f"{_base()}{pfad}"
    try:
        r = httpx.get(url, timeout=timeout)
    except httpx.RequestError as e:
        raise LMStudioFehler(f"LM Studio nicht erreichbar ({url}): {e}") from e
    if r.status_code >= 400:
        raise LMStudioFehler(f"{pfad} -> HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


# --------------------------------------------------------------------------
# Modelle
# --------------------------------------------------------------------------


def list_models() -> list[dict]:
    return _get("/api/v1/models").get("models", [])


def geladene_instanzen() -> list[dict]:
    """Alle gerade geladenen Modelle als [{key, instance_id, type}]."""
    out = []
    for m in list_models():
        for inst in m.get("loaded_instances") or []:
            out.append(
                {
                    "key": m.get("key"),
                    "instance_id": inst.get("id"),
                    "type": m.get("type"),
                    "params": m.get("params_string"),
                }
            )
    return out


def laufendes_sprachmodell() -> str | None:
    """Kennung des ersten geladenen Sprachmodells, sonst None."""
    for inst in geladene_instanzen():
        if inst["type"] == "llm":
            return inst["instance_id"]
    return None


def kleinstes_werkzeugmodell() -> str | None:
    """Kleinstes Sprachmodell, das Werkzeuge bedienen kann.

    Ohne Werkzeug-Faehigkeit ist ein Subagent nutzlos - er koennte weder
    suchen noch Dateien lesen.
    """
    kandidaten = [
        (m.get("size_bytes") or 0, m.get("key"))
        for m in list_models()
        if m.get("type") == "llm" and (m.get("capabilities") or {}).get("trained_for_tool_use")
    ]
    kandidaten = [(g, k) for g, k in kandidaten if k and g > 0]
    return min(kandidaten)[1] if kandidaten else None


def load_model(model: str, context_length: int | None = None) -> dict:
    payload: dict = {"model": model}
    if context_length:
        payload["context_length"] = context_length
    # Laden kann bei grossen Modellen deutlich laenger dauern als eine Anfrage.
    return _post("/api/v1/models/load", payload, timeout=600.0)


def unload_model(instance_id: str) -> dict:
    return _post("/api/v1/models/unload", {"instance_id": instance_id}, timeout=120.0)


def modell_aufloesen(wunsch: str | None = None) -> tuple[str, str]:
    """Bestimmt, welches Modell benutzt wird.

    Gibt (modellname, begruendung) zurueck. Die Begruendung landet im
    Auftragsprotokoll, damit spaeter nachvollziehbar ist, worauf ein
    Subagent tatsaechlich gelaufen ist.

    'auto' (Standard) nimmt das Modell, das ohnehin schon laeuft - damit
    wird nie etwas nachgeladen und der laufende Chat nicht gestoert.
    """
    wunsch = wunsch or config.load()["subagent_modell"]

    if wunsch == "auto":
        laufend = laufendes_sprachmodell()
        if laufend:
            return laufend, "laufendes Modell (auto)"

        # Es laeuft nichts - das ist der Normalfall, nicht der Ausnahmefall:
        # LM Studio entlaedt Modelle nach Leerlauf (Standard 3600s). Statt
        # den Auftrag scheitern zu lassen, das kleinste werkzeugfaehige
        # Modell nehmen. Klein, weil hier gerade offensichtlich niemand
        # aktiv chattet und Ladezeit direkt zaehlt.
        kandidat = kleinstes_werkzeugmodell()
        if kandidat:
            antwort = load_model(kandidat)
            return antwort.get("instance_id", kandidat), (
                f"nichts geladen, kleinstes werkzeugfaehiges Modell gestartet "
                f"({kandidat}, {antwort.get('load_time_seconds', '?')}s)"
            )
        raise LMStudioFehler(
            "Kein Sprachmodell geladen und keines mit Werkzeug-Faehigkeit gefunden. "
            "Lade in LM Studio ein Modell oder trage in config.json einen Namen ein."
        )

    # Konkreter Wunsch: laeuft er schon?
    for inst in geladene_instanzen():
        if inst["key"] == wunsch or inst["instance_id"] == wunsch:
            return inst["instance_id"], "gewuenschtes Modell, lief bereits"

    try:
        antwort = load_model(wunsch)
        return antwort.get("instance_id", wunsch), (
            f"gewuenschtes Modell nachgeladen ({antwort.get('load_time_seconds', '?')}s)"
        )
    except LMStudioFehler as e:
        laufend = laufendes_sprachmodell()
        if laufend:
            return laufend, f"Rueckfall auf laufendes Modell ({wunsch} nicht ladbar: {e})"
        raise


# --------------------------------------------------------------------------
# Antworten
# --------------------------------------------------------------------------


def chat(
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.3,
    timeout: float = 300.0,
) -> dict:
    """Eine Runde ueber die OpenAI-kompatible Schnittstelle.

    Gibt die rohe 'message' des Modells zurueck - inklusive tool_calls,
    falls es Werkzeuge benutzen will.
    """
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if max_tokens:
        payload["max_tokens"] = max_tokens

    antwort = _post("/v1/chat/completions", payload, timeout=timeout)
    auswahl = (antwort.get("choices") or [{}])[0]
    return {
        "message": auswahl.get("message") or {},
        "finish_reason": auswahl.get("finish_reason"),
        "usage": antwort.get("usage") or {},
    }


def embed(texte: list[str], model: str | None = None) -> list[list[float]]:
    """Text in Zahlenvektoren umwandeln. Wirft LMStudioFehler, wenn das
    Embedding-Modell nicht ladbar ist - der Aufrufer faellt dann auf reine
    Stichwortsuche zurueck."""
    model = model or config.load()["embed_modell"]
    antwort = _post("/v1/embeddings", {"model": model, "input": texte}, timeout=300.0)
    daten = sorted(antwort.get("data", []), key=lambda d: d.get("index", 0))
    return [d["embedding"] for d in daten]


def erreichbar() -> bool:
    try:
        _get("/api/v1/models", timeout=5.0)
        return True
    except LMStudioFehler:
        return False
