import json
import sys
import unittest.mock
from pathlib import Path

import pytest

ROOT_DIR = str(Path(__file__).resolve().parent.parent)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
if "/workspace" not in sys.path:
    sys.path.append("/workspace")

from tools.todoist_grocery import (
    _fetch_all,
    _get_api_token,
    _normalize_project_name,
    add_grocery_item,
    complete_task,
    get_or_create_project,
    get_pending_groceries,
    list_projects,
    main,
)


class MockHTTPResponse:
    def __init__(self, data: bytes, status: int = 200):
        self._data = data
        self.status = status

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class TestNormalizeProjectName:
    def test_strips_cart_emojis_and_whitespace(self):
        assert _normalize_project_name("🛒 Whole Foods") == "Whole Foods"
        assert _normalize_project_name("🛒 Costco") == "Costco"
        assert _normalize_project_name("Whole Foods") == "Whole Foods"
        assert _normalize_project_name("  🛒   Whole Foods  ") == "Whole Foods"
        assert _normalize_project_name("🛍️ Costco") == "Costco"
        assert _normalize_project_name("🛍 Costco") == "Costco"
        assert _normalize_project_name("") == ""
        assert _normalize_project_name(None) == ""


class TestTokenResolution:
    def test_resolves_from_environment(self, monkeypatch):
        monkeypatch.setenv("TODOIST_API_TOKEN", "env-token-12345")
        assert _get_api_token() == "env-token-12345"

    def test_resolves_from_secrets_json(self, monkeypatch, tmp_path):
        monkeypatch.delenv("TODOIST_API_TOKEN", raising=False)
        secrets_file = tmp_path / "env.json"
        secrets_file.write_text(json.dumps({"TODOIST_API_TOKEN": "file-token-67890"}))

        with unittest.mock.patch("tools.todoist_grocery.SECRETS_FILE", str(secrets_file)):
            assert _get_api_token() == "file-token-67890"

    def test_missing_token_error_raises_runtime_error(self, monkeypatch):
        monkeypatch.delenv("TODOIST_API_TOKEN", raising=False)
        with (
            unittest.mock.patch("tools.todoist_grocery.SECRETS_FILE", "/nonexistent/secrets.json"),
            pytest.raises(RuntimeError, match="TODOIST_API_TOKEN not found"),
        ):
            _get_api_token()


class TestPaginationAndEnvelopeParsing:
    def test_handles_direct_list_response(self, monkeypatch):
        monkeypatch.setenv("TODOIST_API_TOKEN", "mock-token")
        raw = json.dumps([{"id": "p1", "name": "Whole Foods"}]).encode("utf-8")

        with unittest.mock.patch("urllib.request.urlopen", return_value=MockHTTPResponse(raw)):
            items = _fetch_all("/projects")
            assert len(items) == 1
            assert items[0]["id"] == "p1"

    def test_handles_single_envelope_response(self, monkeypatch):
        monkeypatch.setenv("TODOIST_API_TOKEN", "mock-token")
        raw = json.dumps({"results": [{"id": "p1", "name": "Whole Foods"}], "next_cursor": None}).encode("utf-8")

        with unittest.mock.patch("urllib.request.urlopen", return_value=MockHTTPResponse(raw)):
            items = _fetch_all("/projects")
            assert len(items) == 1
            assert items[0]["name"] == "Whole Foods"

    def test_handles_multi_page_cursor_pagination(self, monkeypatch):
        monkeypatch.setenv("TODOIST_API_TOKEN", "mock-token")
        page1 = json.dumps({"results": [{"id": "t1", "content": "Milk"}], "next_cursor": "cursor_2"}).encode("utf-8")
        page2 = json.dumps({"results": [{"id": "t2", "content": "Eggs"}], "next_cursor": None}).encode("utf-8")

        responses = [MockHTTPResponse(page1), MockHTTPResponse(page2)]
        with unittest.mock.patch("urllib.request.urlopen", side_effect=responses) as mock_urlopen:
            items = _fetch_all("/tasks")
            assert len(items) == 2
            assert items[0]["content"] == "Milk"
            assert items[1]["content"] == "Eggs"
            assert mock_urlopen.call_count == 2


class TestProjectManagement:
    def test_list_projects_returns_projects(self, monkeypatch):
        monkeypatch.setenv("TODOIST_API_TOKEN", "mock-token")
        raw = json.dumps([{"id": "p1", "name": "Whole Foods"}]).encode("utf-8")
        with unittest.mock.patch("urllib.request.urlopen", return_value=MockHTTPResponse(raw)):
            projs = list_projects()
            assert len(projs) == 1
            assert projs[0]["name"] == "Whole Foods"

    def test_matches_existing_project_case_insensitively_with_emoji(self, monkeypatch):
        monkeypatch.setenv("TODOIST_API_TOKEN", "mock-token")
        projects_json = json.dumps([
            {"id": "6j2qFCHfmv6x3QPx", "name": "Whole Foods"},
            {"id": "6j2qFCRPmmhHrQJf", "name": "Costco"},
        ]).encode("utf-8")

        with unittest.mock.patch("urllib.request.urlopen", return_value=MockHTTPResponse(projects_json)):
            proj_id = get_or_create_project("🛒 Whole Foods")
            assert proj_id == "6j2qFCHfmv6x3QPx"

        with unittest.mock.patch("urllib.request.urlopen", return_value=MockHTTPResponse(projects_json)):
            proj_id_costco = get_or_create_project("costco")
            assert proj_id_costco == "6j2qFCRPmmhHrQJf"

    def test_creates_project_when_not_found(self, monkeypatch):
        monkeypatch.setenv("TODOIST_API_TOKEN", "mock-token")
        list_resp = json.dumps([]).encode("utf-8")
        create_resp = json.dumps({"id": "new-proj-777", "name": "Trader Joe's"}).encode("utf-8")

        with unittest.mock.patch("urllib.request.urlopen", side_effect=[MockHTTPResponse(list_resp), MockHTTPResponse(create_resp)]) as mock_urlopen:
            proj_id = get_or_create_project("🛒 Trader Joe's")
            assert proj_id == "new-proj-777"
            assert mock_urlopen.call_count == 2
            # Verify create request body
            create_req = mock_urlopen.call_args_list[1][0][0]
            body = json.loads(create_req.data.decode("utf-8"))
            assert body["name"] == "Trader Joe's"


class TestGroceryTasks:
    def test_get_pending_groceries_filters_completed_and_empty(self, monkeypatch):
        monkeypatch.setenv("TODOIST_API_TOKEN", "mock-token")
        tasks_json = json.dumps([
            {"id": "101", "content": "Organic Bananas", "description": "green", "is_completed": False},
            {"id": "102", "content": "Sourdough Bread", "checked": True},  # completed
            {"id": "103", "content": "   ", "is_completed": False},  # empty
            {"id": "104", "content": "Avocados", "description": "", "is_completed": False},
        ]).encode("utf-8")

        with (
            unittest.mock.patch("tools.todoist_grocery.get_or_create_project", return_value="proj-wf"),
            unittest.mock.patch("urllib.request.urlopen", return_value=MockHTTPResponse(tasks_json)),
        ):
            pending = get_pending_groceries(list_id="proj-wf")
            assert len(pending) == 2
            assert pending[0]["id"] == "101"
            assert pending[0]["title"] == "Organic Bananas"
            assert pending[0]["notes"] == "green"
            assert pending[0]["status"] == "needsAction"
            assert pending[1]["id"] == "104"
            assert pending[1]["title"] == "Avocados"

    def test_add_grocery_item_posts_correct_payload(self, monkeypatch):
        monkeypatch.setenv("TODOIST_API_TOKEN", "mock-token")
        created_json = json.dumps({
            "id": "task-555",
            "content": "Almond Butter",
            "description": "no salt",
            "project_id": "proj-wf",
        }).encode("utf-8")

        with (
            unittest.mock.patch("tools.todoist_grocery.get_or_create_project", return_value="proj-wf"),
            unittest.mock.patch("urllib.request.urlopen", return_value=MockHTTPResponse(created_json)) as mock_urlopen,
        ):
            res = add_grocery_item("Almond Butter", list_id="proj-wf", notes="no salt")
            assert res["id"] == "task-555"
            assert res["title"] == "Almond Butter"

            req = mock_urlopen.call_args[0][0]
            assert req.get_method() == "POST"
            payload = json.loads(req.data.decode("utf-8"))
            assert payload["content"] == "Almond Butter"
            assert payload["project_id"] == "proj-wf"
            assert payload["description"] == "no salt"

    def test_complete_task_closes_task(self, monkeypatch):
        monkeypatch.setenv("TODOIST_API_TOKEN", "mock-token")
        # Todoist returns 204 No Content
        empty_resp = b""

        with unittest.mock.patch("urllib.request.urlopen", return_value=MockHTTPResponse(empty_resp, status=204)) as mock_urlopen:
            res = complete_task("task-555")
            assert res["id"] == "task-555"
            assert res["status"] == "completed"

            req = mock_urlopen.call_args[0][0]
            assert req.get_method() == "POST"
            assert req.full_url.endswith("/tasks/task-555/close")


class TestCLI:
    def test_cli_list_invokes_get_pending(self, monkeypatch, capsys):
        monkeypatch.setenv("TODOIST_API_TOKEN", "mock-token")
        monkeypatch.setattr("sys.argv", ["todoist_grocery.py", "--list", "--list-title", "Whole Foods"])

        with unittest.mock.patch(
            "tools.todoist_grocery.get_pending_groceries",
            return_value=[{"id": "1", "title": "Apples", "notes": "", "status": "needsAction"}],
        ):
            main()
            captured = capsys.readouterr()
            data = json.loads(captured.out)
            assert len(data) == 1
            assert data[0]["title"] == "Apples"

    def test_cli_add_invokes_add_item(self, monkeypatch, capsys):
        monkeypatch.setenv("TODOIST_API_TOKEN", "mock-token")
        monkeypatch.setattr("sys.argv", ["todoist_grocery.py", "--add", "Honeycrisp Apples", "--list-title", "Whole Foods"])

        with unittest.mock.patch(
            "tools.todoist_grocery.add_grocery_item",
            return_value={"id": "99", "title": "Honeycrisp Apples"},
        ):
            main()
            captured = capsys.readouterr()
            assert "Added task 99: Honeycrisp Apples" in captured.out

    def test_cli_complete_invokes_complete_task(self, monkeypatch, capsys):
        monkeypatch.setenv("TODOIST_API_TOKEN", "mock-token")
        monkeypatch.setattr("sys.argv", ["todoist_grocery.py", "--complete", "99"])

        with unittest.mock.patch(
            "tools.todoist_grocery.complete_task",
            return_value={"id": "99", "status": "completed"},
        ):
            main()
            captured = capsys.readouterr()
            assert "Completed task 99" in captured.out
