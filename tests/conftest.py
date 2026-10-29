from collections.abc import Iterator

import pytest
import structlog


def pytest_configure(config: pytest.Config) -> None:
    # The coverage gate measures unit tests; an integration-only run covers a slice by design.
    cov = config.pluginmanager.getplugin("_cov")
    if cov is not None and config.option.markexpr == "integration":
        cov.options.cov_fail_under = 0


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    # CLI tests point structlog at the runner's stderr, which is closed after each invocation.
    yield
    structlog.reset_defaults()
