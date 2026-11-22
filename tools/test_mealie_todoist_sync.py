#!/usr/bin/env python3
"""Unit tests for tools/mealie_todoist_sync.py.

All HTTP and Todoist API calls are mocked. Zero real network I/O or private IPs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Self
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.mealie_todoist_sync as mts


class _FakeHTTPResponse:
    def __init__(self, payload: Any) -> None:
        self._bytes = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._bytes

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def _mock_mealie_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEALIE_BASE_URL", "https://mealie.example.test")
    monkeypatch.setenv("MEALIE_API_TOKEN", "test-mealie-token")


def test_recipe_item_added_to_whole_foods_and_checked_in_mealie() -> None:
    """A recipe-sourced non-pantry ingredient is added to Todoist Whole Foods and checked off in Mealie."""
    mealie_item = {
        "id": "item-avocado-1",
        "shoppingListId": "list-uuid-1",
        "display": "2 large avocados",
        "quantity": 2,
        "checked": False,
        "recipeReferences": [{"recipeId": "rec-123"}],
    }

    with (
        patch.object(mts, "resolve_shopping_list_id", return_value="list-uuid-1"),
        patch.object(mts, "fetch_unchecked_items", return_value=[mealie_item]),
        patch.object(mts, "load_pantry_exclusions", return_value={"kosher salt", "black pepper", "olive oil"}),
        patch.object(mts, "load_staples", return_value=[]),
        patch.object(mts, "get_pending_groceries", return_value=[]) as mock_pending,
        patch.object(mts, "add_grocery_item", return_value={"id": "td-1", "title": "2 large avocados"}) as mock_add,
        patch.object(mts, "mark_item_checked", return_value={"id": "item-avocado-1", "checked": True}) as mock_check,
    ):
        res = mts.sync_mealie_to_todoist()

    assert res["ok"] is True
    assert len(res["added"]) == 1
    assert res["added"][0]["id"] == "item-avocado-1"
    assert res["added"][0]["title"] == "2 large avocados"
    assert res["added"][0]["store"] == "Whole Foods"
    assert res["skipped_pantry"] == []
    assert res["skipped_duplicate"] == []
    assert res["checked_in_mealie"] == 1
    assert res["errors"] == []

    mock_pending.assert_called_once_with(title="Whole Foods")
    mock_add.assert_called_once_with(
        "2 large avocados",
        notes="Synced from Mealie",
        list_title="Whole Foods",
    )
    mock_check.assert_called_once_with(mealie_item)


def test_recipe_pantry_staple_skipped_and_checked_in_mealie() -> None:
    """A recipe-sourced baseline pantry staple is skipped from Todoist and checked off in Mealie."""
    pantry_item = {
        "id": "item-salt-1",
        "shoppingListId": "list-uuid-1",
        "display": "1 tsp kosher salt",
        "food": {"name": "kosher salt"},
        "checked": False,
        "recipeReferences": [{"recipeId": "rec-123"}],
    }

    with (
        patch.object(mts, "resolve_shopping_list_id", return_value="list-uuid-1"),
        patch.object(mts, "fetch_unchecked_items", return_value=[pantry_item]),
        patch.object(mts, "load_pantry_exclusions", return_value={"kosher salt", "black pepper", "cumin"}),
        patch.object(mts, "load_staples", return_value=[]),
        patch.object(mts, "get_pending_groceries", return_value=[]) as mock_pending,
        patch.object(mts, "add_grocery_item") as mock_add,
        patch.object(mts, "mark_item_checked", return_value={"id": "item-salt-1", "checked": True}) as mock_check,
    ):
        res = mts.sync_mealie_to_todoist()

    assert res["ok"] is True
    assert res["added"] == []
    assert len(res["skipped_pantry"]) == 1
    assert res["skipped_pantry"][0]["id"] == "item-salt-1"
    assert res["checked_in_mealie"] == 1
    assert res["errors"] == []

    mock_add.assert_not_called()
    mock_pending.assert_not_called()
    mock_check.assert_called_once_with(pantry_item)


def test_manual_non_recipe_item_bypasses_pantry_filter() -> None:
    """A manually added pantry item (no recipeReferences) bypasses pantry filtering and syncs to Todoist."""
    manual_pantry_item = {
        "id": "item-manual-salt",
        "shoppingListId": "list-uuid-1",
        "display": "Kosher salt",
        "food": {"name": "kosher salt"},
        "checked": False,
        "recipeReferences": [],
    }

    with (
        patch.object(mts, "resolve_shopping_list_id", return_value="list-uuid-1"),
        patch.object(mts, "fetch_unchecked_items", return_value=[manual_pantry_item]),
        patch.object(mts, "load_pantry_exclusions", return_value={"kosher salt", "black pepper"}),
        patch.object(mts, "load_staples", return_value=[]),
        patch.object(mts, "get_pending_groceries", return_value=[]),
        patch.object(mts, "add_grocery_item", return_value={"id": "td-2", "title": "Kosher salt"}) as mock_add,
        patch.object(mts, "mark_item_checked", return_value={"id": "item-manual-salt", "checked": True}) as mock_check,
    ):
        res = mts.sync_mealie_to_todoist()

    assert res["ok"] is True
    assert len(res["added"]) == 1
    assert res["added"][0]["title"] == "Kosher salt"
    assert res["skipped_pantry"] == []
    assert res["checked_in_mealie"] == 1
    mock_add.assert_called_once_with(
        "Kosher salt",
        notes="Synced from Mealie",
        list_title="Whole Foods",
    )
    mock_check.assert_called_once_with(manual_pantry_item)


def test_costco_and_kirkland_store_routing() -> None:
    """Items mentioning Costco/Kirkland or matching a Costco staple route to the Todoist 'Costco' list."""
    items = [
        {
            "id": "item-kirkland-1",
            "shoppingListId": "list-uuid-1",
            "display": "Kirkland paper towels",
            "checked": False,
            "recipeReferences": [],
        },
        {
            "id": "item-staple-costco",
            "shoppingListId": "list-uuid-1",
            "display": "2 packs Shin Ramyun",
            "checked": False,
            "recipeReferences": [],
        },
    ]
    staples = [
        {
            "id": "shin_ramyun",
            "name": "Shin Ramyun Spicy Noodle Soup",
            "search_query": "shin ramyun noodle soup",
            "store": "costco",
        }
    ]

    with (
        patch.object(mts, "resolve_shopping_list_id", return_value="list-uuid-1"),
        patch.object(mts, "fetch_unchecked_items", return_value=items),
        patch.object(mts, "load_pantry_exclusions", return_value=set()),
        patch.object(mts, "load_staples", return_value=staples),
        patch.object(mts, "get_pending_groceries", return_value=[]) as mock_pending,
        patch.object(mts, "add_grocery_item", return_value={"id": "td-c1"}) as mock_add,
        patch.object(mts, "mark_item_checked", return_value={"checked": True}) as mock_check,
    ):
        res = mts.sync_mealie_to_todoist()

    assert res["ok"] is True
    assert len(res["added"]) == 2
    assert res["added"][0]["store"] == "Costco"
    assert res["added"][1]["store"] == "Costco"
    assert res["checked_in_mealie"] == 2
    mock_pending.assert_called_once_with(title="Costco")
    assert mock_add.call_count == 2
    assert mock_check.call_count == 2


def test_duplicate_item_skipped_and_checked_in_mealie() -> None:
    """An item whose normalized ingredient name matches an open Todoist task is skipped and checked in Mealie."""
    mealie_item = {
        "id": "item-dup-1",
        "shoppingListId": "list-uuid-1",
        "display": "2 avocados",
        "checked": False,
        "recipeReferences": [{"recipeId": "rec-99"}],
    }
    open_todoist_tasks = [
        {"id": "td-existing-1", "title": "Hass Avocados", "content": "Hass Avocados"}
    ]

    with (
        patch.object(mts, "resolve_shopping_list_id", return_value="list-uuid-1"),
        patch.object(mts, "fetch_unchecked_items", return_value=[mealie_item]),
        patch.object(mts, "load_pantry_exclusions", return_value=set()),
        patch.object(mts, "load_staples", return_value=[]),
        patch.object(mts, "get_pending_groceries", return_value=open_todoist_tasks),
        patch.object(mts, "add_grocery_item") as mock_add,
        patch.object(mts, "mark_item_checked", return_value={"id": "item-dup-1", "checked": True}) as mock_check,
    ):
        res = mts.sync_mealie_to_todoist()

    assert res["ok"] is True
    assert res["added"] == []
    assert len(res["skipped_duplicate"]) == 1
    assert res["skipped_duplicate"][0]["id"] == "item-dup-1"
    assert res["skipped_duplicate"][0]["store"] == "Whole Foods"
    assert res["checked_in_mealie"] == 1
    mock_add.assert_not_called()
    mock_check.assert_called_once_with(mealie_item)


def test_dry_run_makes_zero_writes() -> None:
    """dry_run=True computes added/skipped lists without calling add_grocery_item or mark_item_checked."""
    items = [
        {
            "id": "item-1",
            "shoppingListId": "list-uuid-1",
            "display": "Organic blueberries",
            "checked": False,
            "recipeReferences": [{"recipeId": "rec-1"}],
        },
        {
            "id": "item-2",
            "shoppingListId": "list-uuid-1",
            "display": "1 tsp kosher salt",
            "checked": False,
            "recipeReferences": [{"recipeId": "rec-1"}],
        },
    ]

    with (
        patch.object(mts, "resolve_shopping_list_id", return_value="list-uuid-1"),
        patch.object(mts, "fetch_unchecked_items", return_value=items),
        patch.object(mts, "load_pantry_exclusions", return_value={"kosher salt"}),
        patch.object(mts, "load_staples", return_value=[]),
        patch.object(mts, "get_pending_groceries", return_value=[]),
        patch.object(mts, "add_grocery_item") as mock_add,
        patch.object(mts, "mark_item_checked") as mock_check,
    ):
        res = mts.sync_mealie_to_todoist(dry_run=True)

    assert res["ok"] is True
    assert len(res["added"]) == 1
    assert len(res["skipped_pantry"]) == 1
    assert res["checked_in_mealie"] == 0
    mock_add.assert_not_called()
    mock_check.assert_not_called()


def test_todoist_failure_leaves_item_unchecked_in_mealie() -> None:
    """If add_grocery_item raises an exception, the item is NOT checked off in Mealie so it retries next cycle."""
    mealie_item = {
        "id": "item-err-1",
        "shoppingListId": "list-uuid-1",
        "display": "Organic whole milk",
        "checked": False,
        "recipeReferences": [],
    }

    with (
        patch.object(mts, "resolve_shopping_list_id", return_value="list-uuid-1"),
        patch.object(mts, "fetch_unchecked_items", return_value=[mealie_item]),
        patch.object(mts, "load_pantry_exclusions", return_value=set()),
        patch.object(mts, "load_staples", return_value=[]),
        patch.object(mts, "get_pending_groceries", return_value=[]),
        patch.object(mts, "add_grocery_item", side_effect=RuntimeError("Todoist 503 Service Unavailable")) as mock_add,
        patch.object(mts, "mark_item_checked") as mock_check,
    ):
        res = mts.sync_mealie_to_todoist()

    assert res["ok"] is False
    assert res["added"] == []
    assert res["checked_in_mealie"] == 0
    assert len(res["errors"]) == 1
    assert "Todoist 503 Service Unavailable" in res["errors"][0]
    mock_add.assert_called_once()
    mock_check.assert_not_called()


def test_mealie_rest_client_pagination_and_put_payload() -> None:
    """Verify resolve_shopping_list_id, paginated fetch_unchecked_items, and mark_item_checked PUT payload."""
    responses = [
        _FakeHTTPResponse({"items": [{"id": "list-abc", "name": "Shopping list"}]}),
        _FakeHTTPResponse({
            "items": [
                {"id": "i-1", "shoppingListId": "list-abc", "display": "Bananas", "checked": False},
                {"id": "i-2", "shoppingListId": "list-abc", "display": "Old item", "checked": True},
            ],
            "page": 1,
            "total_pages": 2,
        }),
        _FakeHTTPResponse({
            "items": [
                {"id": "i-3", "shoppingListId": "list-abc", "quantity": 2, "unit": {"name": "lb"}, "food": {"name": "apples"}, "checked": False},
            ],
            "page": 2,
            "total_pages": 2,
        }),
        _FakeHTTPResponse({"id": "i-3", "checked": True}),
    ]

    captured_requests: list[Any] = []

    def _fake_urlopen(req: Any, timeout: int = 20) -> _FakeHTTPResponse:
        captured_requests.append(req)
        return responses.pop(0)

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        list_id = mts.resolve_shopping_list_id("Shopping list")
        assert list_id == "list-abc"

        unchecked = mts.fetch_unchecked_items(list_id)
        assert len(unchecked) == 2
        assert unchecked[0]["id"] == "i-1"
        assert unchecked[1]["id"] == "i-3"
        assert mts.extract_item_text(unchecked[1]) == "2 lb apples"

        mts.mark_item_checked(unchecked[1])

    put_req = captured_requests[-1]
    assert put_req.get_method() == "PUT"
    assert put_req.full_url == "https://mealie.example.test/api/households/shopping/items/i-3"
    put_body = json.loads(put_req.data.decode("utf-8"))
    assert put_body["checked"] is True
    assert put_body["shoppingListId"] == "list-abc"


def test_cli_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify CLI argument parsing (--dry-run, --no-pantry-filter, --list-id, --list-name, --json)."""
    fake_result = {
        "ok": True,
        "added": [{"id": "1", "title": "Bananas", "normalized": "bananas", "store": "Whole Foods"}],
        "skipped_pantry": [],
        "skipped_duplicate": [],
        "checked_in_mealie": 0,
        "errors": [],
    }
    with patch.object(mts, "sync_mealie_to_todoist", return_value=fake_result) as mock_sync:
        rc = mts.main(["--dry-run", "--no-pantry-filter", "--list-id", "list-123", "--list-name", "Household", "--json"])

    assert rc == 0
    mock_sync.assert_called_once_with(
        list_id="list-123",
        list_name="Household",
        exclude_pantry=False,
        dry_run=True,
    )
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert len(out["added"]) == 1
