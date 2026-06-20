"""Copper CRM connector helpers package."""

from .utils import (
    normalize_company,
    normalize_opportunity,
    normalize_person,
    normalize_task,
    with_retry,
)

__all__ = [
    "normalize_person",
    "normalize_company",
    "normalize_opportunity",
    "normalize_task",
    "with_retry",
]
