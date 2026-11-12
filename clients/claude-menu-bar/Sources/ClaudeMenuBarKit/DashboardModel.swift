import Combine
import Foundation
import Security

protocol CredentialStore {
    func read(_ key: String) -> String?
    func write(_ value: String?, key: String) throws
}
struct KeychainStore: CredentialStore {
    func read(_ key: String) -> String? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: AppIdentity.keychainService, kSecAttrAccount as String: key, kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var result: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }
    func write(_ value: String?, key: String) throws {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: AppIdentity.keychainService, kSecAttrAccount as String: key]
        guard let value else {
            let status = SecItemDelete(query as CFDictionary)
            guard status == errSecSuccess || status == errSecItemNotFound else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
            return
        }
        let data = Data(value.utf8)
        var status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecItemNotFound {
            var item = query; item[kSecValueData as String] = data
            item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            status = SecItemAdd(item as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
    }
}

@MainActor
public final class DashboardModel: ObservableObject {
    @Published public var showingSettings: Bool
    @Published public private(set) var overview: ProxyOverview?
    @Published public private(set) var isRefreshing = false
    @Published public private(set) var isSigningIn = false
    @Published public private(set) var lastUpdated: Date?
    @Published public private(set) var error: String?
    @Published public private(set) var settingsError: String?
    @Published public private(set) var isConfigured: Bool
    let credentials: CredentialStore
    let defaults: UserDefaults
    let session: URLSession
    var baseURL: String { defaults.string(forKey: "proxy.baseURL") ?? "" }
    var username: String { defaults.string(forKey: "proxy.username") ?? "" }

    public convenience init() { self.init(credentials: KeychainStore(), defaults: .standard, session: .shared) }
    init(credentials: CredentialStore, defaults: UserDefaults, session: URLSession) {
        self.credentials = credentials; self.defaults = defaults; self.session = session
        let configured = credentials.read("token") != nil && !(defaults.string(forKey: "proxy.baseURL") ?? "").isEmpty
        isConfigured = configured; showingSettings = !configured
    }
    func authenticate(baseURL: String, username: String, password: String) async {
        guard !isSigningIn && !isRefreshing else { return }
        isSigningIn = true; settingsError = nil
        defer { isSigningIn = false }
        do {
            let user = username.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !user.isEmpty && !password.isEmpty else { throw NSError(domain: "Sign in", code: 1, userInfo: [NSLocalizedDescriptionKey: "Username and password are required."]) }
            let api = try ProxyAPI(baseURL: baseURL, session: session)
            let token = try await api.login(username: user, password: password)
            try credentials.write(password, key: "password"); try credentials.write(token, key: "token")
            defaults.set(api.baseURL.absoluteString, forKey: "proxy.baseURL"); defaults.set(user, forKey: "proxy.username")
            overview = nil; lastUpdated = nil; error = nil
            isConfigured = true; showingSettings = false
            await refresh()
        } catch { settingsError = error.localizedDescription }
    }
    private func readOverview(api: ProxyAPI, token: String) async throws -> ProxyOverview {
        do { return try await api.overview(token: token) }
        catch let error as ProxyAPIError where error.isUnauthorized {
            guard let password = credentials.read("password") else { throw error }
            let newToken = try await api.login(username: username, password: password)
            try credentials.write(newToken, key: "token")
            return try await api.overview(token: newToken)
        }
    }
    public func refresh(force: Bool = false) async {
        guard isConfigured && !isRefreshing else { return }
        isRefreshing = true; error = nil
        defer { isRefreshing = false }
        do {
            let api = try ProxyAPI(baseURL: baseURL, session: session)
            guard let token = credentials.read("token") else { return }
            // Renew an expired session before starting a possibly long force-refresh.
            let next = try await readOverview(api: api, token: token)
            if force {
                let failures = try await api.refreshQuotas(token: credentials.read("token") ?? token)
                overview = try await readOverview(api: api, token: credentials.read("token") ?? token)
                if failures > 0 { error = "\(failures) account(s) could not refresh. Showing latest available quotas." }
            } else { overview = next }
            lastUpdated = Date()
        } catch is CancellationError { }
        catch let failure as URLError where failure.code == .cancelled { }
        catch let failure as ProxyAPIError where failure.isUnauthorized {
            try? credentials.write(nil, key: "token"); try? credentials.write(nil, key: "password")
            overview = nil; lastUpdated = nil; isConfigured = false; showingSettings = true
            settingsError = "Session expired. Sign in again."
        } catch { self.error = error.localizedDescription }
    }
    func signOut() {
        do { try credentials.write(nil, key: "token"); try credentials.write(nil, key: "password") }
        catch { settingsError = error.localizedDescription; return }
        overview = nil; lastUpdated = nil; error = nil; settingsError = nil
        isConfigured = false; showingSettings = true
    }
    public static func resetText(_ date: Date?, now: Date = Date()) -> String {
        guard let date else { return "Next reset unavailable" }
        let seconds = Int(date.timeIntervalSince(now))
        if seconds <= 0 { return "Resetting…" }
        let minutes = seconds / 60, hours = minutes / 60, days = hours / 24
        let span = days > 0 ? "\(days)d \(hours % 24)h" : hours > 0 ? "\(hours)h \(minutes % 60)m" : minutes > 0 ? "\(minutes)m" : "\(seconds)s"
        return "Next reset in \(span)"
    }
}
