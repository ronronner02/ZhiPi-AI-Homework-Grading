/**
 * 截图脚本：跑一遍「教师页建库 → 学生页整页批改」，停在结果页供截图。
 *
 * 与 ui_check_page.js 的区别：那个是断言式走查（返回 bad/ok 数组），
 * 这个只负责把页面推到可截图的状态，配合 probe.mjs 的 --shot 用。
 *
 *   node -e "const{readFileSync}=require('fs');const e=readFileSync('tools/ui_shot_page.js','utf8');
 *            require('child_process').spawnSync('node',['tools/probe.mjs','--eval',e,
 *            '--shot','/tmp/page.png','--url','http://127.0.0.1:8010/'],{stdio:'inherit'})"
 */
return (async function () {
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var $ = function (s) { return document.querySelector(s); };
  async function until(fn, ms) {
    var t0 = Date.now();
    while (Date.now() - t0 < ms) {
      var v; try { v = fn(); } catch (e) { v = false; }
      if (v) return v;
      await sleep(300);
    }
    return null;
  }
  async function pickFile(sel, url, name) {
    var blob = await (await fetch(url)).blob();
    var dt = new DataTransfer();
    dt.items.add(new File([blob], name, { type: blob.type || 'image/png' }));
    var input = $(sel);
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  if ($('#enter-btn') && getComputedStyle($('#app')).display === 'none') {
    $('#enter-btn').click();
    await sleep(700);
  }
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
  if (!teacherUrl) throw new Error('没有内置教师答案页');
  if (!studentUrl) throw new Error('没有内置学生作业页');
  await until(function () {
    var s = $('#teacher-status');
    return s && (s.querySelector('.note--green') || s.querySelector('.note--red'));
  }, 180000);

  await pickFile('#file-input', (imgs[1] || imgs[0]).url, '学生页.png');
  await until(function () { return $('#stage-grade-btn'); }, 8000);
  $('#stage-grade-btn').click();
  await until(function () {
    var r = $('#recog-result');
    return r && getComputedStyle(r).display !== 'none';
  }, 180000);

  var btn = $('#paper-grade-btn');
  if (btn && getComputedStyle(btn).display !== 'none') btn.click();
  await until(function () {
    var b = $('#result-body');
    return b && b.querySelector('.marked__img');
  }, 300000);
  var img = $('.marked__img');
  if (img) await until(function () { return img.complete && img.naturalWidth > 0; }, 20000);
  await sleep(900);   // 让入场动画走完，免得截到半透明的中间态
  window.scrollTo(0, 0);
  return { ready: true, title: (document.querySelector('#result-body h3') || {}).textContent };
})();
