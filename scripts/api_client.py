# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Shared REST client for the seed scripts.

The scripts that write semantic content (``concept_terms.py``,
``custom_analyses.py``) go through the backend API rather than the catalog
tables, because the API is what validates SQL against the catalog and embeds
what it writes. A row inserted straight into Postgres has no vector, so it is
invisible to retrieval — that is the failure mode ``POST
/api/semantic-index/reindex`` exists to repair.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

#: The backend binds no auth of its own (see the README's note that ports 3001
#: and 3002 stay on a private network); the frontend on 3000 requires a session,
#: so these scripts talk to 3001.
DEFAULT_API = "http://127.0.0.1:3001"


class ApiError(RuntimeError):
    """A non-2xx response, carrying the server's own explanation."""


class Api:
    """Minimal JSON client over the backend's write surface."""

    def __init__(self, base: str = DEFAULT_API, *, timeout: float = 120.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode()
        except urllib.error.HTTPError as exc:
            raise ApiError(
                f"{method} {path} → {exc.code}: {exc.read().decode()}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ApiError(
                f"{method} {path} → cannot reach {self.base}: {exc.reason}"
            ) from exc
        return json.loads(payload) if payload.strip() else {}

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("POST", path, body)

    def put(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("PUT", path, body)

    def patch(self, path: str, body: dict[str, Any]) -> Any:
        return self._request("PATCH", path, body)


__all__ = ["Api", "ApiError", "DEFAULT_API"]
