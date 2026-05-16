"""OHM exception hierarchy with exit codes."""


class OHMError(Exception):
    """Base exception for all OHM errors."""

    exit_code = 1

    def __init__(self, message: str, correlation_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.correlation_id = correlation_id

    def __str__(self) -> str:
        if self.correlation_id:
            return f"{self.message} (correlation_id={self.correlation_id})"
        return self.message


class DaemonNotRunningError(OHMError):
    """ohmd daemon is not running."""

    exit_code = 2


class GraphNotFoundError(OHMError):
    """Database or graph not found."""

    exit_code = 2


class AuthenticationError(OHMError):
    """Authentication failure (invalid token)."""

    exit_code = 3


class PermissionDeniedError(OHMError):
    """Permission denied (boundary enforcement)."""

    exit_code = 4


class NodeNotFoundError(OHMError):
    """Node not found in the graph."""

    exit_code = 5


class EdgeNotFoundError(OHMError):
    """Edge not found in the graph."""

    exit_code = 5


class ValidationError(OHMError):
    """Invalid input data."""

    exit_code = 1


class ConfigurationError(OHMError):
    """Configuration error."""

    exit_code = 1


EXIT_CODES = {
    0: "Success",
    1: "General error",
    2: "Graph not found or daemon not running",
    3: "Authentication error",
    4: "Permission denied",
    5: "Node or edge not found",
}