/* 应用入口：hash 路由 + 页面切换 + 系统信息 */
(function () {
  'use strict';
  var EP = window.EP;

  var PAGES = { experts: '社区高手', strategy: '高手跟随策略' };
  var loaded = {};

  EP.go = function (name) {
    if (!PAGES[name]) name = 'experts';
    window.location.hash = '#/' + name;
    show(name);
  };

  function show(name) {
    Object.keys(PAGES).forEach(function (p) {
      document.getElementById('page-' + p).classList.toggle('active', p === name);
      var nav = document.getElementById('nav-' + p);
      if (nav) nav.classList.toggle('active', p === name);
    });
    if (!loaded[name]) {
      var root = document.getElementById(name + '-root');
      if (name === 'experts') EP.loadExperts(root);
      else EP.loadStrategy(root);
      loaded[name] = true;
    } else if (name === 'strategy') {
      // 策略页每次进入都刷新（看板数据/运行状态是实时的）
      EP.loadStrategy(document.getElementById('strategy-root'));
    }
  }

  function currentPage() {
    var h = (window.location.hash || '').replace('#/', '');
    return PAGES[h] ? h : 'experts';
  }

  window.addEventListener('hashchange', function () { show(currentPage()); });

  /* 系统信息（header 右侧） */
  function loadSysInfo() {
    EP.api('/api/system/info').then(function (d) {
      document.getElementById('sysinfo').textContent =
        '高手 ' + (d.experts || 0) + ' · 成交 ' + (d.trades || 0) +
        ' · 买入 ' + (d.expert_buys || 0) + ' · 账户 ' + (d.accounts || 0) +
        ' · 回补 ' + (d.bulk_files || 0) + ' 组合';
    }).catch(function () {});
  }

  document.addEventListener('DOMContentLoaded', function () {
    show(currentPage());
    loadSysInfo();
    setInterval(loadSysInfo, 30000);
  });
})();
