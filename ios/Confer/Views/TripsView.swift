import SwiftUI

struct TripsView: View {
    @EnvironmentObject var model: AppModel
    @State private var creating = false
    @State private var openFirst = false  // DEBUG screenshot hook: -ConferOpenFirst YES

    var body: some View {
        List {
            let active = model.trips.filter { $0.status != "cancelled" }.sorted { $0.startDate < $1.startDate }
            Section {
                if active.isEmpty {
                    if model.loaded { Text("No trips yet. Tap + to start one.").foregroundStyle(.secondary) } else { ProgressView() }
                }
                ForEach(active) { t in
                    NavigationLink(value: t) {
                        VStack(alignment: .leading, spacing: 4) {
                            HStack { Text(t.title).font(.headline); Spacer(); StatusPill.forStatus(t.status) }
                            Text("\(Fmt.day(t.startDate)) → \(Fmt.day(t.endDate))\(t.destination.isEmpty ? "" : " · \(t.destination)")")
                                .font(.subheadline).foregroundStyle(.secondary)
                            Text(t.people.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            let cancelled = model.trips.filter { $0.status == "cancelled" }
            if !cancelled.isEmpty {
                Section("Cancelled") { ForEach(cancelled) { t in NavigationLink(t.title, value: t) } }
            }
        }
        .navigationTitle("Trips")
        .navigationDestination(for: Trip.self) { TripDetailView(tripId: $0.id) }
        .navigationDestination(isPresented: $openFirst) { TripDetailView(tripId: model.trips.first?.id ?? "") }
        .onChange(of: model.trips.count) { _, n in
            #if DEBUG
            if n > 0 && UserDefaults.standard.bool(forKey: "ConferOpenFirst") { openFirst = true }
            #endif
        }
        .refreshable { await model.refreshAll() }
        .toolbar { Button { creating = true } label: { Image(systemName: "plus") } }
        .sheet(isPresented: $creating) { NewTripView() }
    }
}

struct TripDetailView: View {
    @EnvironmentObject var model: AppModel
    let tripId: String
    @State private var sheet: TripSheet?

    enum TripSheet: String, Identifiable {
        case itinerary, travel, ride, room, task, poll, expense, edit
        var id: String { rawValue }
    }

    private var trip: Trip? { model.trips.first { $0.id == tripId } }

    var body: some View {
        if let trip {
            List {
                header(trip)
                itinerary(trip)
                travel(trip)
                rides(trip)
                rooms(trip)
                tasks(trip)
                polls(trip)
                Section {
                    if let lid = trip.links.listId, let lst = model.lists.first(where: { $0.listId == lid }) {
                        NavigationLink { ListDetailView(listId: lst.listId) } label: {
                            Label("Packing list (\(lst.items.filter(\.done).count)/\(lst.items.count))", systemImage: "checklist")
                        }
                    }
                    Button { sheet = .expense } label: { Label("Add a trip expense", systemImage: "dollarsign.circle") }
                    Button(trip.mine ? "Cancel trip" : "Leave trip", role: .destructive) {
                        Task { await model.act("confer_trip_edit", ["trip_id": trip.id, "op": trip.mine ? "trip.update" : "leave",
                                                                   "args": trip.mine ? ["status": "cancelled"] : [:]]) }
                    }
                }
            }
            .navigationTitle(trip.title)
            .navigationBarTitleDisplayMode(.inline)
            .refreshable { await model.refreshAll() }
            .toolbar {
                Menu {
                    Button("Add to itinerary") { sheet = .itinerary }
                    Button("My arrival & departure") { sheet = .travel }
                    Button("Offer a ride") { sheet = .ride }
                    if trip.mine { Button("Add a room") { sheet = .room } }
                    Button("Add a task") { sheet = .task }
                    Button("Start a poll") { sheet = .poll }
                    if trip.mine { Button("Edit trip") { sheet = .edit } }
                } label: { Image(systemName: "plus.circle") }
            }
            .sheet(item: $sheet) { s in TripForm(trip: trip, kind: s) }
        } else {
            EmptyState(icon: "suitcase", title: "Trip not found", message: "You may have left it.")
        }
    }

    private func op(_ trip: Trip, _ op: String, _ args: [String: Any]) {
        Task { await model.act("confer_trip_edit", ["trip_id": trip.id, "op": op, "args": args]) }
    }

    @ViewBuilder private func header(_ t: Trip) -> some View {
        Section {
            HStack { Text(t.title).font(.title2.bold()); Spacer(); StatusPill.forStatus(t.status) }
            Text("\(Fmt.day(t.startDate)) → \(Fmt.day(t.endDate))").font(.headline)
            if !t.destination.isEmpty { Label(t.destination, systemImage: "mappin.and.ellipse") }
            if !t.notes.isEmpty { Text(t.notes).foregroundStyle(.secondary) }
            Text("With " + t.people.joined(separator: ", ")).font(.footnote).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private func itinerary(_ t: Trip) -> some View {
        Section("Itinerary") {
            if t.itinerary.isEmpty { Text("Nothing yet — add flights, lodging, activities.").foregroundStyle(.secondary) }
            ForEach(t.itinerary) { i in
                VStack(alignment: .leading, spacing: 3) {
                    Label(i.title, systemImage: icon(i.kind)).font(.headline)
                    if !i.start.isEmpty { Text(Fmt.when(i.start, tz: model.tz)).font(.subheadline) }
                    if !i.location.isEmpty { Text(i.location).font(.subheadline).foregroundStyle(.secondary) }
                    if !i.confirmation.isEmpty { Text("Confirmation \(i.confirmation)").font(.caption.monospaced()) }
                    if !i.details.isEmpty { Text(i.details).font(.caption).foregroundStyle(.secondary) }
                    if i.url.hasPrefix("https://"), let u = URL(string: i.url) { Link("Open link", destination: u).font(.caption) }
                }
                .swipeActions { Button("Remove", role: .destructive) { op(t, "itinerary.remove", ["id": i.id]) } }
            }
        }
    }

    @ViewBuilder private func travel(_ t: Trip) -> some View {
        Section("Arrivals & departures") {
            if t.travelers.isEmpty { Text("Nobody has added travel details yet.").foregroundStyle(.secondary) }
            ForEach(t.travelers, id: \.name) { tr in
                VStack(alignment: .leading, spacing: 3) {
                    HStack { Text(tr.name).font(.headline); if tr.arrive.needsPickup { StatusPill(text: "needs pickup", color: .orange) } }
                    if !tr.arrive.when.isEmpty || !tr.arrive.how.isEmpty {
                        Text("In: \(Fmt.when(tr.arrive.when, tz: model.tz)) \(tr.arrive.how) \(tr.arrive.where)").font(.subheadline)
                    }
                    if !tr.depart.when.isEmpty || !tr.depart.how.isEmpty {
                        Text("Out: \(Fmt.when(tr.depart.when, tz: model.tz)) \(tr.depart.how) \(tr.depart.where)").font(.subheadline)
                    }
                }
            }
        }
    }

    @ViewBuilder private func rides(_ t: Trip) -> some View {
        if !t.rides.isEmpty {
            Section("Rides") {
                ForEach(t.rides) { r in
                    let mine = r.joined
                    HStack {
                        VStack(alignment: .leading) {
                            Text("\(r.driverName)'s car").font(.headline)
                            Text("\(r.from.isEmpty ? "" : "from \(r.from) ")\(Fmt.when(r.leavesAt, tz: model.tz))").font(.subheadline)
                            Text("\(r.passengers.count)/\(r.seats) · \(r.passengers.joined(separator: ", "))").font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if !r.driving {
                            Button(mine ? "Leave" : "Join") { op(t, mine ? "ride.leave" : "ride.join", ["id": r.id]) }
                                .buttonStyle(.bordered).disabled(!mine && r.passengers.count >= r.seats)
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder private func rooms(_ t: Trip) -> some View {
        if !t.rooms.isEmpty {
            Section("Rooms") {
                ForEach(t.rooms) { r in
                    let mine = r.joined
                    HStack {
                        VStack(alignment: .leading) {
                            Text(r.name).font(.headline)
                            Text("\(r.occupants.count)/\(r.beds) · \(r.occupants.joined(separator: ", "))").font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button(mine ? "Leave" : "Take a bed") { op(t, mine ? "room.leave" : "room.join", ["id": r.id]) }
                            .buttonStyle(.bordered).disabled(!mine && r.occupants.count >= r.beds)
                    }
                }
            }
        }
    }

    @ViewBuilder private func tasks(_ t: Trip) -> some View {
        if !t.tasks.isEmpty {
            Section("Tasks") {
                ForEach(t.tasks) { k in
                    Button {
                        op(t, k.done ? "task.undone" : "task.done", ["id": k.id])
                    } label: {
                        HStack {
                            Image(systemName: k.done ? "checkmark.square.fill" : "square")
                            VStack(alignment: .leading) {
                                Text(k.text).strikethrough(k.done).foregroundStyle(.primary)
                                let meta = [k.assigneeName, k.due.isEmpty ? "" : "due \(Fmt.day(k.due))"].filter { !$0.isEmpty }.joined(separator: " · ")
                                if !meta.isEmpty {
                                    Text(meta).font(.caption).foregroundStyle(k.assigneeName == model.me?.name ? .orange : .secondary)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder private func polls(_ t: Trip) -> some View {
        ForEach(t.polls) { p in
            Section(p.closed ? "\(p.question) (closed)" : p.question) {
                ForEach(p.options) { o in
                    Button {
                        op(t, "poll.vote", ["id": p.id, "option": o.option])
                    } label: {
                        HStack {
                            Image(systemName: p.myVote == o.option ? "largecircle.fill.circle" : "circle")
                            Text(o.text).foregroundStyle(.primary)
                            Spacer()
                            Text("\(o.votes)").monospacedDigit().foregroundStyle(.secondary)
                        }
                    }
                    .disabled(p.closed)
                }
                if t.mine && !p.closed { Button("Close poll") { op(t, "poll.close", ["id": p.id]) } }
            }
        }
    }

    private func icon(_ kind: String) -> String {
        ["flight": "airplane", "lodging": "bed.double", "activity": "figure.hiking", "transport": "car", "meal": "fork.knife"][kind] ?? "star"
    }
}

/// One sheet for every trip form, keyed by what's being added.
struct TripForm: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let trip: Trip
    let kind: TripDetailView.TripSheet

    @State private var title = ""
    @State private var itemKind = "activity"
    @State private var hasTime = true
    @State private var start = Date()
    @State private var end = Date().addingTimeInterval(3600)
    @State private var location = ""
    @State private var confirmation = ""
    @State private var details = ""
    @State private var url = ""
    @State private var arriveWhen = Date()
    @State private var arriveHow = ""
    @State private var arriveWhere = ""
    @State private var pickup = false
    @State private var departWhen = Date()
    @State private var departHow = ""
    @State private var seats = 3
    @State private var beds = 2
    @State private var assignee = ""
    @State private var hasDue = false
    @State private var due = Date()
    @State private var options = "Option A\nOption B"
    @State private var amount = ""
    @State private var destination = ""
    @State private var status = "planning"
    @State private var saving = false

    private var valid: Bool {
        switch kind {
        case .itinerary, .room, .task, .poll, .edit: !title.trimmingCharacters(in: .whitespaces).isEmpty
        case .expense: !title.isEmpty && !amount.isEmpty
        case .travel, .ride: true
        }
    }

    var body: some View {
        NavigationStack {
            Form { fields }
                .navigationTitle(titleText)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Save") { Task { saving = true; if await save() { dismiss() }; saving = false } }
                            .disabled(saving || !valid)
                    }
                }
        }
        .onAppear {
            destination = trip.destination
            status = trip.status
            title = kind == .edit ? trip.title : ""
        }
    }

    private var titleText: String {
        switch kind {
        case .itinerary: "Itinerary item"
        case .travel: "My travel"
        case .ride: "Offer a ride"
        case .room: "Add a room"
        case .task: "Add a task"
        case .poll: "Start a poll"
        case .expense: "Trip expense"
        case .edit: "Edit trip"
        }
    }

    @ViewBuilder private var fields: some View {
        switch kind {
        case .itinerary:
            Picker("Kind", selection: $itemKind) {
                ForEach(["flight", "lodging", "activity", "transport", "meal", "other"], id: \.self) { Text($0.capitalized).tag($0) }
            }
            TextField("Title", text: $title)
            Toggle("Has a time", isOn: $hasTime)
            if hasTime {
                DatePicker("Starts", selection: $start)
                DatePicker("Ends", selection: $end, in: start...)
            }
            TextField("Where", text: $location)
            TextField("Confirmation code", text: $confirmation).textInputAutocapitalization(.characters)
            TextField("Details", text: $details)
            TextField("https:// link", text: $url).keyboardType(.URL).textInputAutocapitalization(.never)
        case .travel:
            Section("Arriving") {
                DatePicker("When", selection: $arriveWhen)
                TextField("How (UA 1234, driving…)", text: $arriveHow)
                TextField("Where (airport, station…)", text: $arriveWhere)
                Toggle("I need a pickup", isOn: $pickup)
            }
            Section("Leaving") {
                DatePicker("When", selection: $departWhen)
                TextField("How", text: $departHow)
            }
        case .ride:
            Stepper("\(seats) seats", value: $seats, in: 1...8)
            TextField("Leaving from", text: $location)
            DatePicker("Leaves at", selection: $start)
        case .room:
            TextField("Room name", text: $title)
            Stepper("\(beds) beds", value: $beds, in: 1...12)
        case .task:
            TextField("What needs doing", text: $title)
            Picker("Who", selection: $assignee) {
                Text("Anyone").tag("")
                Text("Me").tag("me")
                ForEach(trip.people.filter { $0 != model.me?.name }, id: \.self) { Text($0).tag($0) }
            }
            Toggle("Due date", isOn: $hasDue)
            if hasDue { DatePicker("Due", selection: $due, displayedComponents: .date) }
        case .poll:
            TextField("Question", text: $title)
            Section("Options, one per line") { TextEditor(text: $options).frame(minHeight: 100) }
        case .expense:
            TextField("What for", text: $title)
            TextField("Amount you paid", text: $amount).keyboardType(.decimalPad)
            Text("Split equally with everyone else on the trip who's your contact.").font(.footnote).foregroundStyle(.secondary)
        case .edit:
            TextField("Title", text: $title)
            TextField("Destination", text: $destination)
            Picker("Status", selection: $status) {
                ForEach(["planning", "booked", "cancelled"], id: \.self) { Text($0.capitalized).tag($0) }
            }
        }
    }

    private func save() async -> Bool {
        let base: [String: Any] = ["trip_id": trip.id]
        func edit(_ op: String, _ args: [String: Any]) async -> Bool {
            await model.act("confer_trip_edit", base.merging(["op": op, "args": args]) { $1 })
        }
        switch kind {
        case .itinerary:
            var a: [String: Any] = ["kind": itemKind, "title": title, "location": location, "confirmation": confirmation,
                                    "details": details, "url": url]
            if hasTime { a["start"] = Fmt.utc(start); a["end"] = Fmt.utc(end) }
            return await edit("itinerary.add", a)
        case .travel:
            return await edit("traveler.set", [
                "arrive": ["when": Fmt.utc(arriveWhen), "how": arriveHow, "where": arriveWhere, "needs_pickup": pickup],
                "depart": ["when": Fmt.utc(departWhen), "how": departHow, "where": ""],
            ])
        case .ride:
            return await edit("ride.offer", ["seats": seats, "from": location, "leaves_at": Fmt.utc(start)])
        case .room:
            return await edit("room.add", ["name": title, "beds": beds])
        case .task:
            var a: [String: Any] = ["text": title, "assignee": assignee]
            if hasDue { a["due"] = Fmt.ymd(due, tz: model.tz) }
            return await edit("task.add", a)
        case .poll:
            let opts = options.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
            return await edit("poll.add", ["question": title, "options": opts])
        case .expense:
            let names = model.contacts.map(\.name).filter { trip.people.contains($0) }
            return await model.act("confer_split_expense", ["title": title, "amount": amount, "with_contacts": names, "plan_id": trip.id])
        case .edit:
            return await edit("trip.update", ["title": title, "destination": destination, "status": status])
        }
    }
}

struct NewTripView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var destination = ""
    @State private var people: Set<String> = []
    @State private var start = Date().addingTimeInterval(14 * 86400)
    @State private var end = Date().addingTimeInterval(17 * 86400)
    @State private var packing = true

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Trip name", text: $title)
                    TextField("Destination", text: $destination)
                }
                Section("Dates") {
                    DatePicker("Start", selection: $start, displayedComponents: .date)
                    DatePicker("End", selection: $end, in: start..., displayedComponents: .date)
                }
                Section("Who's coming") { ContactPicker(selected: $people) }
                Toggle("Shared packing list", isOn: $packing)
            }
            .navigationTitle("New trip")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Create") {
                        Task {
                            let ok = await model.act("confer_create_trip", [
                                "title": title, "destination": destination, "with_contacts": Array(people),
                                "start_date": Fmt.ymd(start, tz: model.tz), "end_date": Fmt.ymd(end, tz: model.tz), "packing_list": packing,
                            ])
                            if ok { dismiss() }
                        }
                    }
                    .disabled(title.isEmpty)
                }
            }
        }
    }
}
