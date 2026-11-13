"""
Langue de l'appli (français / anglais / allemand).

L("texte français", "English text", "Deutscher Text") renvoie la bonne version selon la langue choisie.
Sans texte allemand, l'allemand retombe sur l'anglais.
dec("3.6") : virgule décimale en français et en allemand, point en anglais.
"""

from __future__ import annotations

LANG = "fr"
LANGS = ("fr", "en", "de")


def set_lang(lang: str) -> None:
    global LANG
    l = str(lang).lower()[:2]
    LANG = l if l in LANGS else "fr"


def en() -> bool:
    return LANG == "en"


def de() -> bool:
    return LANG == "de"


def L(fr: str, en_text: str, de_text: str | None = None) -> str:
    if LANG == "de":
        return en_text if de_text is None else de_text
    return en_text if LANG == "en" else fr


def dec(s: str) -> str:
    return s if LANG == "en" else s.replace(".", ",")


def plural(n: float, fr_word: str, en_word: str | None = None, de_word: str | None = None) -> str:
    """'1 tour' / '3 tours', '1 lap' / '3 laps', '1 Runde' / '3 Runden'."""
    if LANG == "de" and de_word:
        return de_word + ("n" if abs(n) != 1 and not de_word.endswith("n") else "")
    w = en_word if (LANG != "fr" and en_word) else fr_word
    return w + ("s" if abs(n) >= 2 or (LANG != "fr" and abs(n) != 1) else "")


def session(code: str) -> str:
    """Nom de session affiché ("course" -> "race"...)."""
    if LANG == "de":
        return {"course": "Rennen", "qualif": "Qualifying", "essais": "Training", "warmup": "Warm-up"}.get(code, code)
    if LANG != "en":
        return code
    return {"course": "race", "qualif": "qualifying", "essais": "practice", "warmup": "warm-up"}.get(code, code)
