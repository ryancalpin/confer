"""Shared lists: groceries, packing lists, "who's bringing what".

Same hub-and-spoke shape as plans: the owner's node holds the list and is the
only writer. Members send ``list.op`` requests; the owner applies them and
broadcasts the new snapshot (``list.share``) to every member. A node only
accepts lists shared by contacts it granted ``lists``; the owner accepts
edits only from the members it chose.

Wire list: {id, rev, title, owner, owner_name, members: {id: name},
            items: [{id, text, done, claimed_by, claimed_name, added_by_name}]}
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any, Callable

from .store import Contact

if TYPE_CHECKING:
    from .node import Node

MAX_ITEMS = 300
MAX_MEMBERS = 50
MAX_TEXT = 200
OPS = ("add", "check", "uncheck", "remove", "claim", "unclaim", "leave")


class ListError(ValueError):
    pass


def _item_id() -> str:
    return secrets.token_hex(6)


def apply_op(lst: dict, actor: str, actor_name: str, op: str, item_id: str = "", text: str = "") -> bool:
    """Apply one edit to the owner's copy. Returns True if anything changed."""
    if op not in OPS:
        raise ListError(f"unknown list operation {op!r}")
    if op == "leave":
        if actor == lst["owner"]:
            raise ListError("the owner can't leave; delete the list instead")
        return lst["members"].pop(actor, None) is not None
    items = lst["items"]
    if op == "add":
        text = " ".join(str(text).split())[:MAX_TEXT]
        if not text:
            raise ListError("empty item")
        if len(items) >= MAX_ITEMS:
            raise ListError(f"lists hold at most {MAX_ITEMS} items")
        new_id = item_id if (isinstance(item_id, str) and 4 <= len(item_id) <= 24 and item_id.isalnum()) else _item_id()
        if any(i["id"] == new_id for i in items):
            return False  # retried add: already there
        items.append({"id": new_id, "text": text, "done": False, "claimed_by": "", "claimed_name": "", "added_by_name": actor_name[:60]})
        return True
    item = next((i for i in items if i["id"] == item_id), None)
    if item is None:
        return False  # already removed — ops are idempotent
    if op == "remove":
        items.remove(item)
    elif op in ("check", "uncheck"):
        item["done"] = op == "check"
    elif op == "claim":
        if item["claimed_by"] and item["claimed_by"] != actor:
            raise ListError(f"{item['claimed_name'] or 'someone'} already has that")
        item.update(claimed_by=actor, claimed_name=actor_name[:60])
    elif op == "unclaim":
        if item["claimed_by"] == actor or actor == lst["owner"]:
            item.update(claimed_by="", claimed_name="")
    return True


def validate_wire_list(data: Any, owner: str) -> dict:
    if not isinstance(data, dict) or data.get("owner") != owner:
        raise ListError("only the owner may share this list")
    lid = data.get("id")
    if not isinstance(lid, str) or not (8 <= len(lid) <= 32) or not lid.isalnum():
        raise ListError("bad list id")
    if not isinstance(data.get("rev"), int) or data["rev"] < 1:
        raise ListError("bad list revision")
    members = data.get("members")
    items = data.get("items")
    if not isinstance(members, dict) or len(members) > MAX_MEMBERS or not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ListError("bad list")
    clean_items = []
    for i in items:
        if not isinstance(i, dict) or not isinstance(i.get("id"), str) or len(i["id"]) > 24:
            raise ListError("bad list item")
        clean_items.append({
            "id": i["id"], "text": str(i.get("text", ""))[:MAX_TEXT], "done": bool(i.get("done")),
            "claimed_by": str(i.get("claimed_by", ""))[:64], "claimed_name": str(i.get("claimed_name", ""))[:60],
            "added_by_name": str(i.get("added_by_name", ""))[:60],
        })
    return {
        "id": lid, "rev": data["rev"], "title": str(data.get("title", ""))[:120] or "List",
        "owner": owner, "owner_name": str(data.get("owner_name", ""))[:60],
        "members": {str(k)[:64]: str(v)[:60] for k, v in members.items()}, "items": clean_items,
    }


def wire(lst: dict) -> dict:
    return {k: lst[k] for k in ("id", "rev", "title", "owner", "owner_name", "members", "items")}


class ListsMixin:
    """Node methods for shared lists. Mixed into :class:`confer.node.Node`."""

    def create_list(self: "Node", title: str, with_: list[str], items: list[str] | None = None) -> dict:
        from .node import NodeError

        people = [self._active_contact(n) for n in with_]
        if len(people) > MAX_MEMBERS:
            raise NodeError(f"at most {MAX_MEMBERS} members")
        lst = {
            "id": secrets.token_hex(8), "rev": 1, "title": (title.strip() or "List")[:120],
            "owner": self.identity.agent_id, "owner_name": self.name,
            "members": {c.agent_id: c.name for c in people}, "items": [], "role": "owner",
        }
        for text in items or []:
            apply_op(lst, self.identity.agent_id, self.name, "add", text=text)
        self.store.save_list(lst)
        self._broadcast_list(lst)
        return lst

    def get_list(self: "Node", list_id: str) -> dict:
        from .node import NodeError

        lst = self.store.find_list(list_id)
        if not lst:
            raise NodeError(f"no list {list_id!r}")
        return lst

    def lists(self: "Node") -> list[dict]:
        return self.store.all_lists()

    def list_op(self: "Node", list_id: str, op: str, *, item: str | int | None = None, text: str = "") -> dict:
        """Edit a list. ``item`` is an item id or a 1-based position. Owners apply
        directly; members send the edit to the owner's node."""
        from .node import NodeError

        with self.store.transaction():
            lst = self.get_list(list_id)
            item_id = self._resolve_item(lst, item) if op not in ("add", "leave") else ""
            if op == "add":
                item_id = _item_id()
            if lst.get("role") == "owner":
                try:
                    changed = apply_op(lst, self.identity.agent_id, self.name, op, item_id, text)
                except ListError as exc:
                    raise NodeError(str(exc)) from exc
                if changed:
                    lst["rev"] += 1
                    self.store.save_list(lst)
                    self._broadcast_list(lst, kick=False)
            else:
                owner = self.store.contact(lst["owner"])
                if not owner:
                    raise NodeError("the list owner is no longer a contact")
                self._send(owner, "list.op", {"list_id": lst["id"], "op": op, "item_id": item_id, "text": text[:MAX_TEXT]}, kick=False)
                if op == "leave":
                    self.store.delete_list(lst["id"])
        self.kick()
        return lst

    def delete_list(self: "Node", list_id: str) -> None:
        """Owner: close the list for everyone. Member: leave it."""
        lst = self.get_list(list_id)
        if lst.get("role") != "owner":
            self.list_op(list_id, "leave")
            return
        for aid in lst["members"]:
            if c := self.store.contact(aid):
                self._send(c, "list.close", {"list_id": lst["id"]}, kick=False)
        self.store.delete_list(lst["id"])
        self.kick()

    @staticmethod
    def _resolve_item(lst: dict, item: str | int | None) -> str:
        from .node import NodeError

        if isinstance(item, int) or (isinstance(item, str) and item.isdigit()):
            idx = int(item) - 1
            if not 0 <= idx < len(lst["items"]):
                raise NodeError(f"no item #{item}")
            return lst["items"][idx]["id"]
        if isinstance(item, str) and any(i["id"] == item for i in lst["items"]):
            return item
        if isinstance(item, str):  # match by text
            hits = [i for i in lst["items"] if i["text"].lower() == item.lower()]
            if len(hits) == 1:
                return hits[0]["id"]
        raise NodeError(f"no item {item!r}")

    def _broadcast_list(self: "Node", lst: dict, kick: bool = True) -> None:
        body = {"list": wire(lst)}
        for aid in lst["members"]:
            if c := self.store.contact(aid):
                self._send(c, "list.share", body, kick=False)
        if kick:
            self.kick()

    # ------------------------------------------------------------ handlers
    def _list_handlers(self: "Node") -> dict[str, Callable[[Contact, dict], None]]:
        return {"list.share": self._on_list_share, "list.op": self._on_list_op, "list.close": self._on_list_close}

    def _on_list_share(self: "Node", contact: Contact, body: dict) -> None:
        from .node import Rejected

        if not contact.can("lists"):
            raise Rejected(f"{self.name} hasn't allowed shared lists from you")
        try:
            data = validate_wire_list(body.get("list"), contact.agent_id)
        except ListError as exc:
            raise Rejected(str(exc)) from exc
        if self.identity.agent_id not in data["members"]:
            return  # I was removed / left
        with self.store.transaction():
            existing = self.store.get_list(data["id"])
            if existing and (existing.get("owner") != contact.agent_id or existing.get("role") == "owner"):
                raise Rejected("list id collision")
            if existing and existing["rev"] >= data["rev"]:
                return
            self.store.save_list({**data, "role": "member"})
        if not existing:
            self._inbox("list", f"📝 {contact.name} shared the list {data['title']!r} with you ({len(data['items'])} items). "
                        f"confer list show {data['id']}", ref=data["id"], contact=contact.agent_id)

    def _on_list_op(self: "Node", contact: Contact, body: dict) -> None:
        from .node import Rejected

        with self.store.transaction():
            lst = self.store.get_list(str(body.get("list_id", "")))
            if not lst or lst.get("role") != "owner":
                raise Rejected("no such list")
            if contact.agent_id not in lst["members"]:
                raise Rejected("not a member of this list")
            try:
                changed = apply_op(lst, contact.agent_id, contact.name, str(body.get("op", "")), str(body.get("item_id", "")), str(body.get("text", "")))
            except ListError as exc:
                raise Rejected(str(exc)) from exc
            if not changed:
                return
            lst["rev"] += 1
            self.store.save_list(lst)
            self._broadcast_list(lst, kick=False)
            if body.get("op") == "leave" and (c := self.store.contact(contact.agent_id)):
                self._send(c, "list.close", {"list_id": lst["id"]}, kick=False)
        self.kick()

    def _on_list_close(self: "Node", contact: Contact, body: dict) -> None:
        lst = self.store.get_list(str(body.get("list_id", "")))
        if not lst or lst.get("owner") != contact.agent_id or lst.get("role") == "owner":
            return
        self.store.delete_list(lst["id"])
        self._inbox("list", f"📝 {contact.name} closed the list {lst['title']!r}.", ref=lst["id"], contact=contact.agent_id)
