

class NencartaBaseException(Exception):
    """Base exception for nencarta."""

class NoStreamsFoundException(NencartaBaseException):
    """Raised when no streams are found in the input geometry."""
