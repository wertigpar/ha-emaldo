"""Sanity checks for realtime E2E power-flow readings."""

from __future__ import annotations

from typing import Any

# Conservative generic bound used as a second-stage safety net.
# The protocol parser already rejects absurd payloads; this protects entity
# updates in case a malformed-but-parseable packet still slips through.
REALTIME_POWER_ABS_MAX_W = 500_000

# Only these channels feed Home Assistant realtime entities today.
# Outliers on unused/raw channels must not block the whole payload.
REALTIME_POWER_ENTITY_KEYS: tuple[str, ...] = (
    "battery_w",
    "solar_w",
    "grid_w",
    "ev_w",
    "dual_power_w",
)

# Parsed realtime channels that are currently not mapped to HA entities.
# Kept for diagnostics/future use; they are intentionally non-blocking.
REALTIME_POWER_AUX_KEYS: tuple[str, ...] = (
    "addition_load_w",
    "other_load_w",
    "ip2_w",
    "op2_w",
)


def get_invalid_realtime_power_channels(
    data: dict[str, Any],
    *,
    abs_max_w: int = REALTIME_POWER_ABS_MAX_W,
    keys: tuple[str, ...] = REALTIME_POWER_ENTITY_KEYS,
) -> list[str]:
    """Return channel names whose values are invalid/out of sane range.

    A channel is considered invalid when it exists but is not numeric, or when
    ``abs(value) > abs_max_w``.
    """
    invalid: list[str] = []
    for key in keys:
        if key not in data:
            continue
        value = data[key]
        if value is None:
            continue
        if not isinstance(value, (int, float)):
            invalid.append(key)
            continue
        if abs(float(value)) > abs_max_w:
            invalid.append(key)
    return invalid
