"""The source registry, in its own module to keep imports acyclic.

``main`` needs it to build sources, ``introspection`` needs it to render the
URLs a chat will actually hit, and ``chat_config`` needs it to generate those
URLs in the first place. Importing it from ``main`` would make
``scanner.* -> main -> scanner.*`` circular, so it lives here.

Adding a source: import it, add one line to :data:`SOURCE_REGISTRY`, and give
it a block under ``sources:`` in ``config.yml``.
"""

from __future__ import annotations

from typing import Dict, Type

from .sources.base import BaseSource
from .sources.komornik import KomornikSource
from .sources.morizon import MorizonSource
from .sources.olx import OlxSource
from .sources.otodom import OtodomSource

SOURCE_REGISTRY: Dict[str, Type[BaseSource]] = {
    "otodom": OtodomSource,
    "olx": OlxSource,
    "morizon": MorizonSource,
    "komornik": KomornikSource,
}

#: Names shown to users in ``/help`` and the dashboard's source pickers.
KNOWN_SOURCES = tuple(SOURCE_REGISTRY)
