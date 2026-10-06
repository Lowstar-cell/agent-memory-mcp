"""Einstellungen laden.

Reihenfolge: config.json neben dem Projekt, dann Umgebungsvariablen als Override.
Schluessel, die mit '//' beginnen, sind Kommentare in der JSON und werden verworfen.
"""

import json
import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent.parent.resolve()
DATA_DIR = BASE_DIR / "data"
SKILLS_DIR = BASE_DIR / "skills"
ROLES_DIR = BASE_DIR / "roles"

_DEFAULTS: dict[str, Any] = {
    "lmstudio_base_url": "http://localhost:1234",
    "subagent_modell": "auto",
    "embed_modell": "text-embedding-embeddinggemma-300m-qat",
    "subagent": {
        "max_runden": 8,
        "max_gleichzeitig": 2,
        # 300s waren zu knapp: Auf diesem Rechner brauchte ein 35B-MoE fuer
        # EINE Runde 266s, sobald sich zwei Modelle den VRAM teilten. Ein
        # Auftrag braucht mindestens zwei Runden (Werkzeug, dann Antwort).
        "timeout_sekunden": 900,
        "max_antwort_tokens": 1200,
        "kontext_laenge": 16384,
    },
    "erlaubte_mcp_server": ["web-search", "filesystem", "filesystem-full", "terminal"],
    "gateway_server": "alle",
    "recall": {"stichwort_gewicht": 0.5, "bedeutung_gewicht": 0.5, "top_k": 5},
    "verify": {"standard_timeout": 120, "ausgabe_tail_zeichen": 2000},
    "pfade": {
        # Standardort von LM Studio auf allen drei Systemen. Wer sein Profil
        # woanders hat, traegt den Pfad in config.json ein.
        "lmstudio_mcp_json": str(Path.home() / ".lmstudio" / "mcp.json"),
        # Leer = keine. Ein Ordner mit wiki/ und skills/ darin wird
        # mitdurchsucht, aber nie beschrieben.
        "wissensbasis": "",
    },
}

# Namen, unter denen dieser Server selbst laeuft oder frueher lief. Weder ein
# Subagent noch das Gateway darf sie als Werkzeugquelle bekommen - sonst ruft
# der Server sich selbst auf und startet Subagenten, die Subagenten starten.
# Die alten Namen bleiben drin, weil sie in mcp.json noch stehen koennen.
GESPERRTE_MCP_NAMEN = (
    "agent-memory", "agent_memory", "agentmem",
    "max-effi", "max_effi", "maxeffi",
)


def _strip_comments(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not k.startswith("//")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_cache: dict[str, Any] | None = None


def load(reload: bool = False) -> dict[str, Any]:
    """Gibt die zusammengefuehrten Einstellungen zurueck (gecacht)."""
    global _cache
    if _cache is not None and not reload:
        return _cache

    cfg = json.loads(json.dumps(_DEFAULTS))  # tiefe Kopie

    path = Path(os.environ.get("AGENTMEM_CONFIG", BASE_DIR / "config.json"))
    if path.exists():
        try:
            user_cfg = _strip_comments(json.loads(path.read_text(encoding="utf-8")))
            cfg = _deep_merge(cfg, user_cfg)
        except json.JSONDecodeError as e:
            # Kaputte Konfiguration darf den Server nicht am Start hindern.
            cfg["_config_fehler"] = f"{path}: {e}"

    if base_url := os.environ.get("LMSTUDIO_BASE_URL"):
        cfg["lmstudio_base_url"] = base_url
    if modell := os.environ.get("AGENTMEM_SUBAGENT_MODELL"):
        cfg["subagent_modell"] = modell

    cfg["lmstudio_base_url"] = str(cfg["lmstudio_base_url"]).rstrip("/")

    _cache = cfg
    return cfg


def data_dir() -> Path:
    """Datenverzeichnis, per AGENTMEM_DATA_DIR umlegbar (fuer Tests)."""
    return Path(os.environ.get("AGENTMEM_DATA_DIR", DATA_DIR))


def wissensbasis() -> Path | None:
    """Der Wissensbasis-Ordner, oder None wenn keiner eingestellt ist.

    Ohne diese Pruefung wuerde ein leerer Eintrag zu Path('')/'wiki' - also
    einem RELATIVEN Pfad, der je nach Arbeitsverzeichnis zufaellig existiert.
    Wer keine Wissensbasis hat, soll auch keine bekommen.
    """
    roh = str(load()["pfade"].get("wissensbasis", "")).strip()
    return Path(roh) if roh else None
