import SwiftUI

struct OnboardingView: View {
    @EnvironmentObject var model: AppModel
    @State private var url = ""
    @State private var token = ""
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    VStack(alignment: .leading, spacing: 8) {
                        Image(systemName: "person.2.wave.2.fill").font(.system(size: 44)).foregroundStyle(.tint)
                        Text("Confer").font(.largeTitle.bold())
                        Text("Your agent, talking to your people's agents — plans, trips, lists and money, privately.")
                            .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 8)
                }
                Section("Your node") {
                    TextField("https://you.your-tailnet.ts.net", text: $url)
                        .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                    SecureField("API token", text: $token)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                } footer: {
                    Text("On the machine running Confer: `confer api enable` shows the URL and token. Use HTTPS (e.g. `tailscale serve`).")
                }
                if let error {
                    Section { Text(error).foregroundStyle(.red) }
                }
                Section {
                    Button {
                        busy = true
                        Task {
                            error = await model.connect(url: url, token: token)
                            busy = false
                        }
                    } label: {
                        HStack { Spacer(); if busy { ProgressView() } else { Text("Connect").bold() }; Spacer() }
                    }
                    .disabled(url.isEmpty || token.isEmpty || busy)
                }
            }
            .navigationTitle("Welcome")
        }
    }
}

struct StatusPill: View {
    let text: String
    var color: Color = .secondary

    var body: some View {
        Text(text)
            .font(.caption.weight(.semibold))
            .padding(.horizontal, 8).padding(.vertical, 3)
            .background(color.opacity(0.15), in: Capsule())
            .foregroundStyle(color)
    }

    static func forStatus(_ s: String) -> StatusPill {
        switch s {
        case "confirmed", "booked", "accepted", "done": return StatusPill(text: s, color: .green)
        case "proposed", "planning", "pending", "invited": return StatusPill(text: s, color: .orange)
        case "cancelled", "declined", "disputed": return StatusPill(text: s, color: .red)
        case "needs_reschedule": return StatusPill(text: "reschedule", color: .purple)
        default: return StatusPill(text: s)
        }
    }
}

/// Multi-select of contacts by name.
struct ContactPicker: View {
    @EnvironmentObject var model: AppModel
    @Binding var selected: Set<String>

    var body: some View {
        if model.contacts.filter({ $0.status == "active" }).isEmpty {
            Text("No contacts yet — invite someone from People.").foregroundStyle(.secondary)
        }
        ForEach(model.contacts.filter { $0.status == "active" }) { c in
            Button {
                if selected.contains(c.name) { selected.remove(c.name) } else { selected.insert(c.name) }
            } label: {
                HStack {
                    Text(c.name).foregroundStyle(.primary)
                    Spacer()
                    if selected.contains(c.name) { Image(systemName: "checkmark").foregroundStyle(.tint) }
                }
            }
        }
    }
}

struct EmptyState: View {
    let icon: String
    let title: String
    let message: String

    var body: some View {
        ContentUnavailableView(title, systemImage: icon, description: Text(message))
    }
}
