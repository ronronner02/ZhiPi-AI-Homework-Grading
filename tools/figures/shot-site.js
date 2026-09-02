/**
 * 用 Chrome DevTools Protocol 驱动真实浏览器：进入指定页签、等待渲染、截图。
 * Node 24 内置 fetch 与 WebSocket，无需任何依赖。
 *
 *   node shot-site.js <url> <out.png> [--w 1440] [--h 950] [--js "<表达式>"] [--wait 3000] [--full]
 */
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const args = process.argv.slice(2);
const url = args[0], out = args[1];
const opt = (k, d) => { const i = args.indexOf('--' + k); return i < 0 ? d : args[i + 1]; };
const has = k => args.includes('--' + k);
const W = +opt('w', 1440), H = +opt('h', 950);
const js = opt('js', ''), wait = +opt('wait', 3500);

const sleep = ms => new Promise(r => setTimeout(r, ms));
const profile = mkdtempSync(join(tmpdir(), 'zp-cdp-'));
const port = 9300 + Math.floor(process.pid % 900);

const chrome = spawn(CHROME, [
  '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
  '--disable-lcd-text', '--font-render-hinting=none',
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  `--window-size=${W},${H}`, 'about:blank',
], { stdio: 'ignore' });

let target;
for (let i = 0; i < 80; i++) {
  try {
    const r = await fetch(`http://127.0.0.1:${port}/json/list`);
    const list = await r.json();
    target = list.find(t => t.type === 'page');
    if (target) break;
  } catch {}
  await sleep(250);
}
if (!target) { chrome.kill(); try { rmSync(profile, { recursive: true, force: true }); } catch {} throw new Error(`Chrome 未就绪（port ${port}）`); }

const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r, { once: true }));

let id = 0;
const pending = new Map();
ws.addEventListener('message', e => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
});
const send = (method, params = {}) => new Promise(res => {
  const i = ++id;
  pending.set(i, m => res(m.result ?? m.error));
  ws.send(JSON.stringify({ id: i, method, params }));
});

await send('Page.enable');
await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 2, mobile: false });
await send('Page.navigate', { url });
await sleep(wait);

if (js) {
  const r = await send('Runtime.evaluate', { expression: js, awaitPromise: true, returnByValue: true });
  console.log('JS →', JSON.stringify(r?.result?.value ?? r?.exceptionDetails?.text ?? r).slice(0, 1500));
  await sleep(2200);
}

const shotParams = { format: 'png', captureBeyondViewport: true };
if (has('full')) {
  const m = await send('Page.getLayoutMetrics');
  const h = Math.min(Math.ceil(m.cssContentSize.height), 12000);
  shotParams.clip = { x: 0, y: 0, width: W, height: h, scale: 2 };
}
const shot = await send('Page.captureScreenshot', shotParams);
writeFileSync(out, Buffer.from(shot.data, 'base64'));
console.log('OK', out);

ws.close(); chrome.kill();
await sleep(300);
try { rmSync(profile, { recursive: true, force: true }); } catch {}
