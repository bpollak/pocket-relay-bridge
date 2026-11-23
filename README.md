# Pocket Relay Bridge

Pocket Relay Bridge is the computer-side companion for **Pocket Relay**, an iPhone and iPad app
that lets you follow and steer Google Antigravity on your own computer from your phone.

The bridge runs on your Mac, Windows PC or Linux computer. The app on your phone connects to it over
your local network or a private network you run. There is no Pocket Relay server in between.

- App and support: https://pocketrelay.app/
- Privacy: https://pocketrelay.app/privacy/

Pocket Relay is an independent project. It is not affiliated with, endorsed by or sponsored by Google.
Google and Antigravity are trademarks of Google LLC.

## What you need

- Google Antigravity installed and signed in on the computer, with its command-line tool (`agy`).
- macOS, Linux or Windows.
- The Pocket Relay app on an iPhone or iPad running iOS or iPadOS 26 or later.

## Install

**macOS and Linux**

```sh
curl -fsSL https://raw.githubusercontent.com/bpollak/pocket-relay-bridge/main/install.sh | bash
```

**Windows (PowerShell)**

```powershell
irm https://raw.githubusercontent.com/bpollak/pocket-relay-bridge/main/install.ps1 | iex
```

The installer:

1. Checks for the Antigravity command-line tool.
2. Installs [uv](https://docs.astral.sh/uv/), a Python package manager, if it is missing. uv fetches Python for you.
3. Copies the bridge to `~/.antigravity-mobile/runtime` and installs its dependencies.
4. Sets the bridge to start when you log in.
5. Adds the Pocket Relay plugin to Antigravity, which shows the pairing QR code in a side panel.

It does not need administrator rights and changes nothing outside your home folder. You can read
[install.sh](install.sh) and [install.ps1](install.ps1) before running them.

The Windows installer and the Linux path are new and have had less testing than macOS. Please report problems.

## Pair your phone

1. In Antigravity, open the **Pocket Relay** panel. It shows a QR code.
   If you do not see the panel, open `http://127.0.0.1:8765/pair` in a browser on the computer.
2. In the Pocket Relay app, tap **Scan QR Code** and point it at the code.

You can also pick the computer from the app's nearby list when both are on the same Wi‑Fi, then
enter the 6‑digit code shown on the computer. Codes last 10 minutes.

## Security

- The bridge listens on port 8765 on your network so your phone can reach it. Every request other
  than the health check and the pairing handshake needs the token your phone received when it paired.
- A paired phone can send instructions to your agent, approve its actions and open terminals on
  the computer. Pair only devices you control.
- A direct connection over local Wi‑Fi is not encrypted. Use networks you trust, or connect over a
  private network that provides HTTPS, such as Tailscale.
- Pairings, settings, conversation history and logs stay on your computer in `~/.antigravity-mobile`.

## Remote access

Your phone can reach this computer when it is not on the same network, for example on mobile data.

The bridge keeps one outbound connection open to the Pocket Relay relay (`relay.pocketrelay.app`). When your phone cannot reach the computer directly, it connects to the same relay, and the relay passes traffic between the two. No router or firewall changes are needed, and nothing listens for incoming connections from the internet.

That traffic is encrypted end to end. The key is created on this computer and reaches your phone only through the pairing QR code, so the relay never has it and cannot read or change what passes through. Each connection also uses fresh keys. The details are in [`bridge/antigravity_bridge/services/tunnel.py`](bridge/antigravity_bridge/services/tunnel.py) and the relay's source is in [`push-relay/`](push-relay/).

Things to know:

- A phone on the same network still connects directly. The relay is only the fallback.
- Requests through the relay need the same pairing token as direct ones. The pairing page and pairing-code routes cannot be reached through it at all.
- Phones paired before you updated need to scan the QR code again to get the remote access key.
- "Unpair all phones" (rotating the pairing token) also replaces the remote access key.
- To turn remote access off on this computer, set `POCKET_RELAY_REMOTE=0` in the bridge's environment. To use a relay you run yourself, set `POCKET_RELAY_TUNNEL_URL`.

## Push alerts

When your agent needs approval, asks a question or finishes, the bridge can alert your phone even if the app is closed.

Apple only delivers notifications that are signed with the app developer's key, and that key cannot ship in open-source code. So the bridge sends a small request to the Pocket Relay push service (`push.pocketrelay.app`), which signs it and passes it to Apple. The source for that service is in [`push-relay/`](push-relay/).

What the request contains:

- the kind of event (approval, question or finished), this computer's name, and your phone's push token
- the command, question or conversation title, **only** if you turn on **Show Details in Alerts** in the app

Prompts, replies, code and files are never sent to it, and it does not store requests.

Nothing is sent until a phone with alerts turned on has paired. To turn push off on this computer, set `POCKET_RELAY_PUSH=0` in the bridge's environment. To use a relay you run yourself, set `POCKET_RELAY_PUSH_URL`.

## Uninstall

macOS and Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/bpollak/pocket-relay-bridge/main/uninstall.sh | bash
```

Add `-s -- --purge` after `bash` to also remove saved pairings and history.

Windows (PowerShell):

```powershell
Unregister-ScheduledTask -TaskName PocketRelayBridge -Confirm:$false
agy plugin uninstall pocket-relay
Remove-Item -Recurse -Force "$env:USERPROFILE\.antigravity-mobile\runtime"
```

## What is in this repository

- `bridge/`: the bridge, a Python (FastAPI) service that talks to Antigravity through its command-line tool.
- `plugin/pocket-relay/`: the Antigravity plugin that shows the pairing panel and keeps the bridge running.
- `install.sh`, `install.ps1`, `uninstall.sh`: installers.

## Using Antigravity with other tools

Your use of Google Antigravity is governed by Google's terms, including any rules about accessing it
with other software. Check them before you use this bridge.

© 2026 Brett Pollak. All rights reserved.
