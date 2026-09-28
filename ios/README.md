# Confer for iOS

A native SwiftUI client for your Confer node. It talks to the node's REST API
over your tailnet: inbox, plans, trips (itinerary, rides, rooms, tasks, polls),
shared lists, money, status/ETA/location sharing, people and permissions, and
settings.

## Use it

1. On the machine running your node:
   ```bash
   confer api enable              # prints the API URL and token
   tailscale serve --bg 3067      # HTTPS for the node, reachable only on your tailnet
   ```
2. Open the app, enter your node's `https://…ts.net` address and the token, and
   tap **Connect**. The token is stored in the iOS Keychain (this device only).

What the app does and doesn't do:
- **You are the approver.** Every button is you acting, so the app sends the
  `Confer-Human-Approved` header.
- **Location only on request.** It's read only when you tap "Include my
  location", and it expires.
- **Updates:** foreground polling every 20 s, plus background app refresh, with
  local notifications for new items that need you. There are no push servers.

## Build

Requires Xcode 16+ and [XcodeGen](https://github.com/yonaskolb/XcodeGen).

```bash
cd ios
xcodegen generate
xcodebuild build -project Confer.xcodeproj -scheme Confer -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO
```

To run it on your iPhone:
1. Open `Confer.xcodeproj`.
2. Pick your team under Signing & Capabilities.
3. Run.

## UI tests against a demo network

`ConferUITests` taps through real flows: it accepts a money request, and
accepts a plan invitation that the organizer's node then confirms. Before
running them, start the demo network on the same Mac. The demo script starts
three nodes on `127.0.0.1` (ports 3067, 3071 and 3072) and fills them with
data; see `tools/demo_network.py`.

```bash
python tools/demo_network.py &      # needs the confer package installed
xcodebuild test -project Confer.xcodeproj -scheme Confer -destination 'platform=iOS Simulator,name=iPhone 16 Pro'
```

Debug builds also accept the launch arguments `-ConferDemoURL`,
`-ConferDemoToken` and `-ConferTab`. The app ignores them in release builds.
