"""Shielva Rudderstack connector package."""
from connector import RudderstackConnector

# Back-compat alias — older code imported the old capitalisation.
RudderStackConnector = RudderstackConnector

__all__ = ["RudderstackConnector", "RudderStackConnector"]
