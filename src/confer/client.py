"""Tiny stdlib client for a node's REST API — for scripts and agent frameworks.

    from confer.client import ConferClient
    c = ConferClient("https://alex.example.ts.net", token)
    c.call("confer_inbox")
    c.call("confer_split_expense", approved=True, title="Dinner", amount="90", with_contacts=["Sam"])

Framework glue (the model proposes a tool call; your code dispatches it):

    tools = c.schemas("openai")        # or "anthropic" / "gemini"
    result = c.dispatch(name, args, approved=ask_human_if_needed(name))
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class ConferAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message

    @property
    def needs_approval(self) -> bool:
        return self.status == 428


class ConferClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.token = token
        self.timeout = timeout

    def _req(self, method: str, path: str, body: Any = None, approved: bool = False) -> Any:
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if approved:
            headers["Confer-Human-Approved"] = "true"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 (user-configured node)
                out = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                msg = json.loads(exc.read() or b"{}").get("error", exc.reason)
            except ValueError:
                msg = str(exc.reason)
            raise ConferAPIError(exc.code, msg) from exc
        return out.get("result", out)

    def tools(self) -> list[dict]:
        return self._req("GET", "/tools")

    def needs_approval(self, name: str) -> bool:
        return any(t["name"] == name and t["needs_human_ok"] for t in self.tools())

    def call(self, name: str, approved: bool = False, **args: Any) -> Any:
        return self._req("POST", f"/tools/{name}", args, approved)

    def dispatch(self, name: str, args: dict | None, approved: bool = False) -> str:
        """Run a model-proposed tool call and return a string result for the model."""
        try:
            return json.dumps(self.call(name, approved, **(args or {})), default=str)
        except ConferAPIError as exc:
            return json.dumps({"error": exc.message, "needs_human_ok": exc.needs_approval})

    def schemas(self, fmt: str = "openai") -> list[dict]:
        """Tool definitions for a function-calling API, built from the node's live tool list."""
        ts = self.tools()
        if fmt == "openai":
            return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in ts]
        if fmt == "anthropic":
            return [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in ts]
        if fmt == "gemini":
            from .tools import _gemini

            return [{"functionDeclarations": [{"name": t["name"], "description": t["description"], "parameters": _gemini(t["input_schema"])} for t in ts]}]
        raise ValueError("fmt must be openai, anthropic or gemini")
