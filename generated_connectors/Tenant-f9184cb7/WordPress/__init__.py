"""
Shielva WordPress Connector

Syncs posts, pages, users, media, categories, and tags from a WordPress site
via the WordPress REST API v2 using Application Passwords (WP 5.6+).
"""
from connector import CONNECTOR_TYPE, AUTH_TYPE, WordPressConnector

__all__ = ["WordPressConnector", "CONNECTOR_TYPE", "AUTH_TYPE"]
