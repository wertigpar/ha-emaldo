"""UID-base election helpers (#73).

Pure module — no homeassistant imports, so ``tests/test_uid_base.py`` can
load it standalone.

Background: before #68 each config entry derived every entity's unique_id
with a ``home_id`` prefix. #68 switched to device-scoped ids but had to keep
migrating an entry's entity set when legacy home_id uids already existed in
its own registry (scoped per entry — several entries can share a home_id).

The beta41 fix makes the scheme sticky in ``entry.data[CONF_UID_BASE]``:

* fresh entries are born ``"device"`` (config_flow stamps it),
* existing v2 entries get a one-time derivation during ``entry.version``
  migration v2 -> v3 (``async_migrate_entry``), and
* ``legacy_mode`` decides per slot: only a *primary* coordinator on a
  ``"home"`` entry keeps home_id uids; every additional device coordinator
  is device-scoped (true since #68).

The Op-A guard (id-less primary module) also lives on this seam:
``first_valid_device`` picks the rebind target — a device is only a usable
primary when it carries a non-empty string ``id``.
"""

from __future__ import annotations

from typing import Iterable

UID_BASE_HOME = "home"
UID_BASE_DEVICE = "device"


def has_legacy_uid(uid: object, home_id: str) -> bool:
    """True when one unique id uses the pre-#68 home_id-prefixed scheme."""
    return isinstance(uid, str) and uid.startswith(f"{home_id}_")


def any_legacy_uids(existing_uids: Iterable[object], home_id: str) -> bool:
    """True when any registry unique id on this entry is legacy."""
    return any(has_legacy_uid(uid, home_id) for uid in existing_uids)


def derive_uid_base(existing_uids: Iterable[object], home_id: str) -> str:
    """One-time sticky-marker derivation.

    Legacy home_id uids in the entry's own registry -> ``"home"`` (keep the
    existing entity set stable); otherwise -> ``"device"``.
    """
    return UID_BASE_HOME if any_legacy_uids(existing_uids, home_id) else UID_BASE_DEVICE


def legacy_mode(uid_base: str | None, *, is_primary: bool) -> bool:
    """Legacy UID mode for one coordinator slot.

    Only the primary slot of a ``"home"`` entry runs the home_id scheme.
    Non-primary slots are always device-scoped (#68 secondary-battery rule).
    """
    return uid_base == UID_BASE_HOME and is_primary


def is_valid_device(device: dict) -> bool:
    """True when a backend device dict carries a usable non-empty string id.

    The backend can return unprovisioned modules whose ``id`` is missing or
    empty; those must never become (or stay) the primary device (#73).
    """
    id_raw = device.get("id")
    return isinstance(id_raw, str) and bool(id_raw.strip())


def first_valid_device(devices: Iterable[dict]) -> dict | None:
    """First backend device with a usable id (the Op-A rebind target)."""
    return next((d for d in devices if is_valid_device(d)), None)