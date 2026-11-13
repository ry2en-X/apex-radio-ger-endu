"""
Langstrecke / endurance : ce qui compte en course, du sprint de 20 minutes aux 24 heures.

Tout se règle tout seul (0 = automatique) : la longueur de stint vient de la consommation mesurée, donc ça marche
pour n'importe quelle voiture (Hypercar et LMGT3 : énergie virtuelle ; LMP2 / LMP3 : essence), circuit et durée.
En sprint sans arrêt, le module se tait.

- Tours de course : tours faits par TA voiture (lus dans le jeu, donc justes même après un relais ou si Apex est lancé
  en cours de course), tours du leader, tours restants estimés.
- Stints et relais : n° de stint, tours et temps dans le stint, changement de pilote, bilan du stint à l'arrêt.
- Objectif carburant / énergie : cible par tour pour tenir X tours ; « un arrêt de moins si tu économises x % ».
- Énergie virtuelle : alerte avant 0 % (pénalité de 100 s), batterie hybride trop basse avant l'arrêt.
- Arrêt : temps perdu au stand mesuré, et où tu ressors (place, écarts, trafic).
- Drapeau jaune : dans la fenêtre d'arrêt ou pas.
- Pneus : allocation (0 = illimitée), tours sur le train, perte de rythme du train.
- Catégorie : écarts, tours de retard, arrêts des rivaux (lus dans le jeu).
- Trafic : groupe plus lent devant, catégorie plus rapide derrière, voiture devant / derrière de ta catégorie.
- Points d'étape : mi-course, heures restantes, fin de course.
"""
from __future__ import annotations

import math
import re
import statistics
import time

from i18n import L, dec

DEFAULTS = {
    "stint_laps": 0,           # tours par stint (0 = automatique, d'après la consommation)
    "stints_per_driver": 0,    # stints d'affilée par pilote (0 = pas de changement de pilote)
    "tyres_total": 0,          # pneus pour qualif + course (0 = pas de limite)
    "tyres_used": 0,           # pneus déjà montés (compté tout seul, corrigeable)
    "stints_per_set": 2,       # stints par train de pneus (double relais pneus)
    "fuel_target_laps": 0,     # objectif de tours par stint (0 = automatique)
    "fuel_reserve": 0.5,       # % (énergie) ou litres gardés en réserve à l'arrivée au stand
    "pit_loss_s": 0,           # temps perdu par arrêt en s (0 = mesuré tout seul)
    "traffic_calls": True,     # annonces anticipées du trafic
    "traffic_ahead_s": 60,     # prévenir quand on rattrape un groupe plus lent dans ~60 s
    "fuel_calls": True,        # annonces de l'objectif carburant pendant le stint
    "progress_calls": True,    # points d'étape (mi-course, heures restantes)
}
PIT_LOSS_DEFAULT = 65.0        # s, tant qu'aucun arrêt n'a été mesuré sur ce circuit

FUEL_EVERY = 4                 # au plus une annonce carburant tous les 4 tours (sauf dérive nette)
GROUP_GAP = 4.0                # s : voitures à moins de 4 s les unes des autres = un même groupe


def _f(v: float, d: int = 1) -> str:
    return dec(f"{v:.{d}f}")


def _cls(c: str) -> str:
    from alerts import class_name
    return class_name(c or "")


def _lt(t: float) -> str:
    m, r = divmod(t, 60)
    return dec(f"{int(m)}:{r:04.1f}")


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
        e.pop("min_share", None)
        e.setdefault("pit_loss", {})
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

    @staticmethod
    def full_stint(s, plan) -> float | None:
        """Tours possibles avec un plein (essence et énergie), d'après la consommation mesurée."""
        if not plan or not plan.fuel_per_lap:
            return None
        f = s.fuel_capacity / plan.fuel_per_lap if s.fuel_capacity else math.inf
        e = 1.0 / plan.energy_per_lap if (plan.energy_per_lap and s.energy is not None) else math.inf
        v = min(f, e)
        return v if v < 500 else None

    def stint_len(self, s, plan) -> int | None:
        n = int(self.c.get("stint_laps") or 0)
        if n:
            return n
        full = self.full_stint(s, plan)
        return max(1, math.floor(full - 0.2)) if full else None

    def target_laps(self, s=None, plan=None) -> int:
        n = int(self.c.get("fuel_target_laps") or 0)
        if n or s is None:
            return n or int(self.c.get("stint_laps") or 0)
        return self.stint_len(s, plan) or 0

    def pit_loss(self, s) -> tuple[float, bool]:
        """(secondes perdues par arrêt, mesuré ?)"""
        manual = float(self.c.get("pit_loss_s") or 0)
        if manual:
            return manual, True
        m = (self.c.get("pit_loss") or {}).get(s.track)
        return (float(m), True) if m else (PIT_LOSS_DEFAULT, False)

    # ------------------------------------------------------------------ état
    def _reset_session(self) -> None:
        self._plan = None
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
        self.stint_times: list[float] = []   # tours propres du stint
        self.set_times: list[tuple] = []     # (âge du train, temps) tours propres sur ce train de pneus
        self.stints: list[dict] = []         # bilans des stints terminés
        self.pit_in = None                   # (elapsed, lap_frac) à l'entrée des stands
        self.was_yellow = False
        self.race_total = None               # durée totale de la course (s)
        self.prev_left = None
        self.drive_t0 = None
        self.deg_said = False

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
        self._plan = plan
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
        if s.driver != self.cur_driver:
            self.cur_driver, self.drive_t0 = s.driver, s.elapsed
        if s.session == "course":
            self._progress(s, plan, say)
            self._yellow(s, plan, say)
            if not s.in_pits:
                self._critical(s, plan, say)
                self._traffic_calls(s, plan, say)
        self.was_yellow = s.yellow
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

    def fuel_state(self, s, plan=None) -> dict | None:
        plan = plan if plan is not None else self._plan
        manual = bool(int(self.c.get("fuel_target_laps") or 0) or int(self.c.get("stint_laps") or 0))
        tgt = self.target_laps(s, plan)
        cur = self._amount(s)
        if not tgt or cur is None or self.stint_amount0 is None:
            return None
        done = s.laps_done - self.stint_lap0
        if s.session == "course" and plan and plan.laps_to_finish is not None:
            if plan.stops_needed == 0 and (not manual or plan.laps_to_finish < tgt - done):
                return None      # dernier relais ou sprint sans arrêt : la stratégie gère l'essence jusqu'à l'arrivée
        left = max(1, tgt - done)
        reserve = float(self.c.get("fuel_reserve") or 0)
        target = max(0.0, self.stint_amount0 - reserve) / tgt
        need = max(0.0, cur - reserve) / left
        last = self.lap_use[-1] if self.lap_use else None
        avg = statistics.mean(self.lap_use[-3:]) if self.lap_use else None
        reach = (max(0.0, cur - reserve) / avg + done) if avg else None
        return {"target": target, "need": need, "last": last, "avg": avg, "laps": tgt, "done": done,
                "reach": reach, "unit": "%" if s.energy is not None else "L", "energy": s.energy is not None,
                "auto": not manual}

    def _lap_done(self, s, plan, say) -> None:
        cur = self._amount(s)
        dirty = self.lap_dirty
        if cur is not None and self.prev_amount is not None and not self.lap_dirty:
            used = self.prev_amount - cur
            if 0 < used < 30:
                self.lap_use.append(used)
                self.lap_use = self.lap_use[-8:]
        self.prev_amount = cur
        self.lap_dirty = False
        clean = not dirty and not s.yellow and s.wetness < 0.05 and s.last_lap and 20 < s.last_lap < 900
        self.tyre_age = [a + 1 for a in self.tyre_age]
        if clean:
            self.stint_times.append(s.last_lap)
            self.set_times.append((max(self.tyre_age), s.last_lap))
        d = self.drivers.setdefault(s.driver or "?", {"laps": 0, "time": 0.0})
        d["laps"] += 1
        if s.last_lap:
            d["time"] += s.last_lap
        if s.session == "course":
            self._fuel_call(s, plan, say)
            self._stop_saver_call(s, plan, say)
            self._deg_call(s, say)
            self._stint_end_call(s, plan, say)

    def _fuel_call(self, s, plan, say) -> None:
        if not self.c.get("fuel_calls"):
            return
        fs = self.fuel_state(s, plan)
        if not fs or fs["last"] is None or fs["done"] < 2 or fs["done"] >= fs["laps"]:
            return
        over = fs["last"] - fs["need"]
        drift = over > 0.04 * max(fs["need"], 0.01)
        if fs["auto"] and not drift:
            return   # objectif automatique : on ne parle que si le stint prévu ne passe plus
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
            self.pit_in = (s.elapsed, s.lap_frac)
            if s.session == "course":
                self._stint_close(s, plan, say)
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
                if len(changed) >= 2:
                    self.set_times, self.deg_said = [], False
            if s.session == "course":
                self._measure_loss(s, plan)
                self.stint_times = []
                self.stint_no = (s.pit_stops or self.stint_no) + 1
                self.stint_lap0, self.stint_t0 = s.laps_done, s.elapsed
                self.stint_amount0 = self._amount(s)
                self.prev_amount = self.stint_amount0
                self.lap_use = []
                self.fuel_said_lap = s.laps_done
                self._stint_start_call(s, plan, say, changed)
            elif changed:
                left = self.tyres_left()
                if left is not None:
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

    def tyres_left(self) -> int | None:
        if not int(self.c.get("tyres_total") or 0):
            return None
        return max(0, int(self.c.get("tyres_total") or 0) - int(self.c.get("tyres_used") or 0))

    def total_stints(self, s, plan) -> int | None:
        if not plan or plan.stops_needed is None:
            return None
        return self.stint_no + plan.stops_needed

    def change_after(self, stint: int) -> bool:
        k = int(self.c.get("stints_per_driver") or 0)
        return bool(k) and stint % k == 0

    def _stint_start_call(self, s, plan, say, changed) -> None:
        n = self.stint_no
        tot = None
        parts = [L(f"Stint {n}" + (f" sur {tot}" if tot else "") + ".", f"Stint {n}" + (f" of {tot}" if tot else "") + ".",
                   f"Stint {n}" + (f" von {tot}" if tot else "") + ".")]
        left = self.tyres_left()
        if changed and left is not None:
            parts.append(L(f"{len(changed)} pneus neufs, il en reste {left}.", f"{len(changed)} new tyres, {left} left.",
                           f"{len(changed)} neue Reifen, noch {left} übrig."))
        elif not changed and s.pit_stops:
            age = max(self.tyre_age)
            parts.append(L(f"Pneus gardés, {age} tours dessus.", f"Tyres kept, {age} laps on them.",
                           f"Reifen bleiben drauf, {age} Runden alt."))
        fs = self.fuel_state(s, plan)
        tgt = fs["laps"] if fs else 0
        if tgt and fs and not fs["auto"]:
            un = " %" if fs["energy"] else L(" litre", " litres", " Liter")
            parts.append(L(f"Objectif {tgt} tours : {_f(fs['target'], 2)}{un} par tour.",
                           f"Target {tgt} laps: {_f(fs['target'], 2)}{un} a lap.",
                           f"Ziel {tgt} Runden: {_f(fs['target'], 2)}{un} pro Runde."))
        if self.change_after(n) and (tot is None or n < tot):
            parts.append(L("Changement de pilote à la fin de ce stint.", "Driver change at the end of this stint.",
                           "Fahrerwechsel am Ende dieses Stints."))
        if len(parts) > 1:
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

    # ------------------------------------------------------------------ rivaux de catégorie : arrêts
    def _rivals(self, s) -> None:
        for r in s.rivals or []:
            k = r.get("name") or f"#{r.get('cpos') or r.get('place')}"
            st = self.rival_stops.setdefault(k, {"stops": 0, "in": bool(r.get("in_pits"))})
            if r.get("stops") is not None:
                st["stops"] = int(r["stops"])          # lu dans le jeu
            elif r.get("in_pits") and not st["in"]:
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
        tgt = self.stint_len(s, plan) or 0
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
        if self.drive_t0 is not None and s.elapsed - self.drive_t0 > 600:
            m = int((s.elapsed - self.drive_t0) // 60)
            hm = f"{m // 60}:{m % 60:02d}"
            out += " " + L(f"Tu roules depuis {hm}.", f"You've been driving for {hm}.", f"Du fährst seit {hm} Stunden.")
        return out

    def answer_fuel_target(self, s, plan, t: str = "") -> str:
        m = re.search(r"\b(\d{1,2}) ?(runden|laps|tours)\b", t)
        head = ""
        if m and re.search(r"ziel|target|objectif|stint", t):
            self.set("fuel_target_laps", int(m.group(1)))
            head = L(f"Objectif réglé sur {m.group(1)} tours. ", f"Target set to {m.group(1)} laps. ", f"Ziel auf {m.group(1)} Runden gesetzt. ")
        elif re.search(r"(ziel|target|objectif).*\bauto", t):
            self.set("fuel_target_laps", 0)
            head = L("Objectif automatique. ", "Target back to automatic. ", "Ziel wieder automatisch. ")
        fs = self.fuel_state(s, plan)
        if not fs:
            sv = self.stop_saver(s, plan)
            if sv:
                return head + self._saver_text(sv)
            return head + L("Pas d'objectif d'économie pour ce relais : tu as ce qu'il faut. Dis par exemple « objectif 32 tours » pour en fixer un.",
                            "No saving target for this stint: you have what you need. Say \"target 32 laps\" to set one.",
                            "Kein Sparziel für diesen Stint: Du hast, was du brauchst. Sag zum Beispiel „Ziel 32 Runden“, um eins zu setzen.")
        un = " %" if fs["energy"] else L(" litre", " litres", " Liter")
        if fs["done"] >= fs["laps"]:
            return L(f"Objectif de {fs['laps']} tours déjà atteint : {fs['done']} tours dans ce stint.",
                     f"{fs['laps']}-lap target already reached: {fs['done']} laps this stint.",
                     f"Ziel von {fs['laps']} Runden schon erreicht: {fs['done']} Runden in diesem Stint.")
        out = head + L(f"Pour {fs['laps']} tours : {_f(fs['target'], 2)}{un} par tour sur le stint, {_f(fs['need'], 2)}{un} à partir de maintenant.",
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
        sv = self.stop_saver(s, plan)
        if sv:
            out += " " + self._saver_text(sv)
        return out

    def answer_tyres(self, s, plan) -> str:
        left = self.tyres_left()
        sets, single = divmod(left or 0, 4)
        out = ""
        if left is not None:
            out = L(f"Il reste {left} pneus : {sets} trains", f"{left} tyres left: {sets} sets", f"Noch {left} Reifen: {sets} Sätze")
            out += (L(f" et {single} pneus seuls.", f" and {single} single tyres.", f" und {single} Einzelreifen.") if single else ".") + " "
        age = max(self.tyre_age) if self.tyre_age else 0
        out += L(f"Le train actuel a {age} tours.", f"Current set has {age} laps on it.", f"Der aktuelle Satz hat {age} Runden drauf.")
        tot = self.total_stints(s, plan) if s.session == "course" else None
        per = int(self.c.get("stints_per_set") or 0)
        dg = self.deg()
        if dg is not None:
            out += " " + (L(f"Le train a perdu {_f(dg)} s au tour depuis qu'il est neuf.", f"This set has lost {_f(dg)} s a lap since new.",
                            f"Der Satz ist {_f(dg)} s pro Runde langsamer als frisch.") if dg > 0.2 else
                          L("Le rythme tient sur ce train.", "Pace is holding on this set.", "Das Tempo hält auf diesem Satz."))
        if tot and per and left is not None:
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
                 f"Du bist P{me} in der Klasse, {mine} Stopp{'s' if mine != 1 else ''}.")]
        near = sorted((r for r in s.rivals if r.get("cpos") and abs(r["cpos"] - me) <= 2 or r.get("cpos") == 1),
                      key=lambda r: r["cpos"])
        for r in near[:4]:
            k = r.get("name") or f"#{r.get('cpos') or r.get('place')}"
            st = self.rival_stops.get(k, {}).get("stops")
            g = r.get("gap")
            dl = (r.get("laps") or 0) - s.laps_done
            where = (L(f"{_f(abs(g))} s devant", f"{_f(abs(g))} s ahead", f"{_f(abs(g))} s vorne") if g and g > 0 else
                     L(f"{_f(abs(g))} s derrière", f"{_f(abs(g))} s behind", f"{_f(abs(g))} s hinten") if g else
                     L(f"{abs(dl)} {_laps_word(dl)} devant", f"{abs(dl)} {_laps_word(dl)} ahead", f"{abs(dl)} {_laps_word(dl)} vor dir") if dl > 0 else
                     L(f"{abs(dl)} {_laps_word(dl)} derrière", f"{abs(dl)} {_laps_word(dl)} behind", f"{abs(dl)} {_laps_word(dl)} zurück") if dl < 0 else
                     L("pas dans le même tour", "not on the same lap", "nicht in derselben Runde"))
            stops = ""
            if st is not None:
                d = st - mine
                stops = (L(f", {st} arrêts comme toi", f", {st} stops like you", f", {st} Stopp{'s' if st != 1 else ''} wie du") if d == 0 else
                         L(f", {st} arrêts ({d:+d})", f", {st} stops ({d:+d})", f", {st} Stopp{'s' if st != 1 else ''} ({d:+d})"))
            pit = L(", aux stands", ", in the pits", ", in der Box") if r.get("in_pits") else ""
            out.append(f"P{r['cpos']} {where}{stops}{pit}.")
        return " ".join(out)

    # ------------------------------------------------------------------ un arrêt de moins ?
    def stop_saver(self, s, plan) -> dict | None:
        """Peut-on supprimer un arrêt en économisant un peu (8 % au plus) sur tous les relais restants ?"""
        full = self.full_stint(s, plan)
        if (s.session != "course" or not plan or not plan.ready or not full or not plan.stops_needed
                or plan.range_laps is None or plan.laps_to_finish is None):
            return None
        denom = plan.range_laps + (plan.stops_needed - 1) * full
        if denom <= 0:
            return None
        x = plan.laps_to_finish / denom - 1
        if not (0.002 < x <= 0.08):
            return None
        return {"pct": x * 100, "laps": math.ceil(full * (1 + x)), "now": math.floor(full), "stops": plan.stops_needed - 1}

    @staticmethod
    def _saver_text(sv) -> str:
        p, n, k = f"{sv['pct']:.0f}", sv["laps"], sv["stops"]
        if k == 0:
            return L(f"On peut aller au bout sans autre arrêt : économise environ {p} % par tour.",
                     f"We can make it without another stop: save about {p} % a lap.",
                     f"Wir kommen ohne weiteren Stopp durch: Spar etwa {p} % pro Runde.")
        return L(f"Un arrêt de moins est possible : économise environ {p} % par tour, soit {n} tours par stint, et on fait {k} arrêt{'s' if k > 1 else ''} au lieu de {k + 1}.",
                 f"One stop fewer is possible: save about {p} % a lap, {n} laps per stint, and we do {k} stop{'s' if k != 1 else ''} instead of {k + 1}.",
                 f"Ein Stopp weniger ist möglich: Spar etwa {p} % pro Runde, also {n} Runden pro Stint, dann brauchen wir {k} statt {k + 1} Stopps.")

    def _stop_saver_call(self, s, plan, say) -> None:
        if s.in_pits or s.laps_done - self.stint_lap0 < 3 or int(self.c.get("fuel_target_laps") or 0):
            return
        k = f"sav:{self.stint_no}"
        sv = self.stop_saver(s, plan)
        if sv and k not in self.said_keys:
            self.said_keys.add(k)
            say(None, self._saver_text(sv) + L(" Dis « objectif " + str(sv["laps"]) + " tours » si tu veux que je te guide.",
                                               f" Say \"target {sv['laps']} laps\" and I'll guide you.",
                                               f" Sag „Ziel {sv['laps']} Runden“, dann führe ich dich."))

    # ------------------------------------------------------------------ pneus : perte de rythme du train
    def deg(self) -> float | None:
        t = [x for x in self.set_times if x[0] >= 2]
        if len(t) < 8:
            return None
        return statistics.median(x[1] for x in t[-3:]) - statistics.median(x[1] for x in t[:3])

    def _deg_call(self, s, say) -> None:
        d = self.deg()
        if d is not None and d > 0.7 and not self.deg_said:
            self.deg_said = True
            age = max(self.tyre_age)
            say(None, L(f"Les pneus baissent : {_f(d)} seconde au tour de perdu depuis qu'ils sont neufs, {age} tours dessus.",
                        f"Tyres are dropping off: {_f(d)} s a lap slower than when new, {age} laps on them.",
                        f"Die Reifen bauen ab: {_f(d)} Sekunden pro Runde langsamer als frisch, {age} Runden drauf."))

    # ------------------------------------------------------------------ bilan de stint, temps perdu au stand
    def _stint_close(self, s, plan, say) -> None:
        laps = s.laps_done - self.stint_lap0
        if laps < 2 or (self.stints and self.stints[-1]["n"] == self.stint_no):
            return
        ts = self.stint_times
        rec = {"n": self.stint_no, "laps": laps, "avg": statistics.median(ts) if ts else None, "best": min(ts) if ts else None,
               "use": statistics.mean(self.lap_use) if self.lap_use else None, "unit": "%" if s.energy is not None else "L",
               "driver": s.driver, "min": round((s.elapsed - self.stint_t0) / 60, 1), "tyre_age": max(self.tyre_age)}
        self.stints = (self.stints + [rec])[-12:]
        say(None, self.stint_text(rec))

    @staticmethod
    def stint_text(r) -> str:
        out = L(f"Stint {r['n']} terminé : {r['laps']} tours", f"Stint {r['n']} done: {r['laps']} laps", f"Stint {r['n']} vorbei: {r['laps']} Runden")
        if r["avg"]:
            out += L(f", moyenne {_lt(r['avg'])}, meilleur {_lt(r['best'])}", f", average {_lt(r['avg'])}, best {_lt(r['best'])}",
                     f", Schnitt {_lt(r['avg'])}, beste {_lt(r['best'])}")
        if r["use"]:
            u = " %" if r["unit"] == "%" else L(" litre", " litres", " Liter")
            out += L(f", conso {_f(r['use'], 2)}{u} par tour", f", {_f(r['use'], 2)}{u} a lap", f", Verbrauch {_f(r['use'], 2)}{u} pro Runde")
        return out + "."

    def _measure_loss(self, s, plan) -> None:
        if not self.pit_in:
            return
        t0, f0 = self.pit_in
        self.pit_in = None
        avg = (plan.avg_lap if plan else None) or s.best_lap
        lane = s.elapsed - t0
        if not avg or not (15 < lane < 400):
            return
        loss = lane - ((s.lap_frac - f0) % 1) * avg + 2.0
        if 10 < loss < 300:
            self.c.setdefault("pit_loss", {})[s.track] = round(loss, 1)
            self.save(self.cfg)

    # ------------------------------------------------------------------ où tu ressors après l'arrêt
    def rejoin(self, s, plan) -> dict | None:
        if s.session != "course" or not s.rivals:
            return None
        loss, measured = self.pit_loss(s)
        cpos = s.class_position or s.position
        behind = sorted((-r["gap"], r.get("cpos")) for r in s.rivals if r.get("gap") is not None and r["gap"] < 0 and not r.get("in_pits"))
        lost = [b for b in behind if b[0] < loss]
        keep = [b for b in behind if b[0] >= loss]
        out = {"loss": loss, "measured": measured, "pos": cpos + len(lost), "lost": len(lost),
               "ahead": {"gap": loss - lost[-1][0]} if lost else None,
               "behind": {"gap": keep[0][0] - loss} if keep else None, "traffic": 0}
        me, mine, _ = self._paces(s, plan)
        if me and mine:
            where = loss % mine
            for c in s.cars:
                if c.get("me") or c.get("pits") or c["c"] == me["c"]:
                    continue
                bt = ((me["f"] - c["f"]) % 1) * mine
                if where - 6 <= bt <= where + 3:
                    out["traffic"] += 1
        return out

    @staticmethod
    def rejoin_text(r) -> str:
        head = "" if r["measured"] else L(f"Estimation avec {r['loss']:.0f} secondes perdues au stand : ",
                                          f"Estimate with {r['loss']:.0f} seconds lost in the pits: ",
                                          f"Geschätzt mit {r['loss']:.0f} Sekunden Zeitverlust: ")
        out = head + L(f"après l'arrêt tu ressors environ P{r['pos']} de ta catégorie", f"after the stop you rejoin around P{r['pos']} in class",
                       f"nach dem Stopp kommst du etwa auf P{r['pos']} in der Klasse raus")
        if r["ahead"]:
            out += L(f", {r['ahead']['gap']:.0f} secondes derrière P{r['pos'] - 1}", f", {r['ahead']['gap']:.0f} seconds behind P{r['pos'] - 1}",
                     f", {r['ahead']['gap']:.0f} Sekunden hinter P{r['pos'] - 1}")
        if r["behind"]:
            out += L(f", P{r['pos'] + 1} à {r['behind']['gap']:.0f} secondes derrière", f", P{r['pos'] + 1} {r['behind']['gap']:.0f} seconds behind you",
                     f", P{r['pos'] + 1} ist {r['behind']['gap']:.0f} Sekunden dahinter")
        out += ". " + (L("Piste libre à la sortie.", "Clear track at pit exit.", "Freie Strecke an der Boxenausfahrt.") if not r["traffic"] else
                       L(f"Tu ressors dans le trafic, {r['traffic']} voitures d'autres catégories autour de toi.",
                         f"You rejoin in traffic, {r['traffic']} cars from other classes around you.",
                         f"Du kommst im Verkehr raus, {r['traffic']} Autos anderer Klassen um dich herum."))
        return out[:1].upper() + out[1:]

    # ------------------------------------------------------------------ alertes : énergie, batterie, sortie des stands
    def _critical(self, s, plan, say) -> None:
        if not plan or not plan.ready:
            return
        st = self.stint_no
        if (plan.stops_needed and plan.range_laps is not None and plan.laps_to_finish is not None
                and plan.laps_to_finish > plan.range_laps and s.lap_frac > 0.5 and plan.range_laps < (1 - s.lap_frac) + 1.0 and f"crit:{st}" not in self.said_keys):
            self.said_keys.add(f"crit:{st}")
            if plan.limited_by == "énergie":
                say(None, L("Énergie : pas assez pour un tour de plus. Box ce tour, sinon c'est la pénalité de 100 secondes.",
                            "Energy: not enough for another lap. Box this lap, or it's the 100-second penalty.",
                            "Energie reicht nicht für eine weitere Runde. Box diese Runde, sonst gibt es die 100-Sekunden-Strafe."), urgent=True)
            else:
                say(None, L("Essence : pas assez pour un tour de plus. Box ce tour.", "Fuel: not enough for another lap. Box this lap.",
                            "Sprit reicht nicht für eine weitere Runde. Box diese Runde."), urgent=True)
        box_soon = bool(plan.stops_needed) and plan.laps_until_box is not None and plan.laps_until_box <= 1
        if box_soon and s.battery is not None and s.battery < 0.12 and s.lap_frac > 0.4 and f"bat:{st}" not in self.said_keys:
            self.said_keys.add(f"bat:{st}")
            say(None, L(f"Batterie à {s.battery * 100:.0f} %. Recharge-la avant l'entrée des stands pour bien repartir.",
                        f"Battery at {s.battery * 100:.0f} %. Charge it before pit entry so you can pull away.",
                        f"Batterie bei {s.battery * 100:.0f} %. Lade sie vor der Boxeneinfahrt, damit du gut wegkommst."))
        if box_soon and 0.15 < s.lap_frac < 0.6 and f"rej:{st}" not in self.said_keys:
            self.said_keys.add(f"rej:{st}")
            r = self.rejoin(s, plan)
            if r:
                say(None, self.rejoin_text(r))

    def _yellow(self, s, plan, say) -> None:
        if not s.yellow or self.was_yellow or not plan or not plan.stops_needed or s.in_pits:
            return
        sl = self.stint_len(s, plan)
        done = s.laps_done - self.stint_lap0
        if (sl and done >= 0.5 * sl) or (plan.laps_until_box is not None and plan.laps_until_box <= 5):
            say(None, L("Sous drapeau jaune, un arrêt coûte moins cher et tu es dans la fenêtre : rentre si la voie des stands est ouverte.",
                        "Under yellow a stop costs less and you're in the window: box if the pit lane is open.",
                        "Unter Gelb kostet ein Stopp weniger Zeit und du bist im Boxenfenster: Komm rein, wenn die Boxengasse offen ist."))
        else:
            say(None, L("Trop tôt pour s'arrêter sous ce drapeau jaune, reste en piste.", "Too early to stop under this yellow, stay out.",
                        "Zu früh für einen Stopp unter Gelb, bleib draußen."))

    # ------------------------------------------------------------------ points d'étape
    def _status(self, s, plan) -> str:
        cp, st = s.class_position or s.position, s.pit_stops or 0
        n = plan.stops_needed if plan and plan.stops_needed else 0
        out = L(f"P{cp} dans ta catégorie, {st} arrêt{'s' if st > 1 else ''} fait{'s' if st > 1 else ''}",
                f"P{cp} in class, {st} stop{'s' if st != 1 else ''} done", f"P{cp} in der Klasse, {st} Stopp{'s' if st != 1 else ''} gemacht")
        if n:
            out += L(f", encore {n} prévu{'s' if n > 1 else ''}", f", {n} more planned", f", noch {n} geplant")
        return out + "."

    def _progress(self, s, plan, say) -> None:
        if s.time_left is None:
            return
        total = s.elapsed + s.time_left
        self.race_total = max(self.race_total or 0, total)
        total = self.race_total
        prev, self.prev_left = self.prev_left, s.time_left
        if prev is None or not self.c.get("progress_calls") or not (0 < prev - s.time_left < 30):
            return
        marks = []
        half = total / 2 if total >= 2400 else None
        if half:
            marks.append((half, L("Mi-course.", "Halfway.", "Halbzeit.")))
        for h in range(1, int(total // 3600) + 1):
            if h * 3600 < total - 600 and not (half and abs(h * 3600 - half) < 300):
                marks.append((h * 3600, L(f"Encore {h} heure{'s' if h > 1 else ''}.", f"{h} hour{'s' if h > 1 else ''} to go.",
                                          f"Noch {h} Stunde{'n' if h > 1 else ''}.")))
        if total >= 5400:
            marks.append((1800, L("Encore 30 minutes.", "30 minutes to go.", "Noch 30 Minuten.")))
        if total >= 1800:
            marks.append((600, L("Encore 10 minutes.", "10 minutes to go.", "Noch 10 Minuten.")))
        for m, label in marks:
            if prev > m >= s.time_left:
                say(None, f"{label} {self._status(s, plan)}")
                return

    # ------------------------------------------------------------------ tours de course
    def race_laps(self, s, plan) -> dict:
        leader = next((c for c in s.cars or [] if c.get("p") == 1), None)
        cl = next((r for r in s.rivals or [] if r.get("cpos") == 1), None)
        return {"mine": s.laps_done, "leader": leader.get("laps") if leader else None,
                "class_leader": cl.get("laps") if cl else s.laps_done,
                "to_go": plan.laps_to_finish if plan else None, "max": s.max_laps}

    def answer_laps(self, s, plan) -> str:
        r = self.race_laps(s, plan)
        out = L(f"Ta voiture a fait {r['mine']} tours.", f"Your car has done {r['mine']} laps.", f"Dein Auto hat {r['mine']} Runden gefahren.")
        if r["leader"] is not None and r["leader"] != r["mine"]:
            out += " " + L(f"Le leader en est à {r['leader']}.", f"The overall leader is on {r['leader']}.", f"Der Gesamtführende hat {r['leader']}.")
        if r["class_leader"] is not None and r["class_leader"] != r["mine"]:
            out += " " + L(f"Le premier de ta catégorie : {r['class_leader']}.", f"Your class leader: {r['class_leader']}.",
                           f"Der Klassenführende hat {r['class_leader']}.")
        if r["max"]:
            out += " " + L(f"Course de {r['max']} tours.", f"It's a {r['max']}-lap race.", f"Das Rennen geht über {r['max']} Runden.")
        elif r["to_go"]:
            n = round(r["to_go"])
            out += " " + L(f"Encore environ {n} tours, soit {r['mine'] + n} au total.", f"About {n} to go, so around {r['mine'] + n} in total.",
                           f"Noch etwa {n} Runden, also rund {r['mine'] + n} insgesamt.")
        return out

    def answer_rejoin(self, s, plan) -> str:
        r = self.rejoin(s, plan)
        if not r:
            return L("Pas d'autre voiture de ta catégorie pour estimer ta sortie des stands.", "No class rivals to estimate your pit exit against.",
                     "Keine Klassengegner, um deine Boxenausfahrt einzuschätzen.")
        return self.rejoin_text(r) + " " + L(f"Un arrêt coûte environ {r['loss']:.0f} secondes ici.", f"A stop costs about {r['loss']:.0f} seconds here.",
                                             f"Ein Stopp kostet hier etwa {r['loss']:.0f} Sekunden.")

    def answer_last_stint(self, s, plan) -> str:
        if not self.stints:
            return L("Aucun stint terminé pour l'instant.", "No stint finished yet.", "Noch kein Stint beendet.")
        return " ".join(self.stint_text(r) for r in self.stints[-2:])

    # ------------------------------------------------------------------ tableau de bord
    def _ui(self, s, plan) -> dict:
        race = s.session == "course"
        fs = self.fuel_state(s, plan) if s.on_track else None
        try:
            tr = self.traffic(s, plan) if s.on_track else {}
        except Exception:
            tr = {}
        tl = self.tyres_left()
        strip = lambda g: {k: v for k, v in g.items() if k != "ids"} if g else None
        loss, measured = self.pit_loss(s)
        return {
            "cfg": {k: self.c.get(k) for k in DEFAULTS},
            "stint": self.stint_no if race else None,
            "stints_total": self.total_stints(s, plan) if race else None,
            "stint_laps": s.laps_done - self.stint_lap0, "stint_min": round((s.elapsed - self.stint_t0) / 60, 1),
            "stint_len": self.stint_len(s, plan),
            "change_after": self.change_after(self.stint_no),
            "driver": s.driver, "drive_min": round((s.elapsed - self.drive_t0) / 60) if self.drive_t0 is not None else None,
            "fuel": fs, "saver": self.stop_saver(s, plan) if race else None,
            "range": plan.range_laps if plan else None, "limited": (plan.limited_by if plan else "") == "énergie",
            "battery": s.battery,
            "tyres_left": tl, "tyre_sets": (tl or 0) // 4, "tyre_singles": (tl or 0) % 4,
            "tyre_age": max(self.tyre_age) if self.tyre_age else 0, "deg": self.deg(),
            "traffic": {k: strip(v) for k, v in tr.items()} if tr else {},
            "laps": self.race_laps(s, plan) if race else None,
            "rejoin": self.rejoin(s, plan) if race and plan and plan.stops_needed else None,
            "pit_loss": loss, "pit_loss_measured": measured,
            "stints": self.stints[-5:],
            "race_frac": (1 - s.time_left / self.race_total) if (race and self.race_total and s.time_left is not None) else None,
        }

    def ui(self) -> dict:
        return self.info or {"cfg": {k: self.c.get(k) for k in DEFAULTS}}
