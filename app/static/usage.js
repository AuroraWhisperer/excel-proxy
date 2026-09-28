const $ = id => document.getElementById(id);
const number = value => Number(value || 0).toLocaleString('zh-CN');
const date = value =>
  value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
const state = {
  loading: false,
  accountBusy: false,
  globalAction: false,
  pendingAccounts: new Map(),
  accountMessages: new Map(),
  accounts: [],
  selectedId: null,
  statsWindow: null,
  periodSyncFailed: false,
  accountRevision: 0,
  lastQuotaAttempt: 0,
  loginStatus: 'idle',
  refreshingId: null,
  refreshTotal: 0,
  refreshProgress: 0
};
let loginTimer,
  loginPolling = false,
  usageRenderKey,
  lastActionFocus,
  accountQueue = Promise.resolve();
function setText(element, text) {
  if (element.textContent !== String(text)) element.textContent = text;
}
function feedback(id, text, error = false) {
  setText($(id), text);
  $(id).classList.toggle('error', error);
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: 'no-store',
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers }
  });
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(
      payload.message ||
        payload.error?.message ||
        (typeof payload.detail === 'string'
          ? payload.detail
          : `请求失败（HTTP ${response.status}）`)
    );
    error.status = response.status;
    error.category = payload.category;
    throw error;
  }
  return payload;
}
const usd = value =>
  Number.isFinite(value)
    ? '$' +
      value.toLocaleString('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 4
      })
    : '—';
const post = (path, body = {}) =>
  api(path, { method: 'POST', body: JSON.stringify(body) });
function accountRowControls(item, row) {
  const pending = state.pendingAccounts.get(row.id);
  const busy = state.globalAction || (!!pending && !pending.silent);
  if (item.getAttribute('aria-busy') !== String(busy))
    item.setAttribute('aria-busy', String(busy));
  for (const button of item.querySelectorAll('button')) {
    const action = button.dataset.accountAction;
    const disabled =
      action !== 'view' &&
      (busy || (action === 'reset' && !canResetAccount(row)));
    if (button.disabled !== disabled) button.disabled = disabled;
    let label = button.dataset.label;
    if (pending?.action === action && !pending.silent) {
      label = pending.running ? '查询中…' : '等待中…';
      if (pending.running && action === 'remove') label = '移除中…';
      if (pending.running && action === 'reset') label = '处理中…';
      if (pending.running && action === 'test') label = '测试中…';
    } else if (
      state.globalAction &&
      state.refreshingId === row.id &&
      action === 'refresh'
    )
      label = '查询中…';
    setText(button, label);
    if (
      action === 'view' &&
      button.getAttribute('aria-pressed') !==
        String(row.id === state.selectedId)
    )
      button.setAttribute('aria-pressed', String(row.id === state.selectedId));
  }
}
function accountControls() {
  const busy =
    state.globalAction ||
    [...state.pendingAccounts.values()].some(pending => !pending.silent);
  if (
    $('account-panel').getAttribute('aria-busy') !== String(state.globalAction)
  )
    $('account-panel').setAttribute('aria-busy', String(state.globalAction));
  if ($('import-accounts').disabled !== busy)
    $('import-accounts').disabled = busy;
  const refreshDisabled = busy || state.accounts.length === 0;
  if ($('refresh-accounts').disabled !== refreshDisabled)
    $('refresh-accounts').disabled = refreshDisabled;
  setText(
    $('refresh-accounts'),
    state.refreshTotal > 1
      ? `查询中 ${state.refreshProgress}/${state.refreshTotal}`
      : '查询全部额度'
  );
  for (const item of $('account-list').children) {
    const row = state.accounts.find(row => row.id === item.dataset.accountId);
    if (row) accountRowControls(item, row);
  }
  updateLoginControls();
}
function updateLoginControls() {
  const active = ['waiting', 'exchanging'].includes(state.loginStatus);
  const busy =
    state.globalAction ||
    [...state.pendingAccounts.values()].some(pending => !pending.silent);
  $('start-login').disabled = busy || active;
  $('auto-login').disabled = busy || active;
  $('cancel-login').disabled = busy;
  $('cancel-login').hidden = !active;
}
function renderLogin(login = {}) {
  clearTimeout(loginTimer);
  state.loginStatus = login.status || 'idle';
  $('login-status').hidden = state.loginStatus === 'idle';
  setText($('login-message'), login.message || '');
  updateLoginControls();
  if (['waiting', 'exchanging'].includes(state.loginStatus) && !document.hidden)
    loginTimer = setTimeout(pollLogin, 2000);
}
async function pollLogin() {
  if (loginPolling || document.hidden) return;
  loginPolling = true;
  try {
    const login = await api('/api/account-balances/login');
    renderLogin(login);
    if (login.status === 'success' && !state.accountBusy)
      await accountAction(async () => {
        renderAccounts(await api('/api/account-balances'), login.account_id);
        if (state.selectedId === login.account_id)
          renderAccounts(
            await post(`/api/account-balances/${login.account_id}/refresh`)
          );
      });
  } catch {
    $('login-message').textContent =
      '暂时无法读取登录状态，稍后自动重试。请勿关闭服务。';
    if (!document.hidden) loginTimer = setTimeout(pollLogin, 3000);
  } finally {
    loginPolling = false;
  }
}
function canResetAccount(row) {
  const pending = row?.reset_action?.status === 'pending';
  const fresh =
    row?.status === 'ready' && Date.now() / 1000 - row.checked_at <= 60;
  const synced =
    row?.reset_action?.status !== 'confirmed' ||
    row.checked_at > row.reset_action.redeemed_at;
  return (
    !!row &&
    (pending ||
      (fresh &&
        !row.stale &&
        synced &&
        row.quota?.reset_credits?.available_count > 0))
  );
}
const windowLabel = id =>
  id === '5h' ? '5 小时' : id === '7d' ? '7 天' : '额度周期';
function availableWindows(row) {
  const reported = new Set(
    (row?.quota?.windows || []).map(window =>
      window.window_seconds > 0
        ? window.window_seconds <= 6 * 3600
          ? '5h'
          : '7d'
        : window.kind || 'unknown'
    )
  );
  return Object.fromEntries(
    Object.entries(row?.cycles?.windows || {}).filter(([id]) =>
      reported.has(id)
    )
  );
}
function textElement(tag, className, text) {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
}
const summaryUsd = value =>
  Number.isFinite(value)
    ? '$' +
      value.toLocaleString('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
      })
    : '—';
const quotaDate = value =>
  new Date(value * 1000).toLocaleString('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false
  });
function quotaResetText(resetsAt, awaitingRefresh = false) {
  const remaining = resetsAt - Date.now() / 1000;
  if (awaitingRefresh || remaining <= 0) return '等待额度更新';
  const minutes = Math.floor(remaining / 60);
  if (!minutes) return '不足1分钟后重置';
  const days = Math.floor(minutes / 1440),
    hours = Math.floor(minutes / 60) % 24;
  if (days) return `${days}天 ${hours}小时 ${minutes % 60}分钟后重置`;
  if (hours) return `${hours}小时 ${minutes % 60}分钟后重置`;
  return `${minutes}分钟后重置`;
}
function updateQuotaCountdowns() {
  for (const time of document.querySelectorAll('.quota-reset-time')) {
    const text = quotaResetText(
      Number(time.dataset.resetsAt),
      time.dataset.awaitingRefresh === 'true'
    );
    if (time.textContent !== text) time.textContent = text;
  }
}
function renderQuotaWindow(row, id, cycle) {
  const group = document.createElement('div');
  group.className = 'account-window';
  group.setAttribute('aria-label', `${row.name} ${windowLabel(id)}额度`);
  const expired =
    cycle.awaiting_refresh ||
    (cycle.resets_at && Date.now() / 1000 >= cycle.resets_at);
  const fresh =
    row.status === 'ready' && !row.stale && !expired && !state.periodSyncFailed;
  const usage = fresh ? cycle.local_usage : null;
  const estimated = fresh && Number.isFinite(cycle.estimated_total_usd);
  const progress = document.createElement('div');
  progress.className = 'quota-progress';
  progress.append(textElement('span', 'period-tag', windowLabel(id)));
  if (fresh && cycle.used_percent != null) {
    const level =
      cycle.used_percent >= 95
        ? 'high'
        : cycle.used_percent >= 80
          ? 'medium'
          : 'low';
    const meter = document.createElement('meter');
    meter.className = `quota-meter ${level}`;
    meter.min = 0;
    meter.max = 100;
    meter.value = cycle.used_percent;
    meter.setAttribute('aria-label', `${row.name} ${windowLabel(id)}已用比例`);
    progress.append(
      meter,
      textElement(
        'span',
        `quota-percent ${level}`,
        `已用 ${number(cycle.used_percent)}%`
      )
    );
  } else progress.append(textElement('span', 'quota-coverage', '待同步'));
  if (cycle.resets_at) {
    const time = textElement(
      'span',
      'quota-reset-time',
      quotaResetText(cycle.resets_at, cycle.awaiting_refresh)
    );
    time.dataset.resetsAt = String(cycle.resets_at);
    time.dataset.awaitingRefresh = String(!!cycle.awaiting_refresh);
    time.title = `${quotaDate(cycle.resets_at)} 重置`;
    progress.append(time);
  }
  group.append(progress);
  const metrics = [];
  if (usage?.request_count) {
    metrics.push(['请求', number(usage.request_count)]);
    const cost = cycle.api_cost_estimate;
    if (cost)
      metrics.push([
        'tokens',
        new Intl.NumberFormat('en-US', {
          notation: 'compact',
          maximumFractionDigits: 1
        }).format(cost.input_tokens + cost.output_tokens)
      ]);
  }
  if (usage?.request_count && Number.isFinite(usage.cost_usd))
    metrics.push(['估算费用', summaryUsd(usage.cost_usd)]);
  if (estimated) {
    metrics.push(
      ['预计总额度', `≈${summaryUsd(cycle.estimated_total_usd)}`],
      ['预计剩余', `≈${summaryUsd(cycle.estimated_remaining_usd)}`]
    );
  }
  if (metrics.length) {
    const summary = document.createElement('dl');
    summary.className = 'quota-summary';
    for (const [label, value] of metrics) {
      const metric = document.createElement('div');
      metric.append(textElement('dt', '', label), textElement('dd', '', value));
      summary.append(metric);
    }
    group.append(summary);
  }
  if (!estimated)
    group.append(
      textElement(
        'p',
        'quota-coverage',
        !fresh
          ? '额度更新后重新估算'
          : cycle.used_percent === 0
            ? '尚未消耗额度，暂无法估算'
            : '暂无可用于估算的用量记录'
      )
    );
  return group;
}
function accountButton(row, action, label, className = '') {
  const button = textElement('button', `account-link ${className}`, label);
  button.type = 'button';
  button.dataset.accountAction = action;
  button.dataset.accountId = row.id;
  button.dataset.label = label;
  button.setAttribute('aria-label', `${label} · ${row.name}`);
  if (action === 'refresh-cards')
    button.title = '刷新可用重置次数，不消耗重置卡';
  if (action === 'test')
    button.title =
      '使用此账号发送简短模型请求，消耗少量额度，不切换当前账号';
  return button;
}
function accountFeedback(accountId, text, error = false) {
  if (!accountId) {
    feedback('account-feedback', text, error);
    return;
  }
  if (text) state.accountMessages.set(accountId, { text, error });
  else state.accountMessages.delete(accountId);
  const item = [...$('account-list').children].find(
    item => item.dataset.accountId === accountId
  );
  const previous = item?.querySelector('.account-action-feedback');
  if (!text) {
    previous?.remove();
    return;
  }
  if (!item) return;
  const message = previous || document.createElement('p');
  message.className = `account-row-message account-action-feedback ${error ? 'error' : 'hint'}`;
  message.setAttribute('role', 'status');
  setText(message, text);
  if (!previous) item.querySelector('.account-overview').append(message);
}
function patchAccountNode(current, next) {
  if (current.isEqualNode(next)) return;
  if (
    current.nodeType !== next.nodeType ||
    current.nodeName !== next.nodeName
  ) {
    current.replaceWith(next);
    return;
  }
  if (current.nodeType === Node.TEXT_NODE) {
    current.nodeValue = next.nodeValue;
    return;
  }
  for (const attribute of [...current.attributes]) {
    if (!next.hasAttribute(attribute.name))
      current.removeAttribute(attribute.name);
  }
  for (const attribute of next.attributes) {
    if (current.getAttribute(attribute.name) !== attribute.value)
      current.setAttribute(attribute.name, attribute.value);
  }
  const children = [...next.childNodes];
  children.forEach((child, index) => {
    if (current.childNodes[index])
      patchAccountNode(current.childNodes[index], child);
    else current.append(child);
  });
  while (current.childNodes.length > children.length)
    current.lastChild.remove();
}
function renderAccountList(accountIds = null) {
  const active = document.activeElement;
  const focusId = active?.dataset.accountId,
    focusAction = active?.dataset.accountAction;
  const list = $('account-list');
  const existing = new Map(
    [...list.children].map(item => [item.dataset.accountId, item])
  );
  for (const [index, row] of state.accounts.entries()) {
    if (accountIds && !accountIds.includes(row.id) && existing.has(row.id)) {
      existing.delete(row.id);
      continue;
    }
    const quota = row.quota || {};
    const item = document.createElement('tr');
    item.className = 'account-row';
    item.dataset.accountId = row.id;
    const identity = document.createElement('td');
    identity.className = 'account-identity';
    identity.append(textElement('strong', 'account-name', row.name));
    if (row.name === '邮箱未提供')
      identity.title = '凭据中没有邮箱，请重新导入包含邮箱的账号 JSON。';
    const platform = document.createElement('td');
    platform.className = 'account-platform';
    const badges = document.createElement('div');
    badges.className = 'platform-badges';
    badges.append(
      textElement('span', 'platform-badge', 'OpenAI'),
      textElement('span', 'platform-badge', 'OAuth')
    );
    platform.append(badges);
    const plan = quota.plan_type || '';
    const label = /business|team/.test(plan)
      ? 'Business'
      : /pro/.test(plan)
        ? 'Pro'
        : /plus/.test(plan)
          ? 'Plus'
          : /enterprise/.test(plan)
            ? 'Enterprise'
            : /free/.test(plan)
              ? 'Free'
              : '订阅账号';
    const planTag = textElement('span', 'plan-badge', label);
    planTag.title = plan;
    platform.append(planTag);
    const status = document.createElement('td');
    status.className = 'account-state';
    const exhausted = Object.values(availableWindows(row)).some(
      cycle =>
        !cycle.awaiting_refresh &&
        (!cycle.resets_at || cycle.resets_at > Date.now() / 1000) &&
        cycle.used_percent >= 100
    );
    status.append(
      textElement(
        'span',
        `status ${row.status === 'error' || row.stale ? 'error' : row.status === 'ready' ? (exhausted ? 'warning' : 'ok') : ''}`,
        row.stale
          ? '待更新'
          : row.status === 'error'
            ? '查询失败'
            : row.status === 'ready'
              ? exhausted
                ? '额度用尽'
                : '正常'
              : '待查询'
      )
    );
    if (row.checked_at) {
      const time = textElement(
        'span',
        'account-updated',
        new Date(row.checked_at * 1000).toLocaleTimeString('zh-CN', {
          hour: '2-digit',
          minute: '2-digit',
          hour12: false
        })
      );
      time.title = `更新于 ${date(row.checked_at * 1000)}`;
      status.append(time);
    }
    const overview = document.createElement('td');
    overview.className = 'account-overview';
    const quotaContent = document.createElement('div');
    quotaContent.className = 'account-quota';
    const cycles = Object.entries(availableWindows(row));
    for (const [id, cycle] of cycles)
      quotaContent.append(renderQuotaWindow(row, id, cycle));
    if (!cycles.length)
      quotaContent.append(textElement('p', 'quota-coverage', '查询后显示额度'));
    const tools = document.createElement('div');
    tools.className = 'quota-tools';
    const controls = document.createElement('div');
    controls.className = 'quota-actions';
    controls.append(
      accountButton(row, 'refresh', '查询', 'refresh-link'),
      accountButton(
        row,
        'refresh-cards',
        `重置次数 ${quota.reset_credits?.available_count == null ? '—' : number(quota.reset_credits.available_count)}`,
        'refresh-link'
      ),
      accountButton(
        row,
        'reset',
        row.reset_action?.status === 'pending' ? '核对重置' : '重置',
        'reset-link'
      )
    );
    tools.append(controls);
    const meta = document.createElement('div');
    meta.className = 'quota-account-meta';
    const hasCreditBalance = quota.unlimited || Number.isFinite(quota.balance);
    if (hasCreditBalance) {
      const credits = textElement(
        'span',
        'account-credits',
        quota.unlimited
          ? 'Credits：服务方未设定上限'
          : `Credits ${number(quota.balance)}`
      );
      if (Number.isFinite(row.balance_usd))
        credits.title = `折算 ${summaryUsd(row.balance_usd)}`;
      meta.append(credits);
    }
    const expiry = quota.reset_credits?.expires_at?.[0];
    if (expiry)
      meta.append(
        textElement(
          'span',
          'card-expiry',
          `重置卡到期 ${quotaDate(new Date(expiry).getTime() / 1000)}`
        )
      );
    if (meta.childElementCount) tools.append(meta);
    quotaContent.append(tools);
    overview.append(quotaContent);
    if (row.message || quota.warning)
      overview.append(
        textElement(
          'p',
          `account-row-message ${row.status === 'error' ? 'error' : 'hint'}`,
          row.message || quota.warning
        )
      );
    const message = state.accountMessages.get(row.id);
    if (message) {
      const feedback = textElement(
        'p',
        `account-row-message account-action-feedback ${message.error ? 'error' : 'hint'}`,
        message.text
      );
      feedback.setAttribute('role', 'status');
      overview.append(feedback);
    }
    const actions = document.createElement('td');
    actions.className = 'account-row-actions';
    const view = accountButton(row, 'view', '明细');
    view.setAttribute('aria-pressed', String(row.id === state.selectedId));
    view.setAttribute('aria-controls', 'usage-details');
    actions.append(
      accountButton(row, 'test', '测试'),
      view,
      accountButton(row, 'remove', '移除', 'quiet-danger')
    );
    item.append(identity, platform, status, overview, actions);
    accountRowControls(item, row);
    const current = existing.get(row.id);
    if (current) {
      for (const [column, cell] of [...item.cells].entries()) {
        patchAccountNode(current.cells[column], cell);
      }
      accountRowControls(current, row);
    }
    const target = current || item;
    if (list.children[index] !== target)
      list.insertBefore(target, list.children[index] || null);
    existing.delete(row.id);
  }
  for (const [id, item] of existing) {
    item.remove();
    state.accountMessages.delete(id);
  }
  accountControls();
  if (focusId && !active.isConnected)
    [...list.querySelectorAll('button')]
      .find(
        button =>
          button.dataset.accountId === focusId &&
          button.dataset.accountAction === focusAction
      )
      ?.focus({ preventScroll: true });
}
function renderSelectedAccount() {
  const row = state.accounts.find(item => item.id === state.selectedId);
  renderUsage(row);
}
function renderAccounts(
  payload,
  selectedId = state.selectedId,
  accountIds = null
) {
  state.periodSyncFailed = false;
  if (
    ['waiting', 'exchanging'].includes(state.loginStatus) &&
    payload.login?.status === 'success'
  )
    selectedId = payload.login.account_id;
  renderLogin(payload.login);
  state.accounts = payload.accounts;
  state.selectedId = state.accounts.some(row => row.id === selectedId)
    ? selectedId
    : state.accounts[0]?.id;
  $('account-empty').hidden = state.accounts.length > 0;
  $('account-table-wrap').hidden = state.accounts.length === 0;
  setText($('account-count'), `${number(state.accounts.length)} 个`);
  renderAccountList(accountIds);
  renderSelectedAccount();
}
function renderAccountResult(payload, accountId) {
  const updated = payload.accounts.find(row => row.id === accountId);
  const accounts = state.accounts.flatMap(row =>
    row.id === accountId ? (updated ? [updated] : []) : [row]
  );
  if (updated && !accounts.some(row => row.id === accountId))
    accounts.push(updated);
  renderAccounts({ ...payload, accounts }, state.selectedId, [accountId]);
}
async function accountAction(
  action,
  accountId = null,
  actionName = 'refresh',
  { silent = false } = {}
) {
  if ([...state.pendingAccounts.values()].some(pending => pending.silent))
    await accountQueue;
  if (
    state.globalAction ||
    (accountId
      ? state.pendingAccounts.has(accountId)
      : state.accountBusy || state.pendingAccounts.size > 0)
  )
    return;
  const focusedButton = document.activeElement?.closest('#account-list button');
  lastActionFocus = focusedButton;
  const pending = { action: actionName, running: false, silent };
  if (accountId) state.pendingAccounts.set(accountId, pending);
  else state.globalAction = true;
  state.accountRevision++;
  if (!silent) accountFeedback(accountId, '');
  accountControls();
  // The upstream store permits only one quota query or reset at a time.
  const operation = accountQueue.then(async () => {
    state.accountBusy = true;
    pending.running = true;
    accountControls();
    if (!accountId) feedback('account-feedback', '正在处理…');
    try {
      await action();
    } catch (error) {
      accountFeedback(accountId, error.message, true);
    } finally {
      state.accountBusy = false;
      if (accountId) state.pendingAccounts.delete(accountId);
      else state.globalAction = false;
      state.accountRevision++;
      accountControls();
      if (
        focusedButton &&
        focusedButton === lastActionFocus &&
        document.activeElement === document.body
      ) {
        const button = focusedButton.isConnected
          ? focusedButton
          : [...$('account-list').querySelectorAll('button')].find(
              button =>
                button.dataset.accountId === focusedButton.dataset.accountId &&
                button.dataset.accountAction ===
                  focusedButton.dataset.accountAction
            );
        button?.focus({ preventScroll: true });
      }
    }
  });
  accountQueue = operation.catch(() => {});
  return operation;
}
async function refreshAccounts(ids, actionName = 'refresh', options = {}) {
  if (!ids.length) return;
  await accountAction(
    async () => {
      const failures = [];
      state.refreshTotal = ids.length;
      try {
        for (const [index, id] of ids.entries()) {
          state.refreshingId = id;
          state.refreshProgress = index + 1;
          accountControls();
          const name =
            state.accounts.find(row => row.id === id)?.name || '账号';
          try {
            const payload = await post(`/api/account-balances/${id}/refresh`);
            renderAccountResult(payload, id);
            const row = state.accounts.find(item => item.id === id);
            if (!row || row.status !== 'ready' || row.stale)
              failures.push({
                id,
                name,
                message: row?.message || '余额未能更新，请重试。'
              });
          } catch (error) {
            failures.push({ id, name, message: error.message });
          }
        }
        for (const failure of failures) {
          const row = state.accounts.find(item => item.id === failure.id);
          if (row) {
            row.status = 'error';
            row.stale = !!row.quota;
            row.message = failure.message;
          }
        }
        if (failures.length) {
          renderAccountList(failures.map(row => row.id));
          renderSelectedAccount();
        }
        if (ids.length > 1)
          feedback(
            'account-feedback',
            `已刷新 ${number(ids.length - failures.length)} 个账号${failures.length ? `；${number(failures.length)} 个失败：${failures.map(row => row.name).join('、')}，可单独重试。` : '。'}`,
            failures.length > 0
          );
      } finally {
        state.refreshingId = null;
        state.refreshTotal = 0;
        state.refreshProgress = 0;
      }
    },
    ids.length === 1 ? ids[0] : null,
    actionName,
    options
  );
}
$('start-login').addEventListener('click', () =>
  accountAction(async () => {
    renderLogin(await post('/api/account-balances/login/start'));
    feedback('account-feedback', '');
  })
);
$('auto-login').addEventListener('click', async () => {
  let credentials = await window.promptAccountCredentials();
  if (credentials === null) return;
  await accountAction(async () => {
    const request = post('/api/account-balances/login/start', { credentials });
    credentials = null;
    renderLogin(await request);
    feedback('account-feedback', '');
  });
  credentials = null;
});
$('cancel-login').addEventListener('click', () =>
  accountAction(async () => {
    renderLogin(await post('/api/account-balances/login/cancel'));
    feedback('account-feedback', '');
  })
);
$('import-accounts').addEventListener('click', () => $('account-file').click());
$('account-file').addEventListener('change', () =>
  accountAction(async () => {
    const file = $('account-file').files[0];
    $('account-file').value = '';
    if (!file) return;
    if (file.size > 1024 * 1024)
      throw new Error('JSON 文件不能超过 1 MB，请拆分后导入。');
    let document;
    try {
      document = JSON.parse((await file.text()).replace(/^\uFEFF/, ''));
    } catch {
      throw new Error('无法解析 JSON。请使用账号导出的原始 JSON 文件。');
    }
    const payload = await post('/api/account-balances/import', document);
    renderAccounts(payload, payload.imported_ids[0]);
    feedback(
      'account-feedback',
      `已导入 ${number(payload.imported_ids.length)} 个账号，正在查询首个账号…`
    );
    try {
      renderAccounts(
        await post(`/api/account-balances/${state.selectedId}/refresh`)
      );
    } catch (error) {
      throw new Error(`账号已保存，但余额查询失败：${error.message}`);
    }
    if (
      state.accounts.find(row => row.id === state.selectedId)?.status ===
      'ready'
    )
      feedback(
        'account-feedback',
        `已导入 ${number(payload.imported_ids.length)} 个账号。${state.accounts.find(row => row.id === state.selectedId)?.quota?.warning || '余额已更新。'}`
      );
  })
);
$('refresh-accounts').addEventListener('click', () =>
  refreshAccounts(state.accounts.map(row => row.id))
);
$('account-list').addEventListener('click', event => {
  const button = event.target.closest('button[data-account-action]');
  if (!button) return;
  if (button.disabled) return;
  if (['refresh', 'refresh-cards'].includes(button.dataset.accountAction)) {
    void refreshAccounts(
      [button.dataset.accountId],
      button.dataset.accountAction
    );
    return;
  }
  const row = state.accounts.find(item => item.id === button.dataset.accountId);
  if (button.dataset.accountAction === 'test') {
    testAccount(row);
    return;
  }
  if (button.dataset.accountAction === 'reset') {
    resetAccount(row);
    return;
  }
  if (button.dataset.accountAction === 'remove') {
    removeAccount(row);
    return;
  }
  state.selectedId = button.dataset.accountId;
  accountControls();
  renderSelectedAccount();
  const target = $('usage-details');
  target.open = true;
  target.scrollIntoView({ block: 'nearest' });
});
$('stats-window').addEventListener('change', () => {
  state.statsWindow = $('stats-window').value;
  renderUsage(state.accounts.find(item => item.id === state.selectedId));
});
function testAccount(row) {
  if (!row) return;
  void accountAction(
    async () => {
      accountFeedback(row.id, '正在测试连接，最长等待 30 秒…');
      try {
        const result = await api(`/api/account-balances/${row.id}/test`, {
          method: 'POST',
          body: '{}',
          signal: AbortSignal.timeout(35000)
        });
        accountFeedback(
          row.id,
          `连接成功 · HTTP ${result.status_code} · ${result.model_display_name || '所选模型'} · ${number(result.elapsed_ms)} ms · 回复：${result.response_text}`
        );
      } catch (error) {
        const messages = {
          401: '登录已失效，请重新登录此账号。',
          403: '此账号无权访问测试模型。',
          404: '账号或测试端点不存在，请刷新后重试。',
          429: '请求受限，请稍后重试。',
          500: '上游服务内部错误，请稍后重试。',
          502: '未获得完整模型回复，请稍后重试。',
          503: '上游服务暂不可用，请稍后重试。',
          504: '连接测试超时，请检查网络后重试。'
        };
        const message =
          error.category === 'busy'
            ? '已有连接测试正在运行，请稍后再试。'
            : error.category === 'access'
              ? messages[403]
              : messages[error.status] || error.message;
        accountFeedback(
          row.id,
          error.name === 'TimeoutError'
            ? '测试超时：35 秒内未收到结果，未获取到 HTTP 状态码。'
            : error.status
              ? `连接失败 · HTTP ${error.status} · ${message}`
              : '连接失败：无法连接本地服务，未获取到 HTTP 状态码。',
          true
        );
      }
    },
    row.id,
    'test'
  );
}
function resetAccount(row) {
  if (!row || !canResetAccount(row)) return;
  const pending = row.reset_action?.status === 'pending';
  const warning = pending
    ? `核对「${row.name}」上次重置的结果？会沿用上次请求编号，避免重复用卡。`
    : `为「${row.name}」重置额度？将消耗 1 张重置卡，无法撤销。`;
  if (!window.confirm(warning)) return;
  void accountAction(
    async () => {
      const payload = await post(`/api/account-balances/${row.id}/reset`, {
        confirm: true,
        checked_at: row.checked_at
      });
      renderAccountResult(payload, row.id);
      const updated = payload.accounts.find(item => item.id === row.id);
      if (
        updated?.reset_action?.status === 'confirmed' &&
        updated.status === 'ready'
      )
        accountFeedback(
          row.id,
          updated.reset_action.windows_reset > 0
            ? `已重置 ${number(updated.reset_action.windows_reset)} 个额度周期，估算已更新。`
            : '服务方已确认用卡，但未重置任何额度周期。'
        );
      else if (updated?.reset_action?.status === 'no_credit')
        accountFeedback(row.id, '没有可用的重置卡，额度未重置。', true);
    },
    row.id,
    'reset'
  );
}
function removeAccount(row) {
  if (
    !row ||
    !window.confirm(
      `移除「${row.name}」？连接与设置页的同一账号也会移除；服务将停止使用此账号。这不会注销账号，也不会删除原始 JSON 文件。`
    )
  )
    return;
  void accountAction(
    async () => {
      renderAccountResult(
        await post(`/api/account-balances/${row.id}/remove`),
        row.id
      );
      feedback('account-feedback', '账号已移除。');
    },
    row.id,
    'remove'
  );
}
async function refreshAccountPanel() {
  if (state.globalAction || state.pendingAccounts.size > 0) return;
  const revision = state.accountRevision;
  const payload = await api('/api/account-balances');
  if (
    state.globalAction ||
    state.pendingAccounts.size > 0 ||
    revision !== state.accountRevision
  )
    return;
  renderAccounts(payload);
  const row = state.accounts.find(item => item.id === state.selectedId);
  if (
    !row ||
    Date.now() - state.lastQuotaAttempt < 60000 ||
    (row.status === 'ready' && Date.now() / 1000 - row.checked_at < 60)
  )
    return;
  state.lastQuotaAttempt = Date.now();
  await refreshAccounts([row.id], 'refresh', { silent: true });
}
function costRows(id, rows, columns, emptyText = '本期暂无请求记录。') {
  const fragment = document.createDocumentFragment();
  for (const values of rows) {
    const tr = document.createElement('tr');
    values.forEach((value, index) => {
      const td = document.createElement('td');
      td.textContent = value;
      if (index) td.className = 'number';
      tr.append(td);
    });
    fragment.append(tr);
  }
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = columns;
    td.className = 'empty';
    td.textContent = emptyText;
    tr.append(td);
    fragment.append(tr);
  }
  const target = $(id);
  if (
    target.children.length !== fragment.children.length ||
    [...fragment.children].some(
      (row, index) => !target.children[index].isEqualNode(row)
    )
  )
    target.replaceChildren(fragment);
}
function renderCosts(cost, checkedAt) {
  setText($('cost-status'), cost.complete ? '已更新' : '部分记录未计价');
  const statusClass = `status ${cost.complete ? 'ok' : 'warning'}`;
  if ($('cost-status').className !== statusClass)
    $('cost-status').className = statusClass;
  setText($('cost-total'), usd(cost.cost_usd));
  for (const [id, key] of [
    ['cost-input', 'input_fresh'],
    ['cost-cached', 'cached_input'],
    ['cost-write', 'cache_creation'],
    ['cost-output', 'output']
  ]) {
    setText($(id), cost.cost_usd == null ? '—' : usd(cost.cost_breakdown[key]));
  }
  const missing = [];
  if (cost.unpriced_requests)
    missing.push(`${number(cost.unpriced_requests)} 次缺少单价`);
  if (cost.missing_usage_requests)
    missing.push(`${number(cost.missing_usage_requests)} 次缺少用量`);
  feedback(
    'cost-feedback',
    `本期 ${number(cost.request_count)} 次请求，已计价 ${number(cost.priced_requests)} 次。${missing.length ? missing.join('；') + '，未计入合计。' : '包含失败请求已产生的用量。'} 更新于 ${date(checkedAt * 1000)}`
  );
  costRows(
    'cost-model-rows',
    cost.models.map(row => [
      row.model_display_name || '—',
      number(row.request_count),
      number(row.input_tokens - row.cached_input_tokens),
      number(row.cached_input_tokens),
      number(row.output_tokens),
      usd(row.cost_usd) + (row.complete ? '' : '（部分计价）')
    ]),
    6
  );
  const rates = [];
  for (const row of cost.models) {
    const rate = row.rates;
    rates.push([
      row.model_display_name || '—',
      ...[
        'input_per_million',
        'cached_input_per_million',
        'cache_write_per_million',
        'output_per_million'
      ].map(key => usd(rate?.[key]))
    ]);
    if (rate?.long_context_threshold)
      rates.push([
        `${row.model_display_name || '—'} · 单次输入 > ${number(rate.long_context_threshold)}`,
        ...['input', 'cached_input', 'cache_write', 'output'].map(key =>
          usd(rate[`long_context_${key}_per_million`])
        )
      ]);
  }
  costRows('cost-rate-rows', rates, 5);
}
function clearPeriodUsage(message, error = false) {
  for (const id of [
    'request-count',
    'input-count',
    'cached-count',
    'output-count',
    'cost-total',
    'cost-input',
    'cost-cached',
    'cost-write',
    'cost-output'
  ])
    setText($(id), '—');
  setText($('cost-status'), error ? '更新失败' : '等待本期数据');
  const statusClass = `status ${error ? 'error' : ''}`;
  if ($('cost-status').className !== statusClass)
    $('cost-status').className = statusClass;
  costRows('cost-model-rows', [], 6, '暂无本期数据。');
  costRows('cost-rate-rows', [], 5, '暂无本期数据。');
  feedback('cost-feedback', message, error);
}
function renderUsage(row) {
  const windows = availableWindows(row);
  const ids = Object.keys(windows);
  if (!windows[state.statsWindow]) state.statsWindow = ids[0] || null;
  const window = windows[state.statsWindow];
  const awaitingReset =
    window?.awaiting_refresh ||
    (window?.resets_at && Date.now() / 1000 >= window.resets_at);
  const cost =
    row?.status === 'ready' &&
    !row.stale &&
    !awaitingReset &&
    !state.periodSyncFailed
      ? window?.api_cost_estimate
      : null;
  const renderKey = JSON.stringify([
    row?.id,
    row?.name,
    row?.status,
    ids,
    state.statsWindow,
    awaitingReset,
    state.periodSyncFailed,
    cost,
    window?.checked_at
  ]);
  if (usageRenderKey === renderKey) return;
  usageRenderKey = renderKey;
  if (
    JSON.stringify(
      [...$('stats-window').options].map(option => option.value)
    ) !== JSON.stringify(ids)
  ) {
    $('stats-window').replaceChildren(
      ...ids.map(id => {
        const option = document.createElement('option');
        option.value = id;
        option.textContent = windowLabel(id);
        return option;
      })
    );
  }
  $('stats-window').value = state.statsWindow || '';
  $('stats-window-control').hidden = ids.length < 2;
  $('stats-window').disabled = ids.length === 0;
  const label = state.statsWindow ? windowLabel(state.statsWindow) : '';
  setText($('cost-period'), label ? `${label}周期 · USD` : '本期 · USD');
  setText(
    $('usage-scope'),
    row
      ? `${row.name} · ${label ? `${label}周期` : '暂无额度周期'} · 仅统计经本服务发送的请求`
      : '添加账号后即可查看本期用量。'
  );
  if (!cost) {
    clearPeriodUsage(
      !row
        ? '请先添加或选择账号。'
        : awaitingReset
          ? '上期已结束，等待服务方更新本期额度。'
          : '暂无本期数据，请先查询账号额度。',
      row?.status === 'error' || state.periodSyncFailed
    );
    return;
  }
  setText($('request-count'), number(cost.request_count));
  setText($('input-count'), number(cost.input_tokens));
  setText($('cached-count'), number(cost.cached_input_tokens));
  setText($('output-count'), number(cost.output_tokens));
  renderCosts(cost, window.checked_at);
}
async function refreshAll({ silent = false } = {}) {
  if (
    state.loading ||
    (silent && (state.globalAction || state.pendingAccounts.size > 0))
  )
    return;
  state.loading = true;
  if (!silent) $('cost-panel').setAttribute('aria-busy', 'true');
  const results = await Promise.allSettled([refreshAccountPanel()]);
  accountControls();
  if (!silent) $('cost-panel').setAttribute('aria-busy', 'false');
  if (results[0].status === 'rejected') {
    state.periodSyncFailed = true;
    usageRenderKey = undefined;
    clearPeriodUsage('本期数据更新失败，稍后自动重试。', true);
    renderAccountList();
  }
  const failures = results.filter(result => result.status === 'rejected');
  setText($('live-status'), failures.length ? '更新失败' : '服务运行中');
  const statusClass = `status ${failures.length ? 'error' : 'ok'}`;
  if ($('live-status').className !== statusClass)
    $('live-status').className = statusClass;
  if (failures.length) {
    feedback(
      'page-error',
      `${failures[0].reason.message}。请确认服务正在运行，页面会自动重试。`,
      true
    );
    $('page-error').hidden = false;
  } else $('page-error').hidden = true;
  state.loading = false;
}
let refreshTimer, countdownTimer;
window.addEventListener('pageshow', () => {
  clearInterval(refreshTimer);
  clearInterval(countdownTimer);
  updateQuotaCountdowns();
  void refreshAll();
  refreshTimer = setInterval(() => {
    if (!document.hidden) void refreshAll({ silent: true });
  }, 15000);
  countdownTimer = setInterval(() => {
    if (!document.hidden) updateQuotaCountdowns();
  }, 1000);
});
window.addEventListener('pagehide', () => {
  clearInterval(refreshTimer);
  clearInterval(countdownTimer);
  clearTimeout(loginTimer);
});
