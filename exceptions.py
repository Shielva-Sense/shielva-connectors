"""
Gmail Connector — Custom Exception Hierarchy
All HTTP status code → exception mappings originate here.
connector.py catches ONLY these custom exceptions.
"""


class GmailConnectorError(Exception):
    """Base exception for all Gmail connector errors."""


class GmailAuthError(GmailConnectorError):
    """Raised for authentication failures: missing token, refresh failure, 401/403."""


class GmailAPIError(GmailConnectorError):
    """Raised for general Gmail API errors (e.g. 400 malformed MIME, 5xx server errors)."""


class GmailMessageNotFoundError(GmailConnectorError):
    """Raised when a message ID does not exist (HTTP 404)."""


class GmailRateLimitError(GmailConnectorError):
    """Raised when Gmail API returns HTTP 429 Too Many Requests."""


class GmailAttachmentError(GmailConnectorError):
    """Raised when total attachment size exceeds MAX_ATTACHMENT_SIZE_MB (25 MB)."""


class GmailValidationError(GmailConnectorError):
    """Raised when input validation fails (e.g. invalid email address format)."""


# HTTP status code → exception factory
def map_http_error(status_code: int, message_id: str = "", body: str = "") -> GmailConnectorError:
    """Map an HTTP status code to the appropriate domain exception."""
    if status_code == 400:
        return GmailAPIError("Invalid MIME content format for send operation")
    if status_code == 401:
        return GmailAuthError("OAuth token invalid or expired")
    if status_code == 403:
        return GmailAuthError("OAuth token lacks required Gmail API permissions")
    if status_code == 404:
        return GmailMessageNotFoundError(f"Invalid messageId: {message_id}")
    if status_code == 429:
        return GmailRateLimitError("Too many requests")
    if status_code >= 500:
        return GmailAPIError(f"Gmail API server error: {status_code}")
    return GmailAPIError(f"Unexpected Gmail API error: {status_code} — {body}")
