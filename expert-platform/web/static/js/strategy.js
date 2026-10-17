/* 高手跟随策略页（#/expert-strategy）—— 对应原 Streamlit views/strategy.py 的功能面 */
(function () {
  'use strict';
  var EP = window.EP;
  var PAGE = 50;
  var SUB = ['📋 策略看板', '🧪 回测实验室', '🗄️ 实验档案', '🩺 数据体检'];
  var state = { sub: 0, runId: '', charts: [] };

  function shell() {
    var subs = SUB.map(function (t, i) {
      return '<a data-sub="' + i + '" class="' + (i === 0 ? 'active' : '') + '">' + t + '</a>';
    }).join('');
    return '<div class="subtabs">' + subs + '</div><div id="es-body"></div>';
  }

  /* ── ① 策略看板 ── */
  function overview(root) {
    root.innerHTML = '<div class="card"><h3>模拟账户看板</h3>' +
      '<div class="card-sub">前向净值由 paper_sim 按日重放（与回测同一套选人/目标仓位函数，PIT 口径）；发布后点「更新前向净值」再刷新看板。</div>' +
      '<button class="btn primary" id="es-update">🔄 更新前向净值</button> ' +
      '<span class="hint">发布到模拟盘的账户每天按 T 日信号 / T+1 开盘执行记账；满 60 个交易日可与回测对账。</span>' +
      '<div id="es-ov" style="margin-top:10px"></div></div>';
    root.querySelector('#es-update').onclick = function () {
      var btn = this; btn.disabled = true;
      EP.api('/api/expert-follow/overview/update', { method: 'POST' })
        .then(function () { EP.toast('前向净值更新已启动，稍后自动刷新'); pollUpdate(root, btn); })
        .catch(function (e) { EP.toast(e.message, 'error'); btn.disabled = false; });
    };
    loadOverview(root);
  }

  function pollUpdate(root, btn) {
    var n = 0;
    var timer = setInterval(function () {
      EP.api('/api/expert-crawl/status').then(function (d) {
        // paper_update 任务状态在 expert-crawl/status 之外，单独读
      });
      fetch('/api/expert-crawl/status').then(function (r) { return r.json(); }).then(function () {});
      n++;
      if (n > 120) { clearInterval(timer); btn.disabled = false; }
    }, 3000);
    // paper_update 完成后由前端在下次手动刷新时可见；简单起见 3 分钟后恢复按钮
    setTimeout(function () { clearInterval(timer); btn.disabled = false; loadOverview(root); }, 180000);
  }

  function loadOverview(root) {
    EP.api('/api/expert-follow/overview').then(function (d) {
      var el = root.querySelector('#es-ov');
      if (!el) return;
      if (!d.accounts.length) {
        el.innerHTML = '<div class="empty">还没有模拟账户 —— 在「回测实验室」跑一次统一回测并发布方案。</div>';
        return;
      }
      var rows = d.accounts.map(function (a) {
        return {
          id: a.id, name: a.name, config_id: a.config_id, family: a.family,
          tier: a.tier, stage: a.stage, n_days: a.perf.n_days,
          cum: a.perf.cum_return_pct, dd: a.perf.drawdown_pct, mdd: a.perf.max_drawdown_pct,
          excess: a.perf.excess_pct, promo: a.promotion_state,
          progress: Math.min(1, (a.perf.n_days || 0) / 60)
        };
      });
      el.innerHTML = '<div id="es-ov-table"></div><div class="chart" id="es-ov-chart"></div>';
      EP.table(el.querySelector('#es-ov-table'), rows, [
        { key: 'id', label: '账户' },
        { key: 'name', label: '名称' },
        { key: 'config_id', label: '策略文件' },
        { key: 'family', label: '家族' },
        { key: 'tier', label: '等级', fmt: function (v) { return EP.badge(v, 'blue'); } },
        { key: 'stage', label: '阶段', fmt: function (v) {
          var c = { live: 'green', ready: 'blue', observing: 'gray', requested: 'orange', halted: 'red' }[v] || 'gray';
          var t = { live: '实盘', ready: '可晋级', observing: '观察中', requested: '申请中', halted: '已停' }[v] || v;
          return EP.badge(t, c); } },
        { key: 'n_days', label: '天数', num: true, fmt: function (v) { return EP.fmt.num(v, 0); } },
        { key: 'cum', label: '累计%', num: true, fmt: function (v) { return EP.fmt.signedPct(v); } },
        { key: 'dd', label: '回撤%', num: true, fmt: function (v) { return EP.fmt.signedPct(v ? -v : v); } },
        { key: 'mdd', label: '最大回撤%', num: true, fmt: function (v) { return EP.fmt.signedPct(v ? -v : v); } },
        { key: 'excess', label: '超额%', num: true, fmt: function (v) { return EP.fmt.signedPct(v); } },
        { key: 'promo', label: '晋级', fmt: function (v) { return EP.badge(v, 'gray'); } },
        { key: 'progress', label: '60日进度', html: function (v) {
          return '<div style="background:#f0f0f0;border-radius:4px;height:8px;width:80px"><div style="background:var(--c-primary);height:100%;border-radius:4px;width:' +
            Math.round(v * 100) + '%"></div></div>'; } }
      ], { height: 240, pageSize: 50 });
      drawNavChart(d.accounts);
      promotionPanel(root, d.accounts);
    }).catch(function (e) { EP.toast(e.message, 'error'); });
  }

  function drawNavChart(accounts) {
    var el = document.getElementById('es-ov-chart');
    if (!el || !window.echarts) return;
    var ch = echarts.getInstanceByDom(el) || echarts.init(el);
    state.charts.push(ch);
    var series = accounts.filter(function (a) { return a.nav && a.nav.length; }).map(function (a) {
      return { name: (a.name || a.id).slice(0, 20), type: 'line', showSymbol: false,
        data: a.nav.map(function (p) { return [p[0], p[1] / (a.initial_capital || 1e6)]; }),
        lineStyle: { width: 1.8 }, emphasis: { focus: 'series' } };
    });
    if (!series.length) { ch.clear(); return; }
    ch.setOption({ tooltip: { trigger: 'axis' }, legend: { type: 'scroll', textStyle: { fontSize: 11 } },
      grid: { left: 50, right: 16, top: 30, bottom: 28 },
      xAxis: { type: 'time' }, yAxis: { type: 'value', scale: true, name: '净值(归一)' },
      series: series, color: ['#1890ff', '#722ed1', '#52c41a', '#faad14', '#ff4d4f', '#13c2c2'] }, true);
  }

  function promotionPanel(root, accounts) {
    var div = document.createElement('div');
    div.innerHTML = '<div class="card" style="margin-top:14px"><h3>晋级管理（正式交易）</h3>' +
      '<div class="card-sub">晋级只生成待确认单（原平台口径）；approve 后 state=approved。</div>' +
      '<div class="row">' +
      '<div class="field"><label>策略</label><select class="select" id="es-promo-sid" style="min-width:260px">' +
      accounts.map(function (a) { return '<option>' + EP.fmt.esc(a.strategy_id) + '</option>'; }).join('') + '</select></div>' +
      '<div class="field"><label>资金</label><input class="input num" id="es-promo-cap" value="1000000" style="min-width:110px"></div>' +
      '<div class="field"><label>最大回撤%</label><input class="input num" id="es-promo-dd" value="10" style="min-width:80px"></div>' +
      '<div class="field"><label>&nbsp;</label><button class="btn" id="es-promo-req">申请晋级</button> ' +
      '<button class="btn" id="es-promo-ok">批准</button> ' +
      '<button class="btn danger" id="es-promo-no">撤销</button></div></div></div>';
    root.appendChild(div);
    var sid = function () { return div.querySelector('#es-promo-sid').value; };
    div.querySelector('#es-promo-req').onclick = function () {
      EP.api('/api/expert-follow/promote/request', { method: 'POST',
        body: { strategy_id: sid(), capital: parseFloat(div.querySelector('#es-promo-cap').value) || 1e6,
                max_dd_pct: parseFloat(div.querySelector('#es-promo-dd').value) || 10 } })
        .then(function () { EP.toast('已提交申请'); loadOverview(root); })
        .catch(function (e) { EP.toast(e.message, 'error'); });
    };
    div.querySelector('#es-promo-ok').onclick = function () {
      EP.api('/api/expert-follow/promote/approve', { method: 'POST', body: { strategy_id: sid() } })
        .then(function () { EP.toast('已批准'); loadOverview(root); })
        .catch(function (e) { EP.toast(e.message, 'error'); });
    };
    div.querySelector('#es-promo-no').onclick = function () {
      EP.api('/api/expert-follow/promote/revoke', { method: 'POST', body: { strategy_id: sid() } })
        .then(function () { EP.toast('已撤销'); loadOverview(root); })
        .catch(function (e) { EP.toast(e.message, 'error'); });
    };
    EP.api('/api/expert-follow/recon').then(function (d) {
      if (!d.rows || !d.rows.length) return;
      var box = document.createElement('div');
      box.innerHTML = '<div class="card" style="margin-top:14px"><h3>前向 vs 回测 对账</h3><div id="es-recon"></div></div>';
      root.appendChild(box);
      EP.table(box.querySelector('#es-recon'), d.rows,
        Object.keys(d.rows[0]).slice(0, 10).map(function (k) {
          return { key: k, label: k, num: typeof d.rows[0][k] === 'number' };
        }), { height: 200, pageSize: 50 });
    }).catch(function () {});
  }

  /* ── ② 回测实验室 ── */
  function lab(root) {
    root.innerHTML = '<div class="card"><h3>策略文件（configs/expert_follow/strategies）</h3>' +
      '<div class="card-sub">内置策略只读；「另存为」写到 strategies/user/。同一份文件既用于回测也用于模拟盘，改文件 = 新版本（指纹变）。</div>' +
      '<div class="row"><div class="field"><label>查看策略</label><select class="select" id="es-strat" style="min-width:260px"></select></div>' +
      '<button class="btn" id="es-strat-view">查看</button></div>' +
      '<div class="two-col" id="es-strat-detail" style="display:none">' +
      '<div><div class="hint">原文</div><pre class="code" id="es-strat-raw"></pre></div>' +
      '<div><div class="hint">解析 + 中文描述</div><pre class="code" id="es-strat-spec"></pre>' +
      '<div class="hint" id="es-strat-desc"></div></div></div></div>' +

      '<div class="card"><h3>运行统一回测</h3>' +
      '<div class="card-sub">后台运行（strategy.runner 子进程）；一次只跑一个。运行结束后再到「策略看板」发布。</div>' +
      '<div class="row">' +
      '<div class="field"><label>回测配置</label><select class="select" id="es-bt" style="min-width:200px"></select></div>' +
      '<div class="field"><label>数据截止日（空=最新）</label><input class="input" id="es-end" placeholder="YYYY-MM-DD" style="min-width:130px"></div>' +
      '<div class="field"><label>&nbsp;</label><button class="btn primary" id="es-run">▶ 运行</button></div></div>' +
      '<div id="es-run-status"></div><div id="es-run-jobs"></div></div>' +

      '<div class="card"><h3>运行记录</h3><div id="es-runs"></div></div>';
    bindLab(root);
  }

  function bindLab(root) {
    EP.api('/api/expert-follow/backtests').then(function (d) {
      var sel = root.querySelector('#es-bt');
      var ok = d.backtests.filter(function (b) { return !b.error; });
      sel.innerHTML = ok.map(function (b) {
        return '<option value="' + EP.fmt.esc(b.id) + '">' + EP.fmt.esc(b.id) + ' · ' + EP.fmt.esc(b.name) + '</option>';
      }).join('');
    });
    EP.api('/api/expert-follow/strategies').then(function (d) {
      var sel = root.querySelector('#es-strat');
      sel.innerHTML = d.strategies.map(function (s) {
        return '<option value="' + EP.fmt.esc(s.id) + '">' + EP.fmt.esc(s.id) +
          (s.builtin ? '（内置）' : '（用户）') + '</option>';
      }).join('');
    });
    root.querySelector('#es-strat-view').onclick = function () {
      var sid = root.querySelector('#es-strat').value;
      EP.api('/api/expert-follow/strategies/' + encodeURIComponent(sid)).then(function (d) {
        root.querySelector('#es-strat-detail').style.display = '';
        root.querySelector('#es-strat-raw').textContent = d.raw;
        root.querySelector('#es-strat-spec').textContent = JSON.stringify(d.spec, null, 2);
        root.querySelector('#es-strat-desc').innerHTML = '<b>描述</b>：' + EP.fmt.esc(d.describe) +
          '<br><b>指纹</b>：<code>' + EP.fmt.esc(d.hash) + '</code>';
      }).catch(function (e) { EP.toast(e.message, 'error'); });
    };
    root.querySelector('#es-run').onclick = function () {
      EP.api('/api/expert-follow/run', { method: 'POST',
        body: { backtest_id: root.querySelector('#es-bt').value, end: root.querySelector('#es-end').value.trim() } })
        .then(function (d) {
          EP.toast('回测已启动 ' + d.run_id);
          state.runId = d.run_id;
          pollRun(root, d.run_id);
          EP.jobPanel(root.querySelector('#es-run-jobs'), '/api/expert-crawl/status', ['backtest'], ['回测']);
        })
        .catch(function (e) { EP.toast(e.message, 'error'); });
    };
    loadRuns(root);
  }

  function pollRun(root, runId) {
    var box = root.querySelector('#es-run-status');
    function tick() {
      EP.api('/api/expert-follow/run-status?run_id=' + runId).then(function (d) {
        var cls = d.state === 'done' ? 'done' : (d.state === 'error' ? 'error' : 'running');
        box.innerHTML = '<div class="job ' + cls + '">运行 ' + EP.fmt.esc(runId) + '：' +
          EP.fmt.esc(d.state) + (d.stage ? ' · ' + EP.fmt.esc(d.stage) : '') +
          (d.progress ? ' · ' + d.progress + '%' : '') +
          (d.error ? '<div style="color:var(--c-danger)">' + EP.fmt.esc(d.error) + '</div>' : '') + '</div>';
        if (d.state === 'running' || d.state === 'starting') setTimeout(tick, 3000);
        else loadRuns(root);
      }).catch(function (e) {
        box.innerHTML = '<div class="job error">' + EP.fmt.esc(e.message) + '</div>';
      });
    }
    tick();
  }

  function loadRuns(root) {
    EP.api('/api/expert-follow/runs').then(function (d) {
      var el = root.querySelector('#es-runs');
      if (!el) return;
      if (!d.runs.length) { el.innerHTML = '<div class="empty">还没有运行记录</div>'; return; }
      el.innerHTML = '<div id="es-runs-table"></div><div id="es-run-detail"></div>';
      EP.table(el.querySelector('#es-runs-table'), d.runs, [
        { key: 'run_id', label: 'run_id' },
        { key: 'backtest_id', label: '回测' },
        { key: 'data_env', label: '环境' },
        { key: 'status', label: '状态', fmt: function (v) {
          return v === 'done' ? EP.badge('完成', 'green') : EP.badge(v, v === 'error' ? 'red' : 'orange'); } },
        { key: 'started_at', label: '开始', fmt: function (v) { return EP.fmt.ts(v); } },
        { key: 'finished_at', label: '完成', fmt: function (v) { return EP.fmt.ts(v); } },
        { key: 'passed', label: '过闸门', html: function (v) {
          var arr = v || [];
          return arr.length ? '<span class="badge green">' + arr.map(EP.fmt.esc).join('、') + '</span>' : '<span class="badge gray">—</span>'; } },
        { key: 'run_id', label: '操作', html: function (v) {
          return '<button class="btn sm" data-detail="' + EP.fmt.esc(v) + '">详情</button> ' +
            '<button class="btn sm danger" data-arch="' + EP.fmt.esc(v) + '">归档</button>'; } }
      ], { height: 220, pageSize: PAGE, search: false });
      el.querySelectorAll('[data-detail]').forEach(function (b) {
        b.onclick = function () { showRunDetail(root, b.dataset.detail); };
      });
      el.querySelectorAll('[data-arch]').forEach(function (b) {
        b.onclick = function () {
          EP.api('/api/expert-follow/runs/' + encodeURIComponent(b.dataset.arch) + '/archive',
            { method: 'POST', body: { archived: true } })
            .then(function () { EP.toast('已归档'); loadRuns(root); })
            .catch(function (e) { EP.toast(e.message, 'error'); });
        };
      });
    }).catch(function (e) { EP.toast(e.message, 'error'); });
  }

  function showRunDetail(root, runId) {
    EP.api('/api/expert-follow/runs/' + encodeURIComponent(runId)).then(function (d) {
      var box = root.querySelector('#es-run-detail');
      if (d.blocked) {
        box.innerHTML = '<div class="job error">运行被数据体检拦截: ' + EP.fmt.esc(d.reason || '') + '</div>';
        return;
      }
      box.innerHTML = '<div class="hint">回测 ' + EP.fmt.esc(d.backtest_id) + ' · 环境 ' + EP.fmt.esc(d.data_env) +
        ' · ' + EP.fmt.esc((d.period || {}).start) + ' ~ ' + EP.fmt.esc((d.period || {}).end) +
        ' · 耗时 ' + EP.fmt.esc(d.elapsed_s) + 's</div>' +
        '<div id="es-detail-table"></div><div class="chart" id="es-detail-chart"></div>' +
        '<div class="row" style="margin-top:10px">' +
        '<div class="field"><label>发布方案</label><select class="select" id="es-pub-key" style="min-width:220px"></select></div>' +
        '<div class="field"><label>初始资金</label><input class="input num" id="es-pub-cap" value="1000000" style="min-width:110px"></div>' +
        '<div class="field"><label>&nbsp;<br><label style="font-size:12px"><input type="checkbox" id="es-pub-obs"> 前向观察（未过闸门）</label></label></div>' +
        '<div class="field"><label>&nbsp;</label><button class="btn primary" id="es-pub-go">发布到模拟盘</button></div></div>';
      EP.table(box.querySelector('#es-detail-table'), d.variants, [
        { key: 'key', label: '方案' },
        { key: 'ref', label: '策略' },
        { key: 'engine', label: '引擎' },
        { key: 'total_return_pct', label: '总收益%', num: true, fmt: function (v) { return EP.fmt.signedPct(v); } },
        { key: 'annualized_pct', label: '年化%', num: true, fmt: function (v) { return EP.fmt.signedPct(v); } },
        { key: 'max_drawdown_pct', label: '最大回撤%', num: true, fmt: function (v) { return EP.fmt.signedPct(v ? -v : v); } },
        { key: 'sharpe', label: '夏普', num: true, fmt: function (v) { return EP.fmt.num(v); } },
        { key: 'alpha_pct', label: 'α%', num: true, fmt: function (v) { return EP.fmt.signedPct(v); } },
        { key: 'alpha_t', label: 'α t', num: true, fmt: function (v) { return EP.fmt.num(v); } },
        { key: 'n_buys', label: '买入笔数', num: true, fmt: function (v) { return EP.fmt.num(v, 0); } },
        { key: 'passed', label: '过闸门', fmt: function (v) { return v ? EP.badge('✓', 'green') : EP.badge('✗', 'red'); } },
        { key: 'fail_checks', label: '未过项', fmt: function (v) { return EP.fmt.esc((v || []).join('、') || '—'); } }
      ], { height: 360, pageSize: PAGE, sortKey: 'total_return_pct' });
      var keys = d.variants.map(function (v) { return v.key; });
      box.querySelector('#es-pub-key').innerHTML = keys.map(function (k) { return '<option>' + EP.fmt.esc(k) + '</option>'; }).join('');
      // 净值曲线
      var el = document.getElementById('es-detail-chart');
      if (el && window.echarts && Object.keys(d.curves || {}).length) {
        var ch = echarts.getInstanceByDom(el) || echarts.init(el);
        state.charts.push(ch);
        ch.setOption({
          tooltip: { trigger: 'axis' }, legend: { type: 'scroll', textStyle: { fontSize: 11 } },
          grid: { left: 50, right: 16, top: 30, bottom: 28 },
          xAxis: { type: 'time' }, yAxis: { type: 'value', scale: true, name: '净值' },
          series: Object.keys(d.curves).map(function (k, i) {
            return { name: k, type: 'line', showSymbol: false, data: d.curves[k], lineStyle: { width: 1.6 } };
          }),
          color: ['#1890ff', '#722ed1', '#52c41a', '#faad14', '#ff4d4f', '#13c2c2', '#eb2f96', '#2f54eb']
        }, true);
      }
      box.querySelector('#es-pub-go').onclick = function () {
        EP.api('/api/expert-follow/publish', { method: 'POST', body: {
          run_id: runId, key: box.querySelector('#es-pub-key').value,
          observe: box.querySelector('#es-pub-obs').checked,
          capital: parseFloat(box.querySelector('#es-pub-cap').value) || 1e6 } })
          .then(function (r) { EP.toast('已发布账户 ' + r.account_id); })
          .catch(function (e) { EP.toast(e.message, 'error'); });
      };
    }).catch(function (e) { EP.toast(e.message, 'error'); });
  }

  /* ── ③ 实验档案 ── */
  function archive(root) {
    root.innerHTML = '<div class="card"><h3>实验档案</h3>' +
      '<div class="card-sub">归档的运行从实验室列表隐藏，可在这里恢复。</div><div id="es-arch"></div></div>';
    EP.api('/api/expert-follow/runs?archived=1').then(function (d) {
      var el = root.querySelector('#es-arch');
      if (!d.runs.length) { el.innerHTML = '<div class="empty">暂无归档运行</div>'; return; }
      EP.table(el, d.runs, [
        { key: 'run_id', label: 'run_id' },
        { key: 'backtest_id', label: '回测' },
        { key: 'status', label: '状态' },
        { key: 'finished_at', label: '完成', fmt: function (v) { return EP.fmt.ts(v); } },
        { key: 'run_id', label: '操作', html: function (v) {
          return '<button class="btn sm" data-restore="' + EP.fmt.esc(v) + '">恢复</button>'; } }
      ], { height: 300, pageSize: PAGE, search: false });
      el.querySelectorAll('[data-restore]').forEach(function (b) {
        b.onclick = function () {
          EP.api('/api/expert-follow/runs/' + encodeURIComponent(b.dataset.restore) + '/archive',
            { method: 'POST', body: { archived: false } })
            .then(function () { EP.toast('已恢复'); archive(root); })
            .catch(function (e) { EP.toast(e.message, 'error'); });
        };
      });
    }).catch(function (e) { EP.toast(e.message, 'error'); });
  }

  /* ── ④ 数据体检 ── */
  function health(root) {
    root.innerHTML = '<div class="card"><h3>数据体检</h3>' +
      '<div class="card-sub">模拟盘（history_bulk）/ 实盘（rtV3 流水）数据可用性；实盘回测前会先过体检。</div>' +
      '<button class="btn" id="es-h-refresh">🔄 重新体检</button><div id="es-h" style="margin-top:10px"></div></div>';
    root.querySelector('#es-h-refresh').onclick = function () {
      EP.api('/api/expert-follow/data-health/refresh', { method: 'POST' })
        .then(function () { EP.toast('体检已启动，稍后刷新'); setTimeout(function () { loadHealth(root); }, 8000); })
        .catch(function (e) { EP.toast(e.message, 'error'); });
    };
    loadHealth(root);
  }

  function loadHealth(root) {
    EP.api('/api/expert-follow/data-health').then(function (d) {
      var el = root.querySelector('#es-h');
      if (!el) return;
      if (!d.generated_at) { el.innerHTML = '<div class="empty">还没有体检结果，点「重新体检」</div>'; return; }
      var html = '<div class="hint">生成于 ' + EP.fmt.ts(d.generated_at) + '</div>';
      [['sim', '模拟盘'], ['live', '实盘']].forEach(function (pair) {
        var x = (d.envs || {})[pair[0]] || {};
        if (!Object.keys(x).length) return;
        html += '<div style="margin-bottom:8px"><b>' + pair[1] + '</b> ' +
          EP.kvCards([['可用', x.ok ? '✓' : '✗'], ['说明', x.reason || x.source || '—'],
            ['组合数', x.n_experts != null ? x.n_experts : '—']]) + '</div>';
      });
      el.innerHTML = html;
    }).catch(function (e) { EP.toast(e.message, 'error'); });
  }

  /* ── 入口 ── */
  EP.loadStrategy = function (root) {
    state.charts.forEach(function (c) { try { c.dispose(); } catch (e) {} });
    state.charts = [];
    root.innerHTML = shell();
    var body = root.querySelector('#es-body');
    var show = function (i) {
      state.sub = i;
      root.querySelectorAll('.subtabs a').forEach(function (a) {
        a.classList.toggle('active', parseInt(a.dataset.sub, 10) === i);
      });
      [overview, lab, archive, health][i](body);
    };
    root.querySelectorAll('.subtabs a').forEach(function (a) {
      a.onclick = function () { show(parseInt(a.dataset.sub, 10)); };
    });
    show(0);
  };
})();
