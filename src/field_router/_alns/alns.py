"""ALNS для открытого TRSP (Ropke & Pisinger 2006; Kovacs et al. 2012; Gamst & Pisinger 2023).

Разрушение: случайное, Shaw (связанные по месту/времени), худшее, удаление маршрута (для сокращения
числа инженеров), временной срез. Восстановление: жадная вставка, regret-2/3 с шумом.
Принятие: имитация отжига. Все допустимые маршруты складываются в пул для set partitioning.
"""
from __future__ import annotations
import math, random, time
from .instance import Instance
from .solution import Solution, RouteEval


class RoutePool:
    def __init__(self):
        self.routes = {}   # (e, tuple(seq)) -> km

    def add_solution(self, sol: Solution):
        for e, r in sol.routes.items():
            if r.ok and r.seq:
                # Keep memory bounded during long interactive sessions. Existing
                # columns and the final incumbent can always be overwritten.
                if len(self.routes) >= 12000 and (e, tuple(r.seq)) not in self.routes:
                    continue
                self.routes[(e, tuple(r.seq))] = r.km
                n = len(r.seq)
                if n > 1 and len(self.routes) < 6000:     # подмаршруты — дешёвые дополнительные столбцы
                    for k in range(n):
                        sub = r.seq[:k] + r.seq[k + 1:]
                        self.routes.setdefault((e, tuple(sub)), None)

    def materialize(self, inst, deadline=float("inf")):
        out = {}
        for (e, seq), km in self.routes.items():
            if time.perf_counter() >= deadline:
                break
            if km is None:
                r = RouteEval(inst, e, list(seq))
                if not r.ok:
                    continue
                km = r.km
            out[(e, seq)] = km
        return out


class ALNS:
    def __init__(self, inst: Instance, seed=0, fixed=None, max_routes=None):
        """fixed: e -> список заявок, которые обязаны остаться первыми в маршруте (перепланирование)."""
        self.inst, self.rng = inst, random.Random(seed)
        self.compat = [[e for e, eng in enumerate(inst.engineers)
                        if inst.compatible(eng, t) and inst.reachable[e][t.idx]] for t in inst.tasks]
        self.fixed = fixed or {}
        self.fixed_tasks = {j for seq in self.fixed.values() for j in seq}
        self.pool = RoutePool()
        self.destroys = [self.d_random, self.d_shaw, self.d_worst, self.d_route, self.d_timeslice, self.d_conflict]
        self.repairs = [self.r_greedy, self.r_regret2, self.r_regret3]
        # нормированная «связанность» заявок для Shaw
        self.span = max(1.0, float(inst.km.max())) if inst.km.size else 1.0
        self.engineer_weight = inst.N * self.span + 1
        self.deadline = float("inf")
        self._empty = [RouteEval(inst, e, []) for e in range(inst.E)]
        self.max_routes = max_routes      # режим сокращения парка: новые маршруты сверх лимита запрещены

    # ---------- insertion ----------
    def _move(self, sol: Solution, j: int, e: int):
        route = sol.routes.get(e)
        extra = int(route is None)
        if route is None:
            if self.max_routes is not None and len(sol.routes) >= self.max_routes:
                return None
            route = self._empty[e]
        ins = route.insertion(self.inst, j, len(self.fixed.get(e, ())))
        if ins is None:
            return None
        delta, pos = ins
        if self.inst.profile is None:
            return (extra, int(round(delta * 1000))), pos
        candidate = RouteEval(self.inst, e, route.seq[:pos] + [j] + route.seq[pos:])
        change = list(a-b for a, b in zip(candidate.objective_key(self.inst), route.objective_key(self.inst)))
        if self.inst.profile == 'sla' and j in self.inst.target_tasks:
            change[0] -= 1441
        return tuple(change), pos

    def _repair(self, sol: Solution, regret_k: int, noise: float):
        """Cached greedy/regret insertion; coverage priority is always the first key."""
        # Removing a stop can invalidate the remaining route on nonmetric travel.
        # Return such stops to pending instead of ever accepting an invalid state.
        for e, route in list(sol.routes.items()):
            if not route.ok:
                for j in list(route.seq):
                    if j not in self.fixed_tasks:
                        sol.remove(self.inst, j, e)
        pending = set(sol.unassigned)
        cache = {}
        for j in sorted(pending):
            if time.perf_counter() >= self.deadline:
                return sol
            cache[j] = {e: self._move(sol, j, e) for e in self.compat[j]}
        while pending and time.perf_counter() < self.deadline:
            best_j, best_key, best_mv = None, None, None
            for j in sorted(pending):
                moves = sorted((move[0], e, move[1]) for e, move in cache[j].items() if move is not None)
                if not moves:
                    continue
                if noise:
                    # Real diversification, not just random tie-breaking between
                    # almost-never-equal kilometre deltas. Cached exact costs stay
                    # intact; only the exploration ranking is perturbed.
                    distance_index = self.inst.objective_tiers.index('distance_m')
                    def perturbed(move):
                        key = list(move[0])
                        key[distance_index] += self.rng.uniform(-noise * 1000, noise * 1000)
                        return tuple(key)
                    moves.sort(key=perturbed)
                if regret_k == 1:
                    preference = tuple(-v for v in moves[0][0])
                else:
                    top = moves[:regret_k]
                    regrets = tuple(sum(m[0][i] - top[0][0][i] for m in top[1:])
                                    for i in range(len(top[0][0])))
                    preference = (regret_k-len(top),) + regrets
                # Noise affects only tie-breaking within a priority tier.
                key = (self.inst.tasks[j].priority,) + preference + (self.rng.random()*noise,)
                if best_key is None or key > best_key:
                    best_j, best_key, best_mv = j, key, moves[0]
            if best_j is None or time.perf_counter() >= self.deadline:
                break
            _, e, pos = best_mv
            if e not in sol.routes and self.max_routes is not None and len(sol.routes) >= self.max_routes:
                cache[best_j][e] = None
                continue
            sol.insert(self.inst, best_j, e, pos)
            pending.remove(best_j)
            del cache[best_j]
            for j in sorted(pending):
                if time.perf_counter() >= self.deadline:
                    return sol
                if e in cache[j]:
                    cache[j][e] = self._move(sol, j, e)
        return sol

    def r_greedy(self, sol): return self._repair(sol, 1, 0.0)
    def r_regret2(self, sol): return self._repair(sol, 2, 1.5 * self.rng.random())
    def r_regret3(self, sol): return self._repair(sol, 3, 3.0 * self.rng.random())

    # ---------- разрушение ----------
    def _removable(self, sol):
        return [j for r in sol.routes.values() for j in r.seq if j not in self.fixed_tasks]

    def _q(self, sol):
        n = len(self._removable(sol))
        return max(1, min(n, self.rng.randint(max(2, n // 10), max(3, n * 35 // 100))))

    def d_random(self, sol):
        cand = self._removable(sol)
        for j in self.rng.sample(cand, min(len(cand), self._q(sol))):
            sol.remove(self.inst, j)

    def d_shaw(self, sol):
        cand = self._removable(sol)
        if not cand: return
        q, inst = self._q(sol), self.inst
        seed = self.rng.choice(cand)
        ts = inst.tasks[seed]
        def rel(j):
            t = inst.tasks[j]
            return inst.km[inst.E + seed, inst.E + j] / self.span + abs(t.a - ts.a) / 720.0
        for j in sorted(cand, key=rel)[:q]:
            sol.remove(inst, j)

    def d_worst(self, sol):
        inst, cand = self.inst, []
        for e, r in sol.routes.items():
            kmm = inst.kml[inst.engineers[e].mode]
            nodes = [e] + [inst.E + j for j in r.seq]
            for k, j in enumerate(r.seq):
                if j in self.fixed_tasks: continue
                a, b = nodes[k], nodes[k + 1]
                save = kmm[a][b] + (kmm[b][nodes[k + 2]] - kmm[a][nodes[k + 2]] if k + 2 < len(nodes) else 0)
                if len(r.seq) == 1: save += self.engineer_weight
                cand.append((save * (0.7 + 0.6 * self.rng.random()), j))
        cand.sort(reverse=True)
        for _, j in cand[:self._q(sol)]:
            sol.remove(inst, j)

    def d_route(self, sol):
        """Удаляет целиком 1–2 коротких маршрута — главный механизм сокращения числа инженеров."""
        routes = [(len(r.seq) + self.rng.random() * 3, e) for e, r in sol.routes.items() if e not in self.fixed]
        routes.sort()
        for _, e in routes[:self.rng.choice([1, 1, 2])]:
            for j in list(sol.routes[e].seq):
                sol.remove(self.inst, j, e)

    def d_timeslice(self, sol):
        cand = self._removable(sol)
        if not cand: return
        t0 = self.inst.tasks[self.rng.choice(cand)].a
        for j in cand:
            if abs(self.inst.tasks[j].a - t0) <= 60:
                sol.remove(self.inst, j)

    def d_conflict(self, sol):
        """Free time/stock on eligible routes near a pending important job."""
        pending = [j for j in sorted(sol.unassigned) if self.compat[j]]
        if not pending:
            return self.d_shaw(sol)
        priority = max(self.inst.tasks[j].priority for j in pending)
        j = self.rng.choice([j for j in pending if self.inst.tasks[j].priority == priority])
        job, inst = self.inst.tasks[j], self.inst
        eligible = set(self.compat[j])
        candidates = []
        for e, route in sol.routes.items():
            if e not in eligible:
                continue
            for k in route.seq:
                if k in self.fixed_tasks:
                    continue
                t = inst.tasks[k]
                overlap = max(0, min(job.b, t.b) - max(job.a, t.a))
                distance = inst.kml[inst.engineers[e].mode][inst.node(j)][inst.node(k)]
                candidates.append((distance / max(1, self.span) - overlap / 720
                                   + self.rng.random() * .3, k))
        for _, k in sorted(candidates)[:max(2, self._q(sol))]:
            sol.remove(inst, k)

    def accept(self, cost, current, best, temperature):
        """Explore a bounded neighbourhood while the best uses exact ordering.

        Temporarily opening a route or ejecting a job enables exchanges that
        strict greedy acceptance cannot reach. No relaxed candidate is published
        unless its complete lexicographic key improves the feasible incumbent.
        """
        if cost <= current:
            return True
        if any(cost[i] > best[i] + limit for i, limit in enumerate((1, 2, 3))):
            return False
        first = next(i for i, (a, b) in enumerate(zip(cost, current)) if a != b)
        delta = cost[first] - current[first]
        if first < 3:
            penalty = delta * (6, 4, 3)[first]
        else:
            tier = self.inst.objective_tiers[first - 3]
            scale = {'engineers': 3, 'distance_m': .001, 'reassigned': 2,
                     'absolute_start_shift': .04, 'response_score': .04, 'travel_minutes': .04}[tier]
            penalty = delta * scale
        return self.rng.random() < math.exp(-penalty / max(temperature, 1e-6))

    # ---------- основной цикл ----------
    def initial(self) -> Solution:
        inst = self.inst
        sol = Solution(unassigned=set(range(inst.N)))
        for e, seq in self.fixed.items():
            for j in seq:
                sol.insert(inst, j, e, len(sol.routes[e].seq) if e in sol.routes else 0)
        return self.r_regret2(sol)

    def run(self, time_limit=10.0, max_iter=100000, init: Solution | None = None, log=False,
            deadline=None):
        inst, rng = self.inst, self.rng
        t_start = time.perf_counter()
        self.deadline = min(t_start + max(0.0, time_limit), deadline if deadline is not None else float('inf'))
        # Preserve a validated caller incumbent even if repair cannot finish.
        start = init.copy(inst) if init is not None else Solution(unassigned=set(range(inst.N)))
        if init is None:
            for e, seq in self.fixed.items():
                for j in seq:
                    start.insert(inst, j, e, len(start.routes[e].seq) if e in start.routes else 0)
        cur = self.r_regret2(start.copy(inst))
        if any(not r.ok for r in cur.routes.values()):
            cur = start
        best = cur.copy(inst)
        best_cost = cur_cost = cur.objective_key(inst)
        self.pool.add_solution(cur)
        wd, wr = [1.0] * len(self.destroys), [1.0] * len(self.repairs)
        sd, sr = [0.0] * len(self.destroys), [0.0] * len(self.repairs)
        nd, nr = [0] * len(self.destroys), [0] * len(self.repairs)
        temperature = .05 * sum(r.km for r in cur.routes.values()) / max(1, len(cur.routes)) + 1.0
        it, last_improvement, self.restarts, self.history = 0, 0, 0, []
        while it < max_iter and inst.N and time.perf_counter() < self.deadline:
            it += 1
            di = rng.choices(range(len(self.destroys)), weights=wd)[0]
            ri = rng.choices(range(len(self.repairs)), weights=wr)[0]
            cand = cur.copy(inst)
            self.destroys[di](cand)
            self.repairs[ri](cand)
            if any(not r.ok for r in cand.routes.values()):
                continue
            cost = cand.objective_key(inst)
            # A globally inferior plan may contain a route that completes the
            # best partition; don't discard that useful column with the plan.
            self.pool.add_solution(cand)
            score = 0
            if cost < best_cost:
                best, best_cost, score = cand.copy(inst), cost, 33
                last_improvement = it
                self.pool.add_solution(cand)
            if cost < cur_cost:
                cur, cur_cost, score = cand, cost, max(score, 9)
                self.pool.add_solution(cand)
            elif self.accept(cost, cur_cost, best_cost, temperature):
                cur, cur_cost, score = cand, cost, max(score, 3)
                if rng.random() < .3:
                    self.pool.add_solution(cand)
            sd[di] += score; nd[di] += 1; sr[ri] += score; nr[ri] += 1
            if it % 50 == 0:
                for weights, scores, counts in ((wd, sd, nd), (wr, sr, nr)):
                    for k in range(len(weights)):
                        if counts[k]:
                            weights[k] = .8*weights[k] + .2*max(.1, scores[k]/counts[k])
                        scores[k], counts[k] = 0.0, 0
                temperature = max(.15, temperature*.96)
                if rng.random() < .1:
                    cur, cur_cost = best.copy(inst), best_cost
            if it - last_improvement >= 200:
                # Alternate genuinely new construction and a large shake of
                # the incumbent. Share all columns across basins of attraction.
                self.restarts += 1
                if self.restarts % 2:
                    restart = self.initial()
                else:
                    restart = best.copy(inst)
                    for _ in range(3):
                        self.d_route(restart)
                    self.r_regret3(restart)
                if all(r.ok for r in restart.routes.values()):
                    cur, cur_cost = restart, restart.objective_key(inst)
                    self.pool.add_solution(restart)
                    if cur_cost < best_cost:
                        best, best_cost = cur.copy(inst), cur_cost
                last_improvement = it
                temperature = max(temperature, 1.0)
            if log and it % 200 == 0:
                self.history.append((time.perf_counter()-t_start, best_cost))
        self.iterations = it
        self.pool.add_solution(best)
        return best
