const $ = id => document.getElementById(id);
const date = value =>
  value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
const state = {
  session: null,
  settings: null,
  background: null,
  accounts: null,
  testing: false,
  loading: false,
  signingIn: false,
  activating: false
};
let loginTimer = null;
let loginPolling = false;
let loginStatusFailed = false;
let loginGeneration = 0;
let pageActive = true;
let activateAfterLogin = false;
const busy = new Set();
function feedback(id, text, error = false) {
  $(id).textContent = text;
  $(id).classList.toggle('error', error);
}
const api = window.proxyApi;
const post = (path, body, options = {}) =>
  api(path, { method: 'POST', body: JSON.stringify(body), ...options });
function renderSession(payload) {
  state.session = payload;
  const ready = payload.configured && !payload.expired;
  $('session-status').textContent = ready
    ? '已启用'
    : payload.expired
      ? '需要重新登录'
      : '尚未启用';
  $('session-status').className = `status ${ready ? 'ok' : ''}`;
  $('session-copy').textContent =
    payload.source === 'oauth'
      ? `当前账号：${payload.account_name}${payload.expired ? '。登录已过期，请重新登录。' : payload.renewable ? '' : '。有效期至 ' + date(payload.expires_at * 1000) + '，到期后请重新登录。'}`
      : ready
        ? '正在使用 Excel 加载项的登录信息。在此登录账号后，无需再打开 Excel。'
        : '登录后会测试连接，并自动配置 Codex。';
  $('clear-session').disabled =
    payload.source === 'oauth' ||
    !payload.configured ||
    busy.has('clear-session');
  if (payload.models) {
    const selected = $('test-model').value;
    $('test-model').replaceChildren(
      ...payload.models.map(model => new Option(model.display_name, model.id))
    );
    $('test-model').value = payload.models.some(model => model.id === selected)
      ? selected
      : payload.default_model;
  }
  $('test-model').disabled = state.testing || !$('test-model').value;
  $('test-connection').disabled =
    !ready || state.testing || !$('test-model').value;
}
function renderAccountControls() {
  const rows = state.accounts?.accounts || [];
  const locked = state.signingIn || state.activating;
  $('login-proxy').textContent = state.signingIn
    ? '等待登录…'
    : rows.length
      ? '添加账号'
      : '登录并启用代理';
  $('login-proxy').disabled = !state.accounts || locked;
  $('auto-login-proxy').disabled = !state.accounts || locked;
  $('cancel-proxy-login').hidden = !state.signingIn;
  $('proxy-account').disabled = locked;
  const selected = rows.find(row => row.id === $('proxy-account').value);
  $('activate-account').disabled =
    locked || !selected || (selected.active && !selected.pending);
  $('activate-account').textContent = state.activating
    ? '正在连接…'
    : '使用此账号';
  $('remove-proxy-account').disabled =
    locked || busy.has('remove-proxy-account') || !$('proxy-account').value;
  $('read-session').disabled = locked || busy.has('read-session');
}
function renderAccounts(payload) {
  state.accounts = payload;
  const selected = $('proxy-account').value;
  $('proxy-account').replaceChildren(
    ...payload.accounts.map(
      row =>
        new Option(
          `${row.name} · ${row.account_hint}${row.active ? '（当前使用）' : row.expired && !row.renewable ? '（需重新登录）' : ''}`,
          row.id
        )
    )
  );
  if (payload.accounts.some(row => row.id === selected))
    $('proxy-account').value = selected;
  else if (payload.active_id) $('proxy-account').value = payload.active_id;
  $('account-count').textContent = `· ${payload.accounts.length} 个`;
  $('account-switcher').hidden = !payload.accounts.length;
  $('account-management').hidden = !payload.accounts.length;
  renderAccountControls();
}
async function activateAccount(id) {
  if (!id || state.activating) return;
  state.activating = true;
  renderAccountControls();
  feedback('proxy-feedback', '正在测试连接，成功后将启用代理并配置 Codex…');
  try {
    const result = await post(
      `/api/proxy-accounts/${encodeURIComponent(id)}/activate`,
      { model: $('test-model').value },
      { signal: AbortSignal.timeout(65000) }
    );
    renderAccounts(result);
    $('proxy-account').value = result.active_id;
    feedback(
      'proxy-feedback',
      [result.message, ...(result.warnings || [])].join(' '),
      Boolean(result.warnings?.length)
    );
  } catch (error) {
    feedback('proxy-feedback', error.message, true);
  } finally {
    state.activating = false;
    await refreshAll();
    renderAccountControls();
  }
}
async function pollLogin() {
  if (loginPolling || !pageActive) return;
  loginPolling = true;
  clearTimeout(loginTimer);
  loginTimer = null;
  const generation = loginGeneration;
  try {
    const result = await api('/api/proxy-accounts/login');
    if (generation !== loginGeneration || !pageActive) return;
    if (loginStatusFailed) {
      feedback('proxy-feedback', '');
      loginStatusFailed = false;
    }
    const wasSigningIn = state.signingIn;
    state.signingIn = ['waiting', 'exchanging'].includes(result.status);
    renderAccountControls();
    if (state.signingIn) {
      activateAfterLogin = true;
      feedback('proxy-feedback', result.message);
    } else if (result.status === 'success' && activateAfterLogin) {
      activateAfterLogin = false;
      await refreshAll();
      await activateAccount(result.account_id);
    } else if (
      ['error', 'expired', 'cancelled'].includes(result.status) &&
      (activateAfterLogin || wasSigningIn)
    ) {
      activateAfterLogin = false;
      feedback('proxy-feedback', result.message, result.status !== 'cancelled');
    }
  } catch (error) {
    if (generation !== loginGeneration || !pageActive) return;
    state.signingIn = false;
    renderAccountControls();
    loginStatusFailed = true;
    feedback(
      'proxy-feedback',
      `无法读取登录状态：${error.message}。页面会自动重试。`,
      true
    );
  } finally {
    loginPolling = false;
    if (state.signingIn && pageActive) loginTimer = setTimeout(pollLogin, 1200);
  }
}
function renderSettings(payload) {
  if (payload.settings) state.settings = payload.settings;
  const client = payload.clients?.codex;
  if (client) {
    $('codex-status').textContent =
      client.error || (client.configured ? '已连接 Excel Proxy' : '尚未启用');
    $('codex-status').classList.toggle('error', Boolean(client.error));
    $('enable-codex').disabled = busy.has('enable-codex');
    $('enable-codex').textContent = client.configured
      ? '更新模型列表'
      : '启用接入';
    $('disable-codex').disabled =
      !client.configured || busy.has('disable-codex');
  }
  if (state.settings) {
    $('revert-setting').checked = state.settings.revert_on_shutdown;
    $('debug-setting').checked = state.settings.debug_prompt_logging_enabled;
    $('revert-setting').disabled = busy.has('revert-setting');
    $('debug-setting').disabled = busy.has('debug-setting');
  }
}
function renderBackground(payload) {
  state.background = payload;
  $('startup-status').textContent = !payload.startup_supported
    ? '当前系统不支持自动启动'
    : payload.startup_enabled
      ? '已启用'
      : '未启用';
  $('toggle-startup').textContent = payload.startup_enabled ? '停用' : '启用';
  $('toggle-startup').disabled =
    !payload.startup_supported || busy.has('toggle-startup');
  $('commands-status').textContent = !payload.shell_commands_supported
    ? '当前系统不支持快捷命令'
    : payload.shell_commands_installed
      ? Object.values(payload.commands || {}).join(' / ')
      : '安装后可从终端启动和停止代理';
  $('toggle-commands').textContent = payload.shell_commands_installed
    ? '移除命令'
    : '安装命令';
  $('toggle-commands').disabled =
    !payload.shell_commands_supported || busy.has('toggle-commands');
}
async function refreshAll() {
  if (state.loading) return;
  state.loading = true;
  $('page-error').hidden = true;
  const jobs = [
    ['/api/config/excel-session', renderSession],
    ['/api/config/client-proxy', renderSettings],
    ['/api/config/background-proxy', renderBackground],
    ['/api/proxy-accounts', renderAccounts]
  ];
  const results = await Promise.allSettled(
    jobs.map(async ([path, render]) => render(await api(path)))
  );
  const failures = results.filter(result => result.status === 'rejected');
  if (failures.length) {
    feedback(
      'page-error',
      `${failures[0].reason.message}。请确认代理正在运行，页面会自动重试。`,
      true
    );
    $('page-error').hidden = false;
  }
  state.loading = false;
}
async function action(buttonId, feedbackId, work) {
  if (busy.has(buttonId)) return;
  busy.add(buttonId);
  $(buttonId).disabled = true;
  feedback(feedbackId, '正在处理…');
  try {
    feedback(feedbackId, (await work()) || '已保存。');
  } catch (error) {
    feedback(feedbackId, error.message, true);
  } finally {
    busy.delete(buttonId);
    $(buttonId).disabled = false;
    await refreshAll();
  }
}
document
  .querySelector('.page-nav a[href="/ui/usage"]')
  .addEventListener('click', async event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)
      return;
    event.preventDefault();
    const link = event.currentTarget;
    if (link.getAttribute('aria-busy') === 'true') return;
    link.setAttribute('aria-busy', 'true');
    $('navigation-error').hidden = true;
    try {
      const response = await fetch(link.href, {
        cache: 'no-store',
        signal: AbortSignal.timeout(5000)
      });
      if (response.status === 404)
        throw new Error(
          '此页面需要重启代理才能使用。请等当前请求结束后，关闭代理窗口，再运行「启动.vbs」。'
        );
      if (!response.ok)
        throw new Error(
          `页面暂时无法打开（HTTP ${response.status}），请稍后重试。`
        );
      window.location.assign(link.href);
    } catch (error) {
      feedback(
        'navigation-error',
        error.name === 'TimeoutError'
          ? '页面加载超时，请确认代理正在运行后重试。'
          : error.message,
        true
      );
      $('navigation-error').hidden = false;
      $('navigation-error').focus();
    } finally {
      link.removeAttribute('aria-busy');
    }
  });
async function startProxyLogin(credentials = null) {
  if (state.signingIn || state.activating) return;
  loginGeneration += 1;
  state.signingIn = true;
  renderAccountControls();
  try {
    const request = post(
      '/api/proxy-accounts/login/start',
      credentials === null ? {} : { credentials }
    );
    credentials = null;
    await request;
    activateAfterLogin = true;
    await pollLogin();
  } catch (error) {
    state.signingIn = false;
    renderAccountControls();
    feedback('proxy-feedback', error.message, true);
  }
}
$('login-proxy').addEventListener('click', () => startProxyLogin());
$('auto-login-proxy').addEventListener('click', async () => {
  let credentials = await window.promptAccountCredentials();
  if (credentials === null) return;
  const request = startProxyLogin(credentials);
  credentials = null;
  await request;
});
$('cancel-proxy-login').addEventListener('click', async () => {
  loginGeneration += 1;
  $('cancel-proxy-login').disabled = true;
  try {
    await post('/api/proxy-accounts/login/cancel', {});
    activateAfterLogin = false;
    await pollLogin();
  } catch (error) {
    feedback('proxy-feedback', error.message, true);
  } finally {
    $('cancel-proxy-login').disabled = false;
  }
});
$('proxy-account').addEventListener('change', renderAccountControls);
$('activate-account').addEventListener('click', () =>
  activateAccount($('proxy-account').value)
);
$('remove-proxy-account').addEventListener('click', () =>
  action('remove-proxy-account', 'proxy-feedback', async () => {
    const id = $('proxy-account').value;
    const row = state.accounts.accounts.find(account => account.id === id);
    if (
      !row ||
      !confirm(
        `移除「${row.name}」？用量与费用页的同一账号也会移除。${row.active ? '代理将停止使用此账号，不会自动切换。' : ''}这不会注销账号。`
      )
    )
      return '';
    renderAccounts(
      await api(`/api/proxy-accounts/${encodeURIComponent(id)}`, {
        method: 'DELETE'
      })
    );
    return '账号已移除。';
  })
);
$('read-session').addEventListener('click', () =>
  action('read-session', 'session-feedback', async () => {
    const payload = await post('/api/config/excel-session', {
      action: 'read_cached'
    });
    renderSession(payload);
    return payload.configured && !payload.expired
      ? '已读取 Excel 登录信息。'
      : '未找到有效登录，请先在 Excel 的 ChatGPT 加载项中登录。';
  })
);
$('clear-session').addEventListener('click', () =>
  action('clear-session', 'session-feedback', async () => {
    await api('/api/config/excel-session', { method: 'DELETE' });
    return '缓存已清除，不影响 Excel 中的登录。';
  })
);
$('test-model').addEventListener('change', () => feedback('test-feedback', ''));
$('test-connection').addEventListener('click', async () => {
  if (state.testing) return;
  state.testing = true;
  renderSession(state.session);
  feedback('test-feedback', '正在测试，最长等待 30 秒…');
  try {
    const result = await post(
      '/api/config/excel-session/test',
      { model: $('test-model').value },
      { signal: AbortSignal.timeout(35000) }
    );
    feedback(
      'test-feedback',
      result.ok ? '连接正常，模型已回复。' : result.message,
      !result.ok
    );
  } catch (error) {
    feedback(
      'test-feedback',
      error.name === 'TimeoutError'
        ? '测试超时，请检查连接后重试。'
        : error.message,
      true
    );
  } finally {
    state.testing = false;
    renderSession(state.session);
    await refreshAll();
  }
});
for (const [id, mode] of [
  ['enable-codex', 'enable'],
  ['disable-codex', 'disable']
])
  $(id).addEventListener('click', () =>
    action(id, 'settings-feedback', async () => {
      const result = await post('/api/config/client-proxy', {
        target: 'codex',
        action: mode
      });
      if (result.clients?.codex?.error)
        throw new Error(result.clients.codex.error);
      return mode === 'enable'
        ? 'Codex 配置已更新，请重启 Codex。'
        : '已恢复原配置，请重启 Codex。';
    })
  );
for (const id of ['revert-setting', 'debug-setting'])
  $(id).addEventListener('change', () =>
    action(id, 'settings-feedback', async () => {
      await post('/api/config/client-proxy/settings', {
        revert_on_shutdown: $('revert-setting').checked,
        debug_prompt_logging_enabled: $('debug-setting').checked
      });
    })
  );
$('toggle-startup').addEventListener('click', () =>
  action('toggle-startup', 'settings-feedback', async () => {
    await post('/api/config/background-proxy', {
      action: state.background.startup_enabled
        ? 'disable_startup'
        : 'enable_startup'
    });
  })
);
$('toggle-commands').addEventListener('click', () =>
  action('toggle-commands', 'settings-feedback', async () => {
    await post('/api/config/background-proxy', {
      action: state.background.shell_commands_installed
        ? 'uninstall_shell_commands'
        : 'install_shell_commands'
    });
  })
);
let refreshTimer;
window.addEventListener('pageshow', () => {
  pageActive = true;
  clearInterval(refreshTimer);
  void refreshAll().then(pollLogin);
  refreshTimer = setInterval(() => {
    if (!document.hidden) void refreshAll().then(pollLogin);
  }, 15000);
});
window.addEventListener('pagehide', () => {
  pageActive = false;
  loginGeneration += 1;
  clearInterval(refreshTimer);
  clearTimeout(loginTimer);
});
