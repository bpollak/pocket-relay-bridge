// Pocket Relay relay (Cloudflare Worker): push alerts here, remote access tunnel in tunnel.js.
//
// A Pocket Relay Bridge posts a small JSON request here when the user's agent needs them or has
// finished. This worker signs it with the app's Apple push key and forwards it to Apple.
// It keeps no database and writes no request contents to logs.

import { handleTunnel, TunnelRoom } from "./tunnel.js";

export { TunnelRoom };

const TITLES = {
  approval: "Your agent needs approval",
  question: "Your agent has a question",
  finished: "Your agent finished",
};

const FALLBACK_BODY = {
  approval: "Open Pocket Relay to review the request.",
  question: "Open Pocket Relay to answer.",
  finished: "Open Pocket Relay to see the result.",
};

const APNS_HOSTS = {
  production: "https://api.push.apple.com",
  sandbox: "https://api.sandbox.push.apple.com",
};

const MAX_BODY_BYTES = 8192;
const MAX_DEVICES = 10;
const TOKEN_RE = /^[0-9a-f]{64,200}$/;
const ID_RE = /^[A-Za-z0-9._:-]{0,120}$/;

const json = (status, body) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** Checks a request body and returns a cleaned copy, or throws an Error whose message is safe to return. */
export function validate(body) {
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("Body must be a JSON object");
  const kind = body.kind;
  if (!Object.hasOwn(TITLES, kind)) throw new Error("Unknown kind");
  if (!Array.isArray(body.devices) || body.devices.length === 0 || body.devices.length > MAX_DEVICES) {
    throw new Error(`devices must list 1 to ${MAX_DEVICES} devices`);
  }
  const devices = body.devices.map((d) => {
    const token = typeof d?.token === "string" ? d.token.toLowerCase() : "";
    if (!TOKEN_RE.test(token)) throw new Error("Invalid device token");
    const environment = d.environment === "sandbox" ? "sandbox" : "production";
    return { token, environment };
  });
  const text = (value, max) => (typeof value === "string" ? value.replace(/[\u0000-\u001f]+/g, " ").trim().slice(0, max) : "");
  const data = {};
  for (const key of ["approvalId", "conversationId"]) {
    const value = body.data?.[key];
    if (value === undefined || value === null || value === "") continue;
    if (typeof value !== "string" || !ID_RE.test(value)) throw new Error(`Invalid ${key}`);
    data[key] = value;
  }
  const collapseId = typeof body.collapse_id === "string" && ID_RE.test(body.collapse_id) ? body.collapse_id.slice(0, 64) : "";
  const badge = Number.isInteger(body.badge) ? Math.min(Math.max(body.badge, 0), 99) : undefined;
  return { kind, devices, hostName: text(body.host_name, 60), detail: text(body.detail, 180), data, collapseId, badge };
}

/** The notification Apple delivers to the phone. */
export function buildPayload(alert) {
  const aps = {
    alert: { title: TITLES[alert.kind], body: alert.detail || FALLBACK_BODY[alert.kind] },
    sound: "default",
  };
  if (alert.hostName) aps.alert.subtitle = alert.hostName;
  if (alert.badge !== undefined) aps.badge = alert.badge;
  if (alert.data.conversationId) aps["thread-id"] = alert.data.conversationId;
  // Allow and Deny buttons only when the phone is shown what it would be approving.
  if (alert.kind === "approval" && alert.detail && alert.data.approvalId) aps.category = "POCKETRELAY_APPROVAL";
  return { aps, ...alert.data };
}

const b64url = (bytes) => {
  let binary = "";
  for (const b of new Uint8Array(bytes)) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
};

const b64urlText = (text) => b64url(new TextEncoder().encode(text));

let cachedJwt = null; // { token, issuedAt, keyId }

/** Apple accepts a provider token for up to an hour; reuse one for 45 minutes. */
export async function providerToken(env, now = Math.floor(Date.now() / 1000)) {
  if (cachedJwt && cachedJwt.keyId === env.APNS_KEY_ID && now - cachedJwt.issuedAt < 45 * 60) return cachedJwt.token;
  const pem = String(env.APNS_PRIVATE_KEY || "").replace(/\\n/g, "\n");
  const der = Uint8Array.from(atob(pem.replace(/-----[A-Z ]+-----/g, "").replace(/\s+/g, "")), (c) => c.charCodeAt(0));
  const key = await crypto.subtle.importKey("pkcs8", der, { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const signingInput = `${b64urlText(JSON.stringify({ alg: "ES256", kid: env.APNS_KEY_ID }))}.${b64urlText(
    JSON.stringify({ iss: env.APNS_TEAM_ID, iat: now }),
  )}`;
  const signature = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key, new TextEncoder().encode(signingInput));
  cachedJwt = { token: `${signingInput}.${b64url(signature)}`, issuedAt: now, keyId: env.APNS_KEY_ID };
  return cachedJwt.token;
}

export function resetTokenCache() {
  cachedJwt = null;
}

async function sendOne(device, payload, alert, env, jwt) {
  const headers = {
    authorization: `bearer ${jwt}`,
    "apns-topic": env.APNS_BUNDLE_ID,
    "apns-push-type": "alert",
    "apns-priority": "10",
    "apns-expiration": String(Math.floor(Date.now() / 1000) + 3600),
    "content-type": "application/json",
  };
  if (alert.collapseId) headers["apns-collapse-id"] = alert.collapseId;
  const response = await fetch(`${APNS_HOSTS[device.environment]}/3/device/${device.token}`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });
  if (response.ok) return "sent";
  let reason = "";
  try {
    reason = (await response.json()).reason || "";
  } catch {
    // no body
  }
  if (response.status === 410 || reason === "BadDeviceToken" || reason === "Unregistered" || reason === "DeviceTokenNotForTopic") {
    return "invalid";
  }
  return `failed:${response.status}:${reason}`;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/health")) {
      return json(200, { ok: true, service: "pocket-relay-push" });
    }
    if (url.pathname.startsWith("/v1/tunnel/")) return handleTunnel(request, env);
    if (url.pathname !== "/v1/notify") return json(404, { error: "Not found" });
    if (request.method !== "POST") return json(405, { error: "Use POST" });
    if (!env.APNS_PRIVATE_KEY || !env.APNS_KEY_ID || !env.APNS_TEAM_ID || !env.APNS_BUNDLE_ID) {
      return json(503, { error: "Push is not configured" });
    }

    const raw = await request.text();
    if (raw.length > MAX_BODY_BYTES) return json(413, { error: "Request too large" });
    let alert;
    try {
      alert = validate(JSON.parse(raw));
    } catch (error) {
      return json(400, { error: error instanceof SyntaxError ? "Body must be JSON" : error.message });
    }

    let jwt;
    try {
      jwt = await providerToken(env);
    } catch {
      return json(503, { error: "Push is not configured" });
    }
    const payload = buildPayload(alert);
    const results = await Promise.all(
      alert.devices.map((device) => sendOne(device, payload, alert, env, jwt).catch(() => "failed:network:")),
    );
    const invalid = alert.devices.filter((_, i) => results[i] === "invalid").map((d) => d.token);
    const failed = results.filter((r) => r.startsWith("failed"));
    // Reasons only (for example "failed:403:InvalidProviderToken"); never tokens or alert text.
    if (failed.length) console.warn("apns", [...new Set(failed)].join(","));
    return json(200, {
      sent: results.filter((r) => r === "sent").length,
      failed: failed.length,
      invalid_tokens: invalid,
    });
  },
};
