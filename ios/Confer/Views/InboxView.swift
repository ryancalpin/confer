import SwiftUI

struct InboxView: View {
    @EnvironmentObject var model: AppModel
    @State private var showStatus = false
    @State private var showPeople = UserDefaults.standard.bool(forKey: "ConferShowPeople")

    private var open: [InboxItem] { model.inbox.filter { $0.status == "open" } }

    var body: some View {
        List {
            if !model.presence.isEmpty {
                Section("Live now") {
                    ForEach(model.presence) { p in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(p.contact).font(.headline)
                            if let t = p.text, !t.isEmpty { Text(t) }
                            HStack {
                                if let eta = p.etaMinutes { Label("\(eta) min", systemImage: "clock") }
                                if let lat = p.lat, let lon = p.lon,
                                   let url = URL(string: "https://maps.apple.com/?ll=\(lat),\(lon)&q=\(p.contact)") {
                                    Link(destination: url) { Label("Map", systemImage: "map") }
                                }
                            }
                            .font(.subheadline).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            let actionable = open.filter(\.actionable)
            if !actionable.isEmpty {
                Section("Needs you") {
                    ForEach(actionable) { item in row(item) }
                }
            }
            let rest = open.filter { !$0.actionable }
            Section(actionable.isEmpty ? "Inbox" : "Updates") {
                if rest.isEmpty && actionable.isEmpty {
                    if model.loaded { Text("You're all caught up.").foregroundStyle(.secondary) } else { ProgressView() }
                }
                ForEach(rest) { item in row(item) }
            }
        }
        .navigationTitle("Inbox")
        .refreshable { await model.refreshAll() }
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Button { showPeople = true } label: { Label("People & settings", systemImage: "person.2.circle") }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button { showStatus = true } label: { Label("Share status", systemImage: "location.circle") }
            }
        }
        .sheet(isPresented: $showStatus) { ShareStatusView() }
        .sheet(isPresented: $showPeople) { NavigationStack { PeopleView() }.environmentObject(model) }
    }

    @ViewBuilder
    private func row(_ item: InboxItem) -> some View {
        let text = Fmt.summary(item.summary)
        Group {
            if item.kind.hasPrefix("plan"), let plan = model.plans.first(where: { $0.planId == item.ref }) {
                NavigationLink(value: plan) { Text(text) }
            } else if item.kind == "trip", let trip = model.trips.first(where: { $0.id == item.ref }) {
                NavigationLink(value: trip) { Text(text) }
            } else if item.kind == "money", let entry = model.ledger.first(where: { $0.id == item.ref }), entry.needsMyAnswer {
                MoneyRequestRow(entry: entry)
            } else if item.kind == "intro" && item.actionable {
                IntroRow(item: item)
            } else {
                Text(text)
            }
        }
        .swipeActions {
            Button("Done") { Task { await model.act("confer_dismiss", ["item_id": item.id]) } }.tint(.gray)
        }
        .navigationDestination(for: Plan.self) { PlanDetailView(planId: $0.planId) }
        .navigationDestination(for: Trip.self) { TripDetailView(tripId: $0.id) }
    }
}

struct IntroRow: View {
    @EnvironmentObject var model: AppModel
    let item: InboxItem

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(Fmt.summary(item.summary))
            HStack {
                Button("Connect") { Task { await model.act("confer_intro_decide", ["intro_id": item.ref, "accept": true]) } }
                    .buttonStyle(.borderedProminent)
                Button("No thanks") { Task { await model.act("confer_intro_decide", ["intro_id": item.ref, "accept": false]) } }
                    .buttonStyle(.bordered)
            }
        }
    }
}

struct ShareStatusView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @StateObject private var locator = Locator()
    @State private var text = ""
    @State private var eta = ""
    @State private var people: Set<String> = []
    @State private var planId = ""
    @State private var withLocation = false
    @State private var hours = 2.0

    var body: some View {
        NavigationStack {
            Form {
                Section("Status") {
                    TextField("Leaving now, running late…", text: $text)
                    TextField("ETA (minutes)", text: $eta).keyboardType(.numberPad)
                    Toggle("Include my location", isOn: $withLocation)
                    Stepper("Expires in \(Int(hours)) h", value: $hours, in: 1...24)
                }
                Section("Send to everyone in a plan") {
                    Picker("Plan", selection: $planId) {
                        Text("None").tag("")
                        ForEach(model.plans.filter { $0.status != "cancelled" }) { Text($0.title).tag($0.planId) }
                    }
                }
                Section("…or to people") { ContactPicker(selected: $people) }
                if !people.isEmpty {
                    Section {
                        Button("Stop sharing with selected people", role: .destructive) {
                            Task { if await model.act("confer_stop_sharing", ["to_contacts": Array(people)]) { dismiss() } }
                        }
                    }
                }
            }
            .navigationTitle("Share status")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Share") {
                        Task {
                            var args: [String: Any] = ["text": text, "ttl_minutes": Int(hours * 60)]
                            if let m = Int(eta) { args["eta_minutes"] = m }
                            if !planId.isEmpty { args["plan_id"] = planId } else { args["to_contacts"] = Array(people) }
                            if withLocation, let loc = await locator.current() {
                                args["lat"] = loc.coordinate.latitude
                                args["lon"] = loc.coordinate.longitude
                            }
                            if await model.act("confer_share_status", args) { dismiss() }
                        }
                    }
                    .disabled(planId.isEmpty && people.isEmpty)
                }
            }
        }
    }
}
