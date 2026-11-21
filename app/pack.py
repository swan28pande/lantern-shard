"""Split tests across workers to minimise (busiest worker + 2 s per worker)."""

WORKER_MS = 2000


def pack(tests: list[int], ms: dict[int, int]) -> tuple[int, list[list[int]]]:
    """Returns (cost_ms, workers)."""
    if not tests:
        return 0, []
    order = sorted(tests, key=lambda n: -ms[n])
    total = sum(ms[n] for n in order)
    best = None
    for k in range(1, 9):
        if k > len(order):
            break
        lower = max(ms[order[0]], -(-total // k)) + WORKER_MS * k
        if best is not None and lower >= best[0]:
            continue
        loads = [0] * k
        workers: list[list[int]] = [[] for _ in range(k)]
        for n in order:
            i = loads.index(min(loads))
            loads[i] += ms[n]
            workers[i].append(n)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, workers)
    return best  # type: ignore[return-value]
