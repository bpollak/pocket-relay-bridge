// Remote access tunnel: passes sealed records between a Pocket Relay Bridge and its paired phones.
//
// One Durable Object per bridge ("room"). The bridge connects as the host; phones connect as
// clients. Everything they exchange is encrypted end to end with a key this relay never sees,
// so it only moves opaque bytes. Nothing is stored.
//
// Relay <-> host:   binary frames are prefixed with a 4-byte big-endian client id.
//                   Text frames are control messages: {"t":"open"|"close","c":<client id>}.
// Relay <-> client: binary frames, forwarded as they are.

import { DurableObject } from "cloudflare:workers";

export const ROOM_RE = /^[0-9a-f]{40}$/;
const MAX_CLIENTS = 8;
const MAX_FRAME_BYTES = 256 * 1024;

/** Only the bridge that created a room can be its host: the room id is a hash of its secret. */
export async function isRoomHost(room, authorization) {
  const secret = /^bearer\s+(.+)$/i.exec(authorization || "")?.[1]?.trim();
  if (!secret || secret.length < 32 || secret.length > 200) return false;
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(secret));
  const hex = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  return hex.slice(0, 40) === room;
}

/** Parses /v1/tunnel/<room>/<host|client>. */
export function parseTunnelPath(pathname) {
  const match = /^\/v1\/tunnel\/([^/]+)\/(host|client)$/.exec(pathname);
  if (!match || !ROOM_RE.test(match[1])) return null;
  return { room: match[1], role: match[2] };
}

export async function handleTunnel(request, env) {
  const target = parseTunnelPath(new URL(request.url).pathname);
  if (!target) return new Response("Not found", { status: 404 });
  if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
    return new Response("Expected a WebSocket", { status: 426 });
  }
  if (target.role === "host" && !(await isRoomHost(target.room, request.headers.get("Authorization")))) {
    return new Response("Not this room's host", { status: 401 });
  }
  const stub = env.TUNNEL_ROOM.get(env.TUNNEL_ROOM.idFromName(target.room));
  return stub.fetch(new Request(`https://room/${target.role}`, { headers: { Upgrade: "websocket" } }));
}

export class TunnelRoom extends DurableObject {
  /** The bridge's socket, ignoring one that is already closing. */
  #host() {
    return this.ctx.getWebSockets("host").find((socket) => socket.readyState === 1);
  }

  async fetch(request) {
    const role = new URL(request.url).pathname.slice(1);
    const pair = new WebSocketPair();
    const [outside, inside] = Object.values(pair);

    if (role === "host") {
      for (const previous of this.ctx.getWebSockets("host")) previous.close(4409, "replaced by a newer connection");
      for (const client of this.ctx.getWebSockets("client")) client.close(4410, "host reconnected");
      this.ctx.acceptWebSocket(inside, ["host"]);
      inside.serializeAttachment({ role: "host" });
      return new Response(null, { status: 101, webSocket: outside });
    }

    const host = this.#host();
    if (!host) {
      this.ctx.acceptWebSocket(inside, ["rejected"]);
      inside.serializeAttachment({ role: "rejected" });
      inside.close(4404, "host offline");
      return new Response(null, { status: 101, webSocket: outside });
    }
    if (this.ctx.getWebSockets("client").length >= MAX_CLIENTS) {
      this.ctx.acceptWebSocket(inside, ["rejected"]);
      inside.serializeAttachment({ role: "rejected" });
      inside.close(4429, "too many connections");
      return new Response(null, { status: 101, webSocket: outside });
    }
    let id;
    do {
      id = crypto.getRandomValues(new Uint32Array(1))[0];
    } while (id === 0 || this.ctx.getWebSockets(`c:${id}`).length > 0);
    this.ctx.acceptWebSocket(inside, ["client", `c:${id}`]);
    inside.serializeAttachment({ role: "client", id });
    host.send(JSON.stringify({ t: "open", c: id }));
    return new Response(null, { status: 101, webSocket: outside });
  }

  webSocketMessage(ws, message) {
    const info = ws.deserializeAttachment();
    if (info?.role === "host") {
      if (typeof message === "string") {
        let control;
        try {
          control = JSON.parse(message);
        } catch {
          return;
        }
        if (control?.t === "close" && Number.isInteger(control.c)) {
          for (const client of this.ctx.getWebSockets(`c:${control.c}`)) client.close(4403, "closed by host");
        }
        return;
      }
      if (message.byteLength <= 4 || message.byteLength > MAX_FRAME_BYTES + 4) return;
      const id = new DataView(message).getUint32(0);
      for (const client of this.ctx.getWebSockets(`c:${id}`)) client.send(message.slice(4));
      return;
    }
    if (info?.role === "client") {
      if (typeof message === "string" || message.byteLength === 0 || message.byteLength > MAX_FRAME_BYTES) return;
      const host = this.#host();
      if (!host) {
        ws.close(4410, "host went away");
        return;
      }
      const framed = new Uint8Array(message.byteLength + 4);
      new DataView(framed.buffer).setUint32(0, info.id);
      framed.set(new Uint8Array(message), 4);
      host.send(framed);
    }
  }

  webSocketClose(ws, code) {
    // Finish the closing handshake; until then Cloudflare still lists the socket.
    try {
      ws.close(code === 1005 || code === 1006 ? 1000 : code);
    } catch {
      // already closed
    }
    this.#left(ws);
  }

  webSocketError(ws) {
    this.#left(ws);
  }

  #left(ws) {
    const info = ws.deserializeAttachment();
    if (info?.role === "host") {
      // Another host socket may already have replaced this one; only then keep the clients.
      const others = this.ctx.getWebSockets("host").filter((s) => s !== ws && s.readyState === 1);
      if (others.length === 0) {
        for (const client of this.ctx.getWebSockets("client")) client.close(4410, "host went away");
      }
    } else if (info?.role === "client") {
      const host = this.#host();
      try {
        host?.send(JSON.stringify({ t: "close", c: info.id }));
      } catch {
        // host already gone
      }
    }
  }
}
