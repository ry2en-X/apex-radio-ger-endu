"""
Langstrecke / endurance : ce qui compte sur 6, 8, 12 ou 24 heures.

- Stints et relais : n° de stint, tours dans le stint, temps de stint, changement de pilote prévu, minimum de tours par pilote.
- Objectif carburant / énergie : consommation cible par tour pour tenir X tours, écart au tour, ce qu'il faut à partir de maintenant.
- Pneus : jeux restants sur l'allocation (qualif + course), tours sur le train actuel, assez de pneus pour finir ?
- Écarts de catégorie : arrêts faits par les rivaux de ta catégorie, écart et nombre d'arrêts comparés aux tiens.
- Trafic à venir : prochain groupe de voitures plus lentes (combien, à quelle distance, dans combien de temps tu les rattrapes,
  à quel virage), prochaine catégorie plus rapide qui va te prendre un tour, et projection sur la voiture devant / derrière
  dans ta catégorie (quand tu la rattrapes, ou quand elle te rattrape, avant ou après ton prochain arrêt).

Réglages (config.json, clé "endurance") modifiables depuis le tableau de bord. Tout est calculé à partir des données
du jeu ; rien n'est envoyé nulle part.
"""
from __future__ import annotations

import math
import statistics
import time

from i18n import L, dec

DEFAULTS = {
    "stint_laps": 31,          # tours par stint visés
    "stints_per_driver": 2,    # stints d'affilée par pilote (2 = double relais)
    "min_share": 16.7,         # % minimum de la distance par pilote (0 = pas de minimum)
    "tyres_total": 26,         # pneus pour qualif + course (6 trains + 2 pneus seuls = 26)
    "tyres_used": 0,           # pneus déjà montés (compté tout seul, corrigeable)
    "stints_per_set": 2,       # stints par train de pneus (double relais pneus)
    "fuel_target_laps": 0,     # 0 = même valeur que stint_laps
    "fuel_reserve": 0.5,       # % (énergie) ou litres gardés en réserve à l'arrivée au stand
    "traffic_calls": True,     # annonces anticipées du trafic (en plus des annonces à 8–25 s)
    "traffic_ahead_s": 60,     # prévenir quand on rattrape un groupe plus lent dans ~60 s
    "fuel_calls": True,        # annonces de l'objectif carburant pendant le stint
}

FUEL_EVERY = 4                 # au plus une annonce carburant tous les 4 tours (sauf dérive nette)
GROUP_GAP = 4.0                # s : voitures à moins de 4 s les unes des autres = un même groupe


def _f(v: float, d: int = 1) -> str:
    return dec(f"{v:.{d}f}")


def _cls(c: str) -> str:
    from alerts import class_name
    return class_name(c or "")


def _laps_word(n: float) -> str:
    n1 = abs(round(n)) == 1
    return L("tour" if n1 else "tours", "lap" if n1 else "laps", "Runde" if n1 else "Runden")


class Endurance:
    LAST = None

    def __init__(self, cfg: dict, save=None, behavior=None) -> None:
        self.cfg = cfg
        self.save = save or (lambda c: None)
        self.beh = behavior
        e = cfg.setdefault("endurance", {})
        for k, v in DEFAULTS.items():
            e.setdefault(k, v)
        self._reset_session()
        Endurance.LAST = self

    # ------------------------------------------------------------------ réglages
    @property
    def c(self) -> dict:
        return self.cfg["endurance"]

    def set(self, key: str, value) -> None:
        if key == "reset_tyres":
            self.c["tyres_used"] = 0
        elif key in DEFAULTS:
            t = type(DEFAULTS[key])
            try:
                self.c[key] = (value in (True, "true", 1, "1")) if t is bool else t(value)
            except (TypeError, ValueError):
                return
            if t in (int, float):
                self.c[key] = max(0, self.c[key])
        self.save(self.cfg)

    def target_laps(self) -> int:
        return int(self.c.get("fuel_target_laps") or self.c.get("stint_laps") or 0)

    # ------------------------------------------------------------------ état
    def _reset_session(self) -> None:
        self.key = None
        self.last_lap = None            # laps_done au dernier passage
        self.stint_no = 1
        self.stint_lap0 = 0             # laps_done au début du stint
        self.stint_t0 = 0.0             # elapsed au début du stint
        self.stint_amount0 = None       # carburant / énergie au début du stint
        self.was_in_pits = False
        self.wear_before = None         # usure des pneus en entrant au stand
        self.wear_out = None            # dernière usure vue hors des stands
        self.tyre_age = [0, 0, 0, 0]    # tours sur chaque pneu
        self.counted_start = False
        self.lap_use: list[float] = []  # consommation des derniers tours du stint
        self.prev_amount = None
        self.lap_dirty = False
        self.fuel_said_lap = -99
        self.drivers: dict[str, dict] = {}
        self.cur_driver = ""
        self.car_laps: dict = {}        # id -> {"cross": t, "f": f, "laps": [..], "pit": bool}
        self.rival_stops: dict = {}     # nom -> {"stops": n, "in": bool}
        self.traffic_said: dict = {}    # clé de groupe -> heure
        self.last_traffic = -999.0
        self.said_keys: set = set()
        self.info: dict = {}

    # ------------------------------------------------------------------ boucle
    def update(self, s, plan, say) -> None:
        if not s.on_track:
            return
        key = (s.track, s.session, s.car)
        if key != self.key:
            prev_session = self.key[1] if self.key else None
            self._reset_session()
            self.key = key
            self.stint_lap0, self.stint_t0, self.last_lap = s.laps_done, s.elapsed, s.laps_done
            self.stint_no = (s.pit_stops or 0) + 1
            self.stint_amount0 = self._amount(s)
            self.prev_amount = self.stint_amount0
            # nouvelle allocation de pneus : début des qualifs, ou nouveau circuit
            if (s.session == "qualif" and prev_session != "qualif") or self.c.get("tyre_track") != s.track:
                self.c["tyres_used"] = 0
                self.c["tyre_track"] = s.track
                self.save(self.cfg)
        now = s.elapsed
        self._first_set(s)
        self._pits(s, plan, say)
        if self.last_lap is not None and s.laps_done > self.last_lap:
            for _ in range(s.laps_done - self.last_lap):
                self._lap_done(s, plan, say)
            self.last_lap = s.laps_done
        if s.in_pits:
            self.lap_dirty = True
        self._track_cars(s, now)
        self._rivals(s)
        if s.session == "course" and not s.in_pits:
            self._traffic_calls(s, plan, say)
        self.info = self._ui(s, plan)

    # ------------------------------------------------------------------ carburant / énergie
    @staticmethod
    def _amount(s):
        """Énergie virtuelle en % si la voiture en a, sinon carburant en litres."""
        if s.energy is not None:
            return s.energy * 100
        return s.fuel if s.fuel else None

    @staticmethod
    def _unit(s) -> str:
        return "%" if s.energy is not None else L(" litre", " L", " Liter")

    def fuel_state(self, s) -> dict | None:
        tgt = self.target_laps()
        cur = self._amount(s)
        if not tgt or cur is None or self.stint_amount0 is None:
            return None
        done = s.laps_done - self.stint_lap0
        left = max(1, tgt - done)
        reserve = float(self.c.get("fuel_reserve") or 0)
        target = max(0.0, self.stint_amount0 - reserve) / tgt
        need = max(0.0, cur - reserve) / left
        last = self.lap_use[-1] if self.lap_use else None
        avg = statistics.mean(self.lap_use[-3:]) if self.lap_use else None
        reach = (max(0.0, cur - reserve) / avg + done) if avg else None
        return {"target": target, "need": need, "last": last, "avg": avg, "laps": tgt, "done": done,
                "reach": reach, "unit": "%" if s.energy is not None else "L", "energy": s.energy is not None}

    def _lap_done(self, s, plan, say) -> None:
        cur = self._amount(s)
        if cur is not None and self.prev_amount is not None and not self.lap_dirty:
            used = self.prev_amount - cur
            if 0 < used < 30:
                self.lap_use.append(used)
                self.lap_use = self.lap_use[-8:]
        self.prev_amount = cur
        self.lap_dirty = False
        self.tyre_age = [a + 1 for a in self.tyre_age]
        d = self.drivers.setdefault(s.driver or "?", {"laps": 0, "time": 0.0})
        d["laps"] += 1
        if s.last_lap:
            d["time"] += s.last_lap
        if s.session == "course":
            self._fuel_call(s, plan, say)
            self._min_share_call(s, plan, say)
            self._stint_end_call(s, plan, say)

    def _fuel_call(self, s, plan, say) -> None:
        if not self.c.get("fuel_calls"):
            return
        fs = self.fuel_state(s)
        if not fs or fs["last"] is None or fs["done"] < 2 or fs["done"] >= fs["laps"]:
            return
        if plan and plan.stops_needed == 0 and plan.laps_to_finish is not None and plan.laps_to_finish < fs["laps"] - fs["done"]:
            return   # dernier relais plus court : la stratégie s'en occupe
        over = fs["last"] - fs["need"]
        drift = over > 0.04 * max(fs["need"], 0.01)
        if not drift and s.laps_done - self.fuel_said_lap < FUEL_EVERY:
            return
        if drift and s.laps_done - self.fuel_said_lap < 2:
            return
        self.fuel_said_lap = s.laps_done
        u = fs["unit"]
        nd = 2
        last, need, tgt = _f(fs["last"], nd), _f(fs["need"], nd), fs["laps"]
        un = " %" if u == "%" else L(" litre", " litres", " Liter")
        if over > 0.005 * max(fs["need"], 1):
            pct = over / max(fs["last"], 0.01) * 100
            say(None, L(f"Objectif {tgt} tours : dernier tour {last}{un}, il faut {need}{un} à partir de maintenant. Économise environ {pct:.0f} %.",
                        f"{tgt}-lap target: last lap {last}{un}, you need {need}{un} from now on. Save about {pct:.0f} %.",
                        f"Ziel {tgt} Runden: letzte Runde {last}{un}, ab jetzt brauchst du {need}{un}. Spar etwa {pct:.0f} %."))
        else:
            say(None, L(f"Carburant dans l'objectif : {last}{un} au dernier tour, tu as droit à {need}{un}. Les {tgt} tours passent.",
                        f"Fuel on target: {last}{un} last lap, you're allowed {need}{un}. {tgt} laps are on.",
                        f"Sprit im Ziel: {last}{un} letzte Runde, erlaubt sind {need}{un}. Die {tgt} Runden gehen."))

    # ------------------------------------------------------------------ stints et pilotes
    def _pits(self, s, plan, say) -> None:
        if not s.in_pits and s.tyre_wear:
            self.wear_out = list(s.tyre_wear)
        if s.in_pits and not self.was_in_pits:
            self.wear_before = self.wear_out
        if self.was_in_pits and not s.in_pits:
            # sortie des stands : nouveau stint (en course), pneus changés ?
            changed = []
            if self.wear_before and s.tyre_wear:
                changed = [i for i in range(4) if s.tyre_wear[i] - self.wear_before[i] > 0.05 and s.tyre_wear[i] > 0.97]
            if changed:
                self.c["tyres_used"] = int(self.c.get("tyres_used") or 0) + len(changed)
                self.save(self.cfg)
                for i in changed:
                    self.tyre_age[i] = 0
            if s.session == "course":
                self.stint_no = (s.pit_stops or self.stint_no) + 1
                self.stint_lap0, self.stint_t0 = s.laps_done, s.elapsed
                self.stint_amount0 = self._amount(s)
                self.prev_amount = self.stint_amount0
                self.lap_use = []
                self.fuel_said_lap = s.laps_done
                self._stint_start_call(s, plan, say, changed)
            elif changed:
                left = self.tyres_left()
                say(None, L(f"{len(changed)} pneus neufs. Il en reste {left}.", f"{len(changed)} new tyres. {left} left.",
                            f"{len(changed)} neue Reifen. Noch {left} übrig."))
        self.was_in_pits = s.in_pits

    def _first_set(self, s) -> None:
        """Premier train monté dans la séance : compté s'il est neuf (qualif, ou départ sur pneus neufs)."""
        if self.counted_start or s.in_pits or not s.tyre_wear:
            return
        self.counted_start = True
        if s.session in ("qualif", "course") and all(w >= 0.99 for w in s.tyre_wear) and s.laps_done <= 1:
            self.c["tyres_used"] = int(self.c.get("tyres_used") or 0) + 4
            self.save(self.cfg)

    def tyres_left(self) -> int:
        return max(0, int(self.c.get("tyres_total") or 0) - int(self.c.get("tyres_used") or 0))

    def total_stints(self, s, plan) -> int | None:
        sl = int(self.c.get("stint_laps") or 0)
        if not sl or not plan or plan.laps_to_finish is None:
            return None
        total_laps = s.laps_done + plan.laps_to_finish
        return max(1, math.ceil(total_laps / sl))

    def change_after(self, stint: int) -> bool:
        k = int(self.c.get("stints_per_driver") or 0)
        return bool(k) and stint % k == 0

    def _stint_start_call(self, s, plan, say, changed) -> None:
        n = self.stint_no
        tot = self.total_stints(s, plan)
        parts = [L(f"Stint {n}" + (f" sur {tot}" if tot else "") + ".", f"Stint {n}" + (f" of {tot}" if tot else "") + ".",
                   f"Stint {n}" + (f" von {tot}" if tot else "") + ".")]
        if changed:
            left = self.tyres_left()
            parts.append(L(f"{len(changed)} pneus neufs, il en reste {left}.", f"{len(changed)} new tyres, {left} left.",
                           f"{len(changed)} neue Reifen, noch {left} übrig."))
        tgt = self.target_laps()
        fs = self.fuel_state(s)
        if tgt and fs:
            un = " %" if fs["energy"] else L(" litre", " litres", " Liter")
            parts.append(L(f"Objectif {tgt} tours : {_f(fs['target'], 2)}{un} par tour.",
                           f"Target {tgt} laps: {_f(fs['target'], 2)}{un} a lap.",
                           f"Ziel {tgt} Runden: {_f(fs['target'], 2)}{un} pro Runde."))
        if self.change_after(n) and (tot is None or n < tot):
            parts.append(L("Changement de pilote à la fin de ce stint.", "Driver change at the end of this stint.",
                           "Fahrerwechsel am Ende dieses Stints."))
        say(None, " ".join(parts))

    def _stint_end_call(self, s, plan, say) -> None:
        if not plan or plan.laps_until_box is None or plan.stops_needed in (None, 0):
            return
        if plan.laps_until_box == 2 and self.change_after(self.stint_no):
            k = f"chg:{self.stint_no}"
            if k not in self.said_keys:
                self.said_keys.add(k)
                say(None, L("Changement de pilote à ce prochain arrêt, prépare le relais.",
                            "Driver change at this next stop, get ready for the swap.",
                            "Fahrerwechsel beim nächsten Stopp, bereite die Übergabe vor."))

    def _min_share_call(self, s, plan, say) -> None:
        share = float(self.c.get("min_share") or 0)
        if not share or not plan or plan.laps_to_finish is None:
            return
        total = s.laps_done + plan.laps_to_finish
        need = math.ceil(total * share / 100)
        d = self.drivers.get(s.driver or "?")
        if d and d["laps"] == need:
            k = f"min:{s.driver}"
            if k not in self.said_keys:
                self.said_keys.add(k)
                say(None, L(f"{need} tours pour toi : ton minimum de pilotage est atteint.",
                            f"{need} laps for you: your minimum driving distance is done.",
                            f"{need} Runden für dich: deine Mindestfahrzeit ist erreicht."))

    # ------------------------------------------------------------------ rivaux de catégorie : arrêts
    def _rivals(self, s) -> None:
        for r in s.rivals or []:
            k = r.get("name") or str(r.get("place"))
            st = self.rival_stops.setdefault(k, {"stops": 0, "in": bool(r.get("in_pits"))})
            if r.get("in_pits") and not st["in"]:
                st["stops"] += 1
            st["in"] = bool(r.get("in_pits"))

    # ------------------------------------------------------------------ rythme de chaque voiture
    def _track_cars(self, s, now: float) -> None:
        for c in s.cars or []:
            cid = c.get("id")
            if cid is None:
                continue
            st = self.car_laps.setdefault(cid, {"cross": None, "f": c["f"], "laps": [], "pit": False})
            if c.get("pits"):
                st["pit"] = True
            if st["f"] > 0.8 and c["f"] < 0.2:          # passage sur la ligne
                if st["cross"] is not None and not st["pit"]:
                    lt = now - st["cross"]
                    if 20 < lt < 600:
                        st["laps"] = (st["laps"] + [lt])[-4:]
                st["cross"], st["pit"] = now, bool(c.get("pits"))
            st["f"] = c["f"]

    def car_pace(self, c, class_pace: dict) -> float | None:
        st = self.car_laps.get(c.get("id"))
        if st and len(st["laps"]) >= 2:
            return statistics.median(st["laps"][-3:])
        if c.get("best"):
            return c["best"] * 1.015
        return class_pace.get(c.get("c"))

    def _paces(self, s, plan):
        me = next((c for c in s.cars or [] if c.get("me")), None)
        if not me or not s.track_len:
            return None, None, {}
        bests = {}
        for c in s.cars:
            if c.get("best"):
                bests.setdefault(c["c"], []).append(c["best"] * 1.015)
        class_pace = {k: statistics.median(v) for k, v in bests.items()}
        mine = (plan.avg_lap if plan else None) or s.last_lap or self.car_pace(me, class_pace)
        return me, mine, class_pace

    def traffic(self, s, plan) -> dict:
        """Prévision du trafic : groupe plus lent devant, catégorie plus rapide derrière, rivaux devant / derrière."""
        out = {"slow": None, "fast": None, "ahead": None, "behind": None}
        me, mine, class_pace = self._paces(s, plan)
        if not me or not mine:
            return out
        slow, fast = [], []
        for c in s.cars:
            if c.get("me") or c.get("pits") or c["c"] == me["c"]:
                continue
            p = self.car_pace(c, class_pace)
            if not p:
                continue
            if p > mine * 1.02:                                    # plus lente : devant toi sur la piste
                df = (c["f"] - me["f"]) % 1
                rate = 1 / mine - 1 / p                            # tours de piste gagnés par seconde
                slow.append({"gap": df * mine, "t": df / rate, "c": c, "f": c["f"]})
            elif p < mine * 0.98:                                  # plus rapide : derrière toi
                df = (me["f"] - c["f"]) % 1
                rate = 1 / p - 1 / mine
                fast.append({"gap": df * p, "t": df / rate, "c": c})
        out["slow"] = self._group(slow, mine, me)
        out["fast"] = self._group(fast, mine, me)
        # même catégorie
        for side, gap, other in (("ahead", s.gap_class_ahead, s.ahead), ("behind", s.gap_class_behind, s.behind)):
            if gap is None or not other:
                continue
            car = next((c for c in s.cars if c.get("rel") == side), None)
            p = self.car_pace(car, class_pace) if car else other.get("last_lap")
            if not p:
                continue
            closing = (p - mine) if side == "ahead" else (mine - p)  # s gagnées par tour (> 0 : l'écart se réduit)
            laps = max(0.0, (gap - 0.8) / closing) if closing > 0.03 else None
            out[side] = {"gap": gap, "pace": p, "delta": closing, "laps": laps, "name": other.get("name") or "",
                         "place": other.get("place"), "pits": bool(other.get("in_pits"))}
        return out

    def _group(self, items, mine, me) -> dict | None:
        if not items:
            return None
        items.sort(key=lambda x: x["t"])
        first = items[0]
        grp = [first]
        for x in items[1:]:
            if x["gap"] - grp[-1]["gap"] < GROUP_GAP and x["c"]["c"] == first["c"]["c"]:
                grp.append(x)
        t = first["t"]
        corner = None
        if self.beh and "f" in first:
            try:
                corner = self.beh.corner_at((me["f"] + t / mine) % 1)
            except Exception:
                corner = None
        return {"n": len(grp), "cls": _cls(first["c"].get("cls")), "gap": first["gap"], "t": t, "laps": t / mine,
                "corner": corner, "ids": tuple(sorted(x["c"].get("id") for x in grp))}

    def _traffic_calls(self, s, plan, say) -> None:
        if not self.c.get("traffic_calls"):
            return
        now = time.monotonic()
        if now - self.last_traffic < 25:
            return
        tr = self.traffic(s, plan)
        lead = float(self.c.get("traffic_ahead_s") or 60)
        g = tr.get("slow")
        if g and lead * 0.8 <= g["t"] <= lead * 1.1:
            k = ("slow", g["ids"][:1])
            if now - self.traffic_said.get(k, -999) > 180:
                self.traffic_said[k] = self.last_traffic = now
                say(None, self.slow_text(g))
                return
        g = tr.get("fast")
        if g and 30 <= g["t"] <= 50:
            k = ("fast", g["ids"][:1])
            if now - self.traffic_said.get(k, -999) > 180:
                self.traffic_said[k] = self.last_traffic = now
                say(None, self.fast_text(g))

    # ------------------------------------------------------------------ textes (radio et réponses)
    @staticmethod
    def _when(t: float, laps: float) -> str:
        if t < 90:
            return L(f"dans environ {t:.0f} secondes", f"in about {t:.0f} seconds", f"in etwa {t:.0f} Sekunden")
        return L(f"dans environ {_f(laps)} tours", f"in about {_f(laps)} laps", f"in etwa {_f(laps)} Runden")

    def slow_text(self, g) -> str:
        n, cls, gap, when = g["n"], g["cls"], g["gap"], self._when(g["t"], g["laps"])
        at = L(f", vers le virage {g['corner']}", f", around turn {g['corner']}", f", etwa in Kurve {g['corner']}") if g.get("corner") else ""
        if n > 1:
            return L(f"Trafic devant : {n} {cls} à {gap:.0f} secondes, tu les rattrapes {when}{at}.",
                     f"Traffic ahead: {n} {cls} cars {gap:.0f} seconds up the road, you'll catch them {when}{at}.",
                     f"Verkehr vor dir: {n} {cls} mit {gap:.0f} Sekunden Abstand, du holst sie {when} ein{at}.")
        return L(f"Trafic devant : une {cls} à {gap:.0f} secondes, tu la rattrapes {when}{at}.",
                 f"Traffic ahead: a {cls} {gap:.0f} seconds up the road, you'll catch it {when}{at}.",
                 f"Verkehr vor dir: ein {cls} mit {gap:.0f} Sekunden Abstand, du holst ihn {when} ein{at}.")

    def fast_text(self, g) -> str:
        n, cls, when = g["n"], g["cls"], self._when(g["t"], g["laps"])
        if n > 1:
            return L(f"{n} {cls} plus rapides derrière, sur toi {when}. Laisse-les passer proprement.",
                     f"{n} faster {cls} cars behind, on you {when}. Let them by cleanly.",
                     f"{n} schnellere {cls} hinter dir, {when} an dir dran. Lass sie sauber vorbei.")
        return L(f"Une {cls} plus rapide derrière, sur toi {when}.", f"A faster {cls} behind, on you {when}.",
                 f"Ein schnellerer {cls} hinter dir, {when} an dir dran.")

    def rival_text(self, side: str, r, plan) -> str:
        who = f"P{r['place']}" if r.get("place") else L("la voiture", "the car", "das Auto")
        if r["pits"]:
            return L(f"{who} est aux stands.", f"{who} is in the pits.", f"{who} ist in der Box.")
        gap = _f(r["gap"])
        if r["laps"] is None or r["laps"] > 60:
            if side == "ahead":
                return L(f"{who} devant à {gap} s, tu ne le reprends pas à ce rythme.",
                         f"{who} ahead by {gap} s, you're not catching him at this pace.",
                         f"{who} vorne mit {gap} s, bei diesem Tempo holst du ihn nicht ein.")
            return L(f"{who} derrière à {gap} s, il ne revient pas.", f"{who} behind by {gap} s, he's not closing.",
                     f"{who} hinten mit {gap} s, er kommt nicht näher.")
        n = max(1, math.ceil(r["laps"]))
        d = _f(abs(r["delta"]))
        box = plan.laps_until_box if plan and plan.stops_needed else None
        before = None if box is None else (n <= box)
        tail = ""
        if before is True:
            tail = L(", avant ton prochain arrêt", ", before your next stop", ", noch vor deinem nächsten Stopp")
        elif before is False:
            tail = L(", après ton prochain arrêt", ", after your next stop", ", erst nach deinem nächsten Stopp")
        if side == "ahead":
            return L(f"{who} devant à {gap} s, tu lui prends {d} s au tour : sur lui dans environ {n} {_laps_word(n)}{tail}.",
                     f"{who} ahead by {gap} s, you're {d} s a lap quicker: on him in about {n} {_laps_word(n)}{tail}.",
                     f"{who} vorne mit {gap} s, du bist {d} s pro Runde schneller: in etwa {n} {_laps_word(n)} an ihm dran{tail}.")
        return L(f"{who} derrière à {gap} s, il te reprend {d} s au tour : sur toi dans environ {n} {_laps_word(n)}{tail}.",
                 f"{who} behind by {gap} s, he's {d} s a lap quicker: on you in about {n} {_laps_word(n)}{tail}.",
                 f"{who} hinten mit {gap} s, er ist {d} s pro Runde schneller: in etwa {n} {_laps_word(n)} an dir dran{tail}.")

    def answer_traffic(self, s, plan, t: str = "") -> str:
        tr = self.traffic(s, plan)
        parts = []
        want_class = any(w in t for w in ("vordermann", "hintermann", "devant", "derriere", "ahead", "behind", "einhol", "rattrap", "catch"))
        if not want_class or not (tr["ahead"] or tr["behind"]):
            parts.append(self.slow_text(tr["slow"]) if tr["slow"] else
                         L("Pas de voiture plus lente devant pour l'instant.", "No slower cars ahead right now.",
                           "Gerade kein langsameres Auto vor dir."))
            if tr["fast"]:
                parts.append(self.fast_text(tr["fast"]))
        for side in ("ahead", "behind"):
            if tr[side]:
                parts.append(self.rival_text(side, tr[side], plan))
        return " ".join(parts) or L("Je n'ai pas encore assez de données sur le trafic.", "Not enough traffic data yet.",
                                    "Noch nicht genug Daten zum Verkehr.")

    def answer_stint(self, s, plan) -> str:
        if s.session != "course":
            return L("Les stints, c'est pour la course.", "Stints only count in the race.", "Stints gibt es nur im Rennen.")
        done = s.laps_done - self.stint_lap0
        mins = (s.elapsed - self.stint_t0) / 60
        tgt = int(self.c.get("stint_laps") or 0)
        tot = self.total_stints(s, plan)
        out = L(f"Stint {self.stint_no}" + (f" sur {tot}" if tot else "") + f", {done} tours en {mins:.0f} minutes.",
                f"Stint {self.stint_no}" + (f" of {tot}" if tot else "") + f", {done} laps in {mins:.0f} minutes.",
                f"Stint {self.stint_no}" + (f" von {tot}" if tot else "") + f", {done} Runden in {mins:.0f} Minuten.")
        if plan and plan.laps_until_box is not None and plan.stops_needed:
            b = plan.laps_until_box
            out += " " + L(f"Arrêt dans {b} {_laps_word(b)}.", f"Pit in {b} {_laps_word(b)}.", f"Box in {b} {_laps_word(b)}.")
        elif tgt:
            out += " " + L(f"Objectif {tgt} tours.", f"Target {tgt} laps.", f"Ziel {tgt} Runden.")
        k = int(self.c.get("stints_per_driver") or 0)
        if k:
            nxt = math.ceil(self.stint_no / k) * k
            if nxt == self.stint_no:
                out += " " + L("Changement de pilote à la fin de ce stint.", "Driver change at the end of this stint.",
                               "Fahrerwechsel am Ende dieses Stints.")
            else:
                out += " " + L(f"Changement de pilote après le stint {nxt}.", f"Driver change after stint {nxt}.",
                               f"Fahrerwechsel nach Stint {nxt}.")
        share = float(self.c.get("min_share") or 0)
        d = self.drivers.get(s.driver or "?")
        if share and plan and plan.laps_to_finish is not None and d:
            need = math.ceil((s.laps_done + plan.laps_to_finish) * share / 100)
            out += " " + (L(f"Minimum pilote atteint : {d['laps']} tours sur {need}.",
                            f"Driver minimum done: {d['laps']} of {need} laps.",
                            f"Mindestrunden erfüllt: {d['laps']} von {need}.") if d["laps"] >= need else
                          L(f"Tu as {d['laps']} tours, il t'en faut au moins {need}.",
                            f"You have {d['laps']} laps, you need at least {need}.",
                            f"Du hast {d['laps']} Runden, du brauchst mindestens {need}."))
        return out

    def answer_fuel_target(self, s, plan) -> str:
        fs = self.fuel_state(s)
        if not fs:
            return L("Règle d'abord l'objectif de tours par stint dans le tableau de bord.",
                     "Set the target laps per stint in the dashboard first.",
                     "Stell zuerst im Dashboard die Ziel-Runden pro Stint ein.")
        un = " %" if fs["energy"] else L(" litre", " litres", " Liter")
        if fs["done"] >= fs["laps"]:
            return L(f"Objectif de {fs['laps']} tours déjà atteint : {fs['done']} tours dans ce stint.",
                     f"{fs['laps']}-lap target already reached: {fs['done']} laps this stint.",
                     f"Ziel von {fs['laps']} Runden schon erreicht: {fs['done']} Runden in diesem Stint.")
        out = L(f"Pour {fs['laps']} tours : {_f(fs['target'], 2)}{un} par tour sur le stint, {_f(fs['need'], 2)}{un} à partir de maintenant.",
                f"For {fs['laps']} laps: {_f(fs['target'], 2)}{un} a lap over the stint, {_f(fs['need'], 2)}{un} from now on.",
                f"Für {fs['laps']} Runden: {_f(fs['target'], 2)}{un} pro Runde im Stint, ab jetzt {_f(fs['need'], 2)}{un}.")
        if fs["avg"] is not None:
            diff = fs["avg"] - fs["need"]
            out += " " + L(f"Tu es à {_f(fs['avg'], 2)}{un} en moyenne", f"You're averaging {_f(fs['avg'], 2)}{un}",
                           f"Du liegst im Schnitt bei {_f(fs['avg'], 2)}{un}")
            if diff > 0.005:
                pct = diff / max(fs["avg"], 0.01) * 100
                out += L(f", il faut gagner {_f(diff, 2)}{un} par tour, environ {pct:.0f} %.",
                         f", save {_f(diff, 2)}{un} a lap, about {pct:.0f} %.",
                         f", spar {_f(diff, 2)}{un} pro Runde, etwa {pct:.0f} %.")
            else:
                out += L(", c'est bon.", ", you're fine.", ", das passt.")
            if fs["reach"]:
                out += " " + L(f"À ce rythme, {_f(fs['reach'], 1)} tours.", f"At this rate, {_f(fs['reach'], 1)} laps.",
                               f"So reicht es für {_f(fs['reach'], 1)} Runden.")
        return out

    def answer_tyres(self, s, plan) -> str:
        left = self.tyres_left()
        sets, single = divmod(left, 4)
        out = L(f"Il reste {left} pneus : {sets} trains", f"{left} tyres left: {sets} sets", f"Noch {left} Reifen: {sets} Sätze")
        if single:
            out += L(f" et {single} pneus seuls.", f" and {single} single tyres.", f" und {single} Einzelreifen.")
        else:
            out += "."
        age = max(self.tyre_age) if self.tyre_age else 0
        out += " " + L(f"Le train actuel a {age} tours.", f"Current set has {age} laps on it.", f"Der aktuelle Satz hat {age} Runden drauf.")
        tot = self.total_stints(s, plan) if s.session == "course" else None
        per = int(self.c.get("stints_per_set") or 0)
        if tot and per:
            remaining_stints = max(0, tot - self.stint_no)
            need_sets = math.ceil(remaining_stints / per)
            if need_sets <= sets:
                out += " " + L(f"Assez pour finir : {need_sets} trains nécessaires.", f"Enough to finish: {need_sets} sets needed.",
                               f"Reicht bis ins Ziel: {need_sets} Sätze nötig.")
            else:
                out += " " + L(f"Attention, il faudrait {need_sets} trains : prolonge un relais pneus.",
                               f"Careful, we'd need {need_sets} sets: stretch one tyre stint.",
                               f"Achtung, wir bräuchten {need_sets} Sätze: verlängere einen Reifen-Stint.")
        return out

    def answer_class(self, s, plan) -> str:
        if not s.rivals:
            return L("Pas d'autre voiture dans ta catégorie.", "No other car in your class.", "Kein anderes Auto in deiner Klasse.")
        me = s.class_position or s.position
        mine = s.pit_stops or 0
        out = [L(f"Tu es P{me} dans ta catégorie, {mine} arrêts.", f"You're P{me} in class, {mine} stops.",
                 f"Du bist P{me} in der Klasse, {mine} Stopps.")]
        near = sorted((r for r in s.rivals if r.get("cpos") and abs(r["cpos"] - me) <= 2 or r.get("cpos") == 1),
                      key=lambda r: r["cpos"])
        for r in near[:4]:
            k = r.get("name") or str(r.get("place"))
            st = self.rival_stops.get(k, {}).get("stops")
            g = r.get("gap")
            where = (L(f"{_f(abs(g))} s devant", f"{_f(abs(g))} s ahead", f"{_f(abs(g))} s vorne") if g and g > 0 else
                     L(f"{_f(abs(g))} s derrière", f"{_f(abs(g))} s behind", f"{_f(abs(g))} s hinten") if g else
                     L("pas dans le même tour", "not on the same lap", "nicht in derselben Runde"))
            stops = ""
            if st is not None:
                d = st - mine
                stops = (L(f", {st} arrêts comme toi", f", {st} stops like you", f", {st} Stopps wie du") if d == 0 else
                         L(f", {st} arrêts ({d:+d})", f", {st} stops ({d:+d})", f", {st} Stopps ({d:+d})"))
            pit = L(", aux stands", ", in the pits", ", in der Box") if r.get("in_pits") else ""
            out.append(f"P{r['cpos']} {where}{stops}{pit}.")
        return " ".join(out)

    # ------------------------------------------------------------------ tableau de bord
    def _ui(self, s, plan) -> dict:
        fs = self.fuel_state(s) if s.on_track else None
        try:
            tr = self.traffic(s, plan) if s.on_track else {}
        except Exception:
            tr = {}
        d = self.drivers.get(s.driver or "?")
        share = float(self.c.get("min_share") or 0)
        need = math.ceil((s.laps_done + plan.laps_to_finish) * share / 100) if (share and plan and plan.laps_to_finish is not None) else None
        tl = self.tyres_left()
        strip = lambda g: {k: v for k, v in g.items() if k != "ids"} if g else None
        return {
            "cfg": {k: self.c.get(k) for k in DEFAULTS},
            "stint": self.stint_no if s.session == "course" else None,
            "stints_total": self.total_stints(s, plan) if s.session == "course" else None,
            "stint_laps": s.laps_done - self.stint_lap0, "stint_min": round((s.elapsed - self.stint_t0) / 60, 1),
            "change_after": self.change_after(self.stint_no),
            "driver": s.driver, "driver_laps": d["laps"] if d else 0, "driver_min": need,
            "fuel": fs, "tyres_left": tl, "tyre_sets": tl // 4, "tyre_singles": tl % 4,
            "tyre_age": max(self.tyre_age) if self.tyre_age else 0,
            "traffic": {k: strip(v) for k, v in tr.items()} if tr else {},
        }

    def ui(self) -> dict:
        return self.info or {"cfg": {k: self.c.get(k) for k in DEFAULTS}}
