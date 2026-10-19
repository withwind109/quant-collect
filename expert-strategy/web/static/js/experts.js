/* 高手评估页（#/experts）—— 2026-10-19 重构：分渠道数据爬取 + 高手能力评估。
   替代原「按日期浏览」视图（高手买入/高手榜/成交明细/个股聚合/热度榜/信号 6 个子 tab 已删除）。
   能力评估口径见 src/strategy/expert_eval.py（与 follow_backtest 同一套交易成本）。 */
(function () {
  'use strict';
  var EP = window.EP;
  var S = {
    sources: ['eastmoney', 'tonghuashun'],
    env: 'sim',
    minSells: 3,
    sampleOnly: true,
    eval: null,        // 最新一次评估结果（/evaluation）
    wl: {},            // expert_id -> name（关注列表）
    chart: null        // 明细抽屉的 ECharts 实例
  };
  var SRC_LABELS = { eastmoney: '东财', tonghuashun: '同花顺', xueqiu: '雪球', alipay: '支付宝' };
  var LOG_JOBS = [['crawl', '爬取'], ['bulk_history', '批量回补'], ['expert_eval', '能力评估'], ['history', '单组合回补']];

  /* ═══════════ ① 分渠道数据爬取 ═══════════ */

  function channelCards(ch) {
    var cred = ch.credential === 'none' ? EP.badge('未配置凭证', 'gray')
      : EP.badge(ch.credential === 'cookie' ? 'Cookie 已配置' : '端点已配置', 'green');
    return '<div class="card"><h3>' + EP.fmt.esc(ch.label) +
      ' <span class="hint" style="font-weight:400">' + EP.fmt.esc(ch.source) + '</span></h3>' +
      EP.kvCards([
        ['凭证', '', function () { return cred; }],
        ['最新交易日', ch.last_date || '—'],
        ['有数据天数', ch.days, function () { return EP.fmt.num(ch.days, 0); }],
        ['高手数', ch.experts, function () { return EP.fmt.num(ch.experts, 0); }],
        ['成交条数', ch.trades, function () { return EP.fmt.num(ch.trades, 0); }]
      ]) + '</div>';
  }

  function crawlPanel() {
    return '<div class="card"><h3>① 分渠道数据爬取</h3>' +
      '<div class="card-sub">按渠道抓取高手操作流水（请求经无头浏览器通道发出）。各渠道独立统计、独立开关，数据落本地 SQLite（data/experts.db）。' +
      '东财<b>实盘</b>明细需要登录态——请先到右上角<a href="#/accounts" onclick="EP.go(\'accounts\');return false" style="font-weight:600">🏦 账户中心</a>扫码登录。</div>' +
      '<div class="card-grid" id="ec-channels"></div>' +
      '<div class="row" style="margin-top:12px">' +
      '<div class="field"><label>数据源（多选）</label><div id="ec-src-chips">' +
      ['eastmoney', 'tonghuashun', 'xueqiu', 'alipay'].map(function (s) {
        var on = S.sources.indexOf(s) >= 0;
        return '<span class="chip' + (on ? ' on' : '') + '" data-src="' + s + '">' + SRC_LABELS[s] + '</span>';
      }).join('') + '</div></div>' +
      '<div class="field"><label>交易日（空=自动推断）</label><input class="input" id="ec-date" placeholder="YYYY-MM-DD" style="min-width:130px"></div>' +
      '<div class="field"><label>每榜数量</label><input class="input num" id="ec-top" value="50" style="min-width:90px"></div>' +
      '<div class="field"><label>&nbsp;</label><button class="btn primary" id="ec-run">▶ 开始爬取</button></div>' +
      '</div><div id="ec-jobs"></div><div id="ec-log"></div></div>';
  }

  function backfillPanel() {
    return '<div class="card"><h3>调仓历史回补 <span class="hint" style="font-weight:400">（能力评估的数据基础：单日流水只能看最近一天，回补才能算夏普/回撤）</span></h3>' +
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

  /* ── ② 关注列表（东财实盘，与东财账号关注关系双向同步）── */

  function watchPanel() {
    return '<div class="card"><h3>①½ 关注列表 <span class="hint" style="font-weight:400">（东财实盘 · rtV3 调仓明细仅对关注者开放）</span></h3>' +
      '<div class="card-sub">流程：爬榜单 → 在获取的高手里点「关注」→ 这里「拉取调仓明细」入库 → 实盘环境评估即可用。' +
      'live 关注会在<b>东财账号上代为关注</b>该组合（取消时同步取消）；也可以「同步东财关注」把 APP 里已关注的组合拉回来。</div>' +
      '<div class="row" style="margin-top:10px">' +
      '<button class="btn" id="ew-sync">↻ 同步东财关注</button>' +
      '<button class="btn primary" id="ew-fetch" style="margin-left:6px">▶ 拉取全部关注调仓</button>' +
      '<span class="hint" id="ew-meta" style="margin-left:8px"></span></div>' +
      '<div id="ew-table" style="margin-top:10px"></div>' +
      '<div id="ew-result" style="margin-top:10px"></div></div>';
  }

  function renderWatchTable(root) {
    var el = root.querySelector('#ew-table');
    if (!el) return;
    var live = Object.keys(S.wl).filter(function (id) { return S.wl[id] && S.wl[id].__mt === 'live'; });
    if (!live.length) {
      el.innerHTML = '<div class="hint">关注列表为空——在下方评估表/明细抽屉里点「＋ 关注」（实盘环境），或「同步东财关注」</div>';
      return;
    }
    var rows = live.map(function (id) {
      var w = S.wl[id];
      return { expert_id: id, name: w.name || id, note: w.note || '', created_at: w.created_at || '' };
    });
    EP.table(el, rows, [
      { key: 'name', label: '组合', fmt: function (v, r) { return '<b>' + EP.fmt.esc(v) + '</b>'; } },
      { key: 'expert_id', label: '组合号' },
      { key: 'note', label: '来源', fmt: function (v) { return EP.fmt.esc(v || '—'); } },
      { key: 'created_at', label: '关注时间', fmt: function (v) { return EP.fmt.ts(v); } },
      { key: 'expert_id', label: '操作', html: function (v) {
        return '<button class="btn sm" data-ew-fetch="' + EP.fmt.esc(v) + '">拉取调仓明细</button> ' +
          '<button class="btn sm" data-ew-unwatch="' + EP.fmt.esc(v) + '" style="color:var(--c-danger)">取消关注</button>'; } }
    ], { height: 260, pageSize: 20 });
  }

  function fetchLiveDetail(root, expertId) {
    var box = root.querySelector('#ew-result');
    box.innerHTML = '<div class="hint">拉取 rtV3 调仓明细中…（每个组合最近 40 条）</div>';
    EP.api('/api/expert-crawl/watchlist/fetch-detail', { method: 'POST',
      body: { expert_id: expertId || '' } }).then(function (d) {
      if (!d.rows || !d.rows.length) { box.innerHTML = '<div class="hint">没有拉到调仓记录（今日无调仓或组合异常）</div>'; return; }
      box.innerHTML = '<div class="hint">本次拉取 <b>' + d.total + '</b> 条（新入库 ' + d.n + ' 条，重复自动去重）</div><div id="ew-dtable"></div>';
      EP.table(box.querySelector('#ew-dtable'), d.rows, [
        { key: 'expert_name', label: '组合' },
        { key: 'trade_date', label: '日期' },
        { key: 'trade_time', label: '时间', fmt: function (v) { return EP.fmt.esc((v || '').slice(-8)); } },
        { key: 'stock_code', label: '代码' },
        { key: 'stock_name', label: '名称' },
        { key: 'action', label: '方向', fmt: function (v) {
          return v === 'buy' ? '<span class="up">买入</span>' : (v === 'sell' ? '<span class="down">卖出</span>' : EP.fmt.esc(v)); } },
        { key: 'price', label: '价格', num: true, fmt: function (v) { return EP.fmt.num(v); } },
        { key: 'weight_label', label: '仓位', fmt: function (v) { return EP.fmt.esc(v || '—'); } }
      ], { height: 300, pageSize: 50 });
    }).catch(function (e) { box.innerHTML = '<div class="hint">' + EP.fmt.esc(e.message) + '</div>'; });
  }

  function bindWatchCard(root) {
    root.querySelector('#ew-sync').onclick = function () {
      var btn = this; btn.disabled = true;
      EP.api('/api/expert-crawl/watchlist/sync', { method: 'POST' }).then(function (d) {
        EP.toast('东财已关注 ' + d.em_total + ' 个，本次新增 ' + d.added + ' 个');
        loadWatch(root);
      }).catch(function (e) { EP.toast(e.message, 'error'); })
        .finally(function () { btn.disabled = false; });
    };
    root.querySelector('#ew-fetch').onclick = function () {
      if (!Object.keys(S.wl).some(function (id) { return S.wl[id] && S.wl[id].__mt === 'live'; })) {
        EP.toast('关注列表里还没有实盘组合', 'error'); return;
      }
      fetchLiveDetail(root, '');
    };
    // 行内按钮用事件委托（表格重渲染后仍有效）
    root.querySelector('#ew-table').addEventListener('click', function (e) {
      var f = e.target.closest('[data-ew-fetch]'), u = e.target.closest('[data-ew-unwatch]');
      if (f) { fetchLiveDetail(root, f.dataset.ewFetch); }
      else if (u) { toggleWatch(u.dataset.ewUnwatch, root); }
    });
  }

  /* 东财账户中心已独立为 accounts.js（右上角「🏦 账户中心」入口） */

  function loadChannels(root) {
    EP.api('/api/expert-crawl/channels').then(function (d) {
      root.querySelector('#ec-channels').innerHTML =
        (d.channels || []).map(channelCards).join('');
    }).catch(function (e) { EP.toast(e.message, 'error'); });
  }

  function bindCrawl(root) {
    root.querySelectorAll('#ec-src-chips .chip').forEach(function (c) {
      c.onclick = function () {
        var s = c.dataset.src, i = S.sources.indexOf(s);
        if (i >= 0) S.sources.splice(i, 1); else S.sources.push(s);
        c.classList.toggle('on');
      };
    });
    function doRun(btn, root2) {
      EP.api('/api/expert-crawl/run', {
        method: 'POST',
        body: { sources: S.sources, top: parseInt(root2.querySelector('#ec-top').value, 10) || 50,
                date: root2.querySelector('#ec-date').value.trim() }
      }).then(function () {
        EP.toast('爬取已启动');
        bindJobs(root2);
        var logLoad = EP.logPanel(root2.querySelector('#ec-log'), '/api/expert-crawl/log', 'crawl');
        var timer = setInterval(function () {
          EP.api('/api/expert-crawl/status').then(function (d) {
            if (d.crawl && d.crawl.state !== 'running') {
              clearInterval(timer); logLoad(); loadChannels(root2);
            }
          });
        }, 3000);
      }).catch(function (e) { EP.toast(e.message, 'error'); })
        .finally(function () { btn.disabled = false; });
    }

    root.querySelector('#ec-run').onclick = function () {
      if (!S.sources.length) { EP.toast('至少选一个数据源', 'error'); return; }
      var btn = this; btn.disabled = true;
      /* 登录态不再拦截：模拟盘爬取不依赖登录；实盘明细请先在「账户中心」扫码 */
      doRun(btn, root);
    };
  }

  function bindJobs(root) {
    EP.jobPanel(root.querySelector('#ec-jobs'), '/api/expert-crawl/status',
      ['crawl', 'bulk_history', 'expert_eval'], ['爬取', '批量回补', '能力评估']);
    // 日志：可按任务切换
    var sel = '<div class="row" style="margin-top:8px"><div class="field"><label>任务日志</label><select class="select" id="ec-log-job">' +
      LOG_JOBS.map(function (j) { return '<option value="' + j[0] + '">' + j[1] + '</option>'; }).join('') +
      '</select></div></div>';
    root.querySelector('#ec-jobs').insertAdjacentHTML('afterend', sel);
    var logBox = root.querySelector('#ec-log');
    function loadLog() {
      var job = root.querySelector('#ec-log-job').value;
      EP.api('/api/expert-crawl/log?job=' + job).then(function (d) {
        logBox.innerHTML = d.log_tail
          ? '<div class="job done"><div class="log">' + EP.fmt.esc(d.log_tail) + '</div></div>'
          : '<div class="job idle">暂无日志</div>';
      });
    }
    root.querySelector('#ec-log-job').onchange = loadLog;
    loadLog();
  }

  function bindBackfill(root) {
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

  /* ═══════════ ② 高手能力评估 ═══════════ */

  function evalPanel() {
    return '<div class="card"><h3>② 高手能力评估</h3>' +
      '<div class="card-sub">逐高手镜像回测（= 无脑跟他的单）：T+1 开盘 ±0.2% 滑点、佣金万 0.8（最低 5 元）、卖出印花税 0.05%、涨停拦截、噪音过滤。窗口从该高手首笔信号日起。按综合评分（夏普/索提诺/Calmar/胜率 分位秩平均）排序——分高且样本充足的，就是值得跟的人。</div>' +
      '<div class="row">' +
      '<div class="field"><label>数据环境</label><select class="select" id="ee-env">' +
      '<option value="sim">模拟盘（流水+回补）</option><option value="live">实盘（rtV3 流水）</option></select></div>' +
      '<div class="field"><label>最少卖出笔数</label><select class="select" id="ee-min-sells">' +
      [1, 3, 5, 10, 20].map(function (n) { return '<option value="' + n + '"' + (n === 3 ? ' selected' : '') + '>' + n + '</option>'; }).join('') +
      '</select></div>' +
      '<div class="field"><label>&nbsp;</label><label style="display:flex;align-items:center;gap:6px;cursor:pointer">' +
      '<input type="checkbox" id="ee-sample-only" checked> 只看样本充足</label></div>' +
      '<div class="field"><label>&nbsp;</label><button class="btn primary" id="ee-run">▶ 开始评估</button>' +
      '<button class="btn" id="ee-refresh" style="margin-left:6px">刷新结果</button></div>' +
      '</div><div id="ee-summary"></div><div id="ee-table"></div>' +
      '<div style="margin-top:12px"><h3 style="font-size:14px;margin-bottom:8px">关注列表 <span class="hint" style="font-weight:400">（v1 镜像策略的跟随来源）</span></h3><div id="ee-wl"></div></div></div>';
  }

  function num0(v) { return EP.fmt.num(v, 0); }
  function f2(v) { return v == null ? '—' : Number(v).toFixed(2); }

  var EVAL_COLS = [
    { key: 'rank', label: '#', num: true, fmt: num0 },
    { key: 'name', label: '高手', fmt: function (v, r) { return '<b>' + EP.fmt.esc(v || r.expert_id) + '</b>'; } },
    { key: 'expert_id', label: 'ID' },
    { key: 'source', label: '来源', fmt: function (v) { return EP.fmt.esc(SRC_LABELS[v] || v || '—'); } },
    { key: 'total_return_pct', label: '总收益%', num: true, fmt: EP.fmt.signedPct },
    { key: 'annualized_pct', label: '年化%', num: true, fmt: EP.fmt.signedPct },
    { key: 'sharpe', label: '夏普', num: true, fmt: function (v) { return v == null ? '—' : '<b>' + f2(v) + '</b>'; } },
    { key: 'sortino', label: '索提诺', num: true, fmt: f2 },
    { key: 'max_drawdown_pct', label: '最大回撤%', num: true, fmt: function (v) { return v == null ? '—' : '<span class="down">' + f2(v) + '%</span>'; } },
    { key: 'calmar', label: 'Calmar', num: true, fmt: f2 },
    { key: 'win_rate_pct', label: '胜率%', num: true, fmt: function (v) { return v == null ? '—' : v.toFixed(1) + '%'; } },
    { key: 'profit_factor', label: '盈亏比', num: true, fmt: f2 },
    { key: 'n_sell', label: '卖出', num: true, fmt: num0 },
    { key: 'trading_days', label: '覆盖天数', num: true, fmt: num0 },
    { key: 'sample_ok', label: '样本', fmt: function (v) { return v ? EP.badge('充足', 'green') : EP.badge('不足', 'gray'); } },
    { key: 'expert_id', label: '操作', html: function (v) {
      var watched = !!S.wl[v];
      return '<button class="btn sm ' + (watched ? '' : 'primary') + '" data-watch="' + EP.fmt.esc(v) + '">' +
        (watched ? '✓ 已关注' : '＋ 关注') + '</button> ' +
        '<button class="btn sm" data-detail="' + EP.fmt.esc(v) + '">明细</button>'; } }
  ];

  function evalSummary(d) {
    var m = d.meta || {};
    var ok = d.rows.filter(function (r) { return r.sample_ok; });
    var best = ok.slice(0, 3).map(function (r) { return r.name; }).join(' / ') || '—';
    var pct = m.n_codes ? Math.round((m.n_codes_with_price || 0) / m.n_codes * 100) : 0;
    return EP.kvCards([
      ['入榜高手', m.n_experts, function () { return num0(m.n_experts); }],
      ['样本充足', m.n_sample_ok, function () { return num0(m.n_sample_ok); }],
      ['数据范围', (m.data_range || []).join(' ~ ') || '—'],
      ['交易日', m.trading_days, function () { return num0(m.trading_days); }],
      ['标的行情覆盖', m.n_codes_with_price + '/' + m.n_codes + '（' + pct + '%）'],
      ['基准', m.bench_code + (m.bench_available ? '' : '（缺失）')],
      ['Top3', best],
      ['评估时间', EP.fmt.ts(d.generated_at)]
    ]);
  }

  function applyFilters() {
    var t = EP._tables['ee-table'];
    if (!t || !S.eval) return;
    var min = parseInt(S.minSells, 10) || 1;
    t.setRows(S.eval.rows.filter(function (r) {
      return (!S.sampleOnly || r.sample_ok) && (r.n_sell || 0) >= min;
    }));
  }

  function renderEval(root, d) {
    S.eval = d;
    var sum = root.querySelector('#ee-summary'), box = root.querySelector('#ee-table');
    if (!d.ok || !d.rows.length) {
      sum.innerHTML = '';
      box.innerHTML = '<div class="empty">' + EP.fmt.esc(d.msg || '暂无评估结果') + '</div>';
      return;
    }
    var envNote = (d.params && d.params.env !== S.env)
      ? '<div class="hint" style="margin-bottom:6px">当前展示的是 <b>' + EP.fmt.esc(d.params.env) +
        '</b> 环境的结果（门槛：卖出≥' + d.params.min_sells + '、覆盖≥' + d.params.min_days + ' 天）</div>' : '';
    sum.innerHTML = envNote + evalSummary(d);
    EP.table(box, d.rows, EVAL_COLS, { height: 520, pageSize: 50, sortKey: 'rank', sortDir: 1 });
    applyFilters();
    renderWatchlist(root);
  }

  function loadEval(root) {
    EP.api('/api/expert-crawl/evaluation').then(function (d) { renderEval(root, d); })
      .catch(function (e) { EP.toast(e.message, 'error'); });
  }

  function loadWatch(root) {
    return EP.api('/api/expert-crawl/watchlist').then(function (d) {
      S.wl = {};
      (d.rows || []).forEach(function (r) {
        S.wl[r.expert_id] = { name: r.name || '', note: r.note || '',
                              created_at: r.created_at || '', __mt: r.market_type === 'live' ? 'live' : 'sim' };
      });
      renderWatchlist(root);
      renderWatchTable(root);
    }).catch(function () {});
  }

  function renderWatchlist(root) {
    var el = root.querySelector('#ee-wl');
    if (!el) return;
    var ids = Object.keys(S.wl);
    if (!ids.length) { el.innerHTML = '<div class="hint">关注列表为空——在评估表里点「＋ 关注」</div>'; return; }
    el.innerHTML = ids.map(function (id) {
      var w = S.wl[id];
      return '<span class="chip on" style="margin:0 6px 6px 0">' + EP.fmt.esc(w.name || id) +
        (w.__mt === 'live' ? ' <span style="color:var(--c-danger);font-weight:600">实盘</span>' : '') +
        ' <b style="margin-left:4px">' + EP.fmt.esc(id) + '</b>' +
        ' <a href="javascript:void(0)" data-unwatch="' + EP.fmt.esc(id) + '" style="margin-left:6px;color:var(--c-danger);text-decoration:none">✕</a></span>';
    }).join('');
    el.querySelectorAll('[data-unwatch]').forEach(function (a) {
      a.onclick = function () { toggleWatch(a.dataset.unwatch, root); };
    });
  }

  function toggleWatch(expertId, root) {
    var watched = !!S.wl[expertId];
    var mt = S.env === 'live' ? 'live' : 'sim';
    var row = (S.eval && S.eval.rows || []).filter(function (r) { return r.expert_id === expertId; })[0];
    var req = watched
      ? EP.api('/api/expert-crawl/watchlist/' + encodeURIComponent(expertId) + '?market_type=' + mt, { method: 'DELETE' })
      : EP.api('/api/expert-crawl/watchlist', { method: 'POST',
          body: { expert_id: expertId, name: (row || {}).name || '', market_type: mt } });
    req.then(function (d) {
      if (d && d.em && d.em.msg) { EP.toast(d.em.msg, d.em.ok ? 'ok' : 'error'); }
      if (watched) delete S.wl[expertId];
      else S.wl[expertId] = { name: (row || {}).name || '', note: '', created_at: '', __mt: mt };
      EP.toast(watched ? '已取消关注' : '已关注');
      loadWatch(root);
      // 表格里的按钮文案同步
      var btn = root.querySelector('[data-watch="' + expertId + '"]');
      if (btn) {
        var now = !!S.wl[expertId];
        btn.textContent = now ? '✓ 已关注' : '＋ 关注';
        btn.classList.toggle('primary', !now);
      }
      var dbtn = document.querySelector('#ed-watch');
      if (dbtn && document.getElementById('ee-drawer')) {
        dbtn.textContent = now ? '✓ 已关注' : '＋ 关注';
      }
    }).catch(function (e) { EP.toast(e.message, 'error'); });
  }

  /* ── 明细抽屉：净值曲线 + 指标 + 成交 ── */

  function showDetail(root, expertId) {
    var drawer = document.getElementById('ee-drawer');
    if (!drawer) {
      drawer = document.createElement('div');
      drawer.id = 'ee-drawer';
      drawer.style.cssText = 'position:fixed;top:0;right:0;height:100%;width:560px;max-width:92vw;' +
        'background:var(--c-card);box-shadow:var(--sh-lg);z-index:998;overflow-y:auto;padding:20px';
      document.body.appendChild(drawer);
    }
    drawer.innerHTML = '<div class="hint">加载中…</div>';
    drawer.scrollTop = 0;
    EP.api('/api/expert-crawl/evaluation/detail?expert_id=' + encodeURIComponent(expertId) +
           '&env=' + encodeURIComponent(S.env)).then(function (d) {
      var m = d.metrics;
      drawer.innerHTML =
        '<div style="display:flex;justify-content:space-between;align-items:flex-start">' +
        '<div><h3 style="font-size:16px">' + EP.fmt.esc(d.name || d.expert_id) + '</h3>' +
        '<div class="hint">' + EP.fmt.esc(d.expert_id) + ' · ' + EP.fmt.esc(SRC_LABELS[d.source] || d.source || '—') +
        ' · ' + (d.env === 'live' ? '实盘' : '模拟盘') + '</div></div>' +
        '<div><button class="btn sm primary" id="ed-watch">' + (S.wl[d.expert_id] ? '✓ 已关注' : '＋ 关注') + '</button> ' +
        '<button class="btn sm" id="ed-close">关闭</button></div></div>' +
        '<div id="ed-kv" style="margin-top:10px"></div>' +
        '<div id="ed-chart" style="width:100%;height:280px;margin-top:10px"></div>' +
        '<h3 style="font-size:14px;margin:14px 0 6px">成交明细（最近 ' + d.trades.length + ' 笔）</h3>' +
        '<div id="ed-trades"></div>';
      drawer.querySelector('#ed-close').onclick = function () { drawer.remove(); };
      drawer.querySelector('#ed-watch').onclick = function () { toggleWatch(d.expert_id, root); };
      drawer.querySelector('#ed-kv').innerHTML = EP.kvCards([
        ['总收益', EP.fmt.signedPct(m.total_return_pct)],
        ['年化', EP.fmt.signedPct(m.annualized_pct)],
        ['夏普', f2(m.sharpe)],
        ['索提诺', f2(m.sortino)],
        ['最大回撤', m.max_drawdown_pct == null ? '—' : '<span class="down">' + f2(m.max_drawdown_pct) + '%</span>'],
        ['Calmar', f2(m.calmar)],
        ['胜率', m.win_rate_pct == null ? '—' : m.win_rate_pct.toFixed(1) + '%'],
        ['盈亏比', f2(m.profit_factor)],
        ['超额(vs沪深300)', m.excess_pct == null ? '—' : EP.fmt.signedPct(m.excess_pct)],
        ['Alpha(vs中证500)', m.alpha_pct == null ? '—' : EP.fmt.signedPct(m.alpha_pct) + '（β=' + f2(m.beta) + '）'],
        ['买卖笔数', m.n_buy + ' / ' + m.n_sell],
        ['覆盖', m.first_date + ' ~ ' + m.last_date + '（' + m.trading_days + ' 天）']
      ]);
      EP.table(drawer.querySelector('#ed-trades'), d.trades.slice().reverse(), [
        { key: 'date', label: '日期' },
        { key: 'code', label: '代码' },
        { key: 'name', label: '名称' },
        { key: 'side', label: '方向', fmt: function (v) {
          return v === 'buy' ? '<span class="up">买入</span>' : (v === 'sell' ? '<span class="down">卖出</span>' : EP.fmt.esc(v)); } },
        { key: 'price', label: '价格', num: true, fmt: function (v) { return EP.fmt.num(v); } },
        { key: 'return_pct', label: '单笔%', num: true, fmt: EP.fmt.signedPct },
        { key: 'signal_time', label: '信号时间', fmt: function (v) { return EP.fmt.esc((v || '').slice(-8)); } }
      ], { height: 300, pageSize: 50, search: false });
      drawCurve(drawer.querySelector('#ed-chart'), d.nav_daily, d.bench);
    }).catch(function (e) {
      drawer.innerHTML = '<div class="empty">' + EP.fmt.esc(e.message) + '</div>';
    });
  }

  function drawCurve(el, navDaily, bench) {
    if (!navDaily || navDaily.length < 2) { el.innerHTML = '<div class="empty">交易日太少，画不出曲线</div>'; return; }
    if (S.chart) { S.chart.dispose(); S.chart = null; }
    var chart = echarts.init(el);
    S.chart = chart;
    var dates = navDaily.map(function (x) { return x.date; });
    var base = navDaily[0].nav;
    var navIdx = navDaily.map(function (x) { return +(x.nav / base * 100).toFixed(2); });
    var bBase = bench && bench[dates[0]];
    var benchIdx = bBase ? dates.map(function (d) { return bench[d] ? +(bench[d] / bBase * 100).toFixed(2) : null; }) : [];
    chart.setOption({
      tooltip: { trigger: 'axis' },
      legend: { data: bBase ? ['跟随净值', '沪深300'] : ['跟随净值'], top: 0 },
      grid: { left: 48, right: 12, top: 28, bottom: 24 },
      xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10 } },
      yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 10 } },
      series: [
        { name: '跟随净值', type: 'line', data: navIdx, showSymbol: false, smooth: true,
          lineStyle: { width: 2, color: '#f5222d' }, itemStyle: { color: '#f5222d' }, connectNulls: true },
        { name: '沪深300', type: 'line', data: benchIdx, showSymbol: false, smooth: true,
          lineStyle: { width: 1.5, type: 'dashed', color: '#999' }, itemStyle: { color: '#999' }, connectNulls: true }
      ]
    });
    window.addEventListener('resize', function () { chart.resize(); });
  }

  function bindEval(root) {
    // 行内按钮用事件委托（表格筛选/排序会重渲染，逐元素绑定会丢）
    root.querySelector('#ee-table').addEventListener('click', function (e) {
      var w = e.target.closest('[data-watch]'), d = e.target.closest('[data-detail]');
      if (w) { toggleWatch(w.dataset.watch, root); }
      else if (d) { showDetail(root, d.dataset.detail); }
    });
    root.querySelector('#ee-run').onclick = function () {
      var btn = this; btn.disabled = true;
      S.env = root.querySelector('#ee-env').value;
      S.minSells = root.querySelector('#ee-min-sells').value;
      S.sampleOnly = root.querySelector('#ee-sample-only').checked;
      EP.api('/api/expert-crawl/evaluation/run', {
        method: 'POST',
        body: { env: S.env, min_sells: parseInt(S.minSells, 10) || 3, min_days: 20 }
      }).then(function () {
        EP.toast('评估已启动（后台运行，完成后自动刷新）');
        bindJobs(root);
        var timer = setInterval(function () {
          EP.api('/api/expert-crawl/status').then(function (d) {
            if (d.expert_eval && d.expert_eval.state !== 'running') {
              clearInterval(timer);
              btn.disabled = false;
              if (d.expert_eval.state === 'error') { EP.toast('评估失败，看任务日志', 'error'); return; }
              loadEval(root);
              EP.toast('评估完成');
            }
          });
        }, 3000);
      }).catch(function (e) { EP.toast(e.message, 'error'); btn.disabled = false; });
    };
    root.querySelector('#ee-refresh').onclick = function () { loadEval(root); };
    root.querySelector('#ee-min-sells').onchange = function () { S.minSells = this.value; applyFilters(); };
    root.querySelector('#ee-sample-only').onchange = function () { S.sampleOnly = this.checked; applyFilters(); };
    root.querySelector('#ee-env').onchange = function () { S.env = this.value; };
  }

  EP.loadExperts = function (root) {
    root.innerHTML = crawlPanel() + watchPanel() + backfillPanel() + evalPanel();
    loadChannels(root);
    bindCrawl(root); bindJobs(root); bindBackfill(root);
    bindWatchCard(root);
    bindEval(root);
    loadWatch(root);
    loadEval(root);
  };
})();
