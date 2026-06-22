"""Odoo connector helpers (normalizer + retry utilities)."""
from helpers.normalizer import normalize_lead, normalize_partner
from helpers.utils import with_retry

__all__ = ["normalize_lead", "normalize_partner", "with_retry"]
