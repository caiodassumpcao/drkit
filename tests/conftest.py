import pytest


def pytest_configure(config: pytest.Config) -> None:
    # The coverage gate measures unit tests; an integration-only run covers a slice by design.
    cov = config.pluginmanager.getplugin("_cov")
    if cov is not None and config.option.markexpr == "integration":
        cov.options.cov_fail_under = 0
