"""Splitting expenses and settling up — a shared ledger per pair of people.

Confer never moves money. It keeps an agreed record: the payer's node sends
each person their share (``money.expense``); that person accepts or disputes
(``money.ack``). Paying someone back is recorded the same way
(``money.settle``, confirmed by the receiver). Only *accepted* entries count
toward balances, so both sides always compute the same number.

Amounts are integer minor units (cents). ``pay_link`` (optional, e.g. a
Venmo/PayPal/Revolut URL from the payer's config) is shown to the debtor.
"""

from __future__ import annotations

import re
import secrets
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Callable

from .store import Contact

if TYPE_CHECKING:
    from .node import Node

MAX_CENTS = 10_000_000_00  # 10 million in major units — plenty for dinners and trips
_CCY = re.compile(r"^[A-Z]{3}$")


class MoneyError(ValueError):
    pass


def to_cents(amount: str | int | float | Decimal) -> int:
    try:
        d = Decimal(str(amount).replace(",", "").strip().lstrip("$€£"))
    except InvalidOperation as exc:
        raise MoneyError(f"bad amount {amount!r}") from exc
    cents = int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if not 0 < cents <= MAX_CENTS:
        raise MoneyError("amount must be positive")
    return cents


def fmt(cents: int, currency: str) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100:,}.{abs(cents) % 100:02d} {currency}"


def split_equal(total: int, n: int) -> list[int]:
    """Split ``total`` cents into ``n`` shares that differ by at most one cent."""
    base, extra = divmod(total, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def _currency(ccy: str) -> str:
    c = (ccy or "").strip().upper()
    if not _CCY.match(c):
        raise MoneyError(f"currency must be a 3-letter code like USD, not {ccy!r}")
    return c


class MoneyMixin:
    """Node methods for expenses. Mixed into :class:`confer.node.Node`."""

    def add_expense(
        self: "Node",
        title: str,
        amount: str | int | float,
        with_: list[str],
        *,
        currency: str | None = None,
        shares: dict[str, str | int | float] | None = None,
        include_me: bool = True,
        note: str = "",
        plan_id: str = "",
    ) -> list[dict]:
        """I paid ``amount`` for ``title``; ask each person in ``with_`` for their share.
        Equal split by default (optionally including me); or explicit ``shares``
        by contact name. Returns the ledger entries created."""
        from .node import NodeError

        try:
            total = to_cents(amount)
            ccy = _currency(currency or self.config.get("currency", "USD"))
            people = [self._active_contact(n) for n in with_]
            if not people:
                raise MoneyError("split with at least one contact")
            if len({c.agent_id for c in people}) != len(people):
                raise MoneyError("a contact is listed twice")
            if shares:
                by_name = {self._contact_named(k).agent_id: to_cents(v) for k, v in shares.items()}
                if set(by_name) != {c.agent_id for c in people}:
                    raise MoneyError("give a share for exactly the people in the split")
                amounts = [by_name[c.agent_id] for c in people]
                if sum(amounts) > total:
                    raise MoneyError("shares add up to more than the total")
            else:
                parts = split_equal(total, len(people) + (1 if include_me else 0))
                amounts = parts[1:] if include_me else parts  # the payer keeps the rounding cent
        except MoneyError as exc:
            raise NodeError(str(exc)) from exc
        entries = []
        pay_link = self.config.get("pay_link", "")
        for c, share in zip(people, amounts):
            entry = {
                "id": secrets.token_hex(8), "kind": "expense", "contact": c.agent_id, "status": "pending",
                "title": title.strip()[:120] or "Expense", "currency": ccy, "total_cents": total, "cents": share,
                "payer": "me", "note": note[:500], "plan_id": plan_id[:64], "created_at": self.now(),
            }
            self.store.save_entry(entry)
            self._send(c, "money.expense", {
                "entry_id": entry["id"], "title": entry["title"], "currency": ccy, "total_cents": total,
                "share_cents": share, "people": len(people) + (1 if include_me else 0), "note": entry["note"],
                "plan_id": entry["plan_id"], "pay_link": pay_link,
            }, kick=False)
            entries.append(entry)
        self.kick()
        return entries

    def record_payment(self: "Node", to: str, amount: str | int | float, *, currency: str | None = None, note: str = "") -> dict:
        """I paid ``to`` back (cash, Venmo, ...). They confirm it on their side."""
        from .node import NodeError

        c = self._active_contact(to)
        try:
            cents, ccy = to_cents(amount), _currency(currency or self.config.get("currency", "USD"))
        except MoneyError as exc:
            raise NodeError(str(exc)) from exc
        entry = {"id": secrets.token_hex(8), "kind": "settle", "contact": c.agent_id, "status": "pending", "title": "Payment",
                 "currency": ccy, "cents": cents, "payer": "me", "note": note[:500], "created_at": self.now()}
        self.store.save_entry(entry)
        self._send(c, "money.settle", {"entry_id": entry["id"], "currency": ccy, "cents": cents, "note": entry["note"]})
        return entry

    def answer_entry(self: "Node", entry_id: str, accept: bool, note: str = "") -> dict:
        """Accept or dispute an expense share / payment someone recorded with you."""
        from .node import NodeError

        entry = self.store.find_entry(entry_id)
        if not entry or entry["payer"] == "me":  # only the other side's requests need my answer
            raise NodeError(f"no request {entry_id!r} waiting for your answer")
        if entry["status"] not in ("pending", "disputed"):
            raise NodeError(f"that entry is already {entry['status']}")
        entry["status"] = "accepted" if accept else "disputed"
        self.store.save_entry(entry)
        self.store.close_inbox(ref=entry["id"])
        c = self.store.contact(entry["contact"])
        if c:
            self._send(c, "money.ack", {"entry_id": entry["id"], "status": entry["status"], "note": note[:500]})
        return entry

    def cancel_entry(self: "Node", entry_id: str) -> dict:
        from .node import NodeError

        entry = self.store.find_entry(entry_id)
        if not entry or entry["payer"] != "me":
            raise NodeError("you can only cancel entries you created")
        entry["status"] = "cancelled"
        self.store.save_entry(entry)
        if c := self.store.contact(entry["contact"]):
            self._send(c, "money.cancel", {"entry_id": entry["id"]})
        return entry

    def balances(self: "Node") -> list[dict]:
        """Per contact and currency: positive = they owe me; negative = I owe them."""
        buckets: dict[str, dict[tuple[str, str], int]] = {"accepted": {}, "pending": {}, "disputed": {}}
        for e in self.store.entries():
            key = (e["contact"], e["currency"])
            # expense I paid: they owe me; payment I made: reduces what I owe (so +)
            sign = 1 if e["payer"] == "me" else -1
            if (bucket := buckets.get(e["status"])) is not None:
                bucket[key] = bucket.get(key, 0) + sign * e["cents"]
        out = []
        for key in sorted(set().union(*buckets.values())):
            c = self.store.contact(key[0])
            bal = buckets["accepted"].get(key, 0)
            out.append({"contact": c.name if c else key[0][:8], "currency": key[1], "balance_cents": bal,
                        "pending_cents": buckets["pending"].get(key, 0), "disputed_cents": buckets["disputed"].get(key, 0),
                        "balance": fmt(bal, key[1])})
        return out

    def ledger(self: "Node", contact: str | None = None) -> list[dict]:
        cid = self._contact_named(contact).agent_id if contact else None
        return self.store.entries(cid)

    # ------------------------------------------------------------ handlers
    def _money_handlers(self: "Node") -> dict[str, Callable[[Contact, dict], None]]:
        return {"money.expense": self._on_expense, "money.settle": self._on_settle, "money.ack": self._on_money_ack,
                "money.cancel": self._on_money_cancel}

    def _incoming_entry(self: "Node", contact: Contact, body: dict, kind: str) -> dict | None:
        from .node import Rejected, _clean_url

        if not contact.can("money"):
            raise Rejected(f"{self.name} hasn't allowed expense requests from you")
        eid = str(body.get("entry_id", ""))
        if not (8 <= len(eid) <= 32 and eid.isalnum()):
            raise Rejected("bad entry id")
        if (prior := self.store.entry(eid)) is not None:
            if prior["contact"] != contact.agent_id:
                raise Rejected("entry id collision")
            return None  # retry of something already recorded
        cents = body.get("share_cents" if kind == "expense" else "cents")
        if not isinstance(cents, int) or isinstance(cents, bool) or not 0 < cents <= MAX_CENTS:
            raise Rejected("bad amount")
        try:
            ccy = _currency(str(body.get("currency", "")))
        except MoneyError as exc:
            raise Rejected(str(exc)) from exc
        total = body.get("total_cents", cents)
        return {
            "id": eid, "kind": kind, "contact": contact.agent_id, "status": "pending", "payer": "them",
            "title": str(body.get("title", "Payment" if kind == "settle" else "Expense"))[:120], "currency": ccy, "cents": cents,
            "total_cents": total if isinstance(total, int) and cents <= total <= MAX_CENTS else cents,
            "note": str(body.get("note", ""))[:500], "plan_id": str(body.get("plan_id", ""))[:64],
            "pay_link": _clean_url(body.get("pay_link")) if kind == "expense" else "", "created_at": self.now(),
        }

    def _on_expense(self: "Node", contact: Contact, body: dict) -> None:
        entry = self._incoming_entry(contact, body, "expense")
        if entry is None:
            return
        self.store.save_entry(entry)
        pay = f" Pay: {entry['pay_link']}" if entry["pay_link"] else ""
        note = f' — "{entry["note"][:120]}"' if entry["note"] else ""
        self._inbox("money", f"💸 {contact.name} paid {fmt(entry['total_cents'], entry['currency'])} for {entry['title']!r}; "
                    f"your share is {fmt(entry['cents'], entry['currency'])}{note}.{pay} "
                    f"confer money accept {entry['id']} (or dispute)", ref=entry["id"], contact=contact.agent_id, actionable=True)

    def _on_settle(self: "Node", contact: Contact, body: dict) -> None:
        entry = self._incoming_entry(contact, body, "settle")
        if entry is None:
            return
        self.store.save_entry(entry)
        self._inbox("money", f"💵 {contact.name} says they paid you {fmt(entry['cents'], entry['currency'])}. "
                    f"Confirm: confer money accept {entry['id']} (or dispute)", ref=entry["id"], contact=contact.agent_id, actionable=True)

    def _on_money_ack(self: "Node", contact: Contact, body: dict) -> None:
        from .node import Rejected

        entry = self.store.entry(str(body.get("entry_id", "")))
        if not entry or entry["contact"] != contact.agent_id or entry["payer"] != "me":
            raise Rejected("unknown entry")
        status = body.get("status")
        if status not in ("accepted", "disputed") or entry["status"] == "cancelled":
            return
        entry["status"] = status
        self.store.save_entry(entry)
        what = f"{entry['title']!r} ({fmt(entry['cents'], entry['currency'])})"
        why = f' — "{str(body.get("note"))[:200]}"' if body.get("note") else ""
        if status == "accepted":
            self._inbox("money", f"✅ {contact.name} confirmed {what}.", ref=entry["id"], contact=contact.agent_id)
        else:
            self._inbox("money", f"⚠️ {contact.name} disputed {what}{why}.", ref=entry["id"], contact=contact.agent_id, actionable=True)

    def _on_money_cancel(self: "Node", contact: Contact, body: dict) -> None:
        entry = self.store.entry(str(body.get("entry_id", "")))
        if not entry or entry["contact"] != contact.agent_id or entry["payer"] != "them":
            return
        entry["status"] = "cancelled"
        self.store.save_entry(entry)
        self.store.close_inbox(ref=entry["id"])
        self._inbox("money", f"↩️ {contact.name} cancelled {entry['title']!r}.", ref=entry["id"], contact=contact.agent_id)
