import AppKit
import SwiftUI
import XCTest
@testable import ClaudeMenuBarKit

private let overviewJSON = """
{"active_accounts":10,"total_accounts":26,"five_hour_average_pct":75.5,"weekly_average_pct":71.8,
"five_hour_reset_at":"2026-11-04T12:00:00.123456Z","weekly_reset_at":"2026-11-06T12:00:00Z"}
"""
final class StubProtocol: URLProtocol {
    static var handler: ((URLRequest) throws -> (Int, String))?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            let (status, body) = try Self.handler!(request)
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(body.utf8)); client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() { }
}
final class MemoryCredentials: CredentialStore {
    var values = ["token": "test-token", "password": "test-password"]
    func read(_ key: String) -> String? { values[key] }
    func write(_ value: String?, key: String) throws { values[key] = value }
}
final class ProxyModelTests: XCTestCase {
    func session() -> URLSession {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [StubProtocol.self]
        return URLSession(configuration: config)
    }
    func testActualOverviewAndFractionalDatesDecode() throws {
        let data = try ProxyAPI.decoder().decode(ProxyOverview.self, from: Data(overviewJSON.utf8))
        XCTAssertEqual(data.activeAccounts, 10); XCTAssertEqual(data.totalAccounts, 26)
        XCTAssertEqual(data.fiveHourAveragePct, 75.5); XCTAssertNotNil(data.fiveHourResetAt)
        XCTAssertNotNil(data.weeklyResetAt)
    }
    func testNullResetIsUnknownNotZeroUsage() throws {
        let raw = overviewJSON.replacingOccurrences(of: "\"2026-11-04T12:00:00.123456Z\"", with: "null")
        XCTAssertNil(try ProxyAPI.decoder().decode(ProxyOverview.self, from: Data(raw.utf8)).fiveHourResetAt)
    }
    func testURLNormalizationAndValidation() throws {
        for suffix in ["", "/", "/api", "/api/", "/api/v1", "/api/v1/"] {
            XCTAssertEqual(try ProxyAPI(baseURL: "https://proxy.test" + suffix).baseURL.absoluteString, "https://proxy.test/")
        }
        XCTAssertEqual(try ProxyAPI(baseURL: "https://proxy.test/myapi").baseURL.absoluteString, "https://proxy.test/myapi/")
        XCTAssertThrowsError(try ProxyAPI(baseURL: "file:///tmp/test"))
        XCTAssertThrowsError(try ProxyAPI(baseURL: "https://user:password@proxy.test"))
    }
    func testOverviewRequestIsAuthenticatedAndDoesNotFetchAccounts() async throws {
        var paths: [String] = []
        StubProtocol.handler = { request in
            paths.append(request.url!.path)
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer secret")
            return (200, overviewJSON)
        }
        let data = try await ProxyAPI(baseURL: "https://proxy.test/api", session: session()).overview(token: "secret")
        XCTAssertEqual(data.weeklyAveragePct, 71.8); XCTAssertEqual(paths, ["/api/v1/stats/overview"])
    }
    func testForceRefreshDecodesAccountEnvelopeSkipsUnavailableAndReportsFailures() async throws {
        let good = UUID(), failed = UUID(), disabled = UUID(), reauth = UUID()
        var posts = 0
        StubProtocol.handler = { request in
            if request.url!.path == "/api/v1/accounts" {
                return (200, """
                {"accounts":[{"id":"\(good)","status":"ACTIVE","provider_health":"HEALTHY"},
                {"id":"\(failed)","status":"COOLDOWN","provider_health":"HEALTHY"},
                {"id":"\(disabled)","status":"DISABLED","provider_health":"HEALTHY"},
                {"id":"\(reauth)","status":"ACTIVE","provider_health":"REAUTH_REQUIRED"}]}
                """)
            }
            posts += 1; XCTAssertEqual(request.httpMethod, "POST")
            return request.url!.path.contains(failed.uuidString) ? (502, "{\"detail\":\"Provider busy\"}") : (200, "{}")
        }
        let failures = try await ProxyAPI(baseURL: "https://proxy.test", session: session()).refreshQuotas(token: "secret")
        XCTAssertEqual(failures, 1); XCTAssertEqual(posts, 2)
    }
    @MainActor func testSessionRenewalAndSignOut() async throws {
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        defaults.set("https://proxy.test", forKey: "proxy.baseURL"); defaults.set("owner", forKey: "proxy.username")
        let store = MemoryCredentials()
        StubProtocol.handler = { request in
            if request.url!.path.hasSuffix("auth/login") { return (200, "{\"token\":\"renewed\"}") }
            return request.value(forHTTPHeaderField: "Authorization") == "Bearer renewed" ? (200, overviewJSON) : (401, "{\"detail\":\"Expired\"}")
        }
        let model = DashboardModel(credentials: store, defaults: defaults, session: session())
        await model.refresh()
        XCTAssertEqual(model.overview?.activeAccounts, 10); XCTAssertEqual(store.read("token"), "renewed")
        XCTAssertNil(model.error); XCTAssertNotNil(model.lastUpdated)
        model.signOut(); XCTAssertFalse(model.isConfigured); XCTAssertNil(model.overview); XCTAssertNil(store.read("token"))
    }
    @MainActor func testExpiredCredentialsRequireSignInWithoutFakeData() async throws {
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        defaults.set("https://proxy.test", forKey: "proxy.baseURL")
        StubProtocol.handler = { _ in (401, "{\"detail\":\"Unauthorized\"}") }
        let model = DashboardModel(credentials: MemoryCredentials(), defaults: defaults, session: session())
        await model.refresh()
        XCTAssertNil(model.overview); XCTAssertFalse(model.isConfigured); XCTAssertTrue(model.showingSettings)
    }
    @MainActor func testResetFormattingAndCompactNativeLayout() async throws {
        let now = Date(timeIntervalSince1970: 1000)
        XCTAssertEqual(DashboardModel.resetText(nil), "Next reset unavailable")
        XCTAssertEqual(DashboardModel.resetText(now, now: now), "Resetting…")
        XCTAssertEqual(DashboardModel.resetText(now.addingTimeInterval(1140), now: now), "Next reset in 19m")
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        defaults.set("https://proxy.test", forKey: "proxy.baseURL")
        StubProtocol.handler = { _ in (200, overviewJSON) }
        let model = DashboardModel(credentials: MemoryCredentials(), defaults: defaults, session: session())
        await model.refresh()
        _ = NSApplication.shared
        let host = NSHostingView(rootView: PopoverView(model: model).environment(\.colorScheme, .dark))
        let size = host.fittingSize
        XCTAssertEqual(size.width, 360, accuracy: 1); XCTAssertLessThan(size.height, 340)
        host.frame = NSRect(origin: .zero, size: size); host.layoutSubtreeIfNeeded()
        if let output = ProcessInfo.processInfo.environment["APP_SNAPSHOT_DIR"] {
            try FileManager.default.createDirectory(atPath: output, withIntermediateDirectories: true)
            let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds)!
            host.cacheDisplay(in: host.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: output).appendingPathComponent("overview.png"))
        }
    }
    @MainActor func testSignInPersistsConnectionAndLoadsActualOverview() async throws {
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        let store = MemoryCredentials(); store.values = [:]
        StubProtocol.handler = { request in
            if request.url!.path.hasSuffix("auth/login") { return (200, "{\"token\":\"signed-in\"}") }
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer signed-in")
            return (200, overviewJSON)
        }
        let model = DashboardModel(credentials: store, defaults: defaults, session: session())
        XCTAssertTrue(model.showingSettings)
        await model.authenticate(baseURL: "https://proxy.test/api/v1/", username: " owner ", password: "password")
        XCTAssertTrue(model.isConfigured); XCTAssertFalse(model.showingSettings)
        XCTAssertEqual(model.username, "owner"); XCTAssertEqual(model.baseURL, "https://proxy.test/")
        XCTAssertEqual(store.read("password"), "password"); XCTAssertEqual(model.overview?.totalAccounts, 26)
        XCTAssertNil(defaults.string(forKey: "password"))
    }
    @MainActor func testFailedRefreshPreservesLastSuccessfulDataAndTimestamp() async throws {
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        defaults.set("https://proxy.test", forKey: "proxy.baseURL")
        let model = DashboardModel(credentials: MemoryCredentials(), defaults: defaults, session: session())
        StubProtocol.handler = { _ in (200, overviewJSON) }
        await model.refresh()
        let previous = model.overview, updated = model.lastUpdated
        StubProtocol.handler = { _ in (503, "{\"detail\":\"Temporarily unavailable\"}") }
        await model.refresh()
        XCTAssertEqual(model.overview, previous); XCTAssertEqual(model.lastUpdated, updated)
        XCTAssertTrue(model.error?.contains("503") == true); XCTAssertFalse(model.isRefreshing)
    }
    @MainActor func testInvalidConnectionDoesNotPersistCredentials() async throws {
        let defaults = UserDefaults(suiteName: UUID().uuidString)!
        let store = MemoryCredentials(); store.values = [:]
        let model = DashboardModel(credentials: store, defaults: defaults, session: session())
        await model.authenticate(baseURL: "file:///etc", username: "owner", password: "password")
        XCTAssertFalse(model.isConfigured); XCTAssertNotNil(model.settingsError)
        XCTAssertTrue(store.values.isEmpty)
    }

}
