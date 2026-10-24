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
    eligible_codes: Iterable[str],
    pool_codes: Iterable[str],
    strengths: Mapping[str, Mapping],
    *,
    top_n: int = 10,
) -> dict:
    """Rank each window independently; the caller applies structure/risk gates."""
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n <= 0:
        raise ValueError('top_n must be a positive integer')
    eligible = set(eligible_codes)
    outside = eligible - set(pool_codes)
    if outside:
        raise ValueError('Eligible codes outside Pool: ' + ', '.join(sorted(outside)))
    values = {
        code: {window: _number(strengths.get(code, {}).get(f'rps{window}_market'))
               for window in (5, 20, 60)}
        for code in eligible
    }

    def rank(windows):
        primary = windows[0]
        codes = [code for code in eligible if values[code][primary] is not None]
        return sorted(codes, key=lambda code: tuple(
            -values[code][window] if values[code][window] is not None else float('inf')
            for window in windows
        ) + (code,))[:top_n]

    rps5, rps20 = rank((5, 20, 60)), rank((20, 60))
    return {
        'top_n_per_window': top_n,
        'eligible_count': len(eligible),
        'rps5_top10': rps5,
        'rps20_top10': rps20,
        'overlap_codes': sorted(set(rps5) & set(rps20)),
        'union_codes': sorted(set(rps5) | set(rps20)),
        'missing_rps5': sorted(code for code in eligible if values[code][5] is None),
        'missing_rps20': sorted(code for code in eligible if values[code][20] is None),
    }
