#!/usr/bin/env python3
"""Tests for Context7 Documentation Tool."""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from tools.context7 import search_libraries, fetch_library_context, query_docs


class TestContext7(unittest.TestCase):

    @patch("tools.context7._get_cache")
    @patch("urllib.request.urlopen")
    def test_search_libraries_mock(self, mock_urlopen, mock_get_cache):
        mock_get_cache.return_value = None
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "results": [
                {"id": "/websites/fastapi_tiangolo", "title": "FastAPI", "description": "High performance API"}
            ]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = search_libraries("router", "fastapi")
        self.assertIn("results", res)
        self.assertEqual(len(res["results"]), 1)
        self.assertEqual(res["results"][0]["id"], "/websites/fastapi_tiangolo")

    @patch("tools.context7._get_cache")
    @patch("urllib.request.urlopen")
    def test_fetch_library_context_mock(self, mock_urlopen, mock_get_cache):
        mock_get_cache.return_value = None
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"# FastAPI Router Documentation\nUse APIRouter for modular routing."
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = fetch_library_context("/websites/fastapi_tiangolo", "APIRouter")
        self.assertIn("content", res)
        self.assertTrue("APIRouter" in res["content"])

    @patch("tools.context7.search_libraries")
    @patch("tools.context7.fetch_library_context")
    def test_query_docs_e2e_mock(self, mock_fetch, mock_search):
        mock_search.return_value = {
            "results": [{"id": "/pydantic/pydantic", "title": "Pydantic", "description": "Data validation"}]
        }
        mock_fetch.return_value = {"content": "Use BaseModel and Field"}

        res = query_docs("pydantic", "BaseModel")
        self.assertTrue(res["success"])
        self.assertEqual(res["library"], "Pydantic")
        self.assertEqual(res["docs"], "Use BaseModel and Field")


if __name__ == "__main__":
    unittest.main()
