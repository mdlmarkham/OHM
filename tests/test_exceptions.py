"""Tests for OHM exception hierarchy and error handling."""

from ohm.exceptions import (
    EXIT_CODES,
    AuthenticationError,
    ConfigurationError,
    DaemonNotRunningError,
    EdgeNotFoundError,
    GraphNotFoundError,
    NodeNotFoundError,
    OHMError,
    PermissionDeniedError,
    ValidationError,
)


class TestExceptionHierarchy:
    """Tests for exception types and exit codes."""

    def test_base_exception(self):
        error = OHMError("test error")
        assert str(error) == "test error"
        assert error.exit_code == 1

    def test_base_exception_with_correlation_id(self):
        error = OHMError("test error", correlation_id="abc-123")
        assert error.correlation_id == "abc-123"

    def test_daemon_not_running(self):
        error = DaemonNotRunningError("ohmd is not running")
        assert error.exit_code == 2

    def test_graph_not_found(self):
        error = GraphNotFoundError("database not found")
        assert error.exit_code == 2

    def test_authentication_error(self):
        error = AuthenticationError("invalid token")
        assert error.exit_code == 3

    def test_permission_denied(self):
        error = PermissionDeniedError("cannot modify another agent's edge")
        assert error.exit_code == 4

    def test_node_not_found(self):
        error = NodeNotFoundError("node not found")
        assert error.exit_code == 5

    def test_edge_not_found(self):
        error = EdgeNotFoundError("edge not found")
        assert error.exit_code == 5

    def test_validation_error(self):
        error = ValidationError("invalid confidence value")
        assert error.exit_code == 1

    def test_configuration_error(self):
        error = ConfigurationError("missing config file")
        assert error.exit_code == 1

    def test_exit_codes_dict(self):
        assert EXIT_CODES[0] == "Success"
        assert EXIT_CODES[4] == "Permission denied"
        assert EXIT_CODES[5] == "Node or edge not found"


class TestErrorInheritance:
    """Tests that all custom errors inherit from OHMError."""

    def test_isinstance_checks(self):
        assert isinstance(DaemonNotRunningError("x"), OHMError)
        assert isinstance(GraphNotFoundError("x"), OHMError)
        assert isinstance(AuthenticationError("x"), OHMError)
        assert isinstance(PermissionDeniedError("x"), OHMError)
        assert isinstance(NodeNotFoundError("x"), OHMError)
        assert isinstance(EdgeNotFoundError("x"), OHMError)
        assert isinstance(ValidationError("x"), OHMError)
        assert isinstance(ConfigurationError("x"), OHMError)
