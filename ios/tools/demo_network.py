"""Demo network for simulator testing: three nodes on localhost (Mac), seeded with data."""
import shutil, time
from pathlib import Path
from confer.node import Node
from confer.server import ConferServer
from confer import tools as T

root = Path(__file__).resolve().parent / ".demo"
shutil.rmtree(root, ignore_errors=True)
ports = {"Alex": 3067, "Sam": 3071, "Priya": 3072}
nodes, servers = {}, []
for name, port in ports.items():
    n = Node.init(root / name, name, tz="America/Chicago", endpoint=f"http://127.0.0.1:{port}")
    servers.append(ConferServer(n, "127.0.0.1", port, tick_seconds=2).start())
    nodes[name] = n
alex, sam, priya = nodes["Alex"], nodes["Sam"], nodes["Priya"]
def pair(a, b, a_grants=None, b_grants=None):
    b.accept_invite(a.create_invite(b.name, a_grants), grants=b_grants)
    for _ in range(50):
        c = b.store.contact(a.identity.agent_id)
        if c and c.status == "active": return
        time.sleep(0.1)
full = "plans,files,notes,intros,lists,money,trips,location"
pair(alex, sam, full, full)
pair(alex, priya, full, full)
alex.config["api_token"] = "demo-token-for-simulator"
alex.config["pay_link"] = "https://venmo.com/u/alex-demo"
alex.save_config()
# Sam invites Alex to dinner
T.call(sam, "confer_propose_plan", {"title": "Dinner at Luigi's", "with_contacts": ["Alex"], "location": "Luigi's, 5th Ave",
       "explicit_times": ["2026-10-02T19:00/90", "2026-10-03T19:30/90", "2026-10-06T18:30/90"]})
# Alex's trip
t = T.call(alex, "confer_create_trip", {"title": "Tahoe weekend", "with_contacts": ["Sam", "Priya"], "start_date": "2026-10-16",
           "end_date": "2026-10-19", "destination": "Lake Tahoe", "notes": "Bring layers — it gets cold at night."})
tid = t["trip_id"]
for args in ({"kind": "flight", "title": "Flight to Reno (UA 1234)", "start": "2026-10-16T11:05", "end": "2026-10-16T13:40", "location": "ORD → RNO", "confirmation": "K7QX2P"},
             {"kind": "lodging", "title": "Pine Cabin", "start": "2026-10-16T16:00", "end": "2026-10-19T11:00", "location": "12 Pine Rd, South Lake Tahoe", "confirmation": "HMX42", "url": "https://example.com/cabin"},
             {"kind": "activity", "title": "Kayak Emerald Bay", "start": "2026-10-17T10:00", "end": "2026-10-17T13:00", "location": "Emerald Bay State Park"},
             {"kind": "meal", "title": "Dinner at Base Camp Pizza", "start": "2026-10-17T19:00", "location": "Heavenly Village"}):
    T.call(alex, "confer_trip_edit", {"trip_id": tid, "op": "itinerary.add", "args": args})
T.call(alex, "confer_trip_edit", {"trip_id": tid, "op": "ride.offer", "args": {"seats": 3, "from": "RNO airport", "leaves_at": "2026-10-16T14:15"}})
T.call(alex, "confer_trip_edit", {"trip_id": tid, "op": "room.add", "args": {"name": "Loft", "beds": 2}})
T.call(alex, "confer_trip_edit", {"trip_id": tid, "op": "room.add", "args": {"name": "Bunk room", "beds": 3}})
T.call(alex, "confer_trip_edit", {"trip_id": tid, "op": "task.add", "args": {"text": "Book kayaks", "assignee": "Sam", "due": "2026-10-10"}})
T.call(alex, "confer_trip_edit", {"trip_id": tid, "op": "task.add", "args": {"text": "Grocery run for the cabin", "assignee": "me", "due": "2026-10-15"}})
T.call(alex, "confer_trip_edit", {"trip_id": tid, "op": "poll.add", "args": {"question": "Saturday night?", "options": ["Hot tub + cards", "Casino", "Stargazing"]}})
time.sleep(2)
T.call(sam, "confer_trip_edit", {"trip_id": tid, "op": "traveler.set", "args": {"arrive": {"when": "2026-10-16T13:50", "how": "UA 1234", "where": "RNO", "needs_pickup": True}, "depart": {"when": "2026-10-19T12:30", "how": "UA 99"}}})
ride = T.call(sam, "confer_trips", {})[0]["rides"][0]["id"]
T.call(sam, "confer_trip_edit", {"trip_id": tid, "op": "ride.join", "args": {"id": ride}})
poll = T.call(priya, "confer_trips", {})[0]["polls"][0]["id"]
T.call(priya, "confer_trip_edit", {"trip_id": tid, "op": "poll.vote", "args": {"id": poll, "option": "o2"}})
# Priya's BBQ list; Sam's expense; Sam's ETA
T.call(priya, "confer_create_list", {"title": "BBQ at Priya's", "with_contacts": ["Alex"], "items": ["Burgers", "Buns", "Charcoal", "Corn", "Lemonade"]})
T.call(sam, "confer_split_expense", {"title": "Concert tickets", "amount": "180", "with_contacts": ["Alex"], "note": "Khruangbin, Oct 24"})
T.call(alex, "confer_split_expense", {"title": "Cabin deposit", "amount": "450", "with_contacts": ["Sam", "Priya"], "plan_id": tid})
time.sleep(2)
T.call(sam, "confer_share_status", {"to_contacts": ["Alex"], "text": "Leaving work now", "eta_minutes": 25, "lat": 41.8827, "lon": -87.6233})
T.call(sam, "confer_send_note", {"to": "Alex", "text": "Should we get the big kayak or two small ones?", "is_question": True})
entries = T.call(priya, "confer_ledger", {})
if entries: T.call(priya, "confer_answer_money", {"entry_id": entries[0]["id"], "accept": True})
print("seeded; Alex API at http://127.0.0.1:3067 token demo-token-for-simulator", flush=True)
while True:
    time.sleep(60)
