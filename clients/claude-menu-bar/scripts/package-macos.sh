#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
artifacts="${APP_ARTIFACTS_DIR:-$PWD/artifacts}"
mkdir -p "$artifacts"
swift test
swift build --configuration release --arch arm64 --arch x86_64
bin_dir=$(swift build --configuration release --arch arm64 --arch x86_64 --show-bin-path)
staging=$(mktemp -d "${TMPDIR:-/tmp}/proxy-app.XXXXXX")
trap 'rm -rf "$staging"' EXIT
app="$staging/Claude Code Proxy.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$bin_dir/ClaudeMenuBar" "$app/Contents/MacOS/ClaudeMenuBar"
cp Resources/Info.plist "$app/Contents/Info.plist"
executable=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$app/Contents/Info.plist")
test "$executable" = "ClaudeMenuBar"
test -x "$app/Contents/MacOS/$executable"
lipo "$app/Contents/MacOS/$executable" -verify_arch arm64 x86_64
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString ${APP_VERSION:-0.0.0}" "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion ${GITHUB_RUN_NUMBER:-1}" "$app/Contents/Info.plist"
iconset="$staging/AppIcon.iconset"
mkdir -p "$iconset"
for size in 16 32 128 256 512; do
    rsvg-convert -w "$size" -h "$size" Resources/AppIcon.svg -o "$iconset/icon_${size}x${size}.png"
    doubled=$((size * 2))
    rsvg-convert -w "$doubled" -h "$doubled" Resources/AppIcon.svg -o "$iconset/icon_${size}x${size}@2x.png"
done
iconutil -c icns "$iconset" -o "$app/Contents/Resources/AppIcon.icns"
plutil -lint "$app/Contents/Info.plist"
codesign --force --sign - "$app"
codesign --verify --deep --strict --verbose=2 "$app"
installer="$staging/installer"
mkdir -p "$installer"
cp -R "$app" "$installer/Claude Code Proxy.app"
ln -s /Applications "$installer/Applications"
dmg="$artifacts/Claude-Code-Proxy-macOS.dmg"
hdiutil create -volname "Claude Code Proxy" -srcfolder "$installer" -ov -format UDZO "$dmg" >/dev/null
mount="$staging/mounted"
mkdir -p "$mount"
hdiutil attach "$dmg" -nobrowse -readonly -mountpoint "$mount" >/dev/null
installed="$staging/installed/Claude Code Proxy.app"
mkdir -p "$staging/installed"
ditto "$mount/Claude Code Proxy.app" "$installed"
hdiutil detach "$mount" >/dev/null
codesign --verify --deep --strict "$installed"
test -s "$installed/Contents/Resources/AppIcon.icns"
# Launch the exact app extracted from the DMG, then fail if it crashes.
"$installed/Contents/MacOS/$executable" > "$artifacts/app-launch.log" 2>&1 &
app_pid=$!
sleep 5
if ! kill -0 "$app_pid" 2>/dev/null; then
    cat "$artifacts/app-launch.log"
    echo "Packaged app exited during launch verification." >&2
    exit 1
fi
kill "$app_pid"
wait "$app_pid" || true
printf 'Validated universal DMG, icon, signature, executable and launch.\n'
