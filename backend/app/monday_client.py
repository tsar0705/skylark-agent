"""
Thin, read-only client for the monday.com GraphQL API (v2).

Why raw GraphQL over the official SDK: the SDK adds a dependency surface we
don't need for two read-only boards, and raw queries make exactly what data
we pull (and its cost in monday.com's API "complexity" budget) explicit and
auditable. See DECISION_LOG.md for the API-vs-MCP discussion.
"""
import time
from typing import Any

import httpx

from .config import settings

MONDAY_API_URL = "https://api.monday.com/v2"


class MondayAPIError(Exception):
    pass


class MondayClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.MONDAY_API_KEY
        self._cache: dict[str, tuple[float, Any]] = {}

    def _headers(self) -> dict:
        return {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
            "API-Version": "2024-10",
        }

    def _post(self, query: str, variables: dict | None = None) -> dict:
        if not self.api_key:
            raise MondayAPIError(
                "MONDAY_API_KEY is not configured. Set it in your .env file."
            )
        try:
            resp = httpx.post(
                MONDAY_API_URL,
                json={"query": query, "variables": variables or {}},
                headers=self._headers(),
                timeout=30.0,
            )
        except httpx.RequestError as e:
            raise MondayAPIError(f"Network error calling monday.com: {e}") from e

        if resp.status_code != 200:
            raise MondayAPIError(
                f"monday.com API returned HTTP {resp.status_code}: {resp.text[:500]}"
            )

        body = resp.json()
        if "errors" in body:
            raise MondayAPIError(f"monday.com API error: {body['errors']}")
        return body["data"]

    def _cached(self, key: str, fetch_fn):
        now = time.time()
        cached = self._cache.get(key)
        if cached and (now - cached[0]) < settings.DATA_CACHE_TTL_SECONDS:
            return cached[1]
        value = fetch_fn()
        self._cache[key] = (now, value)
        return value

    def clear_cache(self):
        self._cache = {}

    # ------------------------------------------------------------------
    # Board reads
    # ------------------------------------------------------------------

    def get_board_items(self, board_id: str, use_cache: bool = True) -> list[dict]:
        """
        Returns every item on a board as a list of flat dicts:
        {"id": ..., "name": ..., "<column title>": <text value>, ...}

        Handles pagination via monday.com's cursor-based `items_page`.
        """
        cache_key = f"board_items:{board_id}"
        if use_cache:
            return self._cached(cache_key, lambda: self._fetch_all_items(board_id))
        return self._fetch_all_items(board_id)

    def _fetch_all_items(self, board_id: str) -> list[dict]:
        query = """
        query ($boardId: [ID!], $cursor: String) {
          boards(ids: $boardId) {
            name
            columns { id title type }
            items_page(limit: 100, cursor: $cursor) {
              cursor
              items {
                id
                name
                column_values {
                  id
                  text
                  value
                }
              }
            }
          }
        }
        """
        all_items: list[dict] = []
        cursor = None
        columns_by_id: dict[str, str] = {}

        while True:
            data = self._post(query, {"boardId": [board_id], "cursor": cursor})
            boards = data.get("boards") or []
            if not boards:
                raise MondayAPIError(f"Board {board_id} not found or not accessible.")
            board = boards[0]
            if not columns_by_id:
                columns_by_id = {c["id"]: c["title"] for c in board["columns"]}

            page = board["items_page"]
            for item in page["items"]:
                flat = {"id": item["id"], "name": item["name"]}
                for cv in item["column_values"]:
                    title = columns_by_id.get(cv["id"], cv["id"])
                    flat[title] = cv["text"]
                all_items.append(flat)

            cursor = page.get("cursor")
            if not cursor:
                break

        return all_items

    def get_board_schema(self, board_id: str) -> dict:
        query = """
        query ($boardId: [ID!]) {
          boards(ids: $boardId) {
            name
            columns { id title type }
          }
        }
        """
        data = self._post(query, {"boardId": [board_id]})
        boards = data.get("boards") or []
        if not boards:
            raise MondayAPIError(f"Board {board_id} not found or not accessible.")
        return boards[0]

    def ping(self) -> str:
        """Lightweight call to confirm the API key + connectivity work."""
        data = self._post("query { me { name email } }")
        return data["me"]["name"]
