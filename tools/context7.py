#!/usr/bin/env python3
"""Context7 Documentation Intelligence Tool for Zero.

Fetches up-to-date, versioned library documentation and API reference snippets
directly from Context7 to eliminate training-data hallucinations.
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

CACHE_DIR = Path("/workspace/data/context7_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECONDS = 86400 * 3  # 3 days

API_BASE_URL = os.environ.get("CONTEXT7_API_URL", "https://context7.com/api/v2")
TIMEOUT_SECONDS = 30


def _get_api_key() -> Optional[str]:
    """Retrieve optional Context7 API key from env or secrets."""
    if os.environ.get("CONTEXT7_API_KEY"):
        return os.environ["CONTEXT7_API_KEY"].strip()
    
    secrets_file = Path("/secrets/context7.json")
    if secrets_file.exists():
        try:
            with open(secrets_file, "r") as f:
                data = json.load(f)
                return data.get("api_key") or data.get("apiKey")
        except Exception:
            pass
    return None


def _get_cache(cache_key: str) -> Optional[Any]:
    cache_file = CACHE_DIR / f"{cache_key}.json"
    if cache_file.exists():
        try:
            with open(cache_file, "r") as f:
                entry = json.load(f)
                if time.time() - entry.get("timestamp", 0) < CACHE_TTL_SECONDS:
                    return entry.get("data")
        except Exception:
            pass
    return None


def _set_cache(cache_key: str, data: Any) -> None:
    cache_file = CACHE_DIR / f"{cache_key}.json"
    try:
        with open(cache_file, "w") as f:
            json.dump({"timestamp": time.time(), "data": data}, f, indent=2)
    except Exception:
        pass


def _make_headers() -> Dict[str, str]:
    headers = {
        "User-Agent": "Zero-Context7/1.0",
        "X-Context7-Source": "mcp-server",
        "X-Context7-Server-Version": "4.1.1",
    }
    api_key = _get_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def search_libraries(query: str, library_name: str) -> Dict[str, Any]:
    """Search Context7 database for matching libraries and their canonical IDs."""
    cache_key = "search_" + hashlib.sha256(f"{query}:{library_name}".encode()).hexdigest()[:16]
    cached = _get_cache(cache_key)
    if cached is not None:
        return cached

    params = urllib.parse.urlencode({"query": query, "libraryName": library_name})
    url = f"{API_BASE_URL}/libs/search?{params}"
    req = urllib.request.Request(url, headers=_make_headers())

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            _set_cache(cache_key, data)
            return data
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="replace")
        return {"results": [], "error": f"HTTP {e.code}: {err_msg}"}
    except Exception as e:
        return {"results": [], "error": str(e)}


def fetch_library_context(library_id: str, query: str) -> Dict[str, Any]:
    """Fetch structured markdown documentation snippets for a library ID and query."""
    cache_key = "ctx_" + hashlib.sha256(f"{library_id}:{query}".encode()).hexdigest()[:16]
    cached = _get_cache(cache_key)
    if cached is not None:
        return cached

    params = urllib.parse.urlencode({"libraryId": library_id, "query": query})
    url = f"{API_BASE_URL}/context?{params}"
    req = urllib.request.Request(url, headers=_make_headers())

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            # /v2/context returns raw markdown or JSON
            try:
                data = json.loads(raw)
            except Exception:
                data = {"content": raw, "libraryId": library_id, "query": query}
            
            _set_cache(cache_key, data)
            return data
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="replace")
        return {"content": "", "error": f"HTTP {e.code}: {err_msg}"}
    except Exception as e:
        return {"content": "", "error": str(e)}


def query_docs(library: str, topic: str) -> Dict[str, Any]:
    """High-level end-to-end documentation lookup."""
    # 1. Search for matching library
    search_res = search_libraries(query=topic, library_name=library)
    results = search_res.get("results", [])
    if not results:
        err = search_res.get("error", f"No libraries found matching '{library}'")
        return {"success": False, "error": err, "library": library, "topic": topic}

    # Pick top match
    best_lib = results[0]
    lib_id = best_lib.get("id")
    lib_title = best_lib.get("title", library)
    lib_desc = best_lib.get("description", "")

    # 2. Fetch context snippets
    ctx_res = fetch_library_context(library_id=lib_id, query=topic)
    if "error" in ctx_res and not ctx_res.get("content"):
        return {
            "success": False,
            "error": ctx_res["error"],
            "library": lib_title,
            "library_id": lib_id,
            "topic": topic,
        }

    raw_content = ctx_res.get("content", "")
    return {
        "success": True,
        "library": lib_title,
        "library_id": lib_id,
        "description": lib_desc,
        "topic": topic,
        "docs": raw_content,
        "available_libraries": [
            {"id": r.get("id"), "title": r.get("title"), "desc": r.get("description")}
            for r in results[:5]
        ],
    }


def main():
    parser = argparse.ArgumentParser(description="Zero Context7 Docs Intelligence")
    subparsers = parser.add_subparsers(dest="subcommand")

    # search
    p_search = subparsers.add_parser("search", help="Search for library IDs")
    p_search.add_argument("library", help="Library name to search (e.g. fastapi, pydantic, tailwind)")
    p_search.add_argument("--query", "-q", default="", help="Optional specific task query")

    # docs
    p_docs = subparsers.add_parser("docs", help="Fetch docs directly by library ID")
    p_docs.add_argument("library_id", help="Exact library ID (e.g. /websites/fastapi_tiangolo)")
    p_docs.add_argument("query", help="Topic or query (e.g. 'lifespan events')")

    # query (one-shot)
    p_query = subparsers.add_parser("query", help="One-shot library + topic search and doc fetch")
    p_query.add_argument("library", help="Library name (e.g. fastapi)")
    p_query.add_argument("topic", help="Topic or task (e.g. 'lifespan startup shutdown')")

    args = parser.parse_args()

    if args.subcommand == "search":
        res = search_libraries(query=args.query or args.library, library_name=args.library)
        print(json.dumps(res, indent=2))
    elif args.subcommand == "docs":
        res = fetch_library_context(library_id=args.library_id, query=args.query)
        if isinstance(res, dict) and "content" in res:
            print(res["content"])
        else:
            print(json.dumps(res, indent=2))
    elif args.subcommand == "query":
        res = query_docs(library=args.library, topic=args.topic)
        if res.get("success"):
            print(f"=== Library: {res['library']} ({res['library_id']}) ===")
            print(f"=== Topic: {res['topic']} ===\n")
            print(res.get("docs", ""))
        else:
            print(f"Error: {res.get('error')}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
