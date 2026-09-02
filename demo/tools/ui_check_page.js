/**
 * 教师页建题库 → 学生页整页批改 → 原图留痕 的界面走查。
 *
 * 会真实上传一份教师答案页 + 一份学生页并真实调模型（约 60-120 秒），
 * 因此**会写演示会话**（只影响调用者自己那个 session）、会消耗额度。
 *
 * 用法（工作目录 demo/）：
 *   node -e "const{readFileSync}=require('fs');const e=readFileSync('tools/ui_check_page.js','utf8');
 *            require('child_process').spawnSync('node',['tools/probe.mjs','--eval',e,'--url','http://127.0.0.1:8010/'],{stdio:'inherit'})"
 *
 * bad 与 errors 两个数组都为空即通过。
 *
 * 为什么要量 computed style 而不只看 DOM：本项目踩过「源码全对、页面空白」
 * 那一类坑——CSS 命中的是 <span>，display:inline 把 width 静默丢掉，
 * 盒子塌成 0 高。所以关键块一律量真实盒模型。
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
  var $ = function (s) { return document.querySelector(s); };

  async function until(fn, ms, label) {
    var t0 = Date.now();
    while (Date.now() - t0 < ms) {
      var v;
      try { v = fn(); } catch (e) { v = false; }
      if (v) return v;
      await sleep(300);
    }
    out.bad.push('超时：' + label + '（' + ms + 'ms）');
    return null;
  }

  function ok(cond, label) { (cond ? out.ok : out.bad).push(label); return cond; }

  // 量真实盒模型：宽或高为 0 的块在页面上等于不存在，哪怕 DOM 里有内容
  function box(sel, label) {
    var el = $(sel);
    if (!el) { out.bad.push('缺元素：' + label + ' (' + sel + ')'); return null; }
    var r = el.getBoundingClientRect();
    var cs = getComputedStyle(el);
    if (cs.display === 'none' || r.width < 1 || r.height < 1) {
      out.bad.push('塌成 0：' + label + ' display=' + cs.display +
        ' ' + Math.round(r.width) + '×' + Math.round(r.height));
      return null;
    }
    out.ok.push(label + ' ' + Math.round(r.width) + '×' + Math.round(r.height));
    return { el: el, rect: r, cs: cs };
  }

  // 把一个 URL 取成 File，喂给 <input type=file>。测评数据由服务端的
  // /api/page 暴露不了，所以这里用内置样例图当学生页、也当教师页——
  // 这条走查验的是**界面链路**，识别质量由 tools/run_real_eval.py 负责。
  async function pickFile(inputSel, url, name, type) {
    var blob = await (await fetch(url)).blob();
    var file = new File([blob], name, { type: type || blob.type || 'image/png' });
    var dt = new DataTransfer();
    dt.items.add(file);
    var input = $(inputSel);
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  // ---- 进入应用 ----
  if ($('#enter-btn') && getComputedStyle($('#app')).display === 'none') {
    $('#enter-btn').click();
    await sleep(700);
  }
  await until(function () { return $('#screen-submit'); }, 8000, '提交页出现');

  // ---- 两个入口都在，且顺序是「先教师页后学生页」 ----
  var tdz = box('#teacher-dropzone', '教师答案页上传区');
  var sdz = box('#dropzone', '学生作业上传区');
  if (tdz && sdz) {
    ok(tdz.rect.top < sdz.rect.top,
       '教师页入口排在学生页入口之前（先建库再批改）');
  }
  ok(/pdf/i.test($('#file-input').getAttribute('accept') || ''),
     '学生页 input 接受 PDF');
  ok(/pdf/i.test($('#teacher-file-input').getAttribute('accept') || ''),
     '教师页 input 接受 PDF');

  // ---- 教师页 → 建题库 ----
  var demo = await (await fetch('/api/demo-pages')).json();
  var groups = demo.groups || [];
  var teacherUrl = '', studentUrl = '';
  for (var g = 0; g < groups.length; g++) {
    var items = groups[g].items || [];
    for (var i = 0; i < items.length; i++) {
      if (!teacherUrl && items[i].role === 'teacher' && items[i].url) teacherUrl = items[i].url;
      if (!studentUrl && items[i].role === 'student' && items[i].url) studentUrl = items[i].url;
      if (teacherUrl && studentUrl) break;
    }
    if (teacherUrl && studentUrl) break;
  }
  if (!teacherUrl) { out.bad.push('没有内置教师答案页可用于走查'); return out; }
  if (!studentUrl) { out.bad.push('没有内置学生作业页可用于走查'); return out; }

  await pickFile('#teacher-file-input', teacherUrl, 'ui走查答案页.png');
  var built = await until(function () {
    var s = $('#teacher-status');
    return s && (s.querySelector('.note--green') || s.querySelector('.note--red'));
  }, 180000, '题库建立返回');
  if (built) {
    var failed = $('#teacher-status').querySelector('.note--red');
    if (failed) {
      // 没配多模态密钥时这里必然失败，且提示里要说清「跳过也能批改」
      var msg = failed.textContent || '';
      out.info.bankError = msg.slice(0, 160);
      ok(/多模态|密钥|额度/.test(msg), '建库失败时说明了原因（而不是一句「失败」）');
    } else {
      ok(true, '题库建立成功');
      box('.bank-list', '题库清单');
      // 分值来源必须可见：整页总分是各题分值之和，推定占了多少要说出来
      var note = $('#teacher-status').textContent || '';
      ok(/卷面|推定/.test(note), '说明了分值来源（卷面印的 / 系统推定）');
      // 「不用题库」这一项要在，否则用户建了库就再也回不到无题库口径
      ok(!!$('.bank-row--none'), '提供了「不用题库」选项');
    }
  }

  // ---- 学生页 → 识别 ----
  await pickFile('#file-input', studentUrl, 'ui走查学生页.png');
  await until(function () { return $('#stage-box') &&
    getComputedStyle($('#stage-box')).display !== 'none'; }, 8000, '待批清单出现');
  box('.stage-list', '待批清单');
  $('#stage-grade-btn').click();

  var recognized = await until(function () {
    var r = $('#recog-result');
    return r && getComputedStyle(r).display !== 'none';
  }, 180000, '识别返回');
  if (!recognized) {
    out.info.recogStatus = ($('#recog-status') || {}).textContent;
    return out;
  }
  box('#recog-badges', '识别徽章区');

  // ---- 整页批改按钮：有 page_id 就该出现 ----
  var pgBtn = $('#paper-grade-btn');
  var pgVisible = pgBtn && getComputedStyle(pgBtn).display !== 'none';
  ok(pgVisible, '「批改整页」按钮可见（服务端已留底页图）');
  if (!pgVisible) return out;

  out.info.pageBtnLabel = ($('#paper-grade-label') || {}).textContent;
  pgBtn.click();

  var graded = await until(function () {
    var b = $('#result-body');
    return b && b.querySelector('.score-row');
  }, 300000, '整页批改返回');
  if (!graded) {
    out.info.gradeHint = ($('#grade-hint') || {}).textContent;
    return out;
  }

  // ---- 结果页：痕迹图 + 逐题表 ----
  // 先等图真正加载完再量盒子：<img> 未加载时父块只有 padding 的高度，
  // 这时候量出来的「1316×35」不是塌陷，是量早了——会把一个好页面报成坏的。
  var img = await until(function () { return $('.marked__img'); }, 10000, '痕迹图元素出现');
  if (img) {
    await until(function () { return img.complete && img.naturalWidth > 0; },
                20000, '痕迹图加载完成');
    ok(img.naturalWidth > 200, '痕迹图真实加载（' + img.naturalWidth + '×' +
       img.naturalHeight + '）');
    box('.marked', '批改痕迹块');
    ok(/道题的记号/.test(($('.marked__note') || {}).textContent || ''),
       '标注了有多少道题的记号按坐标落笔（坐标不准要如实说）');
  }
  box('.pq-list', '逐题得分表');
  var pqs = document.querySelectorAll('.pq');
  ok(pqs.length > 0, '逐题表有 ' + pqs.length + ' 行');
  // 每行都得有分数，否则这张表没有意义
  var noScore = Array.prototype.filter.call(pqs, function (row) {
    return !(row.querySelector('.pq__score') || {}).textContent;
  }).length;
  ok(noScore === 0, '逐题表每行都有得分（缺 ' + noScore + ' 行）');

  // 总分口径必须写清楚：满分是各题分值之和，有几道是推的
  var basisNote = document.querySelector('.score-card__note');
  ok(basisNote && /题库|推定|卷面/.test(basisNote.textContent),
     '总分卡说明了分值口径');

  // 五因子仍在，且「答案匹配度」有题库时应计分、无题库时应显式标注重归一化
  var chips = Array.prototype.map.call(
    document.querySelectorAll('.factor-wrap .factor'), function (c) { return c.textContent; });
  out.info.factorChips = chips.filter(function (t) { return /匹配|清晰|Rubric|一致|通过率/.test(t); });
  ok(out.info.factorChips.length >= 5, '置信度五因子全部出现');

  // 横向溢出：真实作业页图很宽，容器没兜住就会顶出横向滚动条
  ok(document.documentElement.scrollWidth <= window.innerWidth + 1,
     '页面无横向溢出（scrollWidth ' + document.documentElement.scrollWidth +
     ' vs ' + window.innerWidth + '）');

  out.errors = window.__pErr.slice();
  return out;
})();
