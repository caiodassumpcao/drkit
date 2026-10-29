import pytest

from drkit.sources.base import ItemResult, item_label


@pytest.mark.parametrize(
    ("kind", "project", "name", "label"),
    [
        ("db", "shop", "orders", "shop/db/orders"),
        ("db", "shop", "shop", "shop/db/shop"),
        ("project", "shop", "shop", "shop/project"),
        ("secrets", "shop", "shop", "shop/secrets"),
        ("system", None, "paths", "system/paths"),
    ],
)
def test_item_label(kind: str, project: str | None, name: str, label: str) -> None:
    assert item_label(kind, project, name) == label
    assert ItemResult(kind=kind, project=project, name=name, status="ok").label == label
