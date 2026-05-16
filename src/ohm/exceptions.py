"""OHM exception hierarchy and exit codes.

Exit codes (from docs/cli.md):
    0: Success
    1: General error
    2: Graph not found or ohmd not running
    3: Authentication error (invalid token)
    4: Permission denied (trying to overwrite another agent's edge)
    5: Node or edge not found
"""


class OHMError(Exception):
    """Base exception for all OHM errors."""
    exit_code: int = 1

    def __init__(self, message: str, *, correlation_id: str | None = None):
        self.correlation_id = correlation_id
        super().__init__(message)


class DaemonNotRunningError(OHMError):
    """Raised when ohmd is not running but is required."""
    exit_code = 2


class GraphNotFoundError(OHMError):
    """Raised when the graph database cannot be found or opened."""
    exit_code = 2


class AuthenticationError(OHMError):
    """Raised when token authentication fails."""
    exit_code = 3


class PermissionDeniedError(OHMError):
    """Raised when an agent tries to modify another agent's edge."""
    exit_code = 4


class NodeNotFoundError(OHMError):
    """Raised when a referenced node does not exist."""
    exit_code = 5


class EdgeNotFoundError(OHMError):
    """Raised when a referenced edge does not exist."""
    exit_code = 5


class ValidationError(OHMError):
    """Raised when input validation fails."""
    exit_code = 1


class ConfigurationError(OHMError):
    """Raised when configuration is invalid or missing."""
    exit_code = 1


EXIT_CODES = {
    0: "Success",
    1: "General error",
    2: "Graph not found or ohmd not running",
    3: "Authentication error",
    4: "Permission denied",
    5: "Node or edge not found",
}
