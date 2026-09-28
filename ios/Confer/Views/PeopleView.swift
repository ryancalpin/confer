import CoreLocation
import SwiftUI

struct PeopleView: View {
    @EnvironmentObject var model: AppModel
    @State private var inviting = false
    @State private var accepting = false
    @State private var introducing = false

    var body: some View {
        List {
            if let me = model.me {
                Section("You") {
                    LabeledContent("Name", value: me.name)
                    LabeledContent("Fingerprint", value: me.fingerprint).font(.body.monospaced())
                }
            }
            Section("Trusted people") {
                if model.contacts.isEmpty { Text("Nobody yet — invite someone you trust.").foregroundStyle(.secondary) }
                ForEach(model.contacts) { c in
                    NavigationLink { ContactDetailView(contact: c) } label: {
                        HStack {
                            Text(c.name)
                            Spacer()
                            if c.status != "active" { StatusPill(text: c.status, color: .orange) }
                            if c.grants.contains("location") { Image(systemName: "location.fill").foregroundStyle(.secondary) }
                        }
                    }
                }
            }
            Section {
                Button { inviting = true } label: { Label("Invite someone", systemImage: "person.badge.plus") }
                Button { accepting = true } label: { Label("Accept an invite", systemImage: "envelope.open") }
                Button { introducing = true } label: { Label("Introduce two people", systemImage: "person.2.wave.2") }
                NavigationLink { SettingsView() } label: { Label("Settings", systemImage: "gear") }
            }
        }
        .navigationTitle("People")
        .refreshable { await model.refreshAll() }
        .sheet(isPresented: $inviting) { InviteView() }
        .sheet(isPresented: $accepting) { AcceptInviteView() }
        .sheet(isPresented: $introducing) { IntroduceView() }
    }
}

enum GrantInfo {
    static let all: [(key: String, title: String, detail: String)] = [
        ("plans", "Plans", "Can propose plans to you. You still decide."),
        ("autoconfirm", "Auto-accept plans", "Plans that fit your calendar are accepted without asking."),
        ("trips", "Trips", "Can add you to trips they organize."),
        ("lists", "Shared lists", "Can share lists with you."),
        ("money", "Money", "Can send expense shares and payments for you to confirm."),
        ("files", "Files", "Can send you files (end-to-end encrypted)."),
        ("notes", "Notes", "Can send you short messages."),
        ("intros", "Introductions", "Can introduce people to you. You approve each one."),
        ("location", "Status & location", "Sensitive: can send you their status, ETA and location."),
    ]
}

struct ContactDetailView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let contact: Contact
    @State private var grants: Set<String> = []
    @State private var confirmRemove = false

    var body: some View {
        Form {
            Section {
                LabeledContent("Status", value: contact.status)
                LabeledContent("Fingerprint", value: contact.fingerprint).font(.body.monospaced())
            } footer: { Text("Compare the fingerprint with them in person or on a call to be sure it's really their agent.") }
            Section("What \(contact.name) may do") {
                ForEach(GrantInfo.all, id: \.key) { g in
                    Toggle(isOn: Binding(get: { grants.contains(g.key) },
                                         set: { on in if on { grants.insert(g.key) } else { grants.remove(g.key) } })) {
                        VStack(alignment: .leading) {
                            Text(g.title)
                            Text(g.detail).font(.caption).foregroundStyle(g.key == "location" ? .orange : .secondary)
                        }
                    }
                }
                Button("Save permissions") {
                    Task { await model.act("confer_set_grants", ["name": contact.name, "grants": grants.sorted().joined(separator: ",")]) }
                }
            }
        }
        .navigationTitle(contact.name)
        .onAppear { grants = Set(contact.grants) }
    }
}

struct InviteView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var token: String?

    var body: some View {
        NavigationStack {
            Form {
                if let token {
                    Section {
                        Text(token).font(.footnote.monospaced()).textSelection(.enabled)
                        ShareLink("Send invite", item: token, message: Text("Join me on Confer: confer accept '\(token)'"))
                        Button("Copy") { UIPasteboard.general.string = token }
                    } footer: {
                        Text("It works once. Send it privately — whoever uses it first becomes your contact.")
                    }
                } else {
                    TextField("Their name", text: $name)
                    Text("They'll get the usual permissions (plans, trips, lists, money, files, notes, introductions). Change them later.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Invite someone")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button(token == nil ? "Cancel" : "Done") { dismiss() } }
                if token == nil {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Create") {
                            Task {
                                struct Out: Decodable { let token: String }
                                token = await model.value("confer_invite", ["name": name], as: Out.self)?.token
                            }
                        }
                        .disabled(name.isEmpty)
                    }
                }
            }
        }
    }
}

struct AcceptInviteView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var token = ""
    @State private var name = ""

    var body: some View {
        NavigationStack {
            Form {
                TextField("confer1:…", text: $token, axis: .vertical).font(.footnote.monospaced())
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                TextField("What to call them (optional)", text: $name)
            }
            .navigationTitle("Accept an invite")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Accept") {
                        let clean = token.components(separatedBy: .whitespacesAndNewlines).joined()
                        Task { if await model.act("confer_accept_invite", ["token": clean, "name": name]) { dismiss() } }
                    }
                    .disabled(!token.contains("confer1:"))
                }
            }
        }
    }
}

struct IntroduceView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var a = ""
    @State private var b = ""
    @State private var note = ""

    var body: some View {
        NavigationStack {
            Form {
                Picker("Introduce", selection: $a) {
                    Text("Choose…").tag("")
                    ForEach(model.contacts) { Text($0.name).tag($0.name) }
                }
                Picker("to", selection: $b) {
                    Text("Choose…").tag("")
                    ForEach(model.contacts.filter { $0.name != a }) { Text($0.name).tag($0.name) }
                }
                TextField("Why they should meet (optional)", text: $note)
                Text("Each of them approves; then their agents connect directly.").font(.footnote).foregroundStyle(.secondary)
            }
            .navigationTitle("Introduce")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Send") { Task { if await model.act("confer_introduce", ["contact_a": a, "contact_b": b, "note": note]) { dismiss() } } }
                        .disabled(a.isEmpty || b.isEmpty)
                }
            }
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @State private var s: [String: JSONValue] = [:]
    @State private var saved = false

    private func text(_ key: String) -> Binding<String> {
        Binding(get: { s[key]?.string ?? "" }, set: { s[key] = .string($0) })
    }

    var body: some View {
        Form {
            Section("You") {
                TextField("Name", text: text("name"))
                TextField("Time zone (e.g. America/Chicago)", text: text("tz")).textInputAutocapitalization(.never)
            }
            Section("When you can be scheduled") {
                TextField("From (HH:MM)", text: text("hours_start"))
                TextField("Until (HH:MM)", text: text("hours_end"))
                TextField("Buffer minutes", text: text("buffer_minutes")).keyboardType(.numberPad)
                Toggle("Hold offered times", isOn: Binding(get: { s["tentative_holds"]?.bool ?? true }, set: { s["tentative_holds"] = .bool($0) }))
                Toggle("Trips block my calendar", isOn: Binding(get: { s["trips_block_calendar"]?.bool ?? true },
                                                               set: { s["trips_block_calendar"] = .bool($0) }))
            }
            Section {
                TextField("https:// or webcal:// calendar link", text: text("calendar")).textInputAutocapitalization(.never).keyboardType(.URL)
            } header: { Text("Busy time") } footer: { Text("Your calendar's secret iCal address. It never leaves your node.") }
            Section("Money") {
                TextField("Currency (USD)", text: text("currency")).textInputAutocapitalization(.characters)
                TextField("Pay link (https://venmo.com/u/you)", text: text("pay_link")).textInputAutocapitalization(.never).keyboardType(.URL)
            }
            Section {
                Button(saved ? "Saved ✓" : "Save settings") {
                    Task {
                        let keys = ["name", "tz", "hours_start", "hours_end", "calendar", "currency", "pay_link"]
                        var changes: [String: Any] = [:]
                        for k in keys { if let v = s[k]?.string { changes[k] = v } }
                        if let b = Int(s["buffer_minutes"]?.string ?? "") { changes["buffer_minutes"] = b }
                        changes["tentative_holds"] = s["tentative_holds"]?.bool ?? true
                        changes["trips_block_calendar"] = s["trips_block_calendar"]?.bool ?? true
                        saved = await model.act("confer_update_settings", ["changes": changes])
                    }
                }
            }
            Section("Connection") {
                LabeledContent("Node", value: model.api?.baseURL.absoluteString ?? "")
                Button("Disconnect this phone", role: .destructive) { model.disconnect() }
            }
        }
        .navigationTitle("Settings")
        .task { s = await model.value("confer_settings", as: [String: JSONValue].self) ?? [:] }
    }
}

/// One-shot location for "share my location" — only when the user asks.
@MainActor
final class Locator: NSObject, ObservableObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    private var continuation: CheckedContinuation<CLLocation?, Never>?

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
    }

    func current() async -> CLLocation? {
        if manager.authorizationStatus == .notDetermined { manager.requestWhenInUseAuthorization() }
        return await withCheckedContinuation { c in
            continuation = c
            manager.requestLocation()
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        Task { @MainActor in self.continuation?.resume(returning: locations.last); self.continuation = nil }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        Task { @MainActor in self.continuation?.resume(returning: nil); self.continuation = nil }
    }
}
