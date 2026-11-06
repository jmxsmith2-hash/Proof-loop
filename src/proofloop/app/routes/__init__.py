"""The Loop files & Printers API, one module per feature.

Each module has ``register(api, app, guard, allow_send)``: it adds its routes to ``api``. ``guard`` is the
access-token dependency every route lists, and ``allow_send`` is False for phone access (a paired phone can
watch, never start a print). To add a feature, write a module like these and add it to ``MODULES``.

The order is the order routes are added in, which matters where two paths could match the same URL.
"""

from __future__ import annotations

from proofloop.app.routes import (
    backup,
    batch,
    camera,
    check,
    costs,
    exports,
    filament,
    history,
    homeassistant,
    journal,
    maintenance,
    monitor,
    notifications,
    orders,
    overview,
    plates,
    profiles,
    queue,
    slicing,
    state,
    summary,
)

MODULES = [
    state,
    plates,
    slicing,
    queue,
    summary,
    orders,
    exports,
    costs,
    batch,
    journal,
    history,
    check,
    profiles,
    monitor,
    camera,
    notifications,
    homeassistant,
    filament,
    maintenance,
    overview,
    backup,
]
