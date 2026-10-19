/* 账户中心（独立页 #/accounts）：东财扫码登录 · 实盘令牌管理 · 心跳保活 */
(function () {
  'use strict';
  var EP = window.EP;
  if (!EP) return;

  /* ── 登录态徽章 ─────────────────────────────────────────────────────── */

  function loadStatus(root) {
    var box = root.querySelector('#ac-status');
    EP.api('/api/broker/check').then(function (d) {
      if (!d.ok) { box.innerHTML = EP.badge('检测失败', 'gray'); return; }
      if (d.logged_in) {
        box.innerHTML = EP.badge('✅ 在线 · ' + EP.fmt.esc(d.alias || d.account_id || '') + ' · 实盘令牌齐', 'green') +
          '<span class="hint" style="margin-left:8px">每日爬取会自动把关注列表里的实盘高手并入 rtV3 扫描</span>';
      } else if (d.reason === 'no_tokens') {
        box.innerHTML = EP.badge('⚠️ 已登录但缺实盘令牌（建议重新扫码）', 'orange');
      } else {
        box.innerHTML = EP.badge('❌ 未登录（实盘明细不可用）', 'red') +
          '<span class="hint" style="margin-left:8px">在下方扫码登录后自动捕获令牌</span>';
      }
    }).catch(function (e) {
      box.innerHTML = EP.badge('检测失败：' + e.message, 'gray');
    });
  }

  /* ── 扫码登录（页内流程，非弹框） ───────────────────────────────────── */

  function loginCard(root) {
    var body = root.querySelector('#ac-login-body');
    var sid = null, timer = null;

    function stopPoll() { if (timer) { clearInterval(timer); timer = null; } }

    function cancelSession() {
      if (sid) { EP.api('/api/broker/login/' + sid + '/cancel', { method: 'POST' }).catch(function () {}); sid = null; }
    }

    function fail(msg) {
      stopPoll(); sid = null;
      body.innerHTML = '<p style="color:var(--down,#390)">❌ ' + EP.fmt.esc(msg) + '</p>' +
        '<button class="btn" id="ac-retry">重试扫码</button>';
      body.querySelector('#ac-retry').onclick = initial;
    }

    function poll() {
      EP.api('/api/broker/login/' + sid + '/snapshot').then(function (d) {
        if (d.status === 'success') {
          stopPoll(); sid = null;
          body.innerHTML = '<p style="color:var(--up,#c00);font-weight:600">✅ ' +
            EP.fmt.esc(d.detail || '登录成功，实盘令牌已捕获') + '</p>';
          EP.toast('东财登录成功，实盘令牌已捕获');
          loadStatus(root); loadAccounts(root);
          return;
        }
        if (d.status === 'failed' || d.status === 'cancelled') {
          fail(d.error || d.detail || '登录失败'); return;
        }
        body.innerHTML = '<div style="text-align:center">' +
          '<img alt="登录二维码" src="data:image/png;base64,' + d.image + '" ' +
          'style="max-width:100%;max-height:340px;border:1px solid var(--line,#ddd);border-radius:6px">' +
          '<p class="hint" style="margin-top:8px">' + EP.fmt.esc(d.detail || d.status) + '</p>' +
          '<button class="btn" id="ac-cancel">取消</button></div>';
        body.querySelector('#ac-cancel').onclick = function () {
          stopPoll(); cancelSession(); initial();
        };
      }).catch(function (e) { stopPoll(); fail(e.message); });
    }

    function startQr() {
      var alias = (body.querySelector('#ac-alias') || {}).value || '东财账户';
      body.innerHTML = '<p class="hint">正在打开东财登录页…（首次约需 5-8 秒）</p>';
      EP.api('/api/broker/login/start', { method: 'POST', body: { alias: alias } })
        .then(function (d) {
          if (!d.ok) { fail(d.error || '发起失败'); return; }
          sid = d.session_id;
          poll();
          timer = setInterval(poll, 1600);
        }).catch(function (e) { fail(e.message); });
    }

    function initial() {
      stopPoll(); cancelSession();
      body.innerHTML = '<div class="row">' +
        '<div class="field" style="flex:1"><label>账户备注名</label>' +
        '<input class="input" id="ac-alias" value="东财账户"></div>' +
        '<div class="field"><label>&nbsp;</label><button class="btn primary" id="ac-go">发起扫码登录</button></div></div>';
      body.querySelector('#ac-go').onclick = startQr;
      body.querySelector('#ac-alias').addEventListener('keydown', function (e) {
        if (e.key === 'Enter') startQr();
      });
    }

    return { initial: initial, dispose: function () { stopPoll(); cancelSession(); } };
  }

  /* ── 账户列表 ───────────────────────────────────────────────────────── */

  function loadAccounts(root) {
    var box = root.querySelector('#ac-list');
    EP.api('/api/broker/accounts').then(function (d) {
      var accs = d.accounts || [];
      if (!accs.length) {
        box.innerHTML = '<div class="job idle">暂无账户 —— 在上方「发起扫码登录」添加，扫码成功后自动入库并捕获实盘令牌。</div>';
        return;
      }
      box.innerHTML = '<table class="grid"><thead><tr><th>账户</th><th>状态</th><th>实盘令牌</th>' +
        '<th>上次心跳</th><th>操作</th></tr></thead><tbody>' +
        accs.map(function (a) {
          return '<tr><td>' + EP.fmt.esc(a.alias) + '<br><span class="hint">' +
            EP.fmt.esc(a.id) + '</span></td>' +
            '<td>' + (a.status === 'active' ? EP.badge('在线', 'green') : EP.badge(a.status, 'red')) + '</td>' +
            '<td>' + (a.has_tokens ? '✅' : '—') + '</td>' +
            '<td class="hint">' + EP.fmt.esc(a.last_heartbeat_at || '—') + '</td>' +
            '<td><button class="btn" data-hb="' + a.id + '">心跳</button> ' +
            '<button class="btn" data-del="' + a.id + '">删除</button></td></tr>';
        }).join('') + '</tbody></table>';
      box.querySelectorAll('[data-hb]').forEach(function (b) {
        b.onclick = function () {
          b.disabled = true; b.textContent = '…';
          EP.api('/api/broker/accounts/' + b.dataset.hb + '/heartbeat', { method: 'POST' })
            .then(function (r) {
              EP.toast(r.alive ? '在线，令牌已续期' : '登录态已失效（建议重新扫码）', r.alive ? 'ok' : 'error');
              loadAccounts(root); loadStatus(root);
            });
        };
      });
      box.querySelectorAll('[data-del]').forEach(function (b) {
        b.onclick = function () {
          EP.api('/api/broker/accounts/' + b.dataset.del, { method: 'DELETE' })
            .then(function () { EP.toast('已删除'); loadAccounts(root); loadStatus(root); });
        };
      });
    }).catch(function (e) { box.innerHTML = '<div class="job idle">加载失败：' + EP.fmt.esc(e.message) + '</div>'; });
  }

  /* ── 页面组装 ───────────────────────────────────────────────────────── */

  EP.loadAccounts = function (root) {
    root.innerHTML =
      '<div class="card"><h3>登录状态</h3>' +
      '<div id="ac-status"><span class="hint">检测中…</span></div>' +
      '<div class="card-sub" style="margin-top:8px">机制：无头浏览器打开东财统一登录 → 手机扫码 → 会话内自动捕获 rtV3 实盘令牌' +
      '（未捕获到时用 passport2 身份 cookie 兜底映射）→ 每 30 分钟心跳保活。' +
      '令牌用于 <b>spzhapi rtV3 实盘调仓明细</b>通道；Web Cookie 对该接口无效。</div></div>' +

      '<div class="card"><h3>扫码登录</h3><div id="ac-login-body"></div></div>' +

      '<div class="card"><h3>账户列表</h3><div id="ac-list"><p class="hint">加载中…</p></div></div>' +

      '<div class="card"><h3>说明</h3><div class="card-sub">' +
      '· 实盘扫描名单 = config <code>eastmoney.spzh_follow</code> ∪ 关注列表 live 条目（高手评估页一键关注）。<br>' +
      '· 模拟盘流水（spo_superior_detail）匿名即可抓，无需登录；登录只影响<b>实盘</b>明细采集。<br>' +
      '· 登录态失效时徽章变红，服务端每天最多自动重探 2 次；也可手动点「心跳」验证。</div></div>';

    var login = loginCard(root);
    login.initial();
    loadStatus(root);
    loadAccounts(root);
    /* 页面离开时清理扫码会话 */
    root._acDispose = login.dispose;
  };
})();
