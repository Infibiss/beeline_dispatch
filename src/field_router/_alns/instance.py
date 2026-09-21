"""Internal TRSP data model adapted from the teammate kernel; see PROVENANCE.md.

Nodes 0..E-1 are engineer starts; E..E+N-1 are jobs. Travel matrices are
mandatory: this backend must use exactly the travel model of the application.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np


@dataclass
class Task:
    idx: int
    id: str
    lat: float
    lon: float
    a: int
    b: int
    service: int
    skill: object
    transport: str | None
    priority: int  # 1 normal, 2 high, 3 urgent
    type: str = ''
    hd: str = ''
    district: str = ''
    address: str = ''
    windows: tuple[tuple[int, int], ...] | None = None
    equipment: dict[str, int] = field(default_factory=dict)

    @property
    def service_windows(self):
        return self.windows if self.windows is not None else ((self.a, self.b),)


@dataclass
class Engineer:
    idx: int
    id: str
    name: str
    lat: float
    lon: float
    shift_start: int
    shift_end: int
    skills: frozenset
    mode: str
    start_label: str = 'Office'
    stock: dict[str, int] | None = None


@dataclass
class Instance:
    name: str
    tasks: list[Task]
    engineers: list[Engineer]
    matrices: dict | None = field(repr=False, default=None)
    km: np.ndarray = field(repr=False, default=None)
    km_mode: dict = field(repr=False, default=None)
    tt: dict = field(repr=False, default=None)
    profile: str | None = None
    prev_owner: dict[int, int] = field(default_factory=dict)
    prev_start: dict[int, int] = field(default_factory=dict)
    response_targets: set[int] | None = None

    @property
    def target_tasks(self):
        return (self.response_targets if self.response_targets is not None
                else {j for j, task in enumerate(self.tasks) if task.priority == 3})

    @property
    def objective_tiers(self):
        if self.profile is None:
            return ('engineers', 'distance_m')
        stability = ('reassigned', 'absolute_start_shift')
        travel = ('travel_minutes', 'distance_m')
        if self.profile == 'cost':
            return travel + stability + ('engineers',)
        if self.profile == 'sla':
            return ('response_score',) + stability + travel + ('engineers',)
        return stability + travel + ('engineers',)

    @property
    def E(self):
        return len(self.engineers)

    @property
    def N(self):
        return len(self.tasks)

    def node(self, task_idx):
        return self.E + task_idx

    def build_matrices(self):
        if self.profile not in (None, 'cost', 'sla', 'stable'):
            raise ValueError('Unknown objective profile')
        if any(t.priority not in (1, 2, 3) for t in self.tasks):
            raise ValueError('Task priorities must be 1, 2 or 3')
        n = self.E + self.N
        ext = self.matrices or {}
        ext_km, ext_min = ext.get('km', {}), ext.get('minutes', {})
        self.km_mode, self.tt = {}, {}
        for mode in {e.mode for e in self.engineers}:
            for label, matrices in (('km', ext_km), ('minutes', ext_min)):
                if mode not in matrices:
                    raise ValueError(f'Missing external {label} matrix for {mode}')
                matrix = np.asarray(matrices[mode], dtype=float)
                if matrix.shape != (n, n) or not np.isfinite(matrix).all() or (matrix < 0).any():
                    raise ValueError(f'Invalid {label}[{mode}] matrix; expected finite nonnegative {n}x{n}')
                if label == 'km':
                    self.km_mode[mode] = matrix
                else:
                    self.tt[mode] = np.ceil(matrix).astype(int)
        # A representative distance matrix is only used for Shaw relatedness.
        self.km = next(iter(self.km_mode.values()), np.zeros((n, n)))
        self.ttl = {m: a.tolist() for m, a in self.tt.items()}
        self.kml = {m: a.tolist() for m, a in self.km_mode.items()}
        # Optimistic closure only: service and intermediate eligibility are
        # ignored. Unlike a direct-start filter this is safe on nonmetric input.
        self.reachable = {}
        for mode, matrix in self.tt.items():
            shortest = matrix.copy()
            for k in range(n):
                np.minimum(shortest, shortest[:, k, None] + shortest[None, k, :], out=shortest)
            for e in self.engineers:
                if e.mode == mode:
                    self.reachable[e.idx] = [any(
                        max(e.shift_start + int(shortest[e.idx, self.node(t.idx)]), a)
                        <= min(b, e.shift_end - t.service)
                        for a, b in t.service_windows) for t in self.tasks]
        return self

    def compatible(self, e: Engineer, t: Task) -> bool:
        return (t.skill in e.skills and (t.transport is None or t.transport == e.mode)
                and (e.stock is None or all(t.equipment.get(k, 0) <= e.stock.get(k, 0)
                                           for k in ('router', 'tv_box'))))
