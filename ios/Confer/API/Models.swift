import Foundation

// Shapes mirror the tool outputs in src/confer/tools.py (snake_case → camelCase).

struct WhoAmI: Decodable {
    let name: String
    let agentId: String
    let fingerprint: String
    let endpoint: String
    let tz: String
}

struct InboxItem: Decodable, Identifiable, Hashable {
    let id: Int
    let kind: String
    let summary: String
    let actionable: Bool
    let status: String
    let ref: String
}

struct Events: Decodable {
    let events: [InboxItem]
    let lastId: Int
}

struct Contact: Decodable, Identifiable, Hashable {
    let name: String
    let status: String
    let grants: [String]
    let fingerprint: String
    var id: String { fingerprint }
}

struct PlanOption: Decodable, Hashable, Identifiable {
    let n: Int
    let start: String
    let end: String
    let label: String
    let freeForMe: Bool?
    var id: Int { n }
}

struct PlanPerson: Decodable, Hashable {
    let name: String
    let status: String?
}

struct Plan: Decodable, Identifiable, Hashable {
    let planId: String
    let title: String
    let status: String
    let role: String?
    let organizer: String?
    let location: String
    let notes: String
    let recurrence: String?
    let myStatus: String?
    let options: [PlanOption]
    let chosen: Int?
    let people: [PlanPerson]
    var id: String { planId }
    var isOrganizer: Bool { role == "organizer" }
    var needsMyAnswer: Bool { role == "participant" && status == "proposed" && myStatus == "invited" }
}

struct ListItem: Decodable, Hashable, Identifiable {
    let n: Int
    let id: String
    let text: String
    let done: Bool
    let claimedBy: String
}

struct SharedList: Decodable, Identifiable, Hashable {
    let listId: String
    let title: String
    let owner: String?
    let mine: Bool
    let members: [String]
    let items: [ListItem]
    var id: String { listId }
}

struct Balance: Decodable, Hashable, Identifiable {
    let contact: String
    let currency: String
    let balanceCents: Int
    let pendingCents: Int
    let disputedCents: Int
    let balance: String
    var id: String { contact + currency }
}

struct LedgerEntry: Decodable, Hashable, Identifiable {
    let id: String
    let kind: String
    let status: String
    let title: String
    let currency: String
    let cents: Int
    let payer: String
    let note: String?
    let payLink: String?
    let contact: String
    let amount: String
    let needsMyAnswer: Bool
}

struct Presence: Decodable, Hashable, Identifiable {
    let contact: String
    let summary: String
    let text: String?
    let etaMinutes: Int?
    let lat: Double?
    let lon: Double?
    var id: String { contact }
}

struct ItineraryItem: Decodable, Hashable, Identifiable {
    let id: String
    let kind: String
    let title: String
    let start: String
    let end: String
    let location: String
    let confirmation: String
    let details: String
    let url: String
    let addedByName: String
}

struct TravelLeg: Decodable, Hashable {
    let when: String
    let how: String
    let `where`: String
    let needsPickup: Bool
}

struct Traveler: Decodable, Hashable {
    let name: String
    let notes: String
    let arrive: TravelLeg
    let depart: TravelLeg
}

struct Ride: Decodable, Hashable, Identifiable {
    let id: String
    let driverName: String
    let seats: Int
    let from: String
    let leavesAt: String
    let passengers: [String]
}

struct Room: Decodable, Hashable, Identifiable {
    let id: String
    let name: String
    let beds: Int
    let occupants: [String]
}

struct TripTask: Decodable, Hashable, Identifiable {
    let id: String
    let text: String
    let due: String
    let done: Bool
    let assigneeName: String
}

struct PollOption: Decodable, Hashable, Identifiable {
    let option: String
    let text: String
    let votes: Int
    var id: String { option }
}

struct Poll: Decodable, Hashable, Identifiable {
    let id: String
    let question: String
    let closed: Bool
    let options: [PollOption]
    let myVote: String?
}

struct TripLinks: Decodable, Hashable {
    let listId: String?
}

struct Trip: Decodable, Identifiable, Hashable {
    let id: String
    let title: String
    let destination: String
    let startDate: String
    let endDate: String
    let status: String
    let ownerName: String
    let notes: String
    let itinerary: [ItineraryItem]
    let rides: [Ride]
    let rooms: [Room]
    let tasks: [TripTask]
    let links: TripLinks
    let mine: Bool
    let people: [String]
    let travelers: [Traveler]
    let polls: [Poll]
}

struct TripBudgetLine: Decodable, Hashable {
    let othersOweYou: String
    let youOwe: String
}

// MARK: - Formatting

enum Fmt {
    private static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    static func date(_ s: String) -> Date? { s.isEmpty ? nil : iso.date(from: s) }

    static func when(_ s: String, tz: String) -> String {
        guard let d = date(s) else { return "" }
        let f = DateFormatter()
        f.timeZone = TimeZone(identifier: tz) ?? .current
        f.dateFormat = "EEE MMM d, HH:mm"
        return f.string(from: d)
    }

    static func day(_ ymd: String) -> String {
        let p = DateFormatter()
        p.dateFormat = "yyyy-MM-dd"
        p.timeZone = TimeZone(identifier: "UTC")
        guard let d = p.date(from: ymd) else { return ymd }
        let f = DateFormatter()
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "EEE MMM d"
        return f.string(from: d)
    }

    /// UTC ISO-8601 with Z, which the node passes through without re-interpreting.
    static func utc(_ d: Date) -> String { iso.string(from: d) }

    static func ymd(_ d: Date, tz: String) -> String {
        let f = DateFormatter()
        f.timeZone = TimeZone(identifier: tz) ?? .current
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: d)
    }

    static func hm(_ d: Date, tz: String) -> String {
        let f = DateFormatter()
        f.timeZone = TimeZone(identifier: tz) ?? .current
        f.dateFormat = "HH:mm"
        return f.string(from: d)
    }

    /// Hide the CLI hints the node appends to inbox summaries ("… Reply: confer plan respond …").
    static func summary(_ s: String) -> String {
        for marker in [" Reply: confer ", " confer money accept", " confer intro accept", " Revise it: confer", " Join anyway: confer",
                       " Confirm: confer", " (reply: confer", " confer list show", " confer plan respond", " — confer "] {
            if let r = s.range(of: marker) { return String(s[..<r.lowerBound]).trimmingCharacters(in: .whitespaces) }
        }
        return s
    }
}
