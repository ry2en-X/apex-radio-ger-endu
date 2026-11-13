"""Variété des phrases et ton de la radio.

- pick(clé, variantes) : choisit une formulation au hasard sans reprendre les dernières utilisées.
- dress(texte, ...)    : habille un message selon le ton choisi dans l'appli
                         (« sobre », « radio » d'équipe ou « detente »).
Les messages urgents et le spotter ne sont jamais habillés : ils doivent rester courts.
"""
from __future__ import annotations

import random
from collections import deque

from i18n import L

TONES = ("sobre", "radio", "detente")
TONE = "radio"


def set_tone(tone: str) -> None:
    global TONE
    TONE = tone if tone in TONES else "radio"


# ------------------------------------------------------------------ variantes
_HIST: dict[str, deque] = {}


def pick(key: str, opts: list[tuple[str, ...]]) -> str:
    """opts = [(fr, en), ...] ou [(fr, en, de), ...]. Évite de redire la ou les 2 dernières formulations de cette clé."""
    if not opts:
        return ""
    hist = _HIST.setdefault(key, deque(maxlen=max(1, min(2, len(opts) // 2))))
    free = [i for i in range(len(opts)) if i not in hist] or list(range(len(opts)))
    i = random.choice(free)
    if len(opts) > 1:
        hist.append(i)
    return L(*opts[i])


# ------------------------------------------------------------------ habillage
# Toujours terminés par un point : la phrase qui suit garde sa majuscule.
_OPEN = {
    "radio": {
        "info": [("Info radio.", "Radio check.", "Funkcheck."), ("Écoute bien.", "Listen up.", "Hör zu."), ("OK, info.", "OK, info.", "OK, Info."),
                 ("Du stand.", "From the pit wall.", "Von der Boxenmauer."), ("Petite info.", "Quick one.", "Kurze Info."), ("Pour info.", "Heads up.", "Zur Info.")],
        "answer": [("Reçu.", "Copy.", "Verstanden."), ("Bien reçu.", "Copy that.", "Copy."), ("OK, je regarde.", "OK, checking.", "OK, ich schaue."),
                   ("Copie.", "Copied.", "Kopiert."), ("Je vérifie.", "Stand by.", "Moment."), ("Reçu, cinq sur cinq.", "Loud and clear.", "Laut und deutlich.")],
    },
    "detente": {
        "info": [("Alors.", "Right then.", "Also."), ("Hé, petite info.", "Hey, quick one.", "Hey, kurze Info."), ("Dis donc.", "Hey.", "Hey."),
                 ("Bon.", "So.", "So."), ("Tiens, au fait.", "Oh, by the way.", "Ach, übrigens.")],
        "answer": [("Alors, voyons.", "Let's see.", "Mal sehen."), ("Ah, bonne question.", "Good question.", "Gute Frage."), ("OK.", "OK.", "OK."),
                   ("Attends, je regarde.", "Hang on, checking.", "Moment, ich schaue."), ("Pas de souci.", "No worries.", "Kein Problem.")],
    },
}
_CLOSE = {
    "radio": [("À toi.", "Over.", "Over."), ("On reste concentrés.", "Stay focused.", "Konzentriert bleiben."), ("Bien reçu ?", "Copy?", "Verstanden?"),
              ("On continue comme ça.", "Keep it going.", "Weiter so."), ("Je surveille.", "I'm watching it.", "Ich behalte das im Auge.")],
    "detente": [("Tranquille.", "Easy.", "Locker."), ("On gère.", "We've got this.", "Wir haben das im Griff."), ("Pas de stress.", "No stress.", "Kein Stress."),
                ("Tu assures.", "You're doing great.", "Du machst das super.")],
}
# chances d'ajouter une ouverture / une fin, selon le ton
_RATE = {"radio": (0.35, 0.2), "detente": (0.4, 0.25)}

# réponses qui accusent déjà réception : on n'en rajoute pas
_ALREADY = ("c'est noté", "ok", "reçu", "bien reçu", "compris", "je n'ai", "je t'entends", "radio", "mode silence",
            "spotter", "box", "noted", "copy", "got it", "understood", "i didn't", "i hear",
            "verstanden", "notiert", "alles klar", "ich habe nichts", "ich höre", "funk")


def dress(text: str, answer: bool = False, urgent: bool = False) -> str:
    """Ajoute parfois une ouverture (« Reçu. », « Info radio. ») et une fin (« À toi. ») selon le ton."""
    if TONE == "sobre" or urgent or not text:
        return text
    low = text.strip().lower()
    if len(low.split()) < 4 or low.startswith(_ALREADY) or "!" in text:
        return text
    p_open, p_close = _RATE[TONE]
    if answer:
        p_open, p_close = p_open + 0.15, p_close * 0.5
    out = text.strip()
    if random.random() < p_open:
        out = pick(f"open:{TONE}:{'answer' if answer else 'info'}", _OPEN[TONE]["answer" if answer else "info"]) + " " + out
    if random.random() < p_close and out.endswith("."):
        out = out + " " + pick(f"close:{TONE}", _CLOSE[TONE])
    return out
