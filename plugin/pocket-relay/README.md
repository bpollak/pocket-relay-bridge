# Pocket Relay

Pair an iPhone or iPad with this computer, then follow and steer your agent sessions from the
Pocket Relay app: see what the agent is doing, approve or deny its actions, and answer its questions.

## What this plugin adds

- A **Pair phone** side panel that shows a pairing QR code for the Pocket Relay app.
- A background helper that keeps Pocket Relay Bridge running while Antigravity is open.

It adds no skills, rules, hooks or MCP servers, and it does not change how your agent behaves.

## Setup prerequisites

1. **Pocket Relay Bridge** on this computer. The bridge is the local service the phone connects to.
   Install it with one command (it also installs this plugin):

   - macOS and Linux: `curl -fsSL https://raw.githubusercontent.com/bpollak/pocket-relay-bridge/main/install.sh | bash`
   - Windows (PowerShell): `irm https://raw.githubusercontent.com/bpollak/pocket-relay-bridge/main/install.ps1 | iex`

   If the bridge is missing, the panel says so and points here.
2. **The Pocket Relay app** on an iPhone or iPad running iOS or iPadOS 26 or later.
3. The phone and the computer on the same Wi-Fi, or on a private network you run.

## Example workflow

1. Open the **Pair phone** panel. A QR code appears.
2. In the Pocket Relay app, tap **Scan QR Code** and point it at the code.
3. Start a task in Antigravity and walk away. On the phone you see a one-line status of what the
   agent is doing, and a card when it needs your approval or has a question.

## Privacy and security

The phone talks only to this computer. There is no Pocket Relay server. The pairing QR code carries
a token that lets a phone control this computer, so keep it private. A direct connection over local
Wi-Fi is not encrypted; use networks you trust. Full policy: https://pocketrelay.app/privacy/

## Support

https://pocketrelay.app/support/

Pocket Relay is an independent project. It is not affiliated with, endorsed by or sponsored by Google.
