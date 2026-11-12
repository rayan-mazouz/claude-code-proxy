# Claude Code Proxy menu-bar app

Minimal native SwiftUI app for macOS 13+ (Apple Silicon and Intel).
It shows live enabled/authenticated account coverage, five-hour and weekly
quota averages, useful reset countdowns, and the last successful update.
Refresh and connection settings are in the top-right. No Accounts screen,
activity chart, demo data, or Notification Center widget is included.

Enter your proxy URL and dashboard credentials in Settings. URLs ending in
`/api` or `/api/v1` are supported. Credentials are stored in an app-specific
macOS Keychain service (`com.recallrai.claudecodeproxy.credentials`). Upgrading from the prototype
requires signing in once because the prototypes shared an unsafe Keychain name.
The app renews expired sessions and requests sign-in again if renewal fails.
Normal refresh reads one overview API; Force Refresh explicitly probes each
enabled authenticated account through `refresh-quota`, reports partial failures,
and then reloads the overview. Neither operation redeems reset credits.
Data reloads every minute while the popover is open. Errors retain the last
successful values and timestamp rather than displaying fake zeros.

## Build and verify on macOS

```bash
cd clients/claude-menu-bar
swift test
brew install librsvg
APP_VERSION=0.5.0 scripts/package-macos.sh
```

CI runs unit/network tests, captures the native compact UI, builds both CPU
architectures, verifies the app icon/executable/signature, mounts the generated
DMG, extracts the installed app, and launches that exact binary for five seconds.
The distribution is ad-hoc signed, not Developer-ID signed or notarized;
Gatekeeper may require Control-click → Open. CI does not claim to verify
Gatekeeper acceptance on another person's Mac.

Download `Claude-Code-Proxy-macOS.dmg` from the repository's macOS releases.
