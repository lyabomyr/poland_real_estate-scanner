"""Load ``.env`` into ``os.environ`` for local development.

Turso credentials are mandatory (see :mod:`scanner.storage`), and exporting
two long tokens in every shell is friction that invites shortcuts. This reads
a ``.env`` file if one exists next to the project root.

Deliberately dependency-free and deliberately non-overriding: a real
environment variable always wins, so GitHub Actions and Streamlit Cloud —
which inject secrets as env vars — are never affected by a stray file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Optional[Path] = None) -> int:
    """Merge ``KEY=value`` lines from ``.env`` into the environment.

    Returns how many variables were set. Missing file is not an error —
    hosted environments have no ``.env`` and don't need one.
    """
    env_path = path or (_PROJECT_ROOT / ".env")
    if not env_path.exists():
        return 0

    applied = 0
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\'\"")
        # Never clobber a real env var — the host always wins.
        if key and key not in os.environ:
            os.environ[key] = value
            applied += 1
    return applied
