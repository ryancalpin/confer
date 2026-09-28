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
    @Published var loaded = false  // first refresh finished — before that, show progress instead of empty states

    @AppStorage("nodeURL") private var nodeURL = ""
    @AppStorage("lastEventId") private var lastEventId = 0
    private var poller: Task<Void, Never>?

    var tz: String { me?.tz ?? TimeZone.current.identifier }
    var actionableCount: Int { inbox.filter { $0.actionable && $0.status == "open" }.count }
    var moneyWaiting: Int { ledger.filter(\.needsMyAnswer).count }

    init() {
        #if DEBUG
        // Simulator/UI testing: `-ConferDemoURL http://127.0.0.1:3067 -ConferDemoToken …` (DEBUG builds only)
        let d = UserDefaults.standard
        if let u = d.string(forKey: "ConferDemoURL"), let t = d.string(forKey: "ConferDemoToken"), let url = URL(string: u) {
            api = APIClient(baseURL: url, token: t)
            return
        }
        #endif
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

    /// Load every section independently so the screen fills in as data arrives,
    /// and one failing section can't blank the others.
    func refreshAll() async {
        guard let api else { return }
        func load<T: Decodable>(_ tool: String, _ type: T.Type, _ assign: @escaping @MainActor (T) -> Void) async {
            do { assign(try await api.call(tool, as: T.self)) } catch { banner = error.localizedDescription }
        }
        await withTaskGroup(of: Void.self) { g in
            g.addTask { await load("confer_inbox", [InboxItem].self) { self.inbox = $0 } }
            g.addTask { await load("confer_presence", [Presence].self) { self.presence = $0 } }
            g.addTask { await load("confer_plans", [Plan].self) { self.plans = $0 } }
            g.addTask { await load("confer_ledger", [LedgerEntry].self) { self.ledger = $0 } }
            g.addTask { await load("confer_trips", [Trip].self) { self.trips = $0 } }
            g.addTask { await load("confer_lists", [SharedList].self) { self.lists = $0 } }
            g.addTask { await load("confer_balances", [Balance].self) { self.balances = $0 } }
            g.addTask { await load("confer_contacts", [Contact].self) { self.contacts = $0 } }
            g.addTask { await load("confer_whoami", WhoAmI.self) { self.me = $0 } }
        }
        loaded = true
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
        #if DEBUG
        if UserDefaults.standard.string(forKey: "ConferDemoURL") != nil { return }  // demo/screenshot runs
        #endif
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .badge, .sound]) { _, _ in }
    }

    private func notify(_ item: InboxItem) {
        #if DEBUG
        if UserDefaults.standard.string(forKey: "ConferDemoURL") != nil { return }
        #endif
        let summary = Fmt.summary(item.summary), id = item.id
        UNUserNotificationCenter.current().getNotificationSettings { settings in
            guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else { return }
            let content = UNMutableNotificationContent()
            content.title = "Confer"
            content.body = summary
            content.sound = .default
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "inbox-\(id)", content: content, trigger: nil))
        }
    }

}
