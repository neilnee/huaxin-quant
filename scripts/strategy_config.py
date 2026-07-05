"""
Strategy configuration loader.

Strategy JSON files contain tunable thresholds and weights. Algorithms remain
in model scripts so parameter changes do not require editing core flow code.
"""

import json
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
STRATEGY_DIR = SOURCE_ROOT / "strategies"


def load_strategy_config(filename):
    path = STRATEGY_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"strategy config not found: {path}")
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    version = config.get("strategy_version")
    if not version:
        raise ValueError(f"strategy_version missing in {path}")
    return config, str(path)
