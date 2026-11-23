// Run with: node test.mjs
import assert from "node:assert/strict";
import { register } from "node:module";
// "cloudflare:workers" only exists inside Cloudflare; give Node an empty stand-in for these checks.
register("data:text/javascript," + encodeURIComponent(`
export function resolve(specifier, context, next) {
  if (specifier === "cloudflare:workers") return { url: "data:text/javascript,export class DurableObject { constructor(ctx, env) { this.ctx = ctx; this.env = env; } }", shortCircuit: true };
  return next(specifier, context);
}`));
const { default: worker, buildPayload, providerToken, resetTokenCache, validate } = await import("./src/index.js");
const { isRoomHost, parseTunnelPath, TunnelRoom } = await import("./src/tunnel.js");

const TOKEN = "ab".repeat(32);
const { privateKey, publicKey } = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]);
const pkcs8 = Buffer.from(await crypto.subtle.exportKey("pkcs8", privateKey)).toString("base64");
const env = {
  APNS_PRIVATE_KEY: `-----BEGIN PRIVATE KEY-----\n${pkcs8.match(/.{1,64}/g).join("\n")}\n-----END PRIVATE KEY-----`,
  APNS_KEY_ID: "TESTKEY123",
  APNS_TEAM_ID: "TEAM123456",
  APNS_BUNDLE_ID: "com.example.app",
};
const post = (body) => new Request("https://push.example/v1/notify", { method: "POST", body: typeof body === "string" ? body : JSON.stringify(body) });
const base = { kind: "approval", devices: [{ token: TOKEN, environment: "sandbox" }], host_name: "Studio", collapse_id: "approval-ap1", data: { approvalId: "ap1", conversationId: "c1" }, badge: 2 };

// Provider token verifies against the public key and carries the right claims.
const jwt = await providerToken(env, 1_800_000_000);
const [h, c, s] = jwt.split(".");
assert.deepEqual(JSON.parse(Buffer.from(h, "base64url")), { alg: "ES256", kid: "TESTKEY123" });
assert.deepEqual(JSON.parse(Buffer.from(c, "base64url")), { iss: "TEAM123456", iat: 1_800_000_000 });
assert.ok(await crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, publicKey, Buffer.from(s, "base64url"), new TextEncoder().encode(`${h}.${c}`)));
assert.equal(await providerToken(env, 1_800_000_100), jwt, "token is reused inside 45 minutes");
assert.notEqual(await providerToken(env, 1_800_003_000), jwt, "token is renewed after 45 minutes");
resetTokenCache();

// Payload: no detail means generic text and no Allow/Deny buttons.
const plain = buildPayload(validate(base));
assert.equal(plain.aps.alert.title, "Your agent needs approval");
assert.equal(plain.aps.alert.body, "Open Pocket Relay to review the request.");
assert.equal(plain.aps.alert.subtitle, "Studio");
assert.equal(plain.aps.category, undefined);
assert.equal(plain.aps.badge, 2);
assert.equal(plain.aps["thread-id"], "c1");
assert.equal(plain.approvalId, "ap1");
const detailed = buildPayload(validate({ ...base, detail: "swift test\u0007now" }));
assert.equal(detailed.aps.alert.body, "swift test now");
assert.equal(detailed.aps.category, "POCKETRELAY_APPROVAL");
assert.equal(buildPayload(validate({ ...base, kind: "finished", detail: "Add search" })).aps.category, undefined);

// Validation.
for (const bad of [
  { ...base, kind: "marketing" },
  { ...base, devices: [] },
  { ...base, devices: Array(11).fill({ token: TOKEN }) },
  { ...base, devices: [{ token: "xyz" }] },
  { ...base, data: { approvalId: "a b<script>" } },
  [],
]) assert.throws(() => validate(bad));
assert.equal(validate({ ...base, detail: "x".repeat(500) }).detail.length, 180);

// End to end with Apple stubbed out.
const calls = [];
let reply = () => new Response(null, { status: 200 });
globalThis.fetch = async (url, init) => { calls.push({ url, init }); return reply(url); };

let res = await worker.fetch(post({ ...base, detail: "swift test" }), env);
assert.equal(res.status, 200);
assert.deepEqual(await res.json(), { sent: 1, failed: 0, invalid_tokens: [] });
assert.equal(calls[0].url, `https://api.sandbox.push.apple.com/3/device/${TOKEN}`);
assert.equal(calls[0].init.headers["apns-topic"], "com.example.app");
assert.equal(calls[0].init.headers["apns-collapse-id"], "approval-ap1");
assert.match(calls[0].init.headers.authorization, /^bearer [\w-]+\.[\w-]+\.[\w-]+$/);
assert.equal(JSON.parse(calls[0].init.body).aps.alert.body, "swift test");

const OTHER = "cd".repeat(32);
reply = (url) => url.includes(OTHER) ? new Response(JSON.stringify({ reason: "Unregistered" }), { status: 410 }) : new Response(JSON.stringify({ reason: "TooManyRequests" }), { status: 429 });
const warn = console.warn; let warned = ""; console.warn = (...a) => { warned = a.join(" "); };
res = await worker.fetch(post({ ...base, devices: [{ token: TOKEN }, { token: OTHER }] }), env);
console.warn = warn;
assert.deepEqual(await res.json(), { sent: 0, failed: 1, invalid_tokens: [OTHER] });
assert.ok(calls.at(-1).url.startsWith("https://api.push.apple.com/"), "production is the default");
assert.ok(!warned.includes(TOKEN) && !warned.includes("Studio"), "logs carry reasons only");

assert.equal((await worker.fetch(post("{not json"), env)).status, 400);
assert.equal((await worker.fetch(post({ ...base, kind: "x" }), env)).status, 400);
assert.equal((await worker.fetch(post("x".repeat(9000)), env)).status, 413);
assert.equal((await worker.fetch(post(base), {})).status, 503);
assert.equal((await worker.fetch(new Request("https://push.example/v1/notify"), env)).status, 405);
assert.equal((await worker.fetch(new Request("https://push.example/other"), env)).status, 404);
assert.equal((await worker.fetch(new Request("https://push.example/health"), env)).status, 200);

// ---- Tunnel -------------------------------------------------------------------------------
const secret = "s".repeat(43);
const room = Buffer.from(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(secret))).toString("hex").slice(0, 40);
assert.ok(await isRoomHost(room, `Bearer ${secret}`));
assert.ok(!(await isRoomHost(room, `Bearer ${"t".repeat(43)}`)));
assert.ok(!(await isRoomHost(room, "")));
assert.deepEqual(parseTunnelPath(`/v1/tunnel/${room}/client`), { room, role: "client" });
for (const bad of ["/v1/tunnel/short/client", `/v1/tunnel/${room}/admin`, `/v1/tunnel/${room}/host/extra`, `/v1/tunnel/${room.toUpperCase()}/host`]) {
  assert.equal(parseTunnelPath(bad), null, bad);
}
assert.equal((await worker.fetch(new Request(`https://relay.example/v1/tunnel/${room}/client`), env)).status, 426);
assert.equal((await worker.fetch(new Request(`https://relay.example/v1/tunnel/${room}/host`, { headers: { Upgrade: "websocket", Authorization: "Bearer " + "x".repeat(43) } }), env)).status, 401);

// Forwarding rules, with Cloudflare's socket bookkeeping replaced by a small stand-in.
class FakeSocket {
  constructor() { this.sent = []; this.closed = null; this.tags = []; }
  get readyState() { return this.closed === null ? 1 : 3; }
  serializeAttachment(v) { this.att = v; }
  deserializeAttachment() { return this.att; }
  send(m) { this.sent.push(m); }
  close(code) { if (this.closed === null) this.closed = code; }
}
const sockets = [];
const ctx = {
  acceptWebSocket(ws, tags) { ws.tags = tags; sockets.push(ws); },
  getWebSockets(tag) { return sockets.filter((s) => s.closed === null && s.tags.includes(tag)); },
};
let made = [];
globalThis.WebSocketPair = class { constructor() { const pair = [new FakeSocket(), new FakeSocket()]; made.push(pair[1]); return { 0: pair[0], 1: pair[1] }; } };
const RealResponse = globalThis.Response;
globalThis.Response = class extends RealResponse { constructor(body, init = {}) { const { webSocket, status, ...rest } = init; super(body, { ...rest, status: status === 101 ? 200 : status }); this.webSocket = webSocket; } };
const roomObject = new TunnelRoom(ctx, {});
const join = async (role) => { await roomObject.fetch(new Request(`https://room/${role}`)); return made.at(-1); };

const early = await join("client");
assert.equal(early.closed, 4404, "no host yet");
const hostSocket = await join("host");
const phone = await join("client");
const { c: phoneId } = JSON.parse(hostSocket.sent.at(-1));
assert.equal(JSON.parse(hostSocket.sent.at(-1)).t, "open");

roomObject.webSocketMessage(phone, new Uint8Array([9, 8, 7]).buffer);
const toHost = new Uint8Array(hostSocket.sent.at(-1));
assert.equal(new DataView(toHost.buffer).getUint32(0), phoneId);
assert.deepEqual([...toHost.slice(4)], [9, 8, 7]);

const hostReply = new Uint8Array(4 + 2); new DataView(hostReply.buffer).setUint32(0, phoneId); hostReply.set([5, 6], 4);
roomObject.webSocketMessage(hostSocket, hostReply.buffer);
assert.deepEqual([...new Uint8Array(phone.sent.at(-1))], [5, 6]);

const other = await join("client");
const stray = new Uint8Array(4 + 1); new DataView(stray.buffer).setUint32(0, 12345); stray.set([1], 4);
roomObject.webSocketMessage(hostSocket, stray.buffer);
assert.equal(other.sent.length, 0, "frames only reach the phone they are addressed to");
roomObject.webSocketMessage(phone, "text is ignored");
roomObject.webSocketMessage(phone, new Uint8Array(300 * 1024).buffer);
assert.equal(hostSocket.sent.filter((m) => typeof m !== "string").length, 1, "oversized and text frames are dropped");

roomObject.webSocketMessage(hostSocket, JSON.stringify({ t: "close", c: phoneId }));
assert.equal(phone.closed, 4403);
roomObject.webSocketClose(phone);
assert.deepEqual(JSON.parse(hostSocket.sent.at(-1)), { t: "close", c: phoneId });

roomObject.webSocketClose(hostSocket, 1006);
assert.equal((await join("client")).closed, 4404, "a phone arriving after the host left is told at once");
assert.equal(other.closed, 4410, "phones are dropped when the host goes away");
globalThis.Response = RealResponse;

console.log("relay: all checks passed");
