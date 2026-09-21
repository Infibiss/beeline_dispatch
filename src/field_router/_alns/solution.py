"""Feasibility and strict lexicographic objectives for open engineer routes."""
from __future__ import annotations
from dataclasses import dataclass, field
from .instance import Instance

EPS = 1e-9


def earliest(task, arrival):
    return min((max(arrival, a) for a, b in task.service_windows if max(arrival, a) <= b + EPS),
               default=float('inf'))


def latest(task, limit):
    return max((min(limit, b) for a, b in task.service_windows if min(limit, b) >= a - EPS),
               default=-float('inf'))


class RouteEval:
    """Forward earliest / backward latest starts, with aggregate stock checks.

    Backward starts are projected onto the union of start windows. No triangle
    inequality is assumed, including when removing a task from an existing route.
    """
    __slots__ = ('e', 'seq', 'E', 'L', 'km', 'ok', 'used')

    def __init__(self, inst: Instance, e: int, seq: list):
        self.e, self.seq = e, list(seq)
        self.recompute(inst)

    def recompute(self, inst: Instance):
        eng = inst.engineers[self.e]
        tt, kmm, tasks = inst.ttl[eng.mode], inst.kml[eng.mode], inst.tasks
        n = len(self.seq)
        self.E, self.L, self.km = [0] * n, [0] * n, 0.0
        self.ok = len(set(self.seq)) == n
        self.used = {'router': 0, 'tv_box': 0}
        prev_node, ready = eng.idx, eng.shift_start
        for k, j in enumerate(self.seq):
            tj, node = tasks[j], inst.node(j)
            start = earliest(tj, ready + tt[prev_node][node])
            if not inst.compatible(eng, tj) or start + tj.service > eng.shift_end + EPS:
                self.ok = False
            for device in self.used:
                self.used[device] += tj.equipment.get(device, 0)
            self.E[k] = start
            self.km += kmm[prev_node][node]
            ready, prev_node = start + tj.service, node
        if eng.stock is not None and any(v > eng.stock.get(k, 0) for k, v in self.used.items()):
            self.ok = False
        for k in range(n - 1, -1, -1):
            tj = tasks[self.seq[k]]
            limit = eng.shift_end - tj.service
            if k < n - 1:
                limit = min(limit, self.L[k + 1] - tj.service
                            - tt[inst.node(self.seq[k])][inst.node(self.seq[k + 1])])
            self.L[k] = latest(tj, limit)
        return self

    def profile_key(self, inst):
        if inst.profile == 'sla':
            return (sum(s for j, s in zip(self.seq, self.E) if inst.tasks[j].priority >= 2),)
        if inst.profile == 'stable':
            return (sum(j in inst.prev_owner and inst.prev_owner[j] != self.e for j in self.seq),
                    sum(abs(s - inst.prev_start[j]) for j, s in zip(self.seq, self.E)
                        if j in inst.prev_start))
        return ()

    def insertion(self, inst: Instance, j: int, min_pos: int = 0):
        """Best feasible insertion (delta_km, position), including window gaps."""
        eng, tj = inst.engineers[self.e], inst.tasks[j]
        if not self.ok or j in self.seq or not inst.compatible(eng, tj):
            return None
        if eng.stock is not None and any(self.used[k] + tj.equipment.get(k, 0) > eng.stock.get(k, 0)
                                        for k in self.used):
            return None
        tt, kmm = inst.ttl[eng.mode], inst.kml[eng.mode]
        nj, n, best, best_key = inst.node(j), len(self.seq), None, None
        for p in range(min_pos, n + 1):
            if p == 0:
                prev_node, ready = eng.idx, eng.shift_start
            else:
                prev_node = inst.node(self.seq[p - 1])
                ready = self.E[p - 1] + inst.tasks[self.seq[p - 1]].service
            s = earliest(tj, ready + tt[prev_node][nj])
            if s + tj.service > eng.shift_end + EPS:
                continue
            if p < n:
                nxt = inst.node(self.seq[p])
                if s + tj.service + tt[nj][nxt] > self.L[p] + EPS:
                    continue
                delta = kmm[prev_node][nj] + kmm[nj][nxt] - kmm[prev_node][nxt]
            else:
                delta = kmm[prev_node][nj]
            key = (delta,)
            if inst.profile in ('sla', 'stable'):
                candidate = RouteEval(inst, self.e, self.seq[:p] + [j] + self.seq[p:])
                key = candidate.profile_key(inst) + key
            if best_key is None or key < best_key:
                best, best_key = (delta, p), key
        return best


@dataclass
class Solution:
    routes: dict = field(default_factory=dict)
    unassigned: set = field(default_factory=set)

    def copy(self, inst):
        return Solution({e: RouteEval(inst, e, r.seq) for e, r in self.routes.items()}, set(self.unassigned))

    def objective_key(self, inst: Instance) -> tuple:
        coverage = (sum(inst.tasks[j].priority == 3 for j in self.unassigned),
                    sum(inst.tasks[j].priority >= 2 for j in self.unassigned), len(self.unassigned))
        profiles = [r.profile_key(inst) for r in self.routes.values()]
        size = 2 if inst.profile == 'stable' else 1 if inst.profile == 'sla' else 0
        profile = tuple(sum(p[i] for p in profiles) for i in range(size))
        return coverage + profile + (len(self.routes), round(sum(r.km for r in self.routes.values()), 6))

    def cost(self, inst: Instance) -> tuple:
        """An exact ordering key; never a fixed-ratio priority penalty."""
        return self.objective_key(inst)

    def metrics(self, inst: Instance) -> dict:
        per = {inst.engineers[e].id: round(r.km, 3) for e, r in sorted(self.routes.items())}
        return dict(assigned=inst.N - len(self.unassigned), unassigned=len(self.unassigned),
                    unassigned_urgent=sum(inst.tasks[j].priority == 3 for j in self.unassigned),
                    unassigned_high=sum(inst.tasks[j].priority >= 2 for j in self.unassigned),
                    engineers=len(self.routes), km=round(sum(r.km for r in self.routes.values()), 3),
                    km_per_engineer=per)

    def insert(self, inst, j, e, pos):
        r = self.routes.setdefault(e, RouteEval(inst, e, []))
        r.seq.insert(pos, j)
        r.recompute(inst)
        self.unassigned.discard(j)

    def remove(self, inst, j, where=None):
        e = where if where is not None else self.task_owner().get(j)
        if e is None:
            return
        r = self.routes[e]
        r.seq.remove(j)
        if r.seq:
            r.recompute(inst)
        else:
            del self.routes[e]
        self.unassigned.add(j)

    def task_owner(self):
        return {j: e for e, r in self.routes.items() for j in r.seq}


def validate(inst: Instance, sol: Solution) -> list:
    errors, seen = [], set()
    for e, route in sol.routes.items():
        if not RouteEval(inst, e, route.seq).ok:
            errors.append(f'{inst.engineers[e].id}: incompatible route, stock, window or shift violation')
        for j in route.seq:
            if j in seen:
                errors.append(f'{inst.tasks[j].id}: assigned twice')
            seen.add(j)
    if seen | sol.unassigned != set(range(inst.N)) or seen & sol.unassigned:
        errors.append('Tasks missing or duplicated')
    return errors


def schedule(inst: Instance, e: int, seq: list):
    eng, route = inst.engineers[e], RouteEval(inst, e, seq)
    result, prev, ready = [], eng.idx, eng.shift_start
    for k, j in enumerate(seq):
        node = inst.node(j)
        minutes, km = inst.ttl[eng.mode][prev][node], inst.kml[eng.mode][prev][node]
        start = route.E[k]
        arrival = ready + minutes
        result.append(dict(task=j, arrival=arrival, start=start, end=start + inst.tasks[j].service,
                           leg_km=km, leg_min=minutes, wait=start - arrival))
        ready, prev = start + inst.tasks[j].service, node
    return result
