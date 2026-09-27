from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest

from confer.availability import Interval, StaticProvider
from confer.node import Node
from confer.server import ConferServer

UTC = timezone.utc


def wait_for(cond: Callable[[], object], timeout: float = 10.0, what: str = "condition") -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class Net:
    """Spin up real nodes with real HTTP servers on 127.0.0.1 (OS-assigned ports)."""

    def __init__(self, root: Path):
        self.root = root
        self.servers: list[ConferServer] = []

    def node(self, name: str, *, busy: list[Interval] | None = None, via_relay: str = "", tz: str = "UTC", serve: bool = True) -> Node:
        provider = StaticProvider(busy or [])
        node = Node.init(self.root / name, name, tz=tz, provider=provider)
        if serve:
            srv = ConferServer(node, "127.0.0.1", 0, tick_seconds=0.2).start()
            self.servers.append(srv)
            if not via_relay:
                node.config["endpoint"] = f"http://127.0.0.1:{srv.port}"
        if via_relay:
            node.config["relay"] = via_relay
        node.save_config()
        return node

    def relay(self) -> str:
        node = Node.init(self.root / "relay", "relay")
        srv = ConferServer(node, "127.0.0.1", 0, relay=True, tick_seconds=0.2).start()
        self.servers.append(srv)
        return f"http://127.0.0.1:{srv.port}"

    def pair(self, a: Node, b: Node, a_grants: str = "plans,files,notes", b_grants: str = "plans,files,notes") -> None:
        """a invites b; a grants b ``a_grants``; b grants a ``b_grants``."""
        token = a.create_invite(b.name, a_grants)
        b.accept_invite(token, grants=b_grants)
        wait_for(lambda: (c := b.store.contact(a.identity.agent_id)) and c.status == "active", what=f"{b.name} paired with {a.name}")
        wait_for(lambda: a.store.contact(b.identity.agent_id), what=f"{a.name} knows {b.name}")

    def close(self) -> None:
        for s in self.servers:
            s.stop()


@pytest.fixture
def net(tmp_path: Path):
    n = Net(tmp_path)
    yield n
    n.close()


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A fixed future date so tests don't depend on today."""
    return datetime(2031, 3, day, hour, minute, tzinfo=UTC)


def slot(day: int, hour: int, minutes: int = 60) -> Interval:
    start = at(day, hour)
    return Interval(start, start + timedelta(minutes=minutes))
