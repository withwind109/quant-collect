/* 社区高手页（#/experts）—— 对应原 Streamlit views/experts.py 的功能面 */
(function () {
  'use strict';
  var EP = window.EP;
  var S = { date: '', sources: ['eastmoney', 'tonghuashun'], wl: [], cache: {} };

  var SRC_LABELS = { eastmoney: '东财', tonghuashun: '同花顺', xueqiu: '雪球', alipay: '支付宝' };
  var PAGE = 50;

  /* ── ① 爬取面板 ── */
  function crawlPanel() {
    var chips = ['eastmoney', 'tonghuashun', 'xueqiu', 'alipay'].map(function (s) {
      var on = S.sources.indexOf(s) >= 0;
      return '<span class="chip' + (on ? ' on' : '') + '" data-src="' + s + '">' + SRC_LABELS[s] + '</span>';
    }).join('');
    return '<div class="card"><h3>① 触发爬取</h3>' +
      '<div class="card-sub">东财使用 Cookie 认证（.env 的 EASTMONEY_COOKIE，不再扫码登录）；数据源可多选，未来新增渠道只需在 sources/ 下加一个源。请求经无头浏览器通道发出（模拟真实用户会话）。</div>' +
      '<div class="row">' +
      '<div class="field"><label>数据源</label><div id="ec-src-chips">' + chips + '</div></div>' +
      '<div class="field"><label>交易日（空=自动推断）</label><input class="input" id="ec-date" placeholder="YYYY-MM-DD" style="min-width:130px"></div>' +
      '<div class="field"><label>每榜数量</label><input class="input num" id="ec-top" value="50" style="min-width:90px"></div>' +
      '<div class="field"><label>&nbsp;</label><button class="btn primary" id="ec-run">▶ 开始爬取</button></div>' +
      '</div>' +
      '<div id="ec-jobs"></div><div id="ec-log"></div>' +
      '<div class="hint">产出目录：data/crawl/&lt;日期&gt;/（JSON + SQLite 双写）</div></div>';
  }

  function bindCrawl(root) {
    root.querySelectorAll('#ec-src-chips .chip').forEach(function (c) {
      c.onclick = function () {
        var s = c.dataset.src, i = S.sources.indexOf(s);
        if (i >= 0) S.sources.splice(i, 1); else S.sources.push(s);
        c.classList.toggle('on');
      };
    });
    root.querySelector('#ec-run').onclick = function () {
      if (!S.sources.length) { EP.toast('至少选一个数据源', 'error'); return; }
      var btn = this; btn.disabled = true;
      EP.api('/api/expert-crawl/run', {
        method: 'POST',
        body: { sources: S.sources, top: parseInt(root.querySelector('#ec-top').value, 10) || 50,
                date: root.querySelector('#ec-date').value.trim() }
      }).then(function () {
        EP.toast('爬取已启动');
        bindJobs(root);
        var logLoad = EP.logPanel(root.querySelector('#ec-log'), '/api/expert-crawl/log', 'crawl');
        var timer = setInterval(function () {
          EP.api('/api/expert-crawl/status').then(function (d) {
            if (d.crawl && d.crawl.state !== 'running') { clearInterval(timer); logLoad(); refreshData(root); }
          });
        }, 3000);
      }).catch(function (e) { EP.toast(e.message, 'error'); btn.disabled = false; });
    };
  }

  function bindJobs(root) {
    EP.jobPanel(root.querySelector('#ec-jobs'), '/api/expert-crawl/status',
      ['crawl', 'history', 'bulk_history'], ['爬取', '单组合回补', '批量回补']);
  }

  /* ── ② 历史回补 ── */
  function historyPanel() {
    return '<div class="card"><h3>② 高手调仓历史回补</h3>' +
      '<div class="card-sub">跟随策略回测的数据基础（data/history_bulk/&lt;组合号&gt;.json）。单组合回补立即执行；批量回补在后台跑（自动走轻量后端，不挤浏览器通道）。</div>' +
      '<div class="row">' +
      '<div class="field"><label>模拟盘组合号（1000… 长号）</label><input class="input" id="eh-zjzh" placeholder="1000…" style="min-width:220px"></div>' +
      '<div class="field"><label>起始日（空=全部）</label><input class="input" id="eh-since" placeholder="YYYY-MM-DD" style="min-width:130px"></div>' +
      '<div class="field"><label>&nbsp;</label><button class="btn" id="eh-run">回补</button>' +
      '<button class="btn" id="eh-view" style="margin-left:6px">查看已回补记录</button></div>' +
      '</div><div id="eh-result"></div>' +
      '<div class="row" style="margin-top:10px">' +
      '<div class="field"><label>批量回补：只跑前 N 个（0=全部）</label><input class="input num" id="eh-limit" value="20" style="min-width:90px"></div>' +
      '<div class="field"><label>&nbsp;</label><button class="btn" id="eh-bulk">批量回补</button></div>' +
      '<span class="hint" id="eh-bulk-count"></span></div></div>';
  }

  function bindHistory(root) {
    root.querySelector('#eh-run').onclick = function () {
      var z = root.querySelector('#eh-zjzh').value.trim();
      if (!z) { EP.toast('请输入组合号', 'error'); return; }
      EP.api('/api/expert-crawl/history', { method: 'POST',
        body: { zjzh: z, since: root.querySelector('#eh-since').value.trim() } })
        .then(function () { EP.toast('回补已启动'); bindJobs(root); })
        .catch(function (e) { EP.toast(e.message, 'error'); });
    };
    root.querySelector('#eh-view').onclick = function () {
      var z = root.querySelector('#eh-zjzh').value.trim();
      if (!z) { EP.toast('请输入组合号', 'error'); return; }
      var box = root.querySelector('#eh-result');
      box.innerHTML = '<div class="hint">加载中…</div>';
      EP.api('/api/expert-crawl/history-file?zjzh=' + encodeURIComponent(z)).then(function (d) {
        if (!d.exists || !d.records.length) { box.innerHTML = '<div class="hint">（尚未回补或无记录）</div>'; return; }
        box.innerHTML = '<div class="hint">' + EP.fmt.esc(d.name || z) + ' · ' + d.n + ' 条 · 回补于 ' +
          EP.fmt.ts(d.fetched_at) + '</div><div id="eh-table"></div>';
        EP.table(box.querySelector('#eh-table'), d.records, [
          { key: 'trade_date', label: '日期' },
          { key: 'trade_time', label: '时间' },
          { key: 'stock_code', label: '股票' },
          { key: 'stock_name', label: '股票名' },
          { key: 'action', label: '方向', fmt: function (v) {
            return v === 'buy' ? '<span class="up">买入</span>' : (v === 'sell' ? '<span class="down">卖出</span>' : EP.fmt.esc(v)); } },
          { key: 'price', label: '价格', num: true, fmt: function (v) { return EP.fmt.num(v); } },
          { key: 'weight_before', label: '持仓前%', num: true, fmt: function (v) { return EP.fmt.num(v); } },
          { key: 'weight_after', label: '持仓后%', num: true, fmt: function (v) { return EP.fmt.num(v); } }
        ], { height: 300 });
      }).catch(function (e) { box.innerHTML = '<div class="hint">' + EP.fmt.esc(e.message) + '</div>'; });
    };
    root.querySelector('#eh-bulk').onclick = function () {
      EP.api('/api/expert-crawl/bulk-history', { method: 'POST',
        body: { limit: parseInt(root.querySelector('#eh-limit').value, 10) || 0 } })
        .then(function () { EP.toast('批量回补已启动'); bindJobs(root); })
        .catch(function (e) { EP.toast(e.message, 'error'); });
    };
    EP.api('/api/system/info').then(function (d) {
      root.querySelector('#eh-bulk-count').textContent = '当前已有 ' + d.bulk_files + ' 个组合文件';
    });
  }

  /* ── ③ 关注管理 ── */
  function watchPanel() {
    return '<div class="card"><h3>③ 关注高手</h3>' +
      '<div class="card-sub">关注名单是 v1 镜像策略（universe.source=watchlist）与 v2（include_watchlist）的跟随来源。</div>' +
      '<div class="row">' +
      '<div class="field"><label>高手ID（组合号）</label><input class="input" id="ew-id" placeholder="1000…" style="min-width:220px"></div>' +
      '<div class="field"><label>备注名（可选）</label><input class="input" id="ew-name" style="min-width:160px"></div>' +
      '<div class="field"><label>&nbsp;</label><button class="btn primary" id="ew-add">＋ 关注</button></div>' +
      '</div><div id="ew-table"></div></div>';
  }

  function watchTable(root) {
    EP.api('/api/expert-crawl/watchlist').then(function (d) {
      S.wl = d.rows || [];
      var el = root.querySelector('#ew-table');
      if (!S.wl.length) { el.innerHTML = '<div class="hint">关注列表为空（模拟盘）</div>'; return; }
      EP.table(el, S.wl, [
        { key: 'expert_id', label: '高手ID' },
        { key: 'name', label: '名称' },
        { key: 'created_at', label: '加入时间', fmt: function (v) { return EP.fmt.ts(v); } },
        { key: 'expert_id', label: '操作', html: function (v) {
          return '<button class="btn sm danger" data-del="' + EP.fmt.esc(v) + '">取消关注</button>'; } }
      ], { search: false, height: 220, pageSize: 100 });
      el.querySelectorAll('[data-del]').forEach(function (b) {
        b.onclick = function () {
          EP.api('/api/expert-crawl/watchlist/' + encodeURIComponent(b.dataset.del), { method: 'DELETE' })
            .then(function () { EP.toast('已取消关注'); watchTable(root); })
            .catch(function (e) { EP.toast(e.message, 'error'); });
        };
      });
    });
  }

  function bindWatch(root) {
    root.querySelector('#ew-add').onclick = function () {
      var id = root.querySelector('#ew-id').value.trim();
      if (!id) { EP.toast('请输入高手ID', 'error'); return; }
      EP.api('/api/expert-crawl/watchlist', { method: 'POST',
        body: { expert_id: id, name: root.querySelector('#ew-name').value.trim() } })
        .then(function () { EP.toast('已关注'); watchTable(root); })
        .catch(function (e) { EP.toast(e.message, 'error'); });
    };
    watchTable(root);
  }

  /* ── ④ 按日期浏览 ── */
  var SUB = ['高手买入（主视图）', '高手榜', '成交明细', '个股聚合', '热度榜', '信号'];

  function dataPanel() {
    var subs = SUB.map(function (t, i) {
      return '<a data-sub="' + i + '" class="' + (i === 0 ? 'active' : '') + '">' + t + '</a>';
    }).join('');
    return '<div class="card"><h3>④ 按日期浏览</h3>' +
      '<div class="row"><div class="field"><label>交易日</label><select class="select" id="ed-date" style="min-width:140px"></select></div>' +
      '<span class="hint" id="ed-info"></span></div>' +
      '<div id="ed-summary"></div><div id="ed-coverage"></div>' +
      '<div class="subtabs">' + subs + '</div><div id="ed-table"></div></div>';
  }

  function flattenBuys(ebs) {
    var rows = [];
    (ebs || []).forEach(function (e) {
      (e.stocks || []).forEach(function (s) {
        rows.push({
          expert_name: e.expert_name || e.expert_id, expert_id: e.expert_id,
          owner: e.owner || '', source: e.source,
          source_label: SRC_LABELS[e.source] || e.source || '—',
          market_type: e.market_type, is_live: e.market_type === 'live',
          return_rate: e.return_rate, day_return_rate: e.day_return_rate, followers: e.followers,
          stock_code: s.stock_code, stock_name: s.stock_name || '',
          action: s.action, price: s.price,
          weight_before: s.weight_before, weight_after: s.weight_after,
          trade_time: (s.trade_time || '').slice(-8)
        });
      });
    });
    return rows;
  }

  var BUY_COLS = [
    { key: 'expert_name', label: '高手' },
    { key: 'source_label', label: '来源' },
    { key: 'is_live', label: '类型', fmt: function (v) { return v ? EP.badge('实盘', 'red') : EP.badge('模拟', 'gray'); } },
    { key: 'return_rate', label: '收益率%', num: true, fmt: function (v) { return EP.fmt.signedPct(v); } },
    { key: 'day_return_rate', label: '今日%', num: true, fmt: function (v) { return EP.fmt.signedPct(v); } },
    { key: 'followers', label: '粉丝', num: true, fmt: function (v) { return EP.fmt.num(v, 0); } },
    { key: 'stock_code', label: '股票' },
    { key: 'stock_name', label: '股票名' },
    { key: 'action', label: '方向', fmt: function (v) {
      return v === 'buy' ? '<span class="up">买入</span>' : (v === 'sell' ? '<span class="down">卖出</span>' : EP.fmt.esc(v)); } },
    { key: 'price', label: '价格', num: true, fmt: function (v) { return EP.fmt.num(v); } },
    { key: 'weight_before', label: '持仓前%', num: true, fmt: function (v) { return EP.fmt.num(v); } },
    { key: 'weight_after', label: '持仓后%', num: true, fmt: function (v) { return EP.fmt.num(v); } },
    { key: 'trade_time', label: '时间' }
  ];

  function renderSub(root, sub, d) {
    var el = root.querySelector('#ed-table');
    if (sub === 0) {
      var rows = flattenBuys(d.expert_buys);
      var srcs = {}, srcList = [];
      rows.forEach(function (r) { if (!srcs[r.source_label]) { srcs[r.source_label] = 1; srcList.push(r.source_label); } });
      var chips = srcList.map(function (s) { return '<span class="chip on" data-fs="' + EP.fmt.esc(s) + '">' + EP.fmt.esc(s) + '</span>'; }).join('');
      el.innerHTML = '<div class="table-bar"><div>' + chips +
        '<span class="chip" data-fs="__live">只看实盘</span></div><span class="count" id="ed-count"></span></div><div id="ed-buys"></div>';
      var t = EP.table(el.querySelector('#ed-buys'), rows, BUY_COLS, { height: 460, pageSize: PAGE });
      var off = {}, liveOnly = false;
      el.querySelectorAll('.chip[data-fs]').forEach(function (c) {
        c.onclick = function () {
          var f = c.dataset.fs;
          if (f === '__live') { liveOnly = !liveOnly; c.classList.toggle('on', liveOnly); }
          else { off[f] = !off[f]; c.classList.toggle('on', !off[f]); }
          t.setRows(rows.filter(function (r) {
            return (!liveOnly || r.is_live) && !off[r.source_label];
          }));
        };
      });
    } else if (sub === 1) {
      EP.table(el, d.experts || [], [
        { key: 'name', label: '高手', fmt: function (v, r) { return EP.fmt.esc(v || r.expert_id); } },
        { key: 'expert_id', label: '高手ID' },
        { key: 'source', label: '来源', fmt: function (v) { return EP.fmt.esc(SRC_LABELS[v] || v || '—'); } },
        { key: 'market_type', label: '类型', fmt: function (v) { return v === 'live' ? EP.badge('实盘', 'red') : EP.badge('模拟', 'gray'); } },
        { key: 'rank_label', label: '榜单' },
        { key: 'owner', label: 'owner' },
        { key: 'return_rate', label: '收益率%', num: true, fmt: function (v) { return EP.fmt.signedPct(v); } },
        { key: 'total_return', label: '总收益%', num: true, fmt: function (v) { return EP.fmt.signedPct(v); } },
        { key: 'followers', label: '粉丝', num: true, fmt: function (v) { return EP.fmt.num(v, 0); } }
      ], { height: 460, pageSize: PAGE });
    } else if (sub === 2) {
      EP.table(el, d.trades || [], [
        { key: 'expert_name', label: '高手', fmt: function (v, r) { return EP.fmt.esc(v || r.expert_id); } },
        { key: 'source', label: '来源', fmt: function (v) { return EP.fmt.esc(SRC_LABELS[v] || v || '—'); } },
        { key: 'stock_code', label: '股票' },
        { key: 'stock_name', label: '股票名' },
        { key: 'action', label: '方向', fmt: function (v) {
          return v === 'buy' ? '<span class="up">买入</span>' : (v === 'sell' ? '<span class="down">卖出</span>' : EP.fmt.esc(v)); } },
        { key: 'price', label: '价格', num: true, fmt: function (v) { return EP.fmt.num(v); } },
        { key: 'volume', label: '成交量', num: true, fmt: function (v) { return EP.fmt.num(v, 0); } },
        { key: 'amount', label: '成交额', num: true, fmt: function (v) { return EP.fmt.num(v, 0); } },
        { key: 'trade_time', label: '时间', fmt: function (v) { return EP.fmt.esc((v || '').slice(-8)); } }
      ], { height: 460, pageSize: PAGE });
    } else if (sub === 3) {
      EP.table(el, d.buy_aggregated || [], [
        { key: 'stock_code', label: '股票' },
        { key: 'stock_name', label: '股票名' },
        { key: 'buy_experts', label: '买入高手数', num: true, fmt: function (v) { return EP.fmt.num(v, 0); } },
        { key: 'buy_records', label: '买入笔数', num: true, fmt: function (v) { return EP.fmt.num(v, 0); } },
        { key: 'avg_price', label: '均价', num: true, fmt: function (v) { return EP.fmt.num(v); } },
        { key: 'net_amount', label: '净买入额', num: true, fmt: function (v) { return EP.fmt.num(v, 0); } },
        { key: 'note', label: '备注' }
      ], { height: 460, pageSize: PAGE });
    } else if (sub === 4) {
      EP.table(el, d.stock_rank || [], [
        { key: 'stock_code', label: '股票' },
        { key: 'stock_name', label: '股票名' },
        { key: 'buy_experts', label: '买入高手数', num: true, fmt: function (v) { return EP.fmt.num(v, 0); } },
        { key: 'buy_records', label: '买入笔数', num: true, fmt: function (v) { return EP.fmt.num(v, 0); } },
        { key: 'avg_price', label: '均价', num: true, fmt: function (v) { return EP.fmt.num(v); } },
        { key: 'net_amount', label: '净买入额', num: true, fmt: function (v) { return EP.fmt.num(v, 0); } }
      ], { height: 460, pageSize: PAGE });
    } else {
      EP.table(el, d.signals || [], [
        { key: 'kind', label: '类型' },
        { key: 'expert_name', label: '高手', fmt: function (v, r) { return EP.fmt.esc(v || r.expert_id || ''); } },
        { key: 'action', label: '方向', fmt: function (v) {
          return v === 'buy' ? '<span class="up">买入</span>' : (v === 'sell' ? '<span class="down">卖出</span>' : EP.fmt.esc(v)); } },
        { key: 'stock_code', label: '股票' },
        { key: 'stock_name', label: '股票名' },
        { key: 'trade_time', label: '时间', fmt: function (v) { return EP.fmt.esc((v || '').slice(-8)); } },
        { key: 'note', label: '备注' }
      ], { height: 460, pageSize: PAGE });
    }
  }

  function loadDay(root, date, force) {
    if (!force && S.cache[date]) { renderDay(root, S.cache[date]); return; }
    EP.api('/api/expert-crawl/data' + (date ? '?date=' + date : '')).then(function (d) {
      if (date) S.cache[date] = d;   // 当日数据缓存：切子 tab 不再重复拉全量
      renderDay(root, d);
    }).catch(function (e) { EP.toast(e.message, 'error'); });
  }

  function renderDay(root, d) {
      if (!d.date) {
        root.querySelector('#ed-summary').innerHTML = '<div class="empty">' + EP.fmt.esc(d.msg || '暂无数据') + '</div>';
        root.querySelector('#ed-table').innerHTML = '';
        return;
      }
      S.date = d.date;
      var s = d.summary;
      root.querySelector('#ed-info').textContent = '共 ' + d.dates_total + ' 个交易日有数据；最新 ' + d.date;
      root.querySelector('#ed-summary').innerHTML = EP.kvCards([
        ['高手买入', s.expert_buys], ['成交明细', s.trades], ['实盘高手', s.live_experts],
        ['个股聚合', s.stocks], ['覆盖度', s.coverage + ' 项'], ['日期模式', s.date_mode],
        ['爬取时间', s.crawled_at]
      ]);
      var cov = d.coverage || [];
      root.querySelector('#ed-coverage').innerHTML = cov.length
        ? '<details><summary class="hint" style="cursor:pointer">覆盖度明细（' + cov.length + ' 项）</summary><div id="ed-cov-table"></div></details>' : '';
      if (cov.length) {
        EP.table(root.querySelector('#ed-cov-table'), cov,
          Object.keys(cov[0]).slice(0, 8).map(function (k) {
            return { key: k, label: k, num: typeof cov[0][k] === 'number' };
          }), { height: 200, pageSize: 100 });
      }
      var active = root.querySelector('.subtabs a.active');
      renderSub(root, active ? parseInt(active.dataset.sub, 10) : 0, d);
  }

  function bindData(root) {
    EP.api('/api/expert-crawl/dates').then(function (d) {
      var sel = root.querySelector('#ed-date');
      if (!d.dates.length) {
        sel.innerHTML = '<option>（无数据）</option>';
        root.querySelector('#ed-summary').innerHTML = '<div class="empty">还没有数据 —— 先在上面触发一次爬取</div>';
        return;
      }
      sel.innerHTML = d.dates.map(function (x) { return '<option>' + x + '</option>'; }).join('');
      sel.onchange = function () { loadDay(root, sel.value); };
      loadDay(root, d.dates[0]);
    });
    root.querySelectorAll('.subtabs a').forEach(function (a) {
      a.onclick = function () {
        root.querySelectorAll('.subtabs a').forEach(function (x) { x.classList.remove('active'); });
        a.classList.add('active');
        var d = S.cache[S.date];   // 用当日缓存渲染，不重复请求
        if (d && d.date) renderSub(root, parseInt(a.dataset.sub, 10), d);
      };
    });
  }

  function refreshData(root) {
    S.cache = {};   // 爬取产出新数据，清缓存强制重拉
    EP.api('/api/expert-crawl/dates').then(function (d) {
      if (!d.dates.length) return;
      var sel = root.querySelector('#ed-date');
      sel.innerHTML = d.dates.map(function (x) { return '<option>' + x + '</option>'; }).join('');
      loadDay(root, d.dates[0], true);
    });
    EP.api('/api/system/info').then(function (d) {
      root.querySelector('#eh-bulk-count').textContent = '当前已有 ' + d.bulk_files + ' 个组合文件';
    });
  }

  EP.loadExperts = function (root) {
    root.innerHTML = crawlPanel() + historyPanel() + watchPanel() + dataPanel();
    bindCrawl(root); bindJobs(root);
    EP.logPanel(root.querySelector('#ec-log'), '/api/expert-crawl/log', 'crawl');
    bindHistory(root); bindWatch(root); bindData(root);
  };
})();
