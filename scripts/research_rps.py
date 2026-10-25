"""Pool-bounded RPS rankings for manual opportunity screening."""

from collections.abc import Iterable, Mapping
from math import isfinite


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def select_pool_rps(
    pool_codes: Iterable[str],
    strengths: Mapping[str, Mapping],
    *,
    top_n: int = 10,
) -> dict:
    """Rank the full first-layer Pool; structure review follows selection."""
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n <= 0:
        raise ValueError('top_n must be a positive integer')
    pool = set(pool_codes)
    values = {
        code: {window: _number(strengths.get(code, {}).get(f'rps{window}_market'))
               for window in (5, 20, 60)}
        for code in pool
    }

    def rank(windows):
        primary = windows[0]
        codes = [code for code in pool if values[code][primary] is not None]
        return sorted(codes, key=lambda code: tuple(
            -values[code][window] if values[code][window] is not None else float('inf')
            for window in windows
        ) + (code,))[:top_n]

    rps5, rps20 = rank((5, 20, 60)), rank((20, 60))
    return {
        'top_n_per_window': top_n,
        'ranking_scope': 'full_pool',
        'pool_count': len(pool),
        'rps5_top10': rps5,
        'rps20_top10': rps20,
        'overlap_codes': sorted(set(rps5) & set(rps20)),
        'union_codes': sorted(set(rps5) | set(rps20)),
        'missing_rps5': sorted(code for code in pool if values[code][5] is None),
        'missing_rps20': sorted(code for code in pool if values[code][20] is None),
    }
