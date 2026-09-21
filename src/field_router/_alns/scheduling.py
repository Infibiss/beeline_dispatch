"""Exact minute scheduling for response time, then absolute reference deviations."""
from __future__ import annotations

import numpy as np


def stable_starts(inst, e, seq, earliest, latest, reference, targets=()):
    """Minimize targeted response first, then deviation in disjoint windows.

    The feasibility bounds already include the engineer's actual start location,
    residual shift, all service/travel durations, and the open route's shift end.
    For each job and permitted start minute t, the dynamic program stores
    |t-reference| + W*t (for response targets) + the best predecessor cost.
    W exceeds the maximum total deviation, preserving exact lexicographic order.
    Prefix minima make this O(n H + W) time and O(n H) traceback memory for
    n jobs, shift horizon H and W window intervals; application H is <= 1440.
    A feasible reference schedule takes only O(n + W) time and no DP arrays.
    """
    if not seq or all(value is None for value in reference):
        return tuple(earliest)
    engineer, tasks = inst.engineers[e], inst.tasks
    travel = inst.ttl[engineer.mode]

    # Every target's earliest bound and zero deviation elsewhere give a lower
    # bound. Preserve that schedule without allocating DP arrays when feasible.
    # Unreferenced jobs use their earliest starts so they cannot block a later
    # reference that another placement of these same jobs could have preserved.
    result, ready, previous = [], engineer.shift_start, engineer.idx
    for k, j in enumerate(seq):
        task = tasks[j]
        arrival = ready + travel[previous][inst.node(j)]
        wanted = earliest[k] if targets and targets[k] else reference[k]
        start = wanted if wanted is not None else min(
            (max(arrival, a) for a, b in task.service_windows if max(arrival, a) <= b),
            default=float('inf'))
        if (start < arrival or start > latest[k]
                or not any(a <= start <= b for a, b in task.service_windows)):
            break
        result.append(start)
        ready, previous = start + task.service, inst.node(j)
    else:
        return tuple(result)

    deviation_bound = sum(max(abs(earliest[k] - value), abs(latest[k] - value))
                          for k, value in enumerate(reference) if value is not None)
    response_weight = int(deviation_bound) + 1
    infinity = np.iinfo(np.int64).max // 4
    parents, offsets = [], []
    costs = None
    for k, j in enumerate(seq):
        lower, upper = int(earliest[k]), int(latest[k])
        times = np.arange(lower, upper + 1, dtype=np.int64)
        edges = np.zeros(len(times) + 1, dtype=np.int64)
        for a, b in tasks[j].service_windows:
            first, last = max(lower, a) - lower, min(upper, b) - lower
            if first <= last:
                edges[first] += 1
                edges[last + 1] -= 1
        allowed = np.cumsum(edges[:-1]) > 0
        local = (np.zeros(len(times), dtype=np.int64) if reference[k] is None
                 else np.abs(times - reference[k]))
        if targets and targets[k]:
            local += response_weight * times
        if k:
            # Strict improvement preserves the earliest predecessor on ties.
            prefix = np.minimum.accumulate(costs)
            improvements = np.empty(len(costs), dtype=bool)
            improvements[0] = True
            improvements[1:] = costs[1:] < prefix[:-1]
            best_indices = np.maximum.accumulate(np.where(improvements, np.arange(len(costs)), 0))
            lag = tasks[seq[k - 1]].service + travel[inst.node(seq[k - 1])][inst.node(j)]
            limits = np.minimum(times - lag - offsets[-1], len(costs) - 1)
            # Forward earliest bounds guarantee limits >= 0.
            parent = best_indices[limits]
            local = np.minimum(infinity, local + prefix[limits])
            parents.append(parent)
        else:
            parents.append(None)
        local[~allowed] = infinity
        costs = local
        offsets.append(lower)

    chosen = int(np.argmin(costs))
    result = [0] * len(seq)
    for k in range(len(seq) - 1, -1, -1):
        result[k] = offsets[k] + chosen
        if k:
            chosen = int(parents[k][chosen])
    return tuple(result)
