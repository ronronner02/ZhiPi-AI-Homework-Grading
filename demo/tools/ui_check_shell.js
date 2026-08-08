// 迁移自检：量真实 computed style，不看源码。
// 重点是「CSS 每行都对、页面却什么都没有」那一类：选择器命中的是 <span>，
// display:inline 把 width/aspect-ratio 静默丢掉，盒子塌成 0×0。
/**
 * 外壳自检：量真实 computed style，不看源码。
 *
 * 为什么量 computed style 而不读 CSS：本项目有一类 bug **看源码看不出来**——
 * 选择器命中的元素是 <span>，display:inline 把 width / aspect-ratio 静默丢掉，
 * 盒子塌成 0×0，绝对定位的子元素全部归零。源码每一行都"对"，页面上什么都没有。
 *
 * 用法（工作目录 demo/）：
 *   node -e "const{readFileSync}=require('fs');const e=readFileSync('tools/ui_check_shell.js','utf8');
 *            require('child_process').spawnSync('node',['tools/probe.mjs','--eval',e],{stdio:'inherit'})"
 * 或直接把文件内容作为 --eval 传给 probe.mjs。
 *
 * bad 数组为空即通过。
 */
// probe.mjs 会把整段包进 (function(){ ... })()，所以这里直接 return。
return (function () {
  var out = { bad: [], ok: [], counts: {} };

  function box(sel, label) {
    var el = document.querySelector(sel);
    if (!el) { out.bad.push(label + ' 找不到 ' + sel); return null; }
    var r = el.getBoundingClientRect();
    var cs = getComputedStyle(el);
    var info = { sel: sel, w: Math.round(r.width), h: Math.round(r.height), display: cs.display };
    if (r.width < 1 || r.height < 1) {
      out.bad.push(label + ' 盒子塌了 ' + sel + ' → ' +
        r.width.toFixed(1) + '×' + r.height.toFixed(1) + ' display=' + cs.display);
    } else {
      out.ok.push(label + ' ' + info.w + '×' + info.h);
    }
    return info;
  }

  // ---- 外壳 ----
  box('.app-shell', 'app-shell');
  box('.masthead', 'masthead');
  box('.tabs', 'tabs');
  box('#trail', 'trail');
  box('#screen-submit', 'screen-submit');

  // masthead 必须真 sticky，且 --masthead-h 已被 JS 量过
  var mh = getComputedStyle(document.querySelector('.masthead')).position;
  if (mh !== 'sticky') out.bad.push('masthead position=' + mh + '（应为 sticky）');
  var hv = getComputedStyle(document.documentElement).getPropertyValue('--masthead-h').trim();
  out.counts.mastheadH = hv;
  if (!hv || parseFloat(hv) < 40) out.bad.push('--masthead-h 异常：' + hv);

  // ---- 文件夹（本轮换成扁平夹，重点量夹面与夹舌）----
  var folders = document.querySelectorAll('.folder');
  out.counts.folders = folders.length;
  if (!folders.length) out.bad.push('.folder 一个都没渲染');
  for (var i = 0; i < folders.length; i++) {
    var r = folders[i].getBoundingClientRect();
    if (r.width < 1 || r.height < 1) {
      out.bad.push('.folder[' + i + '] 塌了 ' + r.width + '×' + r.height);
      break;
    }
    // 夹舌是 ::before，量不到盒子，但能确认它参与了渲染
    var cs = getComputedStyle(folders[i], '::before');
    if (cs.content === 'none') { out.bad.push('.folder[' + i + '] 夹舌 ::before 没生成'); break; }
    // 名称与份数必须是 block，否则 margin-top 会被吞
    var nm = folders[i].querySelector('.name');
    if (nm && getComputedStyle(nm).display === 'inline') {
      out.bad.push('.folder .name display:inline —— margin/width 会被静默丢弃');
      break;
    }
  }
  var onF = document.querySelectorAll('.folder.is-on').length;
  out.counts.folderActive = onF;
  if (onF !== 1) out.bad.push('选中夹应恰好 1 个，实为 ' + onF);

  // ---- 链路清单五态 ----
  out.counts.chainRows = document.querySelectorAll('.chain__item').length;
  if (!out.counts.chainRows) out.bad.push('批改链路清单没渲染（诚实度关键）');
  var dot = document.querySelector('.chain__dot');
  if (dot) {
    var dr = dot.getBoundingClientRect();
    if (dr.width < 1) out.bad.push('.chain__dot 塌了（inline 吞掉 width）');
  }

  // ---- 上传区 / 说明块 ----
  box('#dropzone', 'dropzone');
  out.counts.introNote = document.querySelectorAll('#intro-slot .note').length;

  // ---- 图标是否都 hydrate 过（应为 0 个未替换）----
  var dry = 0;
  document.querySelectorAll('[data-icon]').forEach(function (el) {
    if (!el.querySelector('svg')) dry++;
  });
  out.counts.iconsNotHydrated = dry;
  if (dry) out.bad.push(dry + ' 个 data-icon 没 hydrate 成 SVG');

  // ---- 横向溢出（窄屏最容易破的一项）----
  var de = document.documentElement;
  out.counts.scrollW = de.scrollWidth;
  out.counts.clientW = de.clientWidth;
  if (de.scrollWidth > de.clientWidth + 2) {
    out.bad.push('页面横向溢出 ' + (de.scrollWidth - de.clientWidth) + 'px');
    // 找出罪魁：超出右边界的元素
    var over = [];
    document.querySelectorAll('body *').forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.width > 0 && r.right > de.clientWidth + 2) {
        over.push((el.className || el.tagName) + ' right=' + Math.round(r.right));
      }
    });
    out.counts.overflowers = over.slice(0, 6);
  }

  return out;
})()
