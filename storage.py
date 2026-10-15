"""JSON-file persistence for Sync Doctor.

The whole app state is one plain dict ("db") with these keys:
    operators, vehicles, turo_trips, onenow_bookings, blocked_dates, tickets, runs

`data/seed/` holds the pristine demo data (committed to git). `data/*.json` are the
live working copies the app reads and writes; they are created from the seed on first
start and can be restored with `reset()` (the "Reset demo data" button).
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).parent / "data"

# db key -> file name. onenow_bookings.json holds two keys, so it is handled separately.
SIMPLE_FILES = {
    "operators": "operators.json",
    "vehicles": "vehicles.json",
    "turo_trips": "turo_trips.json",
    "tickets": "tickets.json",
    "runs": "runs.json",
}
ONENOW_FILE = "onenow_bookings.json"
ALL_FILES = [*SIMPLE_FILES.values(), ONENOW_FILE]


def _read(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_atomic(path: Path, data) -> None:
    """Write to a temp file then rename, so a crash never leaves half a JSON file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def load_dir(directory: Path | str) -> dict:
    """Load a full db dict from a directory of JSON files."""
    directory = Path(directory)
    db = {key: _read(directory / name) for key, name in SIMPLE_FILES.items()}
    onenow = _read(directory / ONENOW_FILE)
    db["onenow_bookings"] = onenow["bookings"]
    db["blocked_dates"] = onenow["blocked_dates"]
    return db


class Storage:
    def __init__(self, data_dir: Path | str = DEFAULT_DATA_DIR, seed_dir: Path | str | None = None):
        self.data_dir = Path(data_dir)
        self.seed_dir = Path(seed_dir) if seed_dir else DEFAULT_DATA_DIR / "seed"

    def ensure(self) -> None:
        """Create any missing live file from the seed."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name in ALL_FILES:
            if not (self.data_dir / name).exists():
                shutil.copyfile(self.seed_dir / name, self.data_dir / name)

    def reset(self) -> None:
        """Throw away live changes and restore the demo data."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name in ALL_FILES:
            shutil.copyfile(self.seed_dir / name, self.data_dir / name)

    def load(self) -> dict:
        return load_dir(self.data_dir)

    def save(self, db: dict) -> None:
        for key, name in SIMPLE_FILES.items():
            _write_atomic(self.data_dir / name, db[key])
        _write_atomic(
            self.data_dir / ONENOW_FILE,
            {"bookings": db["onenow_bookings"], "blocked_dates": db["blocked_dates"]},
        )
