import re

import drkit


def test_version_is_pep440_like() -> None:
    assert re.match(r"^\d+\.\d+\.\d+", drkit.__version__)
