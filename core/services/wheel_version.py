"""Comparing connector wheel versions.

Its own module, import-free, for the reason install_gate gives: a rule that can
only be reached by importing the whole gateway gets reimplemented by its test,
and the copy then keeps passing while the real one drifts.

🚨 The rule this exists to hold: a pin that is OLDER than what is installed is
never acted on. The pinned version has two sources — the manifest baked into the
image and the catalog snapshot that overlays it at startup — and until the
overlay lands the baked one is authoritative, and it lags. Treating "different
version" as the trigger silently downgraded a connector in that window, undoing
the fix the newer wheel had been published to deliver.
"""

from __future__ import annotations


def version_tuple(version: str) -> tuple[int, ...]:
    """Comparable form of a version string; unparseable segments sort as 0."""
    parts: list[int] = []
    for chunk in str(version or "").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(pinned: str, installed: str) -> bool:
    """Whether `pinned` is a later version than `installed`.

    Compared segment by segment as integers, never as strings: "1.0.10" is newer
    than "1.0.9", and string order says the opposite.
    """
    return version_tuple(pinned) > version_tuple(installed)
