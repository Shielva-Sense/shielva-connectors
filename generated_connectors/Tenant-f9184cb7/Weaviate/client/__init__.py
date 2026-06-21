"""HTTP client subpackage — single owner of httpx for the Weaviate connector."""
from client.http_client import WeaviateHTTPClient

__all__ = ["WeaviateHTTPClient"]
