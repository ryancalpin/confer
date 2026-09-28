import SwiftUI

// MARK: - Lists

struct ListsView: View {
    @EnvironmentObject var model: AppModel
    @State private var creating = false

    var body: some View {
        List {
            if model.lists.isEmpty {
                if model.loaded { Text("No shared lists yet. Tap + to start one.").foregroundStyle(.secondary) } else { ProgressView() }
            }
            ForEach(model.lists) { l in
                NavigationLink { ListDetailView(listId: l.listId) } label: {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(l.title).font(.headline)
                        Text("\(l.items.filter(\.done).count)/\(l.items.count) done · \(l.mine ? "yours" : "from \(l.owner ?? "?")")")
                            .font(.subheadline).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .navigationTitle("Lists")
        .refreshable { await model.refreshAll() }
        .toolbar { Button { creating = true } label: { Image(systemName: "plus") } }
        .sheet(isPresented: $creating) { NewListView() }
    }
}

struct ListDetailView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let listId: String
    @State private var newItem = ""

    private var list: SharedList? { model.lists.first { $0.listId == listId } }

    var body: some View {
        if let l = list {
            List {
                Section {
                    ForEach(l.items) { item in
                        HStack {
                            Button {
                                edit(item.done ? "uncheck" : "check", item.id)
                            } label: {
                                Image(systemName: item.done ? "checkmark.circle.fill" : "circle").font(.title3)
                            }
                            .buttonStyle(.plain)
                            VStack(alignment: .leading) {
                                Text(item.text).strikethrough(item.done)
                                if !item.claimedBy.isEmpty { Text("\(item.claimedBy)'s bringing it").font(.caption).foregroundStyle(.secondary) }
                            }
                            Spacer()
                            if item.claimedBy.isEmpty {
                                Button("I'll bring it") { edit("claim", item.id) }.buttonStyle(.bordered).font(.caption)
                            } else if item.claimedBy == model.me?.name {
                                Button("Unclaim") { edit("unclaim", item.id) }.buttonStyle(.bordered).font(.caption)
                            }
                        }
                        .swipeActions {
                            if l.mine { Button("Remove", role: .destructive) { edit("remove", item.id) } }
                        }
                    }
                    HStack {
                        TextField("Add an item", text: $newItem).onSubmit(add)
                        Button("Add", action: add).disabled(newItem.isEmpty)
                    }
                } header: {
                    Text("With " + l.members.joined(separator: ", "))
                }
                Section {
                    Button(l.mine ? "Delete list for everyone" : "Leave list", role: .destructive) {
                        Task { if await model.act("confer_list_edit", ["list_id": l.listId, "action": l.mine ? "delete" : "leave"]) { dismiss() } }
                    }
                }
            }
            .navigationTitle(l.title)
            .refreshable { await model.refreshAll() }
        } else {
            EmptyState(icon: "checklist", title: "List not found", message: "It may have been closed.")
        }
    }

    private func edit(_ action: String, _ item: String) {
        Task { await model.act("confer_list_edit", ["list_id": listId, "action": action, "item": item]) }
    }

    private func add() {
        let text = newItem.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return }
        newItem = ""
        Task { await model.act("confer_list_edit", ["list_id": listId, "action": "add", "text": text]) }
    }
}

struct NewListView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var items = ""
    @State private var people: Set<String> = []

    var body: some View {
        NavigationStack {
            Form {
                TextField("List name", text: $title)
                Section("Items, one per line") { TextEditor(text: $items).frame(minHeight: 100) }
                Section("Share with") { ContactPicker(selected: $people) }
            }
            .navigationTitle("New list")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Create") {
                        Task {
                            let lines = items.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
                            if await model.act("confer_create_list", ["title": title, "with_contacts": Array(people), "items": lines]) { dismiss() }
                        }
                    }
                    .disabled(title.isEmpty || people.isEmpty)
                }
            }
        }
    }
}

// MARK: - Money

struct MoneyView: View {
    @EnvironmentObject var model: AppModel
    @State private var adding = false
    @State private var paying = false

    var body: some View {
        List {
            let waiting = model.ledger.filter(\.needsMyAnswer)
            if !waiting.isEmpty {
                Section("Waiting for you") { ForEach(waiting) { MoneyRequestRow(entry: $0) } }
            }
            Section("Balances") {
                if model.balances.isEmpty {
                    if model.loaded { Text("All square — no shared expenses yet.").foregroundStyle(.secondary) } else { ProgressView() }
                }
                ForEach(model.balances) { b in
                    HStack {
                        Text(b.contact)
                        Spacer()
                        VStack(alignment: .trailing) {
                            Text(b.balanceCents > 0 ? "owes you \(amount(b.balanceCents, b.currency))"
                                 : b.balanceCents < 0 ? "you owe \(amount(-b.balanceCents, b.currency))" : "square")
                                .foregroundStyle(b.balanceCents > 0 ? .green : b.balanceCents < 0 ? .red : .secondary)
                            if b.pendingCents != 0 { Text("pending \(amount(abs(b.pendingCents), b.currency))").font(.caption).foregroundStyle(.secondary) }
                            if b.disputedCents != 0 { Text("disputed \(amount(abs(b.disputedCents), b.currency))").font(.caption).foregroundStyle(.orange) }
                        }
                    }
                }
            }
            Section("History") {
                ForEach(model.ledger.reversed()) { e in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(e.title)
                            Text(e.kind == "settle" ? (e.payer == "me" ? "You paid \(e.contact)" : "\(e.contact) paid you")
                                 : (e.payer == "me" ? "\(e.contact)'s share" : "Your share to \(e.contact)"))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        VStack(alignment: .trailing) { Text(e.amount); StatusPill.forStatus(e.status) }
                    }
                    .swipeActions {
                        if e.payer == "me" && e.status != "cancelled" {
                            Button("Withdraw", role: .destructive) { Task { await model.act("confer_cancel_money", ["entry_id": e.id]) } }
                        }
                    }
                }
            }
        }
        .navigationTitle("Money")
        .refreshable { await model.refreshAll() }
        .toolbar {
            Menu {
                Button("I paid for something") { adding = true }
                Button("I paid someone back") { paying = true }
            } label: { Image(systemName: "plus") }
        }
        .sheet(isPresented: $adding) { AddExpenseView() }
        .sheet(isPresented: $paying) { RecordPaymentView() }
    }

    private func amount(_ cents: Int, _ ccy: String) -> String {
        let f = NumberFormatter()
        f.numberStyle = .currency
        f.currencyCode = ccy
        return f.string(from: NSNumber(value: Double(cents) / 100)) ?? "\(cents / 100) \(ccy)"
    }
}

struct MoneyRequestRow: View {
    @EnvironmentObject var model: AppModel
    let entry: LedgerEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(entry.kind == "settle" ? "\(entry.contact) says they paid you \(entry.amount)"
                 : "\(entry.contact): your share of \(entry.title) is \(entry.amount)")
            if let note = entry.note, !note.isEmpty { Text(note).font(.caption).foregroundStyle(.secondary) }
            HStack {
                Button(entry.kind == "settle" ? "Confirm" : "Accept") {
                    Task { await model.act("confer_answer_money", ["entry_id": entry.id, "accept": true]) }
                }
                .buttonStyle(.borderedProminent)
                Button("Dispute") { Task { await model.act("confer_answer_money", ["entry_id": entry.id, "accept": false]) } }
                    .buttonStyle(.bordered)
                if let link = entry.payLink, link.hasPrefix("https://"), let url = URL(string: link) {
                    Link("Pay", destination: url).buttonStyle(.bordered)
                }
            }
        }
    }
}

struct AddExpenseView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var amount = ""
    @State private var people: Set<String> = []
    @State private var includeMe = true
    @State private var note = ""

    var body: some View {
        NavigationStack {
            Form {
                TextField("What for", text: $title)
                TextField("Total you paid", text: $amount).keyboardType(.decimalPad)
                Toggle("Include me in the split", isOn: $includeMe)
                TextField("Note", text: $note)
                Section("Split with") { ContactPicker(selected: $people) }
            }
            .navigationTitle("Split an expense")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Send") {
                        Task {
                            if await model.act("confer_split_expense", ["title": title, "amount": amount, "with_contacts": Array(people),
                                                                         "include_me": includeMe, "note": note]) { dismiss() }
                        }
                    }
                    .disabled(title.isEmpty || amount.isEmpty || people.isEmpty)
                }
            }
        }
    }
}

struct RecordPaymentView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var to = ""
    @State private var amount = ""

    var body: some View {
        NavigationStack {
            Form {
                Picker("Paid", selection: $to) {
                    Text("Choose…").tag("")
                    ForEach(model.contacts.filter { $0.status == "active" }) { Text($0.name).tag($0.name) }
                }
                TextField("Amount", text: $amount).keyboardType(.decimalPad)
                Text("They'll be asked to confirm it.").font(.footnote).foregroundStyle(.secondary)
            }
            .navigationTitle("I paid someone back")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Record") { Task { if await model.act("confer_record_payment", ["to": to, "amount": amount]) { dismiss() } } }
                        .disabled(to.isEmpty || amount.isEmpty)
                }
            }
        }
    }
}
