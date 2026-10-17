/* 全局工具：fetch 封装、表格渲染（排序/搜索/分页）、格式化 */
(function () {
  'use strict';

  var EP = window.EP = window.EP || {};

  /* ── fetch 封装 ── */
  EP.api = function (url, opts) {
    opts = opts || {};
    var o = { headers: { 'Content-Type': 'application/json' } };
    if (opts.method) o.method = opts.method;
    if (opts.body !== undefined) o.body = JSON.stringify(opts.body);
    return fetch(url, o).then(function (r) {
      return r.json().then(function (d) {
        if (!r.ok) {
          var msg = (d && (d.detail || d.msg || d.error)) || ('HTTP ' + r.status);
          var err = new Error(msg); err.status = r.status; err.data = d;
          throw err;
        }
        return d;
      });
    });
  };

  /* ── 格式化 ── */
  EP.fmt = {
    num: function (v, d) {
      if (v === null || v === undefined || v === '' || isNaN(v)) return '—';
      d = d === undefined ? 2 : d;
      return Number(v).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
    },
    pct: function (v, d) {
      if (v === null || v === undefined || v === '' || isNaN(v)) return '—';
      d = d === undefined ? 2 : d;
      return Number(v).toFixed(d) + '%';
    },
    signedPct: function (v, d) {
      if (v === null || v === undefined || v === '' || isNaN(v)) return '—';
      d = d === undefined ? 2 : d;
      var n = Number(v), cls = n > 0 ? 'up' : (n < 0 ? 'down' : '');
      return '<span class="' + cls + '">' + (n > 0 ? '+' : '') + n.toFixed(d) + '%</span>';
    },
    ts: function (v) { return v ? String(v).replace('T', ' ').slice(0, 19) : '—'; },
    esc: function (s) {
      return String(s === null || s === undefined ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
  };

  /* ── 指标卡 ── */
  EP.kvCards = function (pairs) {
    return '<div class="kv-cards">' + pairs.map(function (p) {
      return '<div class="kv"><div class="k">' + EP.fmt.esc(p[0]) + '</div>' +
        '<div class="v">' + (p[2] === undefined ? EP.fmt.esc(p[1]) : p[2]()) + '</div></div>';
    }).join('') + '</div>';
  };

  /* ── 徽章 ── */
  EP.badge = function (text, color) {
    return '<span class="badge ' + (color || 'gray') + '">' + EP.fmt.esc(text) + '</span>';
  };

  /* ── 表格：客户端排序 + 搜索 + 分页 ──
     cols: [{key, label, fmt?, num?, html?}]  fmt(v,row) → 显示值; html → 直接 HTML */
  EP.table = function (el, rows, cols, opts) {
    opts = opts || {};
    var pageSize = opts.pageSize || 50;
    var page = 0, sortKey = opts.sortKey || null, sortDir = opts.sortDir || -1;
    var kw = '';

    function val(r, c) { return r[c.key]; }
    function sorted() {
      var rs = rows.slice();
      if (kw) {
        rs = rs.filter(function (r) {
          return cols.some(function (c) { return String(val(r, c) == null ? '' : val(r, c)).toLowerCase().indexOf(kw) >= 0; });
        });
      }
      if (sortKey) {
        var c = cols.filter(function (x) { return x.key === sortKey; })[0];
        rs.sort(function (a, b) {
          var x = val(a, c), y = val(b, c);
          if (x == null) return 1; if (y == null) return -1;
          if (typeof x === 'number' && typeof y === 'number') return (x - y) * sortDir;
          return String(x).localeCompare(String(y), 'zh') * sortDir;
        });
      }
      return rs;
    }
    function render() {
      var rs = sorted();
      var pages = Math.max(1, Math.ceil(rs.length / pageSize));
      page = Math.min(page, pages - 1);
      var slice = rs.slice(page * pageSize, (page + 1) * pageSize);
      var html = '<div class="table-bar">';
      if (opts.search !== false) {
        html += '<input class="input" style="min-width:200px" placeholder="搜索…" value="' + EP.fmt.esc(kw) +
          '" onkeyup="EP._tblSearch(this,\'' + el.id + '\')">';
      }
      html += '<span class="count">共 ' + rs.length + ' 行</span></div>';
      html += '<div class="table-wrap" style="max-height:' + (opts.height || 460) + 'px;overflow:auto"><table class="grid"><thead><tr>';
      cols.forEach(function (c) {
        var arrow = sortKey === c.key ? (sortDir > 0 ? '▲' : '▼') : '⇅';
        html += '<th class="' + (c.num ? 'r' : '') + '" onclick="EP._tblSort(\'' + el.id + '\',\'' + c.key + '\')">' +
          EP.fmt.esc(c.label) + ' <span class="arrow">' + arrow + '</span></th>';
      });
      html += '</tr></thead><tbody>';
      if (!slice.length) {
        html += '<tr><td colspan="' + cols.length + '" class="empty">暂无数据</td></tr>';
      }
      slice.forEach(function (r) {
        html += '<tr>';
        cols.forEach(function (c) {
          var v = val(r, c), disp;
          if (c.html) disp = c.html(v, r);
          else if (c.fmt) disp = c.fmt(v, r);
          else disp = EP.fmt.esc(v == null ? '—' : v);
          html += '<td class="' + (c.num ? 'r' : '') + '">' + disp + '</td>';
        });
        html += '</tr>';
      });
      html += '</tbody></table></div>';
      if (pages > 1) {
        html += '<div class="pager"><button class="btn sm" onclick="EP._tblPage(\'' + el.id + '\',0)"' +
          (page === 0 ? ' disabled' : '') + '>« 首页</button>' +
          '<button class="btn sm" onclick="EP._tblPage(\'' + el.id + '\',' + (page - 1) + ')"' +
          (page === 0 ? ' disabled' : '') + '>‹ 上一页</button>' +
          '<span>第 ' + (page + 1) + ' / ' + pages + ' 页</span>' +
          '<button class="btn sm" onclick="EP._tblPage(\'' + el.id + '\',' + (page + 1) + ')"' +
          (page >= pages - 1 ? ' disabled' : '') + '>下一页 ›</button>' +
          '<button class="btn sm" onclick="EP._tblPage(\'' + el.id + '\',' + (pages - 1) + ')"' +
          (page >= pages - 1 ? ' disabled' : '') + '>末页 »</button></div>';
      }
      el.innerHTML = html;
    }
    var tables = EP._tables = EP._tables || {};
    tables[el.id] = {
      rows: rows, cols: cols, opts: opts, render: render,
      setRows: function (rs) { rows = rs; page = 0; render(); },
      search: function (v) { kw = v; page = 0; render(); },
      sort: function (k) {
        if (sortKey === k) sortDir *= -1; else { sortKey = k; sortDir = -1; }
        render();
      },
      page: function (p) { page = p; render(); }
    };
    render();
    return tables[el.id];
  };

  EP._tblSort = function (id, k) { var t = EP._tables[id]; if (t) t.sort(k); };
  EP._tblSearch = function (el, id) { var t = EP._tables[id]; if (t) t.search(el.value); };
  EP._tblPage = function (id, p) { var t = EP._tables[id]; if (t) t.page(p); };

  /* ── 任务面板（轮询） ── */
  EP.jobPanel = function (el, jobsUrl, names, labels) {
    function load() {
      return EP.api(jobsUrl).then(function (d) {
        var html = '';
        names.forEach(function (n, i) {
          var j = d[n] || {}, st = j.state || 'idle';
          var label = labels && labels[i] ? labels[i] : n;
          var cls = st === 'running' ? 'running' : (st === 'done' ? 'done' : (st === 'error' ? 'error' : 'idle'));
          var txt = { running: '🟡 运行中…', done: '🟢 已完成', error: '🔴 失败', idle: '⚪ 未启动' }[st] || st;
          html += '<div class="job ' + cls + '"><b>' + EP.fmt.esc(label) + '</b>：' + txt +
            (j.started_at ? '（' + EP.fmt.ts(j.started_at) + ' 起）' : '') +
            (j.error ? '<div style="color:var(--c-danger);margin-top:4px">' + EP.fmt.esc(j.error) + '</div>' : '') +
            (j.returncode ? ' <span class="badge gray">退出码 ' + j.returncode + '</span>' : '') + '</div>';
        });
        el.innerHTML = html;
        return st === 'running';
      }).catch(function () { return false; });
    }
    function loop() {
      load().then(function (running) {
        if (running) setTimeout(loop, 3000);
      });
    }
    loop();
  };

  /* ── 日志面板 ── */
  EP.logPanel = function (el, logUrl, job) {
    function load() {
      return EP.api(logUrl + '?job=' + job).then(function (d) {
        el.innerHTML = d.log_tail
          ? '<div class="job done"><div class="log">' + EP.fmt.esc(d.log_tail) + '</div></div>'
          : '<div class="job idle">暂无日志</div>';
      });
    }
    load();
    return load;
  };

  /* ── toast ── */
  EP.toast = function (msg, type) {
    var t = document.createElement('div');
    t.className = 'job ' + (type === 'error' ? 'error' : 'done');
    t.style.cssText = 'position:fixed;top:64px;right:20px;z-index:999;min-width:240px;box-shadow:var(--sh-lg)';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 3500);
  };
})();
