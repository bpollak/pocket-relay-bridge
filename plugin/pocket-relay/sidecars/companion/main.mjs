// Pocket Relay: keep Pocket Relay Bridge running and show its pairing QR in a side panel.
import { spawn } from 'node:child_process';
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join } from 'node:path';
import { SidecarApp, Response } from 'sidecar_sdk';

const BRIDGE = 'http://127.0.0.1:8765';
const BRIDGE_HOME = join(homedir(), '.antigravity-mobile');
const BRIDGE_BIN = process.platform === 'win32'
  ? join(BRIDGE_HOME, 'runtime', '.venv', 'Scripts', 'antigravity-bridge.exe')
  : join(BRIDGE_HOME, 'runtime', '.venv', 'bin', 'antigravity-bridge');
const dataDir = process.env.PLUGIN_DATA || join(tmpdir(), 'pocket-relay');
mkdirSync(dataDir, { recursive: true });
const log = (m) => { try { appendFileSync(join(dataDir, 'helper.log'), `${new Date().toISOString()} pid=${process.pid} ${m}\n`); } catch {} };

const bridgeToken = () => JSON.parse(readFileSync(join(BRIDGE_HOME, 'state.json'), 'utf-8')).auth_token;
const healthy = async () => { try { return (await fetch(`${BRIDGE}/health`, { signal: AbortSignal.timeout(2500) })).ok; } catch { return false; } };

let spawnedAt = 0;
async function ensureBridge() {
  if (await healthy()) return 'running';
  if (!existsSync(BRIDGE_BIN)) return 'missing';
  if (Date.now() - spawnedAt > 20000) {
    spawnedAt = Date.now();
    const child = spawn(BRIDGE_BIN, ['--host', '0.0.0.0', '--port', '8765'], { cwd: join(BRIDGE_HOME, 'runtime'), detached: true, stdio: 'ignore' });
    child.unref();
    log(`started bridge pid=${child.pid}`);
  }
  return 'starting';
}

async function pairing() {
  const state = await ensureBridge();
  if (state !== 'running') return { state };
  const headers = { Authorization: `Bearer ${bridgeToken()}` };
  const [status, qr] = await Promise.all([
    fetch(`${BRIDGE}/api/status`, { headers, signal: AbortSignal.timeout(8000) }).then((r) => r.json()),
    fetch(`${BRIDGE}/api/pairing/qr.png`, { headers, signal: AbortSignal.timeout(8000) }).then((r) => r.arrayBuffer()),
  ]);
  return { state, hostName: status.host_name, models: (status.available_models || []).length, qr: `data:image/png;base64,${Buffer.from(qr).toString('base64')}` };
}

// The page shell holds no secrets. The QR (which carries the pairing token) is only returned by the
// token-checked POST below, so another local web page cannot read it.
const PAGE = `<!doctype html><html><head><meta charset="utf-8"><script src="/preload.js"></script><style>
 body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:20px;color:var(--vscode-foreground,#e6e8f5);background:var(--vscode-editor-background,#0f111a)}
 h2{margin:0 0 4px;font-size:18px} .sub{opacity:.7;font-size:13px;margin:0 0 16px}
 .step{display:flex;gap:10px;margin:10px 0;font-size:14px;line-height:1.4} .n{flex:0 0 22px;height:22px;border-radius:11px;background:#6366f1;color:#fff;font-weight:700;font-size:12px;display:flex;align-items:center;justify-content:center}
 #qr{display:block;width:240px;height:240px;margin:18px auto;background:#fff;border-radius:16px;padding:12px;box-sizing:border-box} #msg{font-size:13px;opacity:.8;text-align:center;margin:12px 0}
 button{display:block;margin:8px auto;padding:8px 14px;border-radius:10px;border:0;background:#6366f1;color:#fff;font-weight:600;cursor:pointer}
</style></head><body>
<h2>Pair your phone</h2><p class="sub" id="host">Checking this computer…</p>
<div class="step"><span class="n">1</span><span>Open <b>Pocket Relay</b> on your iPhone or iPad.</span></div>
<div class="step"><span class="n">2</span><span>Tap <b>Scan QR Code</b> and point it at the code below. You can also use the Camera app.</span></div>
<img id="qr" alt="Pairing QR code" hidden><p id="msg"></p><button id="again">Refresh</button>
<script>
 const token = new URLSearchParams(location.search).get('token') || '';
 async function load(){
   const msg=document.getElementById('msg'), img=document.getElementById('qr'), host=document.getElementById('host');
   msg.textContent='Loading…';
   try{
     const r=await fetch('/pairing',{method:'POST',headers:{'x-sidecar-token':token,'content-type':'application/json'},body:'{}'});
     const d=await r.json();
     if(d.state==='running'){ img.src=d.qr; img.hidden=false; host.textContent=d.hostName+' is ready · '+d.models+' models'; msg.textContent='Keep this code private. It lets a phone control this computer.'; }
     else if(d.state==='starting'){ img.hidden=true; host.textContent='Starting Pocket Relay Bridge…'; msg.textContent=''; setTimeout(load,3000); }
     else { img.hidden=true; host.textContent='Pocket Relay Bridge is not installed on this computer.'; msg.innerHTML='Install it with one command, then press Refresh. Instructions: <b>github.com/bpollak/pocket-relay-bridge</b>'; }
   }catch(e){ img.hidden=true; msg.textContent='Could not load the pairing code: '+e.message; }
 }
 document.getElementById('again').onclick=load; load();
</script></body></html>`;

const app = new SidecarApp();
app.page('/', () => new Response(PAGE, { contentType: 'text/html', headers: { 'Cache-Control': 'no-store' } }));
app.api('/pairing', async () => { try { return await pairing(); } catch (e) { log(`pairing error ${e.message}`); return { state: 'error', error: e.message }; } });
app.run();
log(`started; bridge ${await ensureBridge()}`);
setInterval(() => { ensureBridge().catch(() => {}); }, 30000);
for (const s of ['SIGTERM', 'SIGINT', 'SIGHUP']) process.on(s, () => { log(`signal ${s}`); process.exit(0); });
