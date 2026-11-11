"""Optional integrations, discovered at start-up (ARCHITECTURE §4.6).

Each sub-package here is one integration with an outside system.  It exposes
``register(app)``, which mounts its routers (and anything else it needs) on the FastAPI
application.  :func:`register_all` imports whichever sub-packages are present and calls
that hook, so an integration that is not installed — a directory that is simply absent —
means no routes, no import and no error.

An integration that *is* present but fails to import is a real error and is raised: a
deployment that depends on it must not start quietly without it.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger(__name__)


def discover() -> list[str]:
    """The integration packages present on disk, by name, in a stable order."""
    return sorted(info.name for info in pkgutil.iter_modules(__path__) if info.ispkg)


def register_all(app: FastAPI) -> list[str]:
    """Import every present integration and call its ``register(app)``.  Returns their names."""
    registered = []
    for name in discover():
        module = importlib.import_module(f"{__name__}.{name}")
        register = getattr(module, "register", None)
        if register is None:
            log.warning("integration %s has no register(app); skipped", name)
            continue
        register(app)
        registered.append(name)
    if registered:
        log.info("integrations registered: %s", ", ".join(registered))
    return registered


__all__ = ["discover", "register_all"]
