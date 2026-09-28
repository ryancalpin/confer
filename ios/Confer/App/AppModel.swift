import Foundation
import Security
import SwiftUI
import UserNotifications

enum Keychain {
    private static let service = "io.github.ryancalpin.confer"

    static func save(_ value: String, for key: String) {
        let base: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                   kSecAttrAccount as String: key]
        SecItemDelete(base as CFDictionary)
        var add = base
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(add as CFDictionary, nil)
    }

    static func load(_ key: String) -> String? {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                kSecAttrAccount as String: key, kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var out: AnyObject?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data else { return nil }
        return String(data: d, encoding: .utf8)
    }

    static func delete(_ key: String) {
        SecItemDelete([kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                       kSecAttrAccount as String: key] as CFDictionary)
    }
}

/// App-wide state: the node connection, cached data for each tab, and polling.
@MainActor
final class AppModel: ObservableObject {
    @Published var api: APIClient?
    @Published var me: WhoAmI?
    @Published var inbox: [InboxItem] = []
    @Published var plans: [Plan] = []
    @Published var trips: [Trip] = []
    @Published var lists: [SharedList] = []
    @Published var balances: [Balance] = []
    @Published var ledger: [LedgerEntry] = []
    @Published var contacts: [Contact] = []
    @Published var presence: [Presence] = []
    @Published var banner: String?

    @AppStorage("nodeURL") private var nodeURL = ""
    @AppStorage("lastEventId") private var lastEventId = 0
    private var poller: Task<Void, Never>?

    var tz: String { me?.tz ?? TimeZone.current.identifier }
    var actionableCount: Int { inbox.filter { $0.actionable && $0.status == "open" }.count }
    var moneyWaiting: Int { ledger.filter(\.needsMyAnswer).count }

    init() {
        if let url = URL(string: nodeURL), !nodeURL.isEmpty, let token = Keychain.load("apiToken") {
            api = APIClient(baseURL: url, token: token)
        }
    }

    // MARK: connection

    func connect(url: String, token: String) async -> String? {
        var s = url.trimmingCharacters(in: .whitespacesAndNewlines)
        while s.hasSuffix("/") { s.removeLast() }
        guard let u = URL(string: s), let scheme = u.scheme, ["https", "http"].contains(scheme), u.host != nil else {
            return "Enter the node's address, e.g. https://alex.your-tailnet.ts.net"
        }
        let client = APIClient(baseURL: u, token: token.trimmingCharacters(in: .whitespacesAndNewlines))
        do {
            me = try await client.call("confer_whoami", as: WhoAmI.self)
        } catch {
            return error.localizedDescription
        }
        nodeURL = s
        Keychain.save(token.trimmingCharacters(in: .whitespacesAndNewlines), for: "apiToken")
        api = client
        lastEventId = 0
        await refreshAll()
        return nil
    }

    func disconnect() {
        poller?.cancel()
        Keychain.delete("apiToken")
        nodeURL = ""
        api = nil
        me = nil
    }

    // MARK: loading

    func refreshAll() async {
        guard let api else { return }
        do {
            async let who = api.call("confer_whoami", as: WhoAmI.self)
            async let i = api.call("confer_inbox", as: [InboxItem].self)
            async let p = api.call("confer_plans", as: [Plan].self)
            async let t = api.call("confer_trips", as: [Trip].self)
            async let l = api.call("confer_lists", as: [SharedList].self)
            async let b = api.call("confer_balances", as: [Balance].self)
            async let g = api.call("confer_ledger", as: [LedgerEntry].self)
            async let c = api.call("confer_contacts", as: [Contact].self)
            async let pr = api.call("confer_presence", as: [Presence].self)
            (me, inbox, plans, trips, lists, balances, ledger, contacts, presence) = try await (who, i, p, t, l, b, g, c, pr)
        } catch {
            banner = error.localizedDescription
        }
    }

    /// Run an action, show errors, then refresh. Returns true on success.
    @discardableResult
    func act(_ tool: String, _ args: [String: Any] = [:]) async -> Bool {
        guard let api else { return false }
        do {
            try await api.run(tool, args)
            try? await Task.sleep(nanoseconds: 400_000_000)  // give peers a moment to answer
            await refreshAll()
            return true
        } catch {
            banner = error.localizedDescription
            return false
        }
    }

    func value<T: Decodable>(_ tool: String, _ args: [String: Any] = [:], as: T.Type) async -> T? {
        guard let api else { return nil }
        do { return try await api.call(tool, args, as: T.self) } catch { banner = error.localizedDescription; return nil }
    }

    // MARK: polling + notifications

    func startPolling() {
        poller?.cancel()
        poller = Task { [weak self] in
            while !Task.isCancelled {
                await self?.pollOnce()
                try? await Task.sleep(nanoseconds: 20_000_000_000)
            }
        }
    }

    func stopPolling() { poller?.cancel() }

    func pollOnce() async {
        guard let api, let ev = try? await api.call("confer_events", ["since_id": lastEventId], as: Events.self) else { return }
        let fresh = ev.events.filter { $0.id > lastEventId }
        lastEventId = ev.lastId
        guard !fresh.isEmpty else { return }
        for item in fresh where item.actionable && item.status == "open" {
            notify(item)
        }
        await refreshAll()
    }

    func requestNotificationPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .badge, .sound]) { _, _ in }
    }

    private func notify(_ item: InboxItem) {
        let content = UNMutableNotificationContent()
        content.title = "Confer"
        content.body = Fmt.summary(item.summary)
        content.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "inbox-\(item.id)", content: content, trigger: nil))
    }
}
