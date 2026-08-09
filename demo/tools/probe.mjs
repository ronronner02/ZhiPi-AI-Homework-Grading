/**
 * 前端探针：用 Chrome DevTools Protocol 打开真实页面，跑一段脚本，取回值 / 截图。
 *
 * 为什么需要它：本项目的视觉 bug 有一类**看源码看不出来**——CSS 规则写得对，
 * 但选择器命中的元素是 <span>，display:inline 把 width / aspect-ratio 静默丢掉，
 * 盒子塌成 0 高，绝对定位的子元素全部归零。源码里每一行都"对"，页面上什么都没有。
 * 这类问题只有量真实的 computed style 才能定位，所以做成常驻工具而不是一次性脚本。
 *
 * 直接用 CDP 而不装 puppeteer：本项目零外部依赖是硬约束（断网预案），
 * Node 22+ 自带 WebSocket，够用了。
 *
 * 用法：
 *   node tools/probe.mjs --eval "<JS 表达式>"        求值并打印 JSON
 *   node tools/probe.mjs --shot out.png             截图（整页）
 *   node tools/probe.mjs --eval "..." --shot a.png  两者都要
 *   node tools/probe.mjs --w 1440 --h 900           视口尺寸
 *   node tools/probe.mjs --url http://127.0.0.1:8011/
 *   node tools/probe.mjs --raw                      不自动进应用，停在首屏
 *   node tools/probe.mjs --clip ".folder-grid" --shot g.png   只截某个元素
 *   node tools/probe.mjs --pad 24                   --clip 时四周留白
 *
 * 注意 --w/--h 是**视口**尺寸，经 Emulation.setDeviceMetricsOverride 设置，
 * 不是 --window-size。Windows 上窗口有最小宽度（约 500px），用 --window-size
 * 量移动端会得到「innerWidth 504 然后裁到 390」的假象，量出来的溢出全是假的。
 */
import { spawn } from 'node:child_process';
import { readFileSync, existsSync, writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const DEMO = resolve(HERE, '..');

const CHROME_CANDIDATES = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  `${process.env.LOCALAPPDATA || ''}/Google/Chrome/Application/chrome.exe`,
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
];

function parseArgs(argv) {
  const out = { w: 1440, h: 900, url: 'http://127.0.0.1:8011/', evals: [], shots: [],
                raw: false, wait: 0, clip: null, pad: 16 };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--eval') out.evals.push(argv[++i]);
    else if (a === '--shot') out.shots.push(argv[++i]);
    else if (a === '--w') out.w = Number(argv[++i]);
    else if (a === '--h') out.h = Number(argv[++i]);
    else if (a === '--url') out.url = argv[++i];
    else if (a === '--raw') out.raw = true;
    else if (a === '--wait') out.wait = Number(argv[++i]);
    else if (a === '--clip') out.clip = argv[++i];
    else if (a === '--pad') out.pad = Number(argv[++i]);
  }
  return out;
}

/** 从 deploy/.env 读访问码。刻意不打印——这份输出会被贴进对话和文档。 */
function accessCode() {
  for (const p of [resolve(DEMO, '../deploy/.env'), resolve(DEMO, '.env')]) {
    if (!existsSync(p)) continue;
    for (const line of readFileSync(p, 'utf8').split(/\r?\n/)) {
      const m = /^ZHIPI_ACCESS_CODE\s*=\s*(.*)$/.exec(line.trim());
      if (m) return m[1].trim().replace(/^["']|["']$/g, '');
    }
  }
  return '';
}

function findChrome() {
  for (const p of CHROME_CANDIDATES) if (existsSync(p)) return p;
  throw new Error('找不到 Chrome / Edge，请手动改 CHROME_CANDIDATES');
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * 等 CDP 端点就绪。
 * 必须同时盯着子进程：Chrome 起不来时（profile 被锁、参数不对、被安全软件拦），
 * 它会**立刻退出**，而单纯轮询 HTTP 只会等满超时然后报「端点无响应」，
 * 把「Chrome 根本没起来」误报成「网络慢」，方向全错。
 */
async function getJSON(url, proc, tries = 80) {
  for (let i = 0; i < tries; i++) {
    if (proc.exitCode !== null) {
      throw new Error(`Chrome 启动后立即退出（code=${proc.exitCode}）。`
        + '常见原因：user-data-dir 被占用、被安全软件拦截。');
    }
    try {
      const r = await fetch(url);
      if (r.ok) return await r.json();
    } catch { /* 端口还没起来，继续等 */ }
    await sleep(250);
  }
  throw new Error(`CDP 端点 ${tries * 250}ms 内无响应：${url}`);
}

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.handlers = new Map();
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve: res, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : res(msg.result);
      } else if (msg.method && this.handlers.has(msg.method)) {
        this.handlers.get(msg.method).forEach((fn) => fn(msg.params));
      }
    });
  }
  send(method, params = {}, timeoutMs = 30000) {
    const id = ++this.id;
    return new Promise((res, reject) => {
      this.pending.set(id, { resolve: res, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) { this.pending.delete(id); reject(new Error(`${method} 超时`)); }
      }, timeoutMs);
    });
  }
  on(method, fn) {
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(fn);
  }
  /** 求值并把结果按值取回（不要 objectId，避免跨进程引用）
   *
   * 超时给得比其他命令宽：自检脚本里会等真实识别 + 真实批改回来，
   * 单次 evaluate 动辄 40-90 秒，按 30 秒砍会砍在半路上，
   * 报出来的是「Runtime.evaluate 超时」，看不出是脚本还没跑完。
   */
  async evaluate(expr, timeoutMs = 300000) {
    const r = await this.send('Runtime.evaluate', {
      expression: `(function(){ ${expr} })()`,
      returnByValue: true, awaitPromise: true,
    }, timeoutMs);
    if (r.exceptionDetails) {
      throw new Error('页内异常：' + (r.exceptionDetails.exception?.description
        || r.exceptionDetails.text));
    }
    return r.result.value;
  }
}

/**
 * 拿到真实的调试端口。
 * 刻意传 --remote-debugging-port=0 让 Chrome 自己挑，再从 profile 里的
 * DevToolsActivePort 读回来。原先是自己在 9222–9522 里随机取一个，
 * 撞上已被占用的端口时 Chrome **不会报错也不会退出**——它照常把页面跑起来，
 * 只是没有调试监听。于是表现成"端点超时"，方向完全指错。
 */
async function realPort(profile, proc, tries = 80) {
  const f = resolve(profile, 'DevToolsActivePort');
  for (let i = 0; i < tries; i++) {
    if (proc.exitCode !== null) {
      throw new Error(`Chrome 启动后立即退出（code=${proc.exitCode}）。`
        + '常见原因：user-data-dir 被占用、被安全软件拦截。');
    }
    if (existsSync(f)) {
      const port = Number(readFileSync(f, 'utf8').split('\n')[0].trim());
      if (port > 0) return port;
    }
    await sleep(250);
  }
  throw new Error('Chrome 未写出 DevToolsActivePort，调试端口没起来');
}

async function main() {
  const args = parseArgs(process.argv);
  const chrome = findChrome();
  // 每次跑用独立 profile：并发或前一次没退干净时，共享 profile 会被 Chrome 锁住，
  // 表现为 /json/list 一直不响应，看起来像端口问题，其实是 profile 冲突。
  const profile = resolve(DEMO, `../.probe-profile/${process.pid}-${Date.now()}`);
  mkdirSync(profile, { recursive: true });

  const proc = spawn(chrome, [
    '--headless=new',
    '--remote-debugging-port=0',
    `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--disable-gpu',
    // 机器上装了往 Chrome 注册表键里塞扩展的软件（夸克网盘），
    // 每次启动都刷一屏 external_registry_loader_win 报错并拖慢启动
    '--disable-features=ExternalExtensionLoader',
    '--hide-scrollbars',
    // 字体渲染稳定，多次截图才能做 md5 比对
    '--force-device-scale-factor=1',
    'about:blank',
  ], { stdio: 'ignore' });

  let ws;
  try {
    const port = await realPort(profile, proc);
    const list = await getJSON(`http://127.0.0.1:${port}/json/list`, proc);
    const page = list.find((t) => t.type === 'page') || list[0];
    ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener('open', res);
      ws.addEventListener('error', () => rej(new Error('WebSocket 连接失败')));
    });
    const cdp = new CDP(ws);

    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: args.w, height: args.h, deviceScaleFactor: 1, mobile: args.w < 700,
    });

    // 页内 console.error 与未捕获异常直接抛到我这边——静默的 JS 报错是最难查的
    const jsErrors = [];
    cdp.on('Runtime.exceptionThrown', (p) => {
      jsErrors.push(p.exceptionDetails?.exception?.description || p.exceptionDetails?.text);
    });
    cdp.on('Runtime.consoleAPICalled', (p) => {
      if (p.type === 'error') {
        jsErrors.push(p.args.map((a) => a.value ?? a.description ?? '').join(' '));
      }
    });

    const code = accessCode();
    const url = code ? `${args.url}${args.url.includes('?') ? '&' : '?'}code=${encodeURIComponent(code)}` : args.url;
    const loaded = new Promise((res) => cdp.on('Page.loadEventFired', res));
    await cdp.send('Page.navigate', { url });
    await loaded;
    await sleep(400);

    // 默认自动跨过叙事首屏：真正要看的组件都在应用内
    if (!args.raw) {
      await cdp.evaluate(`
        var b = document.getElementById('enter-btn');
        if (b) b.click();
        return !!b;
      `);
      await sleep(900);   // 等 pageTurn 落位 + /api/folders 回来
    }
    if (args.wait) await sleep(args.wait);

    for (const expr of args.evals) {
      const val = await cdp.evaluate(expr);
      console.log(typeof val === 'string' ? val : JSON.stringify(val, null, 2));
    }

    // --clip：量元素在**页面坐标系**里的位置。用 getBoundingClientRect + scrollY，
    // 因为 captureBeyondViewport 的 clip 原点是文档左上，不是视口左上。
    let clipRect = null;
    if (args.clip) {
      clipRect = await cdp.evaluate(`
        var el = document.querySelector(${JSON.stringify(args.clip)});
        if (!el) return null;
        var r = el.getBoundingClientRect();
        var pad = ${args.pad};
        return { x: Math.max(0, r.left + window.scrollX - pad),
                 y: Math.max(0, r.top + window.scrollY - pad),
                 width: r.width + pad * 2, height: r.height + pad * 2, scale: 1 };
      `);
      if (!clipRect) throw new Error(`--clip 选择器没命中：${args.clip}`);
    }

    for (const shot of args.shots) {
      const { data } = await cdp.send('Page.captureScreenshot', {
        format: 'png', captureBeyondViewport: true,
        ...(clipRect ? { clip: clipRect } : {}),
      });
      const out = resolve(process.cwd(), shot);
      mkdirSync(dirname(out), { recursive: true });
      writeFileSync(out, Buffer.from(data, 'base64'));
      console.log(`[shot] ${out}`);
    }

    if (jsErrors.length) {
      console.log('\n[页内 JS 错误 ' + jsErrors.length + ' 条]');
      jsErrors.forEach((e) => console.log('  ' + e));
      process.exitCode = 1;
    }
  } finally {
    try { ws?.close(); } catch { /* 已断开 */ }
    proc.kill();
    // 清掉临时 profile，不然 .probe-profile 会一次跑一个目录地长起来
    try { rmSync(profile, { recursive: true, force: true }); } catch { /* 被占用就留着 */ }
  }
}

main().catch((e) => { console.error('探针失败：' + e.message); process.exit(1); });
