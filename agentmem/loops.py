"""Schleifen erkennen und durchbrechen.

Kleine Modelle haengen sich fest. Nicht weil sie dumm sind, sondern weil
ihnen an einer Stelle nichts Besseres einfaellt: Der Aufruf hat nichts
gebracht, also machen sie ihn nochmal. Und nochmal. Bis die Rundengrenze
oder der Kontext zu Ende ist. Von aussen sieht das aus wie Arbeit, es ist
aber Stillstand.

Drei Formen kommen wirklich vor:

  Wiederholung   recall("lidar") ... recall("lidar") ... recall("lidar")
                 Gleiches Werkzeug, gleiche Argumente, gleiches Ergebnis.

  Kreis          skill_find() -> open_record() -> skill_find() -> open_record()
                 Zwei oder drei Aufrufe, die einander immer wieder ergeben.

  Fruchtlos      recall("lidar") -> recall("lidar fehler") -> recall("uart lidar")
                 Andere Argumente, aber jedes Mal null Treffer. Das Modell
                 variiert Wortlaute, statt die Sackgasse zu verlassen.

Wichtig ist, WIE der Abbruch aussieht. Ein blosses "Schleife erkannt, Stopp"
bringt nichts - das Modell weiss danach immer noch nicht weiter und
probiert es beim naechsten Zug erneut. Zurueck geht deshalb immer ein
konkreter naechster Schritt.

Und ebenso wichtig: Manches Wiederholen ist richtig. agent_result() fuer
denselben Auftrag mehrfach zu fragen IST der vorgesehene Ablauf. Solche
Werkzeuge stehen in GEDULDIG und werden nie abgebrochen, sondern hoechstens
freundlich auf wait_ms hingewiesen.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import deque
from typing import Any

# Werkzeuge, bei denen Wiederholung zum Ablauf gehoert. Sie werden nie
# gestoppt - sonst zerschlaegt der Waechter genau das Warten auf einen
# Subagenten, zu dem der Server selbst auffordert.
GEDULDIG = frozenset({"agent_result", "status", "get_next_task"})

# Ab wann etwas auffaellt. Zweimal dasselbe kann Zufall sein, dreimal ist
# ein Muster.
WARNEN_AB = 2
STOPPEN_AB = 3
FENSTER = 16
FRUCHTLOS_AB = 3


def signatur(werkzeug: str, args: dict | None) -> str:
    """Ein Aufruf, auf einen kurzen Schluessel eingedampft.

    sort_keys, damit {"a":1,"b":2} und {"b":2,"a":1} als derselbe Aufruf
    gelten - fuer das Modell sind sie es ja auch.
    """
    try:
        roh = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        roh = repr(args)
    return f"{werkzeug}:{hashlib.sha1(roh.encode('utf-8')).hexdigest()[:12]}"


class Befund:
    """Was der Waechter gefunden hat, samt Ausweg."""

    def __init__(self, art: str, stufe: str, werkzeug: str, anzahl: int, rat: str) -> None:
        self.art = art          # wiederholung | kreis | fruchtlos | stillstand
        self.stufe = stufe      # hinweis | stopp
        self.werkzeug = werkzeug
        self.anzahl = anzahl
        self.rat = rat

    def als_dict(self) -> dict:
        return {
            "schleife_erkannt": self.art,
            "stufe": self.stufe,
            "werkzeug": self.werkzeug,
            "wiederholungen": self.anzahl,
            "was_jetzt": self.rat,
        }

    def __repr__(self) -> str:
        return f"<Befund {self.art}/{self.stufe} {self.werkzeug} x{self.anzahl}>"


class Waechter:
    """Merkt sich die letzten Aufrufe eines Gespraechs und schlaegt an.

    Eine Instanz je Gespraech: eine fuer das Hauptmodell, je eine fuer jeden
    Subagenten. Sonst wuerden sich zwei parallele Auftraege gegenseitig als
    Schleife melden.
    """

    def __init__(self, name: str = "haupt", fenster: int = FENSTER) -> None:
        self.name = name
        self._verlauf: deque[str] = deque(maxlen=fenster)
        self._leerlauf: dict[str, int] = {}
        self._lock = threading.Lock()
        self.befunde: list[dict] = []

    # -- Erkennung ---------------------------------------------------------

    def _kreis(self) -> tuple[int, int] | None:
        """Sucht ein sich wiederholendes Muster am Ende des Verlaufs.

        Gibt (Periode, Wiederholungen) zurueck. Periode 1 ist die einfache
        Wiederholung und wird woanders behandelt.
        """
        v = list(self._verlauf)
        for periode in (2, 3, 4):
            if len(v) < periode * 2:
                continue
            letzte = v[-periode:]
            wdh = 1
            pos = len(v) - periode
            while pos - periode >= 0 and v[pos - periode : pos] == letzte:
                wdh += 1
                pos -= periode
            if wdh >= 2:
                return periode, wdh
        return None

    def pruefe(self, werkzeug: str, args: dict | None = None) -> Befund | None:
        """VOR der Ausfuehrung: ist dieser Aufruf Teil einer Schleife?

        Bewusst vorher, damit ein erkannter Doppelaufruf gar nicht erst
        ausgefuehrt wird - bei einer Websuche oder einem Shell-Befehl ist
        die Wiederholung sonst schon bezahlt, bevor jemand sie bemerkt.
        """
        with self._lock:
            sig = signatur(werkzeug, args)
            geduldig = werkzeug in GEDULDIG

            self._verlauf.append(sig)

            # Nur ununterbrochene Wiederholungen zaehlen hart. Der Unterschied
            # ist nicht akademisch, sondern entscheidet ueber Nutzen und
            # Schaden dieser ganzen Datei:
            #
            #   verify(t) verify(t) verify(t)            -> Schleife, abbrechen
            #   write(a) verify(t) write(b) verify(t)     -> normale Arbeit!
            #
            # Im zweiten Fall taucht verify(t) genauso oft im Fenster auf.
            # Wer nur zaehlt, wuergt genau den Ablauf ab, fuer den man das
            # Werkzeug gebaut hat: aendern, pruefen, aendern, pruefen.
            v = list(self._verlauf)
            lauf = 0
            for s in reversed(v):
                if s != sig:
                    break
                lauf += 1
            gesamt = v.count(sig)

            befund: Befund | None = None

            if lauf >= WARNEN_AB:
                stufe = "stopp" if (lauf >= STOPPEN_AB and not geduldig) else "hinweis"
                befund = Befund(
                    "wiederholung", stufe, werkzeug, lauf,
                    self._rat_wiederholung(werkzeug, lauf, geduldig),
                )

            elif (kreis := self._kreis()) is not None and not geduldig:
                # Ein Kreis verlangt gleiche Argumente in jedem Durchgang.
                # Aendert sich etwas - anderer Dateiinhalt, andere Suchworte -
                # ist die Signatur eine andere und hier faellt nichts an.
                periode, wdh = kreis
                befund = Befund(
                    "kreis", "stopp", werkzeug, wdh,
                    f"Du drehst dich im Kreis: dieselben {periode} Aufrufe mit denselben "
                    f"Argumenten wiederholen sich {wdh}-mal. Das liefert nichts Neues. "
                    f"Entscheide jetzt mit dem, was du hast, oder frag den Menschen.",
                )

            elif gesamt >= 4:
                # Verstreut, aber auffaellig oft. Ein Wort dazu, mehr nicht -
                # es koennte immer noch echte Arbeit sein.
                befund = Befund(
                    "wiederholung", "hinweis", werkzeug, gesamt,
                    f"{werkzeug} kam in den letzten Zuegen {gesamt}-mal mit genau "
                    f"denselben Argumenten vor. Falls sich zwischendurch nichts "
                    f"geaendert hat, bringt der naechste Aufruf auch nichts.",
                )

            return self._merken(befund)

    def ergebnis_melden(self, werkzeug: str, leer: bool) -> Befund | None:
        """NACH der Ausfuehrung: kam etwas dabei heraus?

        Nur so laesst sich die fruchtlose Suche erkennen - die, bei der das
        Modell jedes Mal andere Woerter probiert und jedes Mal nichts findet.
        Vorher ist sie nicht sichtbar, denn die Argumente sind ja neu.
        """
        with self._lock:
            if not leer:
                self._leerlauf.pop(werkzeug, None)
                return None

            anzahl = self._leerlauf.get(werkzeug, 0) + 1
            self._leerlauf[werkzeug] = anzahl
            if anzahl < FRUCHTLOS_AB or werkzeug in GEDULDIG:
                return None
            return self._merken(
                Befund("fruchtlos", "stopp", werkzeug, anzahl, self._rat_fruchtlos(werkzeug, anzahl))
            )

    def _merken(self, befund: Befund | None) -> Befund | None:
        if befund is not None:
            self.befunde.append({"gespraech": self.name, **befund.als_dict()})
            del self.befunde[:-20]
        return befund

    def stillstand(self, runde: int) -> Befund:
        """Das Modell hat weder geantwortet noch ein Werkzeug benutzt."""
        b = Befund(
            "stillstand", "stopp", "-", runde,
            "Das Modell hat in dieser Runde nichts geliefert - weder Text noch "
            "einen Werkzeugaufruf. Weitere Runden aendern daran nichts.",
        )
        with self._lock:
            return self._merken(b)

    def zuruecksetzen(self) -> None:
        with self._lock:
            self._verlauf.clear()
            self._leerlauf.clear()

    # -- Rat ---------------------------------------------------------------

    @staticmethod
    def _rat_wiederholung(werkzeug: str, anzahl: int, geduldig: bool) -> str:
        if geduldig:
            return (
                f"{werkzeug} {anzahl}-mal hintereinander gefragt. Das ist erlaubt, "
                f"kostet aber jedes Mal Kontext. Nimm wait_ms=30000, dann wartet der "
                f"Aufruf selbst, statt dass du pollst."
            )
        besonders = {
            "recall": (
                "Die Suche hat dir schon dieselbe Antwort gegeben. Oeffne einen der "
                "Treffer mit open_record(), oder such mit ANDEREN Woertern - nicht mit "
                "denselben. Findest du nichts, gibt es dazu nichts Gemerktes: arbeite "
                "ohne, und leg das Ergebnis danach mit remember() ab."
            ),
            "open_record": (
                "Diesen Eintrag hast du bereits offen. Sein Inhalt aendert sich nicht "
                "vom Nochmal-Oeffnen. Arbeite mit dem, was drinsteht."
            ),
            "skill_find": (
                "Dieselbe Skill-Suche wie eben. Nimm einen der gefundenen Skills mit "
                "skill_run(), oder arbeite ohne Skill weiter."
            ),
            "verify": (
                "Derselbe Befehl, dasselbe Ergebnis. Ein Test wird beim Wiederholen "
                "nicht gruen. Aendere erst den Code oder den Befehl."
            ),
            "tool_call": (
                "Derselbe Werkzeugaufruf mit denselben Argumenten. Das Ergebnis ist "
                "unveraendert. Aendere die Argumente oder nimm ein anderes Werkzeug."
            ),
        }
        return besonders.get(werkzeug) or (
            f"{werkzeug} wurde {anzahl}-mal mit genau denselben Argumenten aufgerufen. "
            f"Das Ergebnis ist jedes Mal dasselbe. Aendere die Argumente, nimm ein "
            f"anderes Werkzeug, oder arbeite mit dem weiter, was du schon hast."
        )

    @staticmethod
    def _rat_fruchtlos(werkzeug: str, anzahl: int) -> str:
        if werkzeug in ("recall", "skill_find", "tool_find"):
            return (
                f"{anzahl} Suchen hintereinander, jede ohne Treffer. Andere Woerter "
                f"aendern daran nichts mehr - es ist schlicht nichts da. Arbeite ohne "
                f"und merk dir das Ergebnis anschliessend mit remember(), dann findet "
                f"es beim naechsten Mal jemand."
            )
        return (
            f"{werkzeug} hat {anzahl}-mal nichts geliefert. Diese Richtung fuehrt "
            f"nicht weiter - waehle einen anderen Weg."
        )


# --------------------------------------------------------------------------
# Waechter je Gespraech
# --------------------------------------------------------------------------

_haupt = Waechter("haupt")
_je_auftrag: dict[str, Waechter] = {}
_reg_lock = threading.Lock()


def haupt() -> Waechter:
    """Der Waechter fuer das Modell, das in LM Studio im Chat sitzt."""
    return _haupt


def fuer_auftrag(job_id: str) -> Waechter:
    with _reg_lock:
        if job_id not in _je_auftrag:
            # Alte Waechter nicht ewig mitschleppen.
            if len(_je_auftrag) > 32:
                for k in list(_je_auftrag)[:16]:
                    _je_auftrag.pop(k, None)
            _je_auftrag[job_id] = Waechter(job_id)
        return _je_auftrag[job_id]


def letzte_befunde(limit: int = 5) -> list[dict]:
    """Was zuletzt angeschlagen hat - fuer status()."""
    alles = list(_haupt.befunde)
    with _reg_lock:
        for w in _je_auftrag.values():
            alles.extend(w.befunde)
    return alles[-limit:]
