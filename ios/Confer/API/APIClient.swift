import Foundation

/// Talks to a Confer node's REST API: `POST /api/v1/tools/<name>` with a bearer token.
/// Every action in this app is taken by the human themselves, so calls that need
/// the human's OK carry the `Confer-Human-Approved` header.
struct APIError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

final class APIClient {
    let baseURL: URL
    private let token: String
    private let session: URLSession
    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    init(baseURL: URL, token: String) {
        self.baseURL = baseURL
        self.token = token
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 20
        cfg.httpAdditionalHeaders = ["User-Agent": "Confer-iOS/0.4"]
        session = URLSession(configuration: cfg)
    }

    private struct Envelope<T: Decodable>: Decodable {
        let ok: Bool
        let result: T?
        let error: String?
    }

    func call<T: Decodable>(_ tool: String, _ args: [String: Any] = [:], as: T.Type = T.self) async throws -> T {
        var req = URLRequest(url: baseURL.appendingPathComponent("api/v1/tools/\(tool)"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.setValue("true", forHTTPHeaderField: "Confer-Human-Approved")  // the person tapping is the human
        req.httpBody = try JSONSerialization.data(withJSONObject: args.compactMapValues { $0 is NSNull ? nil : $0 })
        let (data, response) = try await session.data(for: req)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status == 401 { throw APIError(message: "The node rejected the API token. Check it in Settings.") }
        if status == 404 && data.isEmpty { throw APIError(message: "The REST API is off on this node (run `confer api enable`).") }
        guard let env = try? decoder.decode(Envelope<T>.self, from: data) else {
            if let err = try? decoder.decode(Envelope<JSONValue>.self, from: data), let msg = err.error {
                throw APIError(message: msg)
            }
            throw APIError(message: "Unexpected reply from the node (HTTP \(status)).")
        }
        if let result = env.result, env.ok { return result }
        throw APIError(message: env.error ?? "The node couldn't do that.")
    }

    /// For tools whose result we don't need.
    func run(_ tool: String, _ args: [String: Any] = [:]) async throws {
        _ = try await call(tool, args, as: JSONValue.self)
    }
}

/// Loosely-typed JSON for results we only pass through (settings, acks).
enum JSONValue: Codable, Hashable {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .number(let n): try c.encode(n)
        case .bool(let b): try c.encode(b)
        case .object(let o): try c.encode(o)
        case .array(let a): try c.encode(a)
        case .null: try c.encodeNil()
        }
    }

    var string: String? { if case .string(let s) = self { return s }; if case .number(let n) = self { return n.clean }; return nil }
    var bool: Bool? { if case .bool(let b) = self { return b }; return nil }
}

extension Double {
    var clean: String { truncatingRemainder(dividingBy: 1) == 0 ? String(Int(self)) : String(self) }
}
