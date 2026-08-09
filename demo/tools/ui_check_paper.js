/**
 * 整份试卷 / 一图多题界面走查：题目清单、超限提示、整份批改、同卷标记。
 *
 * 会真实上传一张 3 道题的卷子并真实批改每一道（约 30-120 秒），因此
 * **会写演示会话**（只影响调用者自己那个 session）。
 *
 * 用法（工作目录 demo/）：
 *   node -e "const{readFileSync}=require('fs');const e=readFileSync('tools/ui_check_paper.js','utf8');
 *            require('child_process').spawnSync('node',['tools/probe.mjs','--eval',e,'--url','http://127.0.0.1:8024/'],{stdio:'inherit'})"
 *
 * 想验超限提示：把服务起在 ZHIPI_MAX_PAPER_QUESTIONS=2 上再指过去。
 *
 * bad 与 errors 两个数组都为空即通过。
 */
return (async function () {
  var out = { errors: [], bad: [], ok: [], info: {} };

  window.__pErr = window.__pErr || [];
  if (!window.__pHooked) {
    window.__pHooked = 1;
    var oe = console.error;
    console.error = function () {
      window.__pErr.push(Array.prototype.slice.call(arguments).join(' '));
      oe.apply(console, arguments);
    };
    window.addEventListener('error', function (e) {
      window.__pErr.push('onerror: ' + e.message);
    });
    window.addEventListener('unhandledrejection', function (e) {
      window.__pErr.push('unhandled: ' + (e.reason && e.reason.message || e.reason));
    });
  }

  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

  async function until(fn, ms, label) {
    var t0 = Date.now();
    while (Date.now() - t0 < ms) {
      var v;
      try { v = fn(); } catch (e) { v = false; }
      if (v) return v;
      await sleep(300);
    }
    out.bad.push('等超时(' + Math.round(ms / 1000) + 's): ' + label);
    return false;
  }

  function vis(sel, label) {
    var el = document.querySelector(sel);
    if (!el) { out.bad.push(label + ' 找不到 ' + sel); return null; }
    var r = el.getBoundingClientRect();
    var cs = getComputedStyle(el);
    if (r.width < 1 || r.height < 1) {
      out.bad.push(label + ' 盒子塌了 ' + sel + ' → ' + r.width.toFixed(1) + '×' +
        r.height.toFixed(1) + ' display=' + cs.display);
    } else {
      out.ok.push(label + ' ' + Math.round(r.width) + '×' + Math.round(r.height));
    }
    return el;
  }

  async function clickable(el, label) {
    if (!el) { out.bad.push(label + ' 元素不存在'); return false; }
    var r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) { out.bad.push(label + ' 盒子塌了，点不到'); return false; }
    // behavior 必须写 'instant'：页面设了 html{scroll-behavior:smooth}，
    // 而 'auto' 的语义是「沿用 CSS 的 scroll-behavior」，也就是照样走平滑动画。
    // 手机上这一跳有 1000px 上下，等 250ms 远远不够，量到的是动画中途的坐标，
    // 报出来是"滚动后仍在视口外"——看着像布局 bug，其实是量得太早。
    el.scrollIntoView({ block: 'center', behavior: 'instant' });
    await sleep(120);
    r = el.getBoundingClientRect();
    var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cy < 0 || cy > innerHeight || cx < 0 || cx > innerWidth) {
      out.bad.push(label + ' 滚动后仍在视口外 cy=' + Math.round(cy) + ' vh=' + innerHeight);
      return false;
    }
    var top = document.elementFromPoint(cx, cy);
    if (!top) { out.bad.push(label + ' 命中点无元素'); return false; }
    if (top !== el && !el.contains(top) && !top.contains(el)) {
      out.bad.push(label + ' 被遮挡：命中 ' + top.tagName + '.' +
        (top.className || '').toString().slice(0, 40));
      return false;
    }
    out.ok.push(label + ' 可点');
    return true;
  }

  var enter = document.querySelector('#enter-app, [data-enter], .hero .btn--primary');
  if (enter) { enter.click(); await sleep(600); }
  var t = document.querySelector('[data-tab="upload"]');
  if (t) { t.click(); await sleep(400); }

  // 画一张 3 道题的卷子
  var cv = document.createElement('canvas');
  cv.width = 1000; cv.height = 1150;
  var g = cv.getContext('2d');
  g.fillStyle = '#fff'; g.fillRect(0, 0, cv.width, cv.height);
  var y = 60;
  function pr(text, hand) {
    g.fillStyle = hand ? '#1a3a8f' : '#141414';
    g.font = (hand ? '25px cursive' : '27px serif');
    g.fillText(text, 45, y);
    y += 52;
  }
  pr('一、(6分) 计算 (-3) + 7 - (-2) 的值。');
  pr('解：原式 = -3 + 7 + 2 = 6', 1);
  y += 20;
  pr('二、(8分) 解方程 2x - 5 = 11。');
  pr('解：2x = 16，所以 x = 8', 1);
  y += 20;
  pr('三、(10分) 已知长方形长 8cm，宽 5cm，求周长和面积。');
  pr('解：周长 = (8+5)×2 = 26cm', 1);
  pr('面积 = 8×5 = 40cm²', 1);

  var blob = await new Promise(function (r) { cv.toBlob(r, 'image/png'); });
  var file = new File([blob], 'paper3.png', { type: 'image/png' });
  var input = document.querySelector('input[type="file"]');
  if (!input) { out.bad.push('找不到文件输入框'); return out; }
  var dt = new DataTransfer();
  dt.items.add(file);
  input.files = dt.files;
  input.dispatchEvent(new Event('change', { bubbles: true }));

  var recogOk = await until(function () {
    var rr = document.querySelector('#recog-result');
    return rr && rr.style.display !== 'none' &&
      document.querySelector('#ocr-text') &&
      document.querySelector('#ocr-text').value.length > 0;
  }, 180000, '识别返回');
  if (!recogOk) { out.errors = (window.__pErr || []).slice(0); return out; }

  // ---- 题目清单 ----
  var sheet = document.querySelector('#paper-sheet');
  var nq = (window.__recogCount || 0);
  var rows = sheet ? sheet.querySelectorAll('.paper-q') : [];
  out.info.paperRows = rows.length;
  out.info.badges = (document.querySelector('#recog-badges') || {}).textContent || '';
  out.info.badges = out.info.badges.replace(/\s+/g, ' ').trim();

  if (rows.length < 2) {
    // 只认出一道题时清单该整块隐藏，界面与单题一致
    out.info.singleQuestion = true;
    if (sheet && sheet.style.display !== 'none') {
      out.bad.push('只认出 1 道题，题目清单却仍显示');
    }
    var pb0 = document.querySelector('#paper-grade-btn');
    if (pb0 && pb0.style.display !== 'none') out.bad.push('单题时「批改整份」按钮仍显示');
    out.errors = (window.__pErr || []).slice(0);
    return out;
  }

  vis('#paper-sheet', '题目清单');
  vis('.paper-list', '清单列表');
  for (var i = 0; i < rows.length; i++) {
    var rr = rows[i].getBoundingClientRect();
    if (rr.height < 1) out.bad.push('第 ' + (i + 1) + ' 行盒子塌了');
    // 题面摘要不能是空的：整份批改要靠它让教师认出是哪道题
    var tt = rows[i].querySelector('.paper-q__t');
    if (!tt || !tt.textContent.trim()) out.bad.push('第 ' + (i + 1) + ' 行没有题目标题');
  }
  out.ok.push('题目清单 ' + rows.length + ' 行');

  // 超限提示：有 is-off 行就必须有说明文字
  var offRows = sheet.querySelectorAll('.paper-q.is-off');
  out.info.offRows = offRows.length;
  if (offRows.length) {
    if (sheet.textContent.indexOf('最多批改') < 0) {
      out.bad.push('有超限题却没有「最多批改」说明');
    } else {
      out.ok.push('超限提示可见（' + offRows.length + ' 道超限）');
    }
    vis('.paper-q__off', '超限标记');
  }

  // ---- 整份批改 ----
  var pb = document.querySelector('#paper-grade-btn');
  out.info.paperBtnLabel = (document.querySelector('#paper-grade-label') || {}).textContent || '';
  if (!(await clickable(pb, '批改整份按钮'))) {
    out.errors = (window.__pErr || []).slice(0);
    return out;
  }
  pb.click();

  var doneOk = await until(function () {
    var b = document.querySelector('#batch-done');
    return b && b.style.display !== 'none' && b.textContent.indexOf('整份批改完成') >= 0;
  }, 400000, '整份批改完成');

  if (doneOk) {
    vis('#batch-done', '完成提示');
    var prog = (document.querySelector('#batch-progress') || {}).textContent || '';
    out.info.progress = prog.replace(/\s+/g, ' ').trim();
    if (prog.indexOf('张') >= 0) out.bad.push('整份批改的进度量词错了，显示成「张」');
    var doneTxt = document.querySelector('#batch-done').textContent;
    out.info.doneText = doneTxt.replace(/\s+/g, ' ').trim().slice(0, 150);
    if (doneTxt.indexOf('张') >= 0) out.bad.push('完成提示量词错了，显示成「张」');
    var items = document.querySelectorAll('#batch-list .batch-item');
    out.info.itemCount = items.length;
    var failed = document.querySelectorAll('#batch-list .batch-item.is-fail').length;
    out.info.failedItems = failed;
    if (failed) out.bad.push(failed + ' 道题批改失败（见 batch-list）');
  }

  // ---- 教师表同卷标记 ----
  var tt2 = document.querySelector('[data-tab="teacher"]');
  if (tt2) tt2.click();
  var rowsOk = await until(function () {
    return document.querySelectorAll('.teacher-table tbody tr').length > 0;
  }, 60000, '教师表格出行');
  if (rowsOk) {
    var marks = document.querySelectorAll('.paper-mark');
    out.info.paperMarks = marks.length;
    if (!marks.length) {
      out.bad.push('教师表里没有同卷标记，同一张卷子的多行看不出同源');
    } else {
      vis('.paper-mark', '同卷标记');
      out.info.markText = marks[0].textContent.trim();
    }
  }

  // ---- 班级看板人数口径 ----
  var tb = document.querySelector('[data-tab="board"]');
  if (tb) {
    tb.click();
    var kpiOk = await until(function () {
      var k = document.querySelector('.kpi-row');
      return k && k.textContent.indexOf('参与作答') >= 0;
    }, 60000, '看板 KPI 出现');
    if (kpiOk) {
      var kr = document.querySelector('.kpi-row').textContent.replace(/\s+/g, ' ').trim();
      out.info.kpi = kr.slice(0, 120);
      var sub = document.querySelector('.kpi__sub');
      if (sub) {
        vis('.kpi__sub', '份数说明');
        out.info.kpiSub = sub.textContent.trim();
      }
    }
  }

  out.errors = (window.__pErr || []).slice(0);
  return out;
})();
