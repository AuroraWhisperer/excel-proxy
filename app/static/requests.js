const $ = id => document.getElementById(id);
const number = value => Number(value || 0).toLocaleString('zh-CN');
const date = value =>
  value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
const state = {
  rows: [],
  page: 0,
  pageSize: 50,
  loading: false,
  promptRequest: 0
};
const diagnoses = {
  authentication: [
    '登录失效',
    '请重新登录；使用 Excel 登录时，重新读取登录信息。'
  ],
  permission: ['权限不足', '请检查账号是否有权使用此模型，重试无法解决。'],
  rate_limit: ['请求过于频繁', '请按服务方提示稍后再试，减少同时发送的请求。'],
  tool_contract: [
    '工具调用格式错误',
    '请检查 JSON 引号、反斜杠和参数，不要执行格式有误的命令。'
  ],
  incomplete_stream: [
    '回复中断',
    '请检查网络，并确认操作是否已完成，避免重复执行。'
  ],
  response_contract: [
    '回复格式异常',
    '请保留请求 ID，排查服务方返回的数据格式。'
  ],
  timeout: ['请求超时', '操作可能已经执行，请确认结果后再试。'],
  connection: ['连接失败', '请检查网络和服务。重试前确认请求是否已发送。'],
  request_validation: ['请求参数错误', '请检查接口、输入大小和参数格式。'],
  upstream_service: [
    '服务暂不可用',
    '请稍后再试；涉及修改的操作，请先确认是否已执行。'
  ],
  cancelled: ['请求已取消', '不会自动重试已取消的工具操作。'],
  unknown: ['原因待确认', '请保留请求 ID，查看详情后再决定是否重试。']
};
function diagnosisText(row) {
  const diagnosis =
    row.failure_diagnosis || row.tool_call_recovery?.failure_diagnosis;
  const recovery = row.tool_call_recovery;
  const detail = row.tool_call_diagnostics || recovery?.diagnostics || {};
  const parts = [];
  if (detail.reason === 'unknown_tool') {
    parts.push(
      `调用了本次请求不支持的工具${detail.target_name ? `：${detail.target_name}` : '（名称未记录或已隐藏）'}。`
    );
    const skipped = {
      tool_history: '此前已调用工具，为避免重复操作，未自动重试。',
      tool_batch: '本批包含多个工具，已整批拦截。',
      no_client_tools: '本次请求未启用工具。',
      response_not_completed: '回复尚未完成，未自动重试。',
      response_shape: '无法自动修复此调用。'
    };
    if (skipped[detail.recovery_skipped])
      parts.push(skipped[detail.recovery_skipped]);
  } else if (diagnosis) {
    const description = diagnoses[diagnosis.category] || diagnoses.unknown;
    parts.push(`${description[0]}：${description[1]}`);
  }
  if (recovery) {
    if (recovery.mode === 'unknown_tool_regeneration') {
      parts.push(
        recovery.outcome === 'succeeded'
          ? '已用可用工具重新生成并校验通过，已交给客户端执行。'
          : '重新生成后仍未通过校验，本批工具未执行。'
      );
    } else
      parts.push(
        recovery.outcome === 'succeeded'
          ? '已修正调用格式并校验通过，已交给客户端执行。'
          : '调用格式修复失败，本批工具未执行。'
      );
    if (detail.json_line != null)
      parts.push(
        `JSON 第 ${detail.json_line} 行，第 ${detail.json_column} 列（${detail.json_error || 'invalid_json'}）。`
      );
  }
  return parts.join(' ');
}
function feedback(id, text, error = false) {
  $(id).textContent = text;
  $(id).classList.toggle('error', error);
}
const api = window.proxyApi;
function renderRows() {
  const query = $('request-filter').value.trim().toLowerCase();
  const rows = state.rows.filter(row =>
    [
      row.resolved_model,
      row.requested_model,
      row.model_display_name,
      row.request_id,
      row.status_code,
      diagnosisText(row)
    ]
      .join(' ')
      .toLowerCase()
      .includes(query)
  );
  const pages = Math.max(1, Math.ceil(rows.length / state.pageSize));
  state.page = Math.min(state.page, pages - 1);
  const fragment = document.createDocumentFragment();
  for (const row of rows.slice(
    state.page * state.pageSize,
    (state.page + 1) * state.pageSize
  )) {
    const tr = document.createElement('tr');
    const usage = row.usage || {};
    const cells = [
      date(row.started_at),
      row.model_display_name || '—',
      row.status_code || '处理中',
      number(usage.input_tokens),
      number(
        usage.cached_input_tokens ?? usage.input_tokens_details?.cached_tokens
      ),
      number(usage.output_tokens),
      row.time_to_first_token_ms != null
        ? `${(row.time_to_first_token_ms / 1000).toFixed(1)}s`
        : '—',
      row.duration_ms != null ? `${(row.duration_ms / 1000).toFixed(1)}s` : '—'
    ];
    cells.forEach((value, index) => {
      const td = document.createElement('td');
      td.textContent = value;
      if (index > 2) td.className = 'number';
      if (index === 2 && row.status_code >= 400) td.className = 'error';
      tr.append(td);
    });
    const diagnosis = diagnosisText(row);
    if (diagnosis) {
      const status = tr.children[2];
      status.title = diagnosis;
      const category = row.failure_diagnosis?.category;
      const toolReason =
        row.tool_call_diagnostics?.reason ||
        row.tool_call_recovery?.diagnostics?.reason;
      status.textContent += ` · ${row.tool_call_recovery?.outcome === 'succeeded' ? '已纠正' : toolReason === 'unknown_tool' ? '未知工具' : (diagnoses[category] || diagnoses.unknown)[0]}`;
    }
    const td = document.createElement('td');
    const button = document.createElement('button');
    button.textContent = '查看';
    button.disabled = !row.request_id;
    button.setAttribute(
      'aria-label',
      `查看 ${row.model_display_name || '模型'} 的请求详情`
    );
    button.addEventListener('click', () => showPrompt(row));
    td.append(button);
    tr.append(td);
    fragment.append(tr);
  }
  if (!rows.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 9;
    td.className = 'empty';
    td.textContent = query
      ? '没有匹配的请求，试试其他筛选条件。'
      : '暂无请求。启用连接后，在 Codex 中发起对话即可查看。';
    tr.append(td);
    fragment.append(tr);
  }
  $('request-rows').replaceChildren(fragment);
  $('page-label').textContent =
    `${rows.length} 条记录 · 第 ${state.page + 1} / ${pages} 页`;
  $('previous-page').disabled = state.page === 0;
  $('next-page').disabled = state.page >= pages - 1;
}
function renderUsage(payload) {
  state.rows = payload.recent_requests || [];
  const month = payload.current_month || {};
  $('usage-period').textContent = month.label || '本月';
  $('updated-at').textContent = ` 更新于 ${date(payload.generated_at)}`;
  renderRows();
}
async function refreshAll() {
  if (state.loading) return;
  state.loading = true;
  $('refresh').disabled = true;
  $('page-error').hidden = true;
  const jobs = [['/api/dashboard', renderUsage]];
  const results = await Promise.allSettled(
    jobs.map(async ([path, render]) => render(await api(path)))
  );
  const failures = results.filter(result => result.status === 'rejected');
  $('live-status').textContent = failures.length ? '更新失败' : '服务运行中';
  $('live-status').className = `status ${failures.length ? 'error' : 'ok'}`;
  if (failures.length) {
    feedback(
      'page-error',
      `${failures[0].reason.message}。请确认服务正在运行，然后刷新。`,
      true
    );
    $('page-error').hidden = false;
  }
  state.loading = false;
  $('refresh').disabled = false;
}
async function showPrompt(row) {
  const sequence = ++state.promptRequest;
  $('prompt-panel').hidden = false;
  $('prompt-id').textContent = row.request_id;
  $('prompt-content').textContent = '正在读取详情…';
  $('close-prompt').focus();
  $('request-diagnosis').textContent = diagnosisText(row);
  $('request-diagnosis').hidden = !$('request-diagnosis').textContent;
  try {
    const payload = await api(
      `/api/request-prompt/${encodeURIComponent(row.request_id)}`
    );
    if (sequence !== state.promptRequest) return;
    const text =
      payload.prompt_text ||
      (payload.request_prompt
        ? JSON.stringify(payload.request_prompt, null, 2)
        : '此请求未记录全文。可在「高级设置」中开启「记录请求全文」，仅对后续请求生效。');
    $('prompt-content').textContent =
      `状态：${row.status_code || '处理中'}\n模型：${row.model_display_name || '—'}\n\n${text}`;
  } catch (error) {
    if (sequence === state.promptRequest)
      $('prompt-content').textContent = error.message;
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
          '此页面需要重启服务才能使用。请等当前请求结束后，关闭服务窗口，再运行「启动.vbs」。'
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
          ? '页面加载超时，请确认服务正在运行后重试。'
          : error.message,
        true
      );
      $('navigation-error').hidden = false;
      $('navigation-error').focus();
    } finally {
      link.removeAttribute('aria-busy');
    }
  });
$('refresh').addEventListener('click', refreshAll);
$('request-filter').addEventListener('input', () => {
  state.page = 0;
  renderRows();
});
$('previous-page').addEventListener('click', () => {
  state.page--;
  renderRows();
});
$('next-page').addEventListener('click', () => {
  state.page++;
  renderRows();
});
$('close-prompt').addEventListener('click', () => {
  state.promptRequest++;
  $('prompt-panel').hidden = true;
  $('request-filter').focus();
});
let refreshTimer;
window.addEventListener('pageshow', () => {
  clearInterval(refreshTimer);
  void refreshAll();
  refreshTimer = setInterval(() => {
    if (!document.hidden) void refreshAll();
  }, 15000);
});
window.addEventListener('pagehide', () => {
  clearInterval(refreshTimer);
});
