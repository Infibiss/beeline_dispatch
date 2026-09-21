"""Feasibility and strict lexicographic objectives for open engineer routes."""
from __future__ import annotations
from dataclasses import dataclass, field
from .instance import Instance
from .scheduling import stable_starts

EPS = 1e-9


def earliest(task, arrival):
    best = float('inf')
    for a, b in task.service_windows:
        start = arrival if arrival >= a else a
        if start <= b + EPS and start < best:
            best = start
    return best


def latest(task, limit):
    best = -float('inf')
    for a, b in task.service_windows:
        start = limit if limit <= b else b
        if start >= a - EPS and start > best:
            best = start
    return best


class RouteEval:
    """Forward earliest / backward latest starts, with aggregate stock checks.

    Backward starts are projected onto the union of start windows. No triangle
    inequality is assumed, including when removing a task from an existing route.
    """
    __slots__ = ('e', 'seq', 'E', 'L', 'km', 'travel_minutes', 'ok', 'used', '_stable_starts', '_stable_reference')

    def __init__(self, inst: Instance, e: int, seq: list):
        self.e, self.seq = e, list(seq)
        self.recompute(inst)

    def recompute(self, inst: Instance):
        self._stable_starts = self._stable_reference = None
        eng = inst.engineers[self.e]
        tt, kmm, tasks = inst.ttl[eng.mode], inst.kml[eng.mode], inst.tasks
        n = len(self.seq)
        self.E, self.L, self.km = [0] * n, [0] * n, 0.0
        self.travel_minutes = 0
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
            self.travel_minutes += tt[prev_node][node]
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

    def copy(self):
        """Clone evaluated state without recomputing unchanged route bounds."""
        copied = object.__new__(RouteEval)
        copied.e, copied.km, copied.ok = self.e, self.km, self.ok
        copied.travel_minutes = self.travel_minutes
        copied.seq, copied.E, copied.L = self.seq.copy(), self.E.copy(), self.L.copy()
        copied.used = self.used.copy()
        # Cached schedules and reference snapshots are immutable tuples.
        copied._stable_starts, copied._stable_reference = self._stable_starts, self._stable_reference
        return copied

    def start_times(self, inst):
        """Actual schedule; E remains the earliest bound used by insertions."""
        if inst.profile is None or not self.ok:
            return self.E
        reference = tuple(inst.prev_start.get(j) for j in self.seq)
        target_tasks = inst.target_tasks if inst.profile == 'sla' else set()
        targets = tuple(j in target_tasks for j in self.seq) if inst.profile == 'sla' else ()
        signature = (inst.profile, reference, targets)
        if self._stable_starts is None or self._stable_reference != signature:
            self._stable_starts = stable_starts(inst, self.e, self.seq, self.E, self.L, reference, targets)
            self._stable_reference = signature
        return self._stable_starts

    def profile_key(self, inst):
        if inst.profile == 'sla':
            targets = inst.target_tasks
            return (sum(s for j, s in zip(self.seq, self.start_times(inst)) if j in targets),)
        if inst.profile in ('stable', 'cost'):
            return (sum(j in inst.prev_owner and inst.prev_owner[j] != self.e for j in self.seq),
                    sum(abs(s - inst.prev_start[j]) for j, s in zip(self.seq, self.start_times(inst))
                        if j in inst.prev_start))
        return ()

    def objective_values(self, inst):
        starts = self.start_times(inst)
        targets = inst.target_tasks
        return {'engineers': int(bool(self.seq)), 'distance_m': int(round(self.km * 1000)),
                'travel_minutes': self.travel_minutes,
                'reassigned': sum(j in inst.prev_owner and inst.prev_owner[j] != self.e for j in self.seq),
                'absolute_start_shift': sum(abs(s - inst.prev_start[j]) for j, s in zip(self.seq, starts)
                                            if j in inst.prev_start),
                'response_score': sum(s for j, s in zip(self.seq, starts) if j in targets)}

    def objective_key(self, inst):
        values = self.objective_values(inst)
        return tuple(values[tier] for tier in inst.objective_tiers)

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
            if inst.profile is not None:
                candidate = RouteEval(inst, self.e, self.seq[:p] + [j] + self.seq[p:])
                key = candidate.objective_key(inst)
            if best_key is None or key < best_key:
                best, best_key = (delta, p), key
        return best


@dataclass
class Solution:
    routes: dict = field(default_factory=dict)
    unassigned: set = field(default_factory=set)

    def copy(self, inst):
        return Solution({e: r.copy() for e, r in self.routes.items()}, set(self.unassigned))

    def objective_key(self, inst: Instance) -> tuple:
        coverage = (sum(inst.tasks[j].priority == 3 for j in self.unassigned),
                    sum(inst.tasks[j].priority >= 2 for j in self.unassigned), len(self.unassigned))
        values = [r.objective_values(inst) for r in self.routes.values()]
        totals = {tier: sum(value[tier] for value in values) for tier in inst.objective_tiers}
        if inst.profile == 'sla':
            totals['response_score'] += 1441 * len(self.unassigned & inst.target_tasks)
        return coverage + tuple(totals[tier] for tier in inst.objective_tiers)

    def cost(self, inst: Instance) -> tuple:
        """An exact ordering key; never a fixed-ratio priority penalty."""
        return self.objective_key(inst)

    def metrics(self, inst: Instance) -> dict:
        per = {inst.engineers[e].id: round(r.km, 3) for e, r in sorted(self.routes.items())}
        return dict(assigned=inst.N - len(self.unassigned), unassigned=len(self.unassigned),
                    unassigned_urgent=sum(inst.tasks[j].priority == 3 for j in self.unassigned),
                    unassigned_high=sum(inst.tasks[j].priority >= 2 for j in self.unassigned),
                    engineers=len(self.routes), km=round(sum(r.km for r in self.routes.values()), 3),
                    travel_minutes=sum(r.travel_minutes for r in self.routes.values()),
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
    starts = route.start_times(inst)
    result, prev, ready = [], eng.idx, eng.shift_start
    for k, j in enumerate(seq):
        node = inst.node(j)
        minutes, km = inst.ttl[eng.mode][prev][node], inst.kml[eng.mode][prev][node]
        start = starts[k]
        arrival = ready + minutes
        result.append(dict(task=j, arrival=arrival, start=start, end=start + inst.tasks[j].service,
                           leg_km=km, leg_min=minutes, wait=start - arrival))
        ready, prev = start + inst.tasks[j].service, node
    return result
