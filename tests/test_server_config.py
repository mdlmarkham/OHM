"""Tests for ohmd config-path behavior (OHM-ylkf)."""

import json
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    return tmp_path / "ohmd.json"


def test_ohmd_init_token_uses_config_flag(tmp_config: Path) -> None:
    """--init-token must write to the path supplied via --config."""
    result = subprocess.run(
        [
            "python3",
            "-m",
            "ohm.server",
            "--config",
            str(tmp_config),
            "--init-token",
            "test-agent",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert f"Config saved to {tmp_config}" in result.stdout
    assert tmp_config.exists()

    data = json.loads(tmp_config.read_text())
    assert "test-agent" in data.get("tokens", {})
    assert "hash" in data["tokens"]["test-agent"]

    # Ensure the default config path was NOT created
    default_config = Path.home() / ".ohm" / "ohmd.json"
    if default_config.exists():
        # If a default config already exists, make sure it was not modified by this test
        # by checking that the token only exists in the supplied path. We cannot safely
        # assert non-existence of the default file if tests share a home directory.
        default_data = json.loads(default_config.read_text())
        assert "test-agent" not in default_data.get("tokens", {}), (
            "--init-token wrote token to default config despite --config"
        )


def test_ohmd_init_customer_token_uses_config_flag(tmp_config: Path) -> None:
    """--init-customer-token must write to the path supplied via --config."""
    result = subprocess.run(
        [
            "python3",
            "-m",
            "ohm.server",
            "--config",
            str(tmp_config),
            "--init-customer-token",
            "test-customer",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert f"Config saved to {tmp_config}" in result.stdout
    assert tmp_config.exists()

    data = json.loads(tmp_config.read_text())
    assert "test-customer" in data.get("customer_tokens", {})
    assert "hash" in data["customer_tokens"]["test-customer"]
