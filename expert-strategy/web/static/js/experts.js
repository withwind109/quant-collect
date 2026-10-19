/* 高手评估页（#/experts）—— 2026-10-19 重构：渠道tab + 实盘高手榜(手动加载) +
   关注列表 + 高手能力评估。仅实盘链路（模拟盘爬取通道已整体删除，
   评估数据环境固定 live）。能力评估口径见 src/strategy/expert_eval.py。 */
(function () {
  'use strict';
  var EP = window.EP;
  var S = {
    channel: 'eastmoney',  // 当前渠道 tab（其余渠道暂未接入）
    sources: ['eastmoney'],
    env: 'live',           // 评估数据环境：仅实盘（模拟盘通道已删除）
    minSells: 3,
    watchOnly: true,       // 评估列表默认只看关注的高手
    fresh: {},             // expert_id -> 新鲜度 {last_trade_date,fetched_at,stale,...}
    boardRows: null,   // ① 榜当前渲染的榜单行（用于提示「已关注但不在榜」）
    eval: null,        // 最新一次评估结果（/evaluation）
    wl: {},            // expert_id -> {name,note,created_at,__mt}
    chart: null        // 明细抽屉的 ECharts 实例
  };
  var SRC_LABELS = { eastmoney: '东财' };
  var CHANNELS = [
    { s: 'eastmoney', ok: true },
    { s: 'tonghuashun', ok: false },
    { s: 'xueqiu', ok: false },
    { s: 'alipay', ok: false }
  ];
  /* ═══════════ 渠道 tab（仅东财可点，其余置灰） ═══════════ */

  function channelTabs() {
    return '<div class="card" style="padding:12px 16px"><div class="row" style="align-items:center;gap:0;margin:0">' +
      '<span class="hint" style="margin-right:12px">数据渠道</span>' +
      CHANNELS.map(function (t) {
        if (t.ok) return '<span class="chip on" style="font-size:14px;padding:6px 20px;font-weight:600">' + SRC_LABELS[t.s] + '</span>';
        return '<span class="chip" data-ch="' + t.s + '" title="暂未接入，敬请期待" style="opacity:.4;cursor:not-allowed">' + SRC_LABELS[t.s] + '</span>';
      }).join('') +
      '<span class="hint" style="margin-left:12px">其余渠道爬虫规划中——tab 先占位，点击无效</span></div></div>';
  }

  function bindChannelTabs(root) {
    root.querySelectorAll('[data-ch]').forEach(function (c) {
      c.onclick = function () { EP.toast(SRC_LABELS[c.dataset.ch] + ' 渠道暂未接入', 'error'); };
    });
  }

  /* ═══════════ ① 实盘高手榜（手动加载 · 累计收益 + 最近操作 + 活跃度）═══════════ */

  function boardPanel() {
    return '<div class="card"><h3>① 实盘高手榜 <span class="hint" style="font-weight:400">（东财 rtV1 日榜+总榜合并 · 前50 · 手动加载）</span></h3>' +
      '<div class="card-sub">累计收益来自<b>总榜</b>口径。最近操作来自本地实盘流水（rtV3）——只有<b>已关注</b>的组合才有明细，' +
      '未关注组合显示「无明细」不代表不活跃；判断活跃度请以关注后拉到的调仓为准。</div>' +
      '<div class="row" style="margin-top:10px">' +
      '<div class="field"><label>&nbsp;</label><button class="btn primary" id="eb-load">⬇ 加载最新</button></div>' +
      '<div class="field"><label>&nbsp;</label><button class="btn" id="eb-refresh">↻ 从本地刷新</button></div>' +
      '<div class="field"><label>榜单</label><div id="eb-chips">' +
      '<span class="chip on" data-board="total">总榜（累计收益）</span>' +
      '<span class="chip" data-board="day">日榜（当日收益）</span></div></div>' +
      '<span class="hint" id="eb-meta" style="margin-left:8px;align-self:flex-end"></span></div>' +
      '<div id="eb-progress" style="display:none;margin-top:8px">' +
      '<div style="background:var(--c-border);border-radius:6px;height:8px;overflow:hidden">' +
      '<div id="eb-bar" style="height:100%;width:0;background:var(--c-primary);transition:width .3s"></div></div>' +
      '<div class="hint" id="eb-progress-text" style="margin-top:4px"></div></div>' +
      '<div id="eb-table" style="margin-top:10px"><div class="empty">正在从本地加载数据…</div></div>' +
      '<div id="eb-offboard"></div></div>';
  }

  function opSummary(r) {
    if (!r.last_trade_date) return '<span class="hint">无明细</span>';
    var gap = r.trade_days_ago;
    var ops = (r.recent || []).map(function (t) {
      var side = t.action === '买入' ? '<span class="up">买</span>' : (t.action === '卖出' ? '<span class="down">卖</span>' : EP.fmt.esc(t.action));
      return side + ' ' + EP.fmt.esc(t.stock_name || t.stock_code);
    }).join('、');
    var ago = gap === 0 ? '今天' : (gap + ' 天前');
    return '<div style="font-size:12px">' + ops + '</div>' +
      '<div class="hint" style="font-size:11px">最近操作 ' + ago + '（' + EP.fmt.esc(r.last_trade_date) + '，共 ' + r.n_trades + ' 条流水）</div>';
  }

  function loadLiveExperts(root) {
    var box = root.querySelector('#eb-table');
    if (!box) return;
    var board = S.board || 'total';
    EP.api('/api/expert-crawl/live-experts?board=' + board + '&limit=50').then(function (d) {
      if (!d.rows || !d.rows.length) {
        root.querySelector('#eb-meta').textContent = '';
        box.innerHTML = '<div class="empty">本地暂无实盘榜数据——点「⬇ 加载最新」从东财拉取</div>';
        return;
      }
      root.querySelector('#eb-meta').textContent = '榜单取数日 ' + d.latest_date +
        (d.fetched_at ? ' · 拉取于 ' + d.fetched_at : '');
      var rows = d.rows.map(function (r, i) { r.rank = i + 1; return r; });
      EP.table(box, rows, [
        { key: 'rank', label: '#', num: true, fmt: num0 },
        { key: 'name', label: '组合', fmt: function (v, r) {
          return '<b>' + EP.fmt.esc(v || r.expert_id) + '</b>' +
            (S.wl[r.expert_id] && S.wl[r.expert_id].__mt === 'live' ? ' <span style="color:var(--c-danger)">★</span>' : '') +
            '<div class="hint" style="font-size:11px">' + EP.fmt.esc(r.expert_id) + (r.owner ? ' · ' + EP.fmt.esc(r.owner) : '') + '</div>'; } },
        { key: 'followers', label: '关注数', num: true, fmt: num0 },
        { key: 'return_rate', label: '累计收益%', num: true, fmt: function (v) {
          return v == null ? '<span class="hint">—</span>' : '<b class="' + (v >= 0 ? 'up' : 'down') + '">' + EP.fmt.signedPct(v) + '</b>'; } },
        { key: 'daily_gain', label: '当日%', num: true, fmt: EP.fmt.signedPct },
        { key: 'recent', label: '最近操作', fmt: opSummary },
        { key: 'trade_days_ago', label: '活跃度', fmt: function (v, r) {
          if (v == null) return EP.badge('无明细', 'gray');
          if (v <= 7) return EP.badge('活跃', 'green');
          if (v <= 20) return EP.badge('一般', 'orange');
          return EP.badge('沉寂', 'red'); } },
        { key: 'expert_id', label: '操作', html: function (v, r) {
          var watched = !!(S.wl[v] && S.wl[v].__mt === 'live');
          return '<button class="btn sm ' + (watched ? '' : 'primary') + '" data-eb-watch="' + EP.fmt.esc(v) + '" data-name="' + EP.fmt.esc(r.name || '') + '">' +
            (watched ? '✓ 已关注' : '＋ 关注') + '</button>' +
            (watched ? ' <button class="btn sm" data-eb-fetch="' + EP.fmt.esc(v) + '">拉明细</button>' : ''); } }
      ], { height: 560, pageSize: 50 });
      S.boardRows = rows;
      renderOffboard(root);
    }).catch(function (e) { box.innerHTML = '<div class="hint">' + EP.fmt.esc(e.message) + '</div>'; });
  }

  /* 已关注但不在榜：榜单只覆盖东财排行前几百，不在榜 ≠ 不存在（用户常在这里迷路） */
  function renderOffboard(root) {
    var offEl = root.querySelector('#eb-offboard');
    if (!offEl) return;
    var onBoard = {};
    (S.boardRows || []).forEach(function (r) { onBoard[r.expert_id] = 1; });
    var off = Object.keys(S.wl).filter(function (id) {
      return S.wl[id] && S.wl[id].__mt === 'live' && !onBoard[id];
    });
    offEl.innerHTML = off.length
      ? '<div class="hint" style="margin-top:6px">另有 <b>' + off.length + '</b> 个已关注组合不在本榜（榜单只含东财排行前几百）：' +
        off.map(function (id) {
          return EP.fmt.esc((S.wl[id] && S.wl[id].name) || id) + '（' + EP.fmt.esc(id) + '）';
        }).join('、') + '——它们仍会参与评估，取数情况见 ② 关注列表。</div>'
      : '';
  }

  function setProgress(root, pct, text) {
    var box = root.querySelector('#eb-progress');
    if (!box) return;
    box.style.display = text ? 'block' : 'none';
    root.querySelector('#eb-bar').style.width = pct + '%';
    root.querySelector('#eb-progress-text').textContent = text || '';
  }

  /* 手动加载最新：分两步直拉东财（日榜→总榜），每步上报进度 */
  function loadLatest(root) {
    var btn = root.querySelector('#eb-load');
    if (btn.disabled) return;
    btn.disabled = true;
    var steps = [{ board: 'day', label: '拉取日榜' }, { board: 'total', label: '拉取总榜' }];
    var i = 0;
    setProgress(root, 4, '连接东财 rtV1…');
    (function next() {
      if (i >= steps.length) {
        setProgress(root, 100, '完成，已更新本地榜单');
        EP.toast('榜单已更新');
        loadLiveExperts(root);
        setTimeout(function () { setProgress(root, 0, ''); }, 1500);
        btn.disabled = false;
        return;
      }
      var st = steps[i];
      setProgress(root, Math.round(i / steps.length * 100), st.label + '…');
      EP.api('/api/expert-crawl/live-experts/refresh', {
        method: 'POST', body: { board: st.board, limit: 100 }
      }).then(function (d) {
        setProgress(root, Math.round((i + 1) / steps.length * 100),
          st.label + '：获取 ' + d.fetched + ' 位（更新 ' + d.updated + ' · 新增 ' + d.inserted + '）');
        i++; next();
      }).catch(function (e) {
        setProgress(root, 0, '');
        btn.disabled = false;
        EP.toast(e.message, 'error');
      });
    })();
  }

  function bindLiveExperts(root) {
    root.querySelectorAll('#eb-chips .chip').forEach(function (c) {
      c.onclick = function () {
        root.querySelectorAll('#eb-chips .chip').forEach(function (x) { x.classList.remove('on'); });
        c.classList.add('on');
        S.board = c.dataset.board;
        loadLiveExperts(root);
      };
    });
    root.querySelector('#eb-load').onclick = function () { loadLatest(root); };
    root.querySelector('#eb-refresh').onclick = function () { loadLiveExperts(root); };
    root.querySelector('#eb-table').addEventListener('click', function (e) {
      var w = e.target.closest('[data-eb-watch]'), f = e.target.closest('[data-eb-fetch]');
      if (w) {
        toggleWatch(w.dataset.ebWatch, root, 'live', w.dataset.name || '');
        setTimeout(function () { loadWatch(root); loadLiveExperts(root); }, 900);
      } else if (f) {
        fetchLiveDetail(root, f.dataset.ebFetch);
      }
    });
  }

  /* ── ② 关注列表（东财实盘，与东财账号关注关系双向同步）── */

  function watchPanel() {
    return '<div class="card"><h3>② 关注列表 <span class="hint" style="font-weight:400">（东财实盘 · rtV3 调仓明细仅对关注者开放）</span></h3>' +
      '<div class="card-sub">流程：爬榜单 → 在获取的高手里点「关注」→ 这里「拉取调仓明细」入库 → 实盘环境评估即可用。' +
      'live 关注会在<b>东财账号上代为关注</b>该组合（取消时同步取消）；也可以「同步东财关注」把 APP 里已关注的组合拉回来。</div>' +
      '<div class="card-sub" style="margin-top:6px">组合不在榜上（或 APP 里能看到、这里搜不到）时，直接填<b>组合号</b>手动关注——' +
      '榜单最多只覆盖东财排行前几百个组合，不在榜 ≠ 不存在。</div>' +
      '<div class="row" style="margin-top:10px">' +
      '<div class="field"><label>组合号</label><input class="input" id="ew-new-id" placeholder="如 900407488" style="width:150px"></div>' +
      '<div class="field"><label>备注名（可选）</label><input class="input" id="ew-new-name" placeholder="如 凡人炒股" style="width:150px"></div>' +
      '<div class="field"><label>&nbsp;</label><button class="btn primary" id="ew-add">＋ 手动关注</button></div>' +
      '<div class="field"><label>&nbsp;</label><button class="btn" id="ew-sync">↻ 同步东财关注</button></div>' +
      '<div class="field"><label>&nbsp;</label><button class="btn" id="ew-fetch">▶ 拉取全部关注调仓</button></div>' +
      '<span class="hint" id="ew-meta" style="margin-left:8px;align-self:flex-end"></span></div>' +
      '<div id="ew-probe" style="margin-top:6px"></div>' +
      '<div id="ew-table" style="margin-top:10px"></div>' +
      '<div id="ew-result" style="margin-top:10px"></div></div>';
  }

  /* 组合取数状态 → 一格人话（平台侧拒绝时给出原始 code，便于对账） */
  function comboStatusCell(v, r) {
    var code = r.status_code;
    if (code == null) return '<span class="hint">未探测</span>';
    if (code === 0) return EP.badge('正常', 'green');
    var tip = EP.fmt.esc((r.status_msg || '') + (r.status_at ? '（' + r.status_at + ' 探测）' : ''));
    var short = { 30001: '未授权公开', 9061: '组合不存在', 9062: '组合已隐藏',
                  9035: '未关注', '-1': '请求异常' }[String(code)] || (r.status_msg || '失败');
    return '<span title="' + tip + '">' + EP.badge('⛔ ' + short, 'red') + '</span>' +
      '<div class="hint" style="font-size:11px">code ' + EP.fmt.esc(String(code)) + '</div>';
  }

  function renderWatchTable(root) {
    var el = root.querySelector('#ew-table');
    if (!el) return;
    var live = Object.keys(S.wl).filter(function (id) { return S.wl[id] && S.wl[id].__mt === 'live'; });
    if (!live.length) {
      el.innerHTML = '<div class="hint">关注列表为空——在上面填组合号手动关注，或在评估表里点「＋ 关注」，或「同步东财关注」</div>';
      return;
    }
    var rows = live.map(function (id) {
      var w = S.wl[id];
      return { expert_id: id, name: w.name || id, note: w.note || '', created_at: w.created_at || '',
               status_code: w.status_code, status_msg: w.status_msg, status_at: w.status_at };
    });
    EP.table(el, rows, [
      { key: 'name', label: '组合', fmt: function (v, r) {
        return '<b>' + EP.fmt.esc(v) + '</b>' +
          (!v || v === r.expert_id ? ' <span class="hint" style="font-size:11px">（平台未提供名称）</span>' : ''); } },
      { key: 'expert_id', label: '组合号' },
      { key: 'status_code', label: '取数状态', fmt: function (v, r) { return comboStatusCell(v, r); } },
      { key: 'note', label: '来源', fmt: function (v) {
        var t = String(v || ''); 
        return t.indexOf('东财拒绝') === 0
          ? '<span class="down" title="' + EP.fmt.esc(t) + '">关注被拒</span>'
          : EP.fmt.esc(v || '—'); } },
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
      var blocked = d.blocked || [];
      var bHtml = '';
      if (blocked.length) {
        bHtml = '<div class="hint" style="color:var(--c-danger);margin-bottom:6px">⚠️ ' + blocked.length +
          ' 个组合被平台拒绝取数：' + blocked.map(function (b) {
            return '<b>' + EP.fmt.esc(b.name || b.expert_id) + '</b>（' + EP.fmt.esc(String(b.code)) +
              ' ' + EP.fmt.esc(b.message || '') + '）';
          }).join('；') + '</div>';
      }
      if (!d.rows || !d.rows.length) {
        box.innerHTML = bHtml + '<div class="hint">没有拉到调仓记录' +
          (bHtml ? '——原因见上（组合被主人隐藏/未授权公开时，rtV3 永远不会返回数据）。' : '（今日无调仓或组合异常）。') + '</div>';
        loadWatch(root);
        return;
      }
      box.innerHTML = bHtml + '<div class="hint">本次拉取 <b>' + d.total + '</b> 条（新入库 ' + d.n + ' 条，重复自动去重）</div><div id="ew-dtable"></div>';
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

  /* 手动按组合号关注：先探测（组合是否存在/可取数），再落到关注列表。
     解决「APP 里能看到这个人、榜上却没有 → 页面根本没法关注」的问题。 */
  function manualAddWatch(root) {
    var idEl = root.querySelector('#ew-new-id'), nmEl = root.querySelector('#ew-new-name');
    var box = root.querySelector('#ew-probe');
    var eid = (idEl.value || '').trim();
    if (!/^\d{4,}$/.test(eid)) { EP.toast('请填纯数字组合号，如 900407488', 'error'); return; }
    box.innerHTML = '<div class="hint">探测组合 ' + EP.fmt.esc(eid) + ' …</div>';
    EP.api('/api/expert-crawl/watchlist/probe?expert_id=' + encodeURIComponent(eid)).then(function (p) {
      var verdict = (p.code === 0)
        ? '<span class="up">可取数（' + p.n_rows + ' 条）</span>'
        : '<span class="down">⛔ ' + EP.fmt.esc(p.message || '取数被拒') + '（code ' + EP.fmt.esc(String(p.code)) + '）</span>';
      box.innerHTML = '<div class="hint">组合：<b>' + EP.fmt.esc(p.name || eid) + '</b>' +
        (p.owner ? ' · 主人 ' + EP.fmt.esc(p.owner) : '') +
        (p.followers ? ' · 关注 ' + EP.fmt.esc(String(p.followers)) : '') + ' · ' + verdict +
        (p.code === 0 ? '' : '<br>仍可加入关注列表（本地保留，平台放开隐私后会自动恢复取数）。') + '</div>';
      return EP.api('/api/expert-crawl/watchlist', { method: 'POST',
        body: { expert_id: eid, name: (nmEl.value || '').trim() || p.name || '', market_type: 'live' } });
    }).then(function (d) {
      if (!d) return;
      if (d.em && !d.em.ok) EP.toast('已加入关注列表，但' + d.em.msg, 'error');
      else EP.toast('已关注并在东财账号同步');
      idEl.value = ''; nmEl.value = '';
      loadWatch(root);
    }).catch(function (e) { box.innerHTML = '<div class="hint">' + EP.fmt.esc(e.message) + '</div>'; });
  }

  function bindWatchCard(root) {
    root.querySelector('#ew-add').onclick = function () { manualAddWatch(root); };
    ['#ew-new-id', '#ew-new-name'].forEach(function (sel) {
      var el = root.querySelector(sel);
      if (el) el.onkeydown = function (e) { if (e.key === 'Enter') manualAddWatch(root); };
    });
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

  /* ═══════════ ③ 高手能力评估（仅实盘 rtV3 流水）═══════════ */

  function evalPanel() {
    return '<div class="card"><h3>③ 高手能力评估 <span class="hint" style="font-weight:400">（数据环境：实盘 rtV3 流水 · 模拟盘通道已下线）</span></h3>' +
      '<div class="card-sub">逐高手镜像回测（= 无脑跟他的单）：T+1 开盘 ±0.2% 滑点、佣金万 0.8（最低 5 元）、卖出印花税 0.05%、涨停拦截、噪音过滤。窗口从该高手首笔信号日起。按综合评分（夏普/索提诺/Calmar/胜率 分位秩平均）排序——分高且样本充足的，就是值得跟的人。</div>' +
      '<div class="card-sub">⚠️ <b>口径说明</b>：rtV3 只返回最近 40 条调仓（约覆盖 1 个月），本表评估的就是这个窗口；东财 APP 显示的总收益/近1年/近一月是组合成立以来的累计口径，包含窗口外的收益与持仓，<b>与本表不可直接对比</b>。本表衡量的是「T+1 跟单能吃到的那部分」——日内抢帽子型高手（如财神咒）的日内价差跟不到，镜像收益显著低于 APP 展示属正常现象。</div>' +
      '<div class="row">' +
      '<div class="field"><label>最少卖出笔数</label><select class="select" id="ee-min-sells">' +
      [1, 3, 5, 10, 20].map(function (n) { return '<option value="' + n + '"' + (n === 3 ? ' selected' : '') + '>' + n + '</option>'; }).join('') +
      '</select></div>' +
      '<div class="field"><label>&nbsp;</label><label style="display:flex;align-items:center;gap:6px;cursor:pointer">' +
      '<input type="checkbox" id="ee-watch-only" checked> 只看关注</label></div>' +
      '<div class="field"><label>&nbsp;</label><button class="btn primary" id="ee-run">▶ 开始评估</button>' +
      '<button class="btn" id="ee-refresh" style="margin-left:6px">刷新结果</button></div>' +
      '</div><div id="ee-stale"></div><div id="ee-progress"></div><div id="ee-summary"></div>' +
      '<div id="ee-filter-hint"></div><div id="ee-table"></div></div>';
  }

  function num0(v) { return EP.fmt.num(v, 0); }
  function f2(v) { return v == null ? '—' : Number(v).toFixed(2); }

  /* 数据新鲜度单元格：最近调仓日期 + 最近一次 rtV3 拉取时间，过旧标红 */
  function freshCell(v) {
    var w = S.wl[v];
    /* 平台侧拒绝取数（未授权公开/已隐藏/不存在）优先提示——否则「未拉取」会把
       「平台不给」误导成「我们没拉」 */
    if (w && w.status_code != null && w.status_code !== 0) {
      var short = { 30001: '未授权公开持仓', 9061: '组合不存在', 9062: '组合已隐藏',
                    9035: '未关注', '-1': '请求异常' }[String(w.status_code)] || (w.status_msg || '取数失败');
      return EP.badge('⛔ 取数受阻', 'red') +
        '<div class="hint" style="font-size:11px">' + EP.fmt.esc(short) +
        '（code ' + EP.fmt.esc(String(w.status_code)) + '）</div>';
    }
    var f = S.fresh[v];
    if (!f) return '<span class="hint">—</span>';
    if (!f.fetched_at) return EP.badge('未拉取', 'red');
    var ago = '';
    if (f.hours_since_fetch != null) {
      ago = f.hours_since_fetch < 1 ? Math.max(1, Math.round(f.hours_since_fetch * 60)) + '分钟前'
          : (f.hours_since_fetch < 24 ? f.hours_since_fetch.toFixed(1) + '小时前'
          : Math.round(f.hours_since_fetch / 24) + '天前');
    }
    var html = EP.badge(f.stale ? '过旧' : '已更新', f.stale ? 'red' : 'green') +
      '<div style="font-size:12px;margin-top:2px">最近调仓 ' + EP.fmt.esc(f.last_trade_date || '—') +
      (f.days_since_trade != null ? '（' + f.days_since_trade + ' 天前）' : '') + '</div>' +
      '<div class="hint" style="font-size:11px">拉取于 ' + EP.fmt.esc(f.fetched_at) + (ago ? '（' + ago + '）' : '') + '</div>';
    return html;
  }

  var EVAL_COLS = [
    { key: 'rank', label: '#', num: true, fmt: num0 },
    { key: 'name', label: '高手', fmt: function (v, r) {
      var w = S.wl[r.expert_id];
      var blocked = !!(r.no_data && w && w.status_code != null && w.status_code !== 0);
      return '<b>' + EP.fmt.esc(v || r.expert_id) + '</b>' +
        (r.no_data ? ' ' + EP.badge(blocked ? '平台拒绝取数' : '未拉取数据', blocked ? 'red' : 'gray') : ''); } },
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
    { key: 'first_date', label: '评估窗口', fmt: function (v, r) {
      if (!v) return '—';
      var sameYear = String(v).slice(0, 4) === String(r.last_date || '').slice(0, 4);
      var a = sameYear ? String(v).slice(5) : String(v), b = String(r.last_date || '').slice(5);
      return '<span class="hint" style="font-size:12px" title="' + EP.fmt.esc(v + ' ~ ' + r.last_date) + '">' +
        EP.fmt.esc(a + ' ~ ' + b) + '</span>'; } },
    { key: 'trading_days', label: '覆盖天数', num: true, fmt: num0 },
    { key: 'sample_ok', label: '样本', fmt: function (v) { return v ? EP.badge('充足', 'green') : EP.badge('不足', 'gray'); } },
    { key: 'expert_id', label: '数据新鲜度', fmt: freshCell },
    { key: 'expert_id', label: '操作', html: function (v) {
      var watched = !!S.wl[v];
      return '<button class="btn sm ' + (watched ? '' : 'primary') + '" data-watch="' + EP.fmt.esc(v) + '">' +
        (watched ? '✓ 已关注' : '＋ 关注') + '</button> ' +
        '<button class="btn sm" data-detail="' + EP.fmt.esc(v) + '">明细</button>' +
        (watched ? ' <button class="btn sm" data-efetch="' + EP.fmt.esc(v) + '" title="重新拉取该高手的 rtV3 调仓明细">⟳ 更新</button>' : ''); } }
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

  /* 调仓数据比评估快照新 → 提示重算（评估结果是静态快照，不会自动跟着更新） */
  function updateStaleHint(root) {
    var el = root.querySelector('#ee-stale');
    if (!el) return;
    el.innerHTML = '';
    if (!S.eval || !S.eval.generated_at) return;
    var gen = String(S.eval.generated_at).replace('T', ' ').slice(0, 19);
    var newer = Object.keys(S.fresh).some(function (id) {
      var f = S.fresh[id];
      return f && f.fetched_at && String(f.fetched_at).replace('T', ' ').slice(0, 19) > gen;
    });
    if (newer) {
      el.innerHTML = '<div class="hint" style="color:var(--c-danger);margin-bottom:6px">' +
        '关注组合有新拉取的调仓数据尚未计入评估（评估是静态快照）——请点「▶ 开始评估」重算。</div>';
    }
  }

  function applyFilters() {
    var t = EP._tables['ee-table'];
    if (!t || !S.eval) return;
    var min = parseInt(S.minSells, 10) || 1;
    var hasWatch = Object.keys(S.wl).some(function (id) { return S.wl[id] && S.wl[id].__mt === 'live'; });
    var all = evalRows(S.eval);
    var filtered = all.filter(function (r) {
      var watchOk = !S.watchOnly || !hasWatch || (S.wl[r.expert_id] && S.wl[r.expert_id] && S.wl[r.expert_id].__mt === 'live');
      if (r.no_data) return watchOk;          // 占位行只受「只看关注」影响
      return watchOk && (r.n_sell || 0) >= min;
    });
    t.setRows(filtered);
    /* 过滤把结果全藏起来时给出原因提示，避免"完成后表格无内容"的困惑 */
    var hint = document.querySelector('#ee-filter-hint');
    if (hint) {
      if (!filtered.length && all.length) {
        var why = [];
        if (S.watchOnly) why.push('只看关注');
        why.push('卖出 ≥ ' + min + ' 笔');
        hint.innerHTML = '<div class="hint" style="margin-top:6px">⚠️ 共 ' + all.length +
          ' 条结果，但被当前筛选（' + why.join(' + ') + '）全部隐藏——取消上方勾选或降低卖出笔数门槛即可看到。</div>';
      } else {
        hint.innerHTML = '';
      }
    }
  }

  /* 关注但未出现在评估结果里的高手 → 占位行（未拉取调仓数据）。
     保证"关注了就一定能在评估列表看到"，而不是悄悄消失 */
  function evalRows(d) {
    var have = {};
    (d.rows || []).forEach(function (r) { have[r.expert_id] = 1; });
    ((d.meta || {}).skipped || []).forEach(function (x) { have[x.expert_id] = 1; });
    var extra = [];
    Object.keys(S.wl).forEach(function (id) {
      if (S.wl[id] && S.wl[id].__mt === 'live' && !have[id]) {
        extra.push({ expert_id: id, name: S.wl[id].name || id, source: 'eastmoney',
                     market_type: 'live', no_data: true, rank: null,
                     total_return_pct: null, annualized_pct: null, sharpe: null,
                     sortino: null, max_drawdown_pct: null, calmar: null,
                     win_rate_pct: null, profit_factor: null, n_sell: null,
                     trading_days: null, sample_ok: false });
      }
    });
    return (d.rows || []).concat(extra);
  }

  function renderEval(root, d) {
    S.eval = d;
    var sum = root.querySelector('#ee-summary'), box = root.querySelector('#ee-table');
    if (!d.ok || (!d.rows.length && !evalRows(d).length)) {
      sum.innerHTML = '';
      box.innerHTML = '<div class="empty">' + EP.fmt.esc(d.msg || '暂无评估结果') + '</div>';
      return;
    }
    var envNote = (d.params && d.params.env !== S.env)
      ? '<div class="hint" style="margin-bottom:6px">当前展示的是 <b>' + EP.fmt.esc(d.params.env) +
        '</b> 环境的结果（门槛：卖出≥' + d.params.min_sells + '、覆盖≥' + d.params.min_days + ' 天）</div>' : '';
    var m = d.meta || {};
    var skipNote = '';
    if (m.n_skipped > 0) {
      var sk = (m.skipped || []).slice(0, 4).map(function (x) {
        return EP.fmt.esc(x.name) + '（' + EP.fmt.esc(x.reason) + '）';
      }).join('；');
      if ((m.skipped || []).length > 4) sk += ' 等';
      skipNote = '<div class="hint" style="margin-top:6px">另有 <b>' + m.n_skipped + '</b> 位组合未参与评估：' +
        sk + '。常见原因：rtV3 只有最近 40 条（卖出早于数据起点 → 起点持仓不明），或平台隐私隐藏。</div>';
    }
    sum.innerHTML = envNote + evalSummary(d) + skipNote;
    EP.table(box, evalRows(d), EVAL_COLS, { height: 520, pageSize: 50, sortKey: 'rank', sortDir: 1 });
    applyFilters();
    updateStaleHint(root);
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
                              created_at: r.created_at || '', __mt: r.market_type === 'live' ? 'live' : 'sim',
                              status_code: r.status_code, status_msg: r.status_msg || '',
                              status_at: r.status_at || '' };
      });
      renderWatchlist(root);
      renderWatchTable(root);
      renderOffboard(root);   // 关注变化后同步「不在榜」提示
      loadFresh(root);
    }).catch(function () {});
  }

  /* 关注组合调仓数据新鲜度（评估表「数据新鲜度」列 + 过旧提醒） */
  function loadFresh(root) {
    return EP.api('/api/expert-crawl/watchlist/freshness').then(function (d) {
      S.fresh = {};
      (d.rows || []).forEach(function (r) { S.fresh[r.expert_id] = r; });
      applyFilters();   // setRows 会整表重渲染，新鲜度列随之刷新
      renderWatchTable(root);
      updateStaleHint(root);
    }).catch(function () {});
  }

  /* 单个关注高手手动更新 rtV3 调仓明细（评估表行内「⟳ 更新」） */
  function updateExpertTrades(root, expertId) {
    var nm = (S.wl[expertId] && S.wl[expertId].name) || expertId;
    EP.toast('正在拉取「' + nm + '」的 rtV3 调仓…');
    EP.api('/api/expert-crawl/watchlist/fetch-detail',
           { method: 'POST', body: { expert_id: expertId } })
      .then(function (d) {
        EP.toast('更新完成：拉到 ' + d.total + ' 条，新入库 ' + d.n + ' 条');
        loadFresh(root);
      }).catch(function (e) { EP.toast(e.message, 'error'); });
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

  function toggleWatch(expertId, root, mtOverride, nameOverride) {
    var watched = !!S.wl[expertId];
    var mt = mtOverride || (S.env === 'live' ? 'live' : 'sim');
    var row = (S.eval && S.eval.rows || []).filter(function (r) { return r.expert_id === expertId; })[0] || {};
    var nm = nameOverride || row.name || '';
    var req = watched
      ? EP.api('/api/expert-crawl/watchlist/' + encodeURIComponent(expertId) + '?market_type=' + mt, { method: 'DELETE' })
      : EP.api('/api/expert-crawl/watchlist', { method: 'POST',
          body: { expert_id: expertId, name: nm, market_type: mt } });
    req.then(function (d) {
      if (d && d.em && d.em.msg) { EP.toast(d.em.msg, d.em.ok ? 'ok' : 'error'); }
      if (watched) delete S.wl[expertId];
      else S.wl[expertId] = { name: nm, note: '', created_at: '', __mt: mt };
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
      var health = d.data_health || {};
      var hNote = health.reason
        ? '<div class="hint" style="margin-top:4px">数据体检：' + EP.fmt.esc(health.reason) +
          '（原始 ' + (health.n_records || 0) + ' 条 → 可用 ' + (health.n_used || 0) + ' 条，卖出后幽灵仓位 ' +
          (health.phantom_end || 0) + '）</div>' : '';
      drawer.innerHTML =
        '<div style="display:flex;justify-content:space-between;align-items:flex-start">' +
        '<div><h3 style="font-size:16px">' + EP.fmt.esc(d.name || d.expert_id) + '</h3>' +
        '<div class="hint">' + EP.fmt.esc(d.expert_id) + ' · ' + EP.fmt.esc(SRC_LABELS[d.source] || d.source || '—') +
        ' · ' + (d.env === 'live' ? '实盘' : '模拟盘') + '</div>' + hNote + '</div>' +
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
      var w = e.target.closest('[data-watch]'), d = e.target.closest('[data-detail]'),
          ef = e.target.closest('[data-efetch]');
      if (w) { toggleWatch(w.dataset.watch, root); }
      else if (d) { showDetail(root, d.dataset.detail); }
      else if (ef) { updateExpertTrades(root, ef.dataset.efetch); }
    });
    root.querySelector('#ee-run').onclick = function () {
      var btn = this; btn.disabled = true;
      S.minSells = root.querySelector('#ee-min-sells').value;
      EP.api('/api/expert-crawl/evaluation/run', {
        method: 'POST',
        body: { min_sells: parseInt(S.minSells, 10) || 3, min_days: 20 }
      }).then(function () {
        EP.toast('评估已启动（完成后自动刷新）');
        /* 进度：转圈 + 已耗时秒数，每秒更新；完成后显示耗时并自动刷新结果 */
        var el = root.querySelector('#ee-progress'), t0 = Date.now(), secs = 0;
        function prog(html) { if (el) el.innerHTML = html; }
        var tick = setInterval(function () {
          secs = Math.round((Date.now() - t0) / 1000);
          prog('<div class="hint" style="margin-top:8px"><span class="spin">⏳</span> 评估进行中… 已运行 <b>' +
            secs + '</b> 秒（逐高手镜像回测，通常十几秒内完成）</div>');
        }, 1000);
        var timer = setInterval(function () {
          EP.api('/api/expert-crawl/status').then(function (d) {
            var st = d.expert_eval && d.expert_eval.state;
            if (!st || st === 'running') return;
            clearInterval(timer); clearInterval(tick);
            btn.disabled = false;
            if (st === 'error') {
              prog('<div style="color:var(--c-danger);margin-top:8px">❌ 评估失败（服务端日志见 logs/，请重试）</div>');
              EP.toast('评估失败', 'error'); return;
            }
            var cost = Math.round((Date.now() - t0) / 1000);
            prog('<div class="hint" style="margin-top:8px">✅ 评估完成，耗时 ' + cost + ' 秒</div>');
            loadEval(root);
            EP.toast('评估完成');
          }).catch(function () {});
        }, 2000);
      }).catch(function (e) { EP.toast(e.message, 'error'); btn.disabled = false; });
    };
    root.querySelector('#ee-refresh').onclick = function () { loadEval(root); };
    root.querySelector('#ee-watch-only').onchange = function () { S.watchOnly = this.checked; applyFilters(); };
    root.querySelector('#ee-min-sells').onchange = function () { S.minSells = this.value; applyFilters(); };
  }

  EP.loadExperts = function (root) {
    /* 2026-10-19 二次重构：渠道tab → ①实盘高手榜(手动加载) → ②关注列表 → ③能力评估；
       榜单数据持久化在 crawl_expert（带 fetched_at），页面加载即从本地渲染——
       刷新不丢；「加载最新」只用于从东财拉新。rtV3 调仓由调度器自动累积（启动+每8h），
       评估列表默认只看关注 + 数据新鲜度 + 行内手动更新。 */
    root.innerHTML = channelTabs() + boardPanel() + watchPanel() + evalPanel();
    bindChannelTabs(root);
    bindLiveExperts(root);
    loadLiveExperts(root);           // 榜单持久化在本地，进页面直接渲染
    bindWatchCard(root);
    bindEval(root);
    loadWatch(root);
    loadEval(root);
  };
})();
