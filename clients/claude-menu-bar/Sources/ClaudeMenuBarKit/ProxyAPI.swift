import Foundation

public struct ProxyOverview: Decodable, Equatable {
    public let activeAccounts: Int
    public let totalAccounts: Int
    public let fiveHourAveragePct: Double
    public let weeklyAveragePct: Double
    public let fiveHourResetAt: Date?
    public let weeklyResetAt: Date?
    enum CodingKeys: String, CodingKey {
        case activeAccounts = "active_accounts", totalAccounts = "total_accounts"
        case fiveHourAveragePct = "five_hour_average_pct", weeklyAveragePct = "weekly_average_pct"
        case fiveHourResetAt = "five_hour_reset_at", weeklyResetAt = "weekly_reset_at"
    }
}

struct RefreshAccount: Decodable {
    let id: UUID
    let status: String
    let providerHealth: String
    enum CodingKeys: String, CodingKey { case id, status, providerHealth = "provider_health" }
    var eligible: Bool { status.uppercased() != "DISABLED" && providerHealth.uppercased() != "REAUTH_REQUIRED" }
}
struct RefreshAccounts: Decodable { let accounts: [RefreshAccount] }
struct LoginResponse: Decodable { let token: String }
struct ErrorResponse: Decodable { let detail: String }

enum ProxyAPIError: LocalizedError {
    case invalidURL, invalidResponse, http(Int, String)
    var errorDescription: String? {
        switch self {
        case .invalidURL: return "Enter a valid HTTP or HTTPS proxy URL."
        case .invalidResponse: return "The proxy returned an invalid response."
        case .http(let status, let message): return "\(message) (HTTP \(status))"
        }
    }
    var isUnauthorized: Bool { if case .http(401, _) = self { return true }; return false }
}

struct ProxyAPI {
    let baseURL: URL
    let session: URLSession
    init(baseURL: String, session: URLSession = .shared) throws {
        guard var parts = URLComponents(string: baseURL.trimmingCharacters(in: .whitespacesAndNewlines)),
              ["http", "https"].contains(parts.scheme?.lowercased() ?? ""),
              parts.host != nil, parts.user == nil, parts.password == nil,
              parts.query == nil, parts.fragment == nil else { throw ProxyAPIError.invalidURL }
        var path = parts.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        if (path == "api/v1" || path.hasSuffix("/api/v1")) { path = String(path.dropLast(6)) }
        else if (path == "api" || path.hasSuffix("/api")) { path = String(path.dropLast(3)) }
        path = path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        parts.path = path.isEmpty ? "/" : "/\(path)/"
        guard let url = parts.url else { throw ProxyAPIError.invalidURL }
        self.baseURL = url; self.session = session
    }
    static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { value in
            let raw = try value.singleValueContainer().decode(String.self)
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = formatter.date(from: raw) { return date }
            formatter.formatOptions = [.withInternetDateTime]
            if let date = formatter.date(from: raw) { return date }
            throw DecodingError.dataCorrupted(.init(codingPath: value.codingPath, debugDescription: "Invalid proxy timestamp"))
        }
        return decoder
    }
    func request(_ path: String, token: String? = nil, method: String = "GET", body: Data? = nil) async throws -> Data {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/v1/" + path))
        request.httpMethod = method; request.httpBody = body
        request.timeoutInterval = 30; request.cachePolicy = .reloadIgnoringLocalCacheData
        if let token { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse else { throw ProxyAPIError.invalidResponse }
        guard (200..<300).contains(response.statusCode) else {
            let detail = (try? JSONDecoder().decode(ErrorResponse.self, from: data).detail) ?? "Proxy request failed"
            throw ProxyAPIError.http(response.statusCode, detail)
        }
        return data
    }
    func login(username: String, password: String) async throws -> String {
        let body = try JSONSerialization.data(withJSONObject: ["username": username, "password": password])
        return try JSONDecoder().decode(LoginResponse.self, from: await request("auth/login", method: "POST", body: body)).token
    }
    func overview(token: String) async throws -> ProxyOverview {
        try Self.decoder().decode(ProxyOverview.self, from: await request("stats/overview", token: token))
    }
    func refreshQuotas(token: String) async throws -> Int {
        let accounts = try JSONDecoder().decode(RefreshAccounts.self, from: await request("accounts", token: token)).accounts.filter(\.eligible)
        var failed = 0
        // Sequential probes prevent a button click from flooding the provider.
        for account in accounts {
            try Task.checkCancellation()
            do { _ = try await request("accounts/\(account.id.uuidString)/refresh-quota", token: token, method: "POST") }
            catch let error as ProxyAPIError where error.isUnauthorized { throw error }
            catch is CancellationError { throw CancellationError() }
            catch { failed += 1 }
        }
        return failed
    }
}
