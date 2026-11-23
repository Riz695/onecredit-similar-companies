import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything under tests/integration, so `pytest -m "not integration"` runs without Docker."""
    for item in items:
        if "integration" in item.path.parts:
            item.add_marker(pytest.mark.integration)
