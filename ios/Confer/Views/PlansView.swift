import SwiftUI

struct PlansView: View {
    @EnvironmentObject var model: AppModel
    @State private var creating = false
    @State private var openFirst = false  // DEBUG screenshot hook: -ConferOpenFirst YES

    var body: some View {
        List {
            let waiting = model.plans.filter(\.needsMyAnswer)
            if !waiting.isEmpty {
                Section("Waiting for your answer") { ForEach(waiting) { row($0) } }
            }
            let active = model.plans.filter { !$0.needsMyAnswer && $0.status != "cancelled" }
            Section("Plans") {
                if active.isEmpty && waiting.isEmpty {
                    if model.loaded { Text("No plans yet. Tap + to propose one.").foregroundStyle(.secondary) } else { ProgressView() }
                }
                ForEach(active) { row($0) }
            }
            let cancelled = model.plans.filter { $0.status == "cancelled" }
            if !cancelled.isEmpty { Section("Cancelled") { ForEach(cancelled) { row($0) } } }
        }
        .navigationTitle("Plans")
        .navigationDestination(for: Plan.self) { PlanDetailView(planId: $0.planId) }
        .navigationDestination(isPresented: $openFirst) { PlanDetailView(planId: model.plans.first?.planId ?? "") }
        .onChange(of: model.plans.count) { _, n in
            #if DEBUG
            if n > 0 && UserDefaults.standard.bool(forKey: "ConferOpenFirst") { openFirst = true }
            #endif
        }
        .refreshable { await model.refreshAll() }
        .toolbar { Button { creating = true } label: { Image(systemName: "plus") } }
        .sheet(isPresented: $creating) { NewPlanView() }
    }

    private func row(_ p: Plan) -> some View {
        NavigationLink(value: p) {
            VStack(alignment: .leading, spacing: 4) {
                HStack { Text(p.title).font(.headline); Spacer(); StatusPill.forStatus(p.status) }
                if let c = p.chosen, let opt = p.options.first(where: { $0.n == c }) {
                    Text(opt.label).font(.subheadline)
                } else {
                    Text("\(p.options.count) option\(p.options.count == 1 ? "" : "s") · \(p.isOrganizer ? "you organize" : "from \(p.organizer ?? "?")")")
                        .font(.subheadline).foregroundStyle(.secondary)
                }
            }
        }
    }
}

struct PlanDetailView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let planId: String
    @State private var picked: Set<Int> = []
    @State private var preferred: Set<Int> = []
    @State private var note = ""

    private var plan: Plan? { model.plans.first { $0.planId == planId } }

    var body: some View {
        if let plan {
            Form {
                Section {
                    HStack { Text(plan.title).font(.title2.bold()); Spacer(); StatusPill.forStatus(plan.status) }
                    if !plan.location.isEmpty { Label(plan.location, systemImage: "mappin.and.ellipse") }
                    if let r = plan.recurrence { Label(r, systemImage: "repeat") }
                    if !plan.notes.isEmpty { Text(plan.notes).foregroundStyle(.secondary) }
                    Text(plan.isOrganizer ? "You're organizing" : "Organized by \(plan.organizer ?? "?")").font(.footnote)
                }
                if plan.needsMyAnswer {
                    Section {
                        ForEach(plan.options) { opt in
                            HStack {
                                Button {
                                    if picked.contains(opt.n) { picked.remove(opt.n); preferred.remove(opt.n) } else { picked.insert(opt.n) }
                                } label: {
                                    HStack {
                                        Image(systemName: picked.contains(opt.n) ? "checkmark.circle.fill" : "circle")
                                        VStack(alignment: .leading) {
                                            Text(opt.label).foregroundStyle(.primary)
                                            if opt.freeForMe == true { Text("Free on your calendar").font(.caption).foregroundStyle(.green) }
                                        }
                                    }
                                }
                                Spacer()
                                if picked.contains(opt.n) {
                                    Button {
                                        if preferred.contains(opt.n) { preferred.remove(opt.n) } else { preferred.insert(opt.n) }
                                    } label: { Image(systemName: preferred.contains(opt.n) ? "star.fill" : "star").foregroundStyle(.yellow) }
                                        .accessibilityLabel("Prefer this time")
                                }
                            }
                            .buttonStyle(.plain)
                        }
                    } header: { Text("Which times work?") } footer: { Text("Star the ones you'd prefer — the group lands on the most-liked time.") }
                    Section {
                        TextField("Note (optional)", text: $note)
                        Button("Accept") {
                            Task {
                                if await model.act("confer_respond", ["plan_id": plan.planId, "decision": "accept", "options": Array(picked),
                                                                     "preferred": Array(preferred), "note": note]) { dismiss() }
                            }
                        }
                        .disabled(picked.isEmpty)
                        Button("Can't make any", role: .destructive) {
                            Task { if await model.act("confer_respond", ["plan_id": plan.planId, "decision": "decline", "note": note]) { dismiss() } }
                        }
                    }
                    .onAppear { picked = Set(plan.options.filter { $0.freeForMe == true }.map(\.n)) }
                } else {
                    Section(plan.chosen == nil ? "Options" : "When") {
                        ForEach(plan.options) { opt in
                            HStack {
                                Text(opt.label)
                                Spacer()
                                if plan.chosen == opt.n { Image(systemName: "checkmark.seal.fill").foregroundStyle(.green) }
                            }
                        }
                    }
                }
                Section("People") {
                    ForEach(plan.people, id: \.name) { p in
                        HStack { Text(p.name); Spacer(); if let s = p.status { StatusPill.forStatus(s) } }
                    }
                }
                if plan.status != "cancelled" {
                    Section {
                        Button(plan.isOrganizer ? "Cancel plan" : "I can't make it", role: .destructive) {
                            Task { if await model.act("confer_cancel", ["plan_id": plan.planId]) { dismiss() } }
                        }
                    }
                }
            }
            .navigationTitle("Plan")
            .navigationBarTitleDisplayMode(.inline)
        } else {
            EmptyState(icon: "calendar", title: "Plan not found", message: "It may have been removed.")
        }
    }
}

struct NewPlanView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var people: Set<String> = []
    @State private var duration = 60
    @State private var from = Date()
    @State private var to = Date().addingTimeInterval(7 * 86400)
    @State private var limitTimes = false
    @State private var earliest = Calendar.current.date(bySettingHour: 18, minute: 0, second: 0, of: Date()) ?? Date()
    @State private var latest = Calendar.current.date(bySettingHour: 21, minute: 0, second: 0, of: Date()) ?? Date()
    @State private var location = ""
    @State private var quorumAll = true
    @State private var quorum = 2

    var body: some View {
        NavigationStack {
            Form {
                Section { TextField("Dinner, game night…", text: $title) }
                Section("With") { ContactPicker(selected: $people) }
                Section("When") {
                    DatePicker("From", selection: $from, displayedComponents: .date)
                    DatePicker("To", selection: $to, in: from..., displayedComponents: .date)
                    Stepper("\(duration) minutes", value: $duration, in: 15...600, step: 15)
                    Toggle("Only between certain times", isOn: $limitTimes)
                    if limitTimes {
                        DatePicker("Earliest", selection: $earliest, displayedComponents: .hourAndMinute)
                        DatePicker("Latest", selection: $latest, displayedComponents: .hourAndMinute)
                    }
                }
                Section("Details") {
                    TextField("Where (optional)", text: $location)
                    Toggle("Everyone must be able to come", isOn: $quorumAll)
                    if !quorumAll { Stepper("At least \(quorum) people", value: $quorum, in: 1...max(1, people.count)) }
                }
            }
            .navigationTitle("New plan")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Propose") {
                        Task {
                            let tz = model.tz
                            var args: [String: Any] = ["title": title, "with_contacts": Array(people), "duration_minutes": duration,
                                                       "window_from": Fmt.ymd(from, tz: tz), "window_to": Fmt.ymd(to, tz: tz),
                                                       "location": location, "quorum": quorumAll ? "all" : String(quorum)]
                            if limitTimes { args["between"] = "\(Fmt.hm(earliest, tz: tz))-\(Fmt.hm(latest, tz: tz))" }
                            if await model.act("confer_propose_plan", args) { dismiss() }
                        }
                    }
                    .disabled(title.isEmpty || people.isEmpty)
                }
            }
        }
    }
}
