import AppKit
import SwiftUI

public struct PopoverView: View {
    @ObservedObject var model: DashboardModel
    public init(model: DashboardModel) { self.model = model }
    public var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 9) {
                Image(systemName: AppIdentity.symbol).foregroundStyle(AppIdentity.accent).frame(width: 24, height: 24)
                Text(AppIdentity.name).font(.system(size: 13, weight: .semibold))
                Spacer()
                Button { Task { await model.refresh(force: true) } } label: {
                    Image(systemName: "arrow.clockwise").rotationEffect(.degrees(model.isRefreshing ? 180 : 0))
                }.disabled(!model.isConfigured || model.isRefreshing || model.isSigningIn).help("Force refresh provider limits").accessibilityLabel("Force refresh")
                Button { model.showingSettings.toggle() } label: { Image(systemName: model.showingSettings && model.isConfigured ? "xmark" : "gearshape") }
                    .disabled(!model.isConfigured || model.isSigningIn).help("Connection settings").accessibilityLabel("Settings")
            }.buttonStyle(.borderless).padding(14)
            if model.showingSettings { ConnectionSettings(model: model).padding(.horizontal, 16).padding(.bottom, 14) }
            else {
                VStack(spacing: 12) {
                    HStack {
                        Text("Active accounts").foregroundStyle(.secondary)
                        Spacer()
                        Text(model.overview.map { "\($0.activeAccounts) / \($0.totalAccounts)" } ?? "—").monospacedDigit()
                    }.font(.system(size: 11))
                    HStack(alignment: .top, spacing: 8) {
                        QuotaCell(label: "5-hour average", value: model.overview?.fiveHourAveragePct, reset: model.overview?.fiveHourResetAt)
                        QuotaCell(label: "Weekly average", value: model.overview?.weeklyAveragePct, reset: model.overview?.weeklyResetAt)
                    }
                    if let error = model.error { Text(error).font(.caption2).foregroundStyle(.orange).frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled) }
                }.padding(.horizontal, 14).padding(.bottom, 14)
            }
            Divider()
            HStack {
                if model.isRefreshing { Text("Refreshing limits…") }
                else if let date = model.lastUpdated { Text("Updated ") + Text(date, style: .relative) + Text(" ago") }
                else { Text("Not updated yet") }
                Spacer()
                Button("Quit") { NSApplication.shared.terminate(nil) }.buttonStyle(.plain)
            }.font(.system(size: 9)).foregroundStyle(.secondary).padding(.horizontal, 14).padding(.vertical, 9)
        }
        .frame(width: 360)
        .background(Color(nsColor: .windowBackgroundColor))
        .task {
            while !Task.isCancelled {
                await model.refresh()
                do { try await Task.sleep(for: .seconds(60)) } catch { break }
            }
        }
    }
}

private struct QuotaCell: View {
    let label: String
    let value: Double?
    let reset: Date?
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(label).font(.system(size: 10)).foregroundStyle(.secondary)
            Text(value.map { String(format: "%.1f%%", min(100, max(0, $0))) } ?? "—")
                .font(.system(size: 25, weight: .semibold, design: .monospaced)).minimumScaleFactor(0.7).lineLimit(1)
            TimelineView(.periodic(from: .now, by: 30)) { context in
                Text(DashboardModel.resetText(reset, now: context.date)).font(.system(size: 10)).foregroundStyle(.secondary)
            }
        }.frame(maxWidth: .infinity, alignment: .leading).padding(11)
            .background(Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.primary.opacity(0.09)))
    }
}

private struct ConnectionSettings: View {
    @ObservedObject var model: DashboardModel
    @State private var baseURL: String
    @State private var username: String
    @State private var password = ""
    init(model: DashboardModel) {
        self.model = model
        _baseURL = State(initialValue: model.baseURL); _username = State(initialValue: model.username)
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Connection").font(.headline)
            VStack(alignment: .leading, spacing: 4) { Text("Proxy URL").font(.caption); TextField("https://your-proxy.example", text: $baseURL) }
            VStack(alignment: .leading, spacing: 4) { Text("Username").font(.caption); TextField("Dashboard username", text: $username) }
            VStack(alignment: .leading, spacing: 4) { Text("Password").font(.caption); SecureField("Dashboard password", text: $password) }
            if let error = model.settingsError { Text(error).font(.caption).foregroundStyle(.red).textSelection(.enabled) }
            HStack {
                if model.isConfigured { Button("Sign out") { model.signOut(); password = "" } }
                Spacer()
                Button(model.isSigningIn ? "Signing in…" : "Sign in") {
                    Task { await model.authenticate(baseURL: baseURL, username: username, password: password); password = "" }
                }.buttonStyle(.borderedProminent).tint(AppIdentity.accent).keyboardShortcut(.defaultAction)
            }.disabled(model.isSigningIn || model.isRefreshing)
        }.textFieldStyle(.roundedBorder)
    }
}
