/**
 * 内置样例夹自检：四夹 → 夹内清单 → 加入待批清单，量真实 computed style。
 *
 * 为什么单独一个脚本：内置样例取代 11 张合成图之后，「样例怎么进批改」这条路
 * 整个换了实现——从 selectSample() 直接调接口，变成 fetch 文件字节包成 File 再
 * 走 stageFiles()。链路上任何一环断掉，页面表现都是「按钮点了没反应」，
 * 与 CSS 塌陷一样看源码看不出来。
 *
 * **刻意不点「用这份建题库」**：那一步是真实 VLM 调用，一次几十秒且花钱。
 * 这里只验到「按钮在、绑定对」为止，建库本身由 ui_check_page.js 走查。
 *
 * 用法（工作目录 demo/）：
 *   node -e "const{readFileSync}=require('fs');const e=readFileSync('tools/ui_check_builtin.js','utf8');
 *            require('child_process').spawnSync('node',['tools/probe.mjs','--eval',e],{stdio:'inherit'})"
 *
 * bad 数组为空即通过。
 */
return (function () {
  var out = { bad: [], ok: [], counts: {} };

  function $(s) { return document.querySelector(s); }
  function $$(s) { return Array.prototype.slice.call(document.querySelectorAll(s)); }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  // 一律轮询到目标出现，不拍固定 sleep——等不够的表现是静默的。
  async function until(fn, ms, tag) {
    var deadline = Date.now() + (ms || 8000);
    while (Date.now() < deadline) {
      var v = fn();
      if (v) return v;
      await sleep(200);
    }
    if (tag) out.bad.push('等不到：' + tag);
    return null;
  }

  // 量盒子：宽或高为 0 就是塌陷（本项目最常见的一类 bug）
  function box(sel, tag) {
    var el = $(sel);
    if (!el) { out.bad.push(tag + ' 不存在（' + sel + '）'); return null; }
    var r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) {
      out.bad.push(tag + ' 塌陷 ' + Math.round(r.width) + '×' + Math.round(r.height));
      return null;
    }
    out.ok.push(tag + ' ' + Math.round(r.width) + '×' + Math.round(r.height));
    return r;
  }

  return (async function () {
    // ---------------- 四个内置夹都在，且顺序是 语文/数学/英语/题库 ----------------
    await until(function () { return $$('.folder').length; }, 8000, '文件夹网格渲染');
    var ids = $$('[data-folder]').map(function (el) { return el.dataset.folder; });
    out.counts.folderIds = ids;
    ['bi-chinese', 'bi-math', 'bi-english', 'bi-bank'].forEach(function (id) {
      if (ids.indexOf(id) < 0) out.bad.push('缺内置夹 ' + id);
    });

    // ---------------- 打开数学夹：夹内清单 ----------------
    var mathCard = $('[data-folder="bi-math"]');
    if (!mathCard) { out.bad.push('找不到数学夹卡片'); return out; }
    mathCard.click();
    await sleep(300);
    $('#folder-open-btn').click();

    await until(function () { return $$('.file-row').length; }, 8000, '夹内清单出现');
    out.counts.mathRows = $$('.file-row').length;
    box('.file-list', '夹内清单');

    // 缩略图必须真加载出来：src 指向 /api/demo-pages/thumb/*，
    // 打包漏生成缩略图时这里是 0×0 而不是报错。
    var thumb = $('.file-row__thumb img');
    if (!thumb) {
      out.bad.push('夹内清单没有缩略图 <img>');
    } else {
      await until(function () { return thumb.complete && thumb.naturalWidth > 0; },
                  10000, '缩略图加载完成');
      out.counts.thumbNatural = thumb.naturalWidth + '×' + thumb.naturalHeight;
      if (!(thumb.naturalWidth > 0)) out.bad.push('缩略图加载失败：' + thumb.getAttribute('src'));
      box('.file-row__thumb', '缩略图盒子');
    }

    // 学生夹里每一行都该是「加入待批清单」，一个「用这份建题库」都不该有
    out.counts.stageBtns = $$('[data-demo-stage]').length;
    out.counts.bankBtnsInMath = $$('[data-demo-bank]').length;
    if (!out.counts.stageBtns) out.bad.push('学生夹没有 data-demo-stage 入口');
    if (out.counts.bankBtnsInMath) out.bad.push('学生夹里出现了建题库入口');

    // ---------------- 点「加入待批清单」：走真实 fetch→blob→File→stageFiles ----------------
    var before = $$('.stage-item').length;
    $('[data-demo-stage]').click();
    // 取文件字节要过一次网络，1-2MB 的卷子在本机也要几百毫秒
    await until(function () { return $$('.stage-item').length > before; }, 20000,
                '内置样例进入待批清单');
    out.counts.stageRows = $$('.stage-item').length;
    if (out.counts.stageRows > before) {
      out.ok.push('内置样例 → 待批清单（' + before + ' → ' + out.counts.stageRows + '）');
      box('#stage-box', '待批清单区');
      // 学生名是个 input，取 value；缩略图是 blob: URL，另外量
      var nameEl = $('.stage-item__name');
      out.counts.stageName = nameEl ? (nameEl.value || '').trim().slice(0, 40) : '(无输入框)';
      if (!nameEl) out.bad.push('待批清单缺学生姓名输入框');
      // 名字里不该带「（N页）」——那是标题不是学生名，打包时已剥掉
      if (/（\d+页）/.test(out.counts.stageName)) {
        out.bad.push('待批清单里的名字带了页数：' + out.counts.stageName);
      }
      // 缩略图用的是 blobUrl：fetch 回来的字节真的成了 File 才有这个
      var sThumb = $('.stage-item__thumb');
      if (sThumb && sThumb.tagName === 'IMG') {
        await until(function () { return sThumb.complete && sThumb.naturalWidth > 0; },
                    10000, '待批缩略图加载');
        out.counts.stageThumb = sThumb.naturalWidth + '×' + sThumb.naturalHeight;
        if (!/^blob:/.test(sThumb.src)) {
          out.bad.push('待批缩略图不是 blob URL，文件可能没真的取回来');
        }
      }
    }
    // 批改按钮该亮起来（真点会调模型，只验存在与可见）
    var gradeBtn = $('#stage-grade-btn');
    if (!gradeBtn) out.bad.push('待批清单没有「全部批改」按钮');
    else if (getComputedStyle(gradeBtn).display === 'none') out.bad.push('「全部批改」按钮不可见');
    else out.ok.push('「全部批改」按钮可见');

    // ---------------- 题库夹：按科目分节 + 合并建库 ----------------
    $('[data-folder="bi-bank"]').click();
    await sleep(300);
    $('#folder-open-btn').click();
    await until(function () { return $$('[data-demo-bank]').length; }, 8000, '题库夹清单出现');
    out.counts.bankRows = $$('.file-row').length;
    out.counts.bankBtns = $$('[data-demo-bank]').length;
    // 分节渲染进 #folder-detail-body（renderBankFolder 的落点），不是 #bank-box
    out.counts.subjectHeads = $$('#folder-detail-body .block-title').map(function (el) {
      return (el.textContent || '').trim();
    });
    out.counts.bankSections = $$('#folder-detail-body .file-list').length;
    if (out.counts.subjectHeads.length < 2) {
      out.bad.push('题库夹没有按科目分节，只有 ' + out.counts.subjectHeads.length + ' 个小标题');
    }
    // 合并建库这条工具条：勾选框 + 按钮 + 提示，缺一个就没法「一次建一套库」
    if (!$('#bank-merge-btn')) out.bad.push('题库夹缺「合并建库」按钮');
    if (!$('[data-bank-pick]')) out.bad.push('题库夹缺勾选框');
    var merge = $('#bank-merge-btn');
    if (merge && !merge.disabled) out.bad.push('未勾选时「合并建库」应禁用');
    // 勾一个，按钮该解禁并报数
    var cb = $('[data-bank-pick]');
    if (cb) {
      cb.click();
      await sleep(200);
      if (merge && merge.disabled) out.bad.push('勾选后「合并建库」仍禁用');
      out.counts.mergeLabel = ($('#bank-merge-label') || {}).textContent;
      $('#bank-pick-clear').click();
      await sleep(200);
      if (merge && !merge.disabled) out.bad.push('清空勾选后「合并建库」应重新禁用');
    }

    // 横向溢出：题库夹一行信息最长（带「对应学生页：…」），最容易撑破
    var de = document.documentElement;
    out.counts.scrollW = de.scrollWidth;
    out.counts.clientW = de.clientWidth;
    if (de.scrollWidth > de.clientWidth + 2) {
      out.bad.push('题库夹横向溢出 ' + (de.scrollWidth - de.clientWidth) + 'px');
    }

    return out;
  })();
})();
