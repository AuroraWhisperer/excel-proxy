"""Separate dashboard pages retain their existing behaviors."""

import unittest
import re

import httpx

import proxy


class DashboardPagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_scripts_load_once_in_dependency_order(self):
        for path, name in (
            ("/ui", "connection"),
            ("/ui/requests", "requests"),
            ("/ui/usage", "usage"),
        ):
            response = await self.client.get(path)
            scripts = re.findall(r'<script src="([^"]+)"', response.text)
            self.assertNotIn("<script>", response.text)
            self.assertEqual(scripts.count(f"/ui/{name}.js"), 1)
            if name != "usage":
                self.assertLess(
                    scripts.index("/ui/api.js"), scripts.index(f"/ui/{name}.js")
                )
            for script in scripts:
                loaded = await self.client.get(script)
                self.assertEqual(loaded.status_code, 200)
                self.assertIn("text/javascript", loaded.headers["content-type"])
                self.assertEqual(loaded.headers["cache-control"], "no-cache")
                blocked = await self.client.get(
                    script, headers={"Origin": "https://untrusted.example"}
                )
                self.assertEqual(blocked.status_code, 403)
        self.assertEqual((await self.client.get("/ui/proxy.py")).status_code, 404)

    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def page_source(self, response):
        """Inspect HTML plus its page script; shared credential UI has separate checks."""
        source = response.text
        for script in re.findall(
            r'<script src="(/ui/(?:connection|requests|usage)\.js)"></script>',
            response.text,
        ):
            loaded = await self.client.get(script)
            self.assertEqual(loaded.status_code, 200, script)
            source += "\n" + loaded.text
        # Source contracts should not depend on indentation or line wrapping.
        return re.sub(r"\s+", " ", source)

    async def test_home_focuses_on_connection_and_settings(self):
        response = await self.client.get("/ui")
        source = await self.page_source(response)
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/ui/requests"', source)
        self.assertIn('href="/ui/usage"', source)
        self.assertIn('id="session-heading"', source)
        self.assertIn('id="codex-heading"', source)
        self.assertIn('id="settings-feedback"', source)
        self.assertNotIn('id="request-rows"', source)
        self.assertNotIn('id="cost-panel"', source)
        self.assertNotIn("/api/dashboard", source)

    async def test_direct_login_is_primary_and_legacy_controls_are_advanced(self):
        response = await self.client.get("/ui")
        source = await self.page_source(response)
        html = source
        self.assertIn("浏览器登录", html)
        for marker in (
            'id="proxy-account"',
            'id="activate-account"',
            'id="cancel-proxy-login"',
            "/api/proxy-accounts/login/start",
        ):
            self.assertIn(marker, html)
        self.assertLess(
            html.index('id="advanced-settings"'), html.index('id="read-session"')
        )
        self.assertLess(
            html.index('id="advanced-settings"'), html.index('id="test-connection"')
        )
        self.assertNotIn("localStorage", html)

    async def test_connection_copy_keeps_actions_and_important_warnings(self):
        response = await self.client.get("/ui")
        source = await self.page_source(response)
        html = source
        for marker in (
            "<summary>高级设置</summary>",
            "使用此账号",
            "读取 Excel 登录",
            "测试会消耗少量额度",
            "可能包含敏感信息",
            "请重启 Codex",
            "不会自动切换",
            "这不会注销账号",
        ):
            self.assertIn(marker, html)
        for redundant in (
            "连接诊断、Excel 兼容入口与配置恢复",
            "会话就绪后，确认模型能否正常响应。",
            "账密自动登录",
        ):
            self.assertNotIn(redundant, html)

    async def test_usage_copy_keeps_estimate_limits_and_reset_consequences(self):
        response = await self.client.get("/ui/usage")
        source = await self.page_source(response)
        for marker in (
            "额度周期",
            "估算费用",
            "不代表实际扣费或订阅剩余额度",
            "不保证与最新官方报价一致",
            "未计入合计",
            "将消耗 1 张重置卡，无法撤销",
            "沿用上次请求编号",
            "不会删除原始 JSON 文件",
        ):
            self.assertIn(marker, source)
        for jargon in (
            "用卡回执已确认",
            "周期估算已重新取样",
            "不沿用旧账号或旧周期费用",
        ):
            self.assertNotIn(jargon, source)

    async def test_request_copy_explains_empty_states_and_safe_recovery(self):
        response = await self.client.get("/ui/requests")
        source = await self.page_source(response)
        for marker in (
            "输入、缓存和输出的单位为 token",
            "暂无请求。启用连接后",
            "仅对后续请求生效",
            "操作可能已经执行",
            "请确认结果后再试",
            "本批工具未执行",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("查看请求状态、耗时与提示词详情", source)
        self.assertNotIn("检查上游响应契约", source)

    async def test_home_starts_with_connections_and_updates_automatically(self):
        response = await self.client.get("/ui")
        source = await self.page_source(response)
        html = source
        for marker in (
            "connection-banner",
            "<h1>Excel Proxy</h1>",
            "无需打开 Excel",
            'id="refresh"',
            'id="live-status"',
            "$('refresh')",
            "$('live-status')",
            "点击刷新",
        ):
            self.assertNotIn(marker, html)
        self.assertLess(
            html.index("</nav>"), html.index('class="connection-workspace"')
        )
        self.assertIn("void refreshAll().then(pollLogin);", html)
        self.assertIn(
            "setInterval(() => { if (!document.hidden) void refreshAll().then(pollLogin); }, 15000)",
            html,
        )
        self.assertIn("clearInterval(refreshTimer)", html)
        self.assertIn("页面会自动重试", html)

    async def test_usage_page_preserves_costs_and_pricing_disclaimer(self):
        response = await self.client.get("/ui/usage")
        source = await self.page_source(response)
        self.assertEqual(response.status_code, 200)
        for marker in (
            'id="cost-heading"',
            'id="cost-total"',
            'id="cost-model-rows"',
            "API 费用估算",
            "参考单价",
            "不代表实际扣费",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("/api/dashboard", source)
        self.assertIn('id="stats-window"', source)
        self.assertIn("window?.api_cost_estimate", source)
        self.assertIn("已计价合计", source)
        self.assertNotIn("本月已计价合计", source)
        self.assertIn("renderUsage(row)", source)
        self.assertIn("clearPeriodUsage(", source)
        self.assertIn("!state.periodSyncFailed", source)
        self.assertIn("Date.now() / 1000 >= window.resets_at", source)
        self.assertNotIn("/api/config/excel-session", source)
        self.assertNotIn('id="request-rows"', source)
        for marker in (
            "/api/account-quota",
            "check-quota",
            "quotaTimer",
            "Codex 账号额度",
        ):
            self.assertNotIn(marker, source)

    async def test_all_pages_share_three_tabs_and_light_theme(self):
        for path in ("/ui", "/ui/requests", "/ui/usage"):
            with self.subTest(path=path):
                response = await self.client.get(path)
                source = await self.page_source(response)
                self.assertEqual(response.status_code, 200)
                self.assertIn('name="color-scheme" content="light"', source)
                self.assertEqual(source.count('aria-current="page"'), 1)
                self.assertIn(f'href="{path}" aria-current="page"', source)
                for target in ("/ui", "/ui/requests", "/ui/usage"):
                    self.assertIn(f'href="{target}"', source)
                self.assertNotIn("<header>", source)
                if path != "/ui":
                    self.assertIn(
                        'id="refresh-accounts"'
                        if path == "/ui/usage"
                        else 'id="refresh"',
                        source,
                    )
                if path != "/ui":
                    self.assertNotIn(
                        "通过已登录的 ChatGPT Excel 会话连接 Codex", source
                    )
                else:
                    self.assertLess(
                        source.index("</nav>"),
                        source.index('class="connection-workspace"'),
                    )

    async def test_page_titles_only_appear_in_navigation(self):
        for path, title in (
            ("/ui", "连接与设置"),
            ("/ui/requests", "最近请求"),
            ("/ui/usage", "用量与费用"),
        ):
            with self.subTest(path=path):
                response = await self.client.get(path)
                source = await self.page_source(response)
                self.assertEqual(response.status_code, 200)
                self.assertRegex(
                    source, rf'aria-current="page"\s*>\s*{re.escape(title)}</a\s*>'
                )
                self.assertNotRegex(source, rf"<h[1-6]\b[^>]*>\s*{title}\s*</h[1-6]>")
                if path != "/ui":
                    self.assertIn(
                        'id="refresh-accounts"'
                        if path == "/ui/usage"
                        else 'id="refresh"',
                        source,
                    )

        requests = await self.client.get("/ui/requests")
        requests_source = await self.page_source(requests)
        self.assertIn(
            '<section class="content-panel" aria-label="最近请求">', requests_source
        )
        self.assertNotIn('aria-labelledby="requests-heading"', requests_source)

    async def test_usage_import_is_separate_and_reset_requires_explicit_confirmation(
        self,
    ):
        response = await self.client.get("/ui/usage")
        source = await self.page_source(response)
        for marker in (
            'id="import-accounts"',
            'id="account-file"',
            "hasCreditBalance",
            "/api/account-balances/import",
            "canResetAccount(row)",
            "accountButton(row, 'remove', '移除'",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("/consume", source)
        self.assertIn("function resetAccount(row)", source)
        self.assertIn("if (!window.confirm(warning)) return", source)
        self.assertIn("{ confirm: true, checked_at: row.checked_at }", source)
        self.assertEqual(source.count("function resetAccount(row)"), 1)
        self.assertNotIn("localStorage", source)

    async def test_account_login_stays_in_private_window_without_password_form(self):
        response = await self.client.get("/ui/usage")
        source = await self.page_source(response)
        for marker in (
            'id="start-login"',
            'id="cancel-login"',
            "登录添加账号",
            "/api/account-balances/login/start",
        ):
            self.assertIn(marker, source)
        self.assertNotIn('type="password"', source)
        self.assertEqual(source.count("$('start-login').addEventListener"), 1)

    async def test_both_pages_share_transient_credential_dialog(self):
        for path, button in (("/ui", "auto-login-proxy"), ("/ui/usage", "auto-login")):
            response = await self.client.get(path)
            source = await self.page_source(response)
            self.assertIn(f'id="{button}"', source)
            self.assertIn("账号密码登录", source)
            self.assertEqual(source.count('src="/ui/account-login.js"'), 1)
            self.assertEqual(source.count(f"$('{button}').addEventListener"), 1)
        response = await self.client.get("/ui/account-login.js")
        source = await self.page_source(response)
        self.assertEqual(response.status_code, 200)
        self.assertIn('type="password"', source)
        self.assertIn("input.value = ''", source)
        self.assertIn("2fa.fun", source)
        self.assertIn("第三方网站 2fa.fun", source)
        self.assertIn("邮箱----密码----2FA密钥", source)
        self.assertIn("密码和密钥仅用于本次登录", source)
        self.assertNotIn("localStorage", source)

    async def test_usage_accounts_have_compact_list_and_refresh_actions(self):
        response = await self.client.get("/ui/usage")
        source = await self.page_source(response)
        for marker in (
            'id="account-list"',
            'id="refresh-accounts"',
            "data-account-action",
            "查询全部额度",
            "refreshAccounts(",
            '<th scope="col">邮箱</th>',
        ):
            self.assertIn(marker, source)
        self.assertNotIn('id="account-select"', source)
        self.assertNotIn("登录或导入账号，查看服务方余额", source)
        self.assertEqual(source.count("$('refresh-accounts').addEventListener"), 1)
        self.assertEqual(source.count("$('account-list').addEventListener"), 1)
        for marker in (
            "account-content",
            "account-detail-name",
            "conversion-settings",
            "conversion-form",
            "credit-rate",
            "save-rate",
            "cycle-method",
            "cycle-history",
            "reset-account",
            "remove-account",
            "rateDirty",
        ):
            self.assertNotIn(marker, source)
        self.assertIn("if (!row || !canResetAccount(row)) return", source)
        self.assertIn("if (button.disabled) return", source)

    async def test_usage_toolbar_groups_account_management_above_inline_feedback(self):
        response = await self.client.get("/ui/usage")
        source = await self.page_source(response)
        html = source
        toolbar = html.split('<div class="page-heading section-heading">', 1)[1].split(
            '<section id="account-panel"', 1
        )[0]
        header = html.split('<section id="account-panel"', 1)[1].split(
            '<div id="login-status"', 1
        )[0]
        title = header.split('<div class="session-title">', 1)[1].split("</div>", 1)[0]
        for element_id in ("import-accounts", "start-login", "account-file"):
            self.assertIn(f'id="{element_id}"', toolbar)
            self.assertNotIn(f'id="{element_id}"', header)
            self.assertEqual(html.count(f'id="{element_id}"'), 1)
        for element_id in ("accounts-heading", "account-count", "account-feedback"):
            self.assertIn(f'id="{element_id}"', title)
        self.assertEqual(html.count('id="account-feedback"'), 1)
        self.assertIn('id="refresh-accounts"', header)
        self.assertNotIn("更新统计", toolbar)
        self.assertRegex(header, r"查询全部额度\s*</button>")
        self.assertRegex(
            html,
            r"\$\('refresh-accounts'\)\.addEventListener\('click', \(\) => "
            r"refreshAccounts\(state\.accounts\.map\(row => row\.id\)\)\s*\)",
        )

    async def test_usage_statistics_update_automatically_without_a_manual_button(self):
        response = await self.client.get("/ui/usage")
        source = await self.page_source(response)
        html = source
        self.assertNotIn('id="refresh"', html)
        self.assertNotIn("$('refresh')", html)
        self.assertIn("window.addEventListener('pageshow',", html)
        self.assertIn("void refreshAll();", html)
        self.assertIn(
            "setInterval(() => { if (!document.hidden) void refreshAll({ silent: true }); }, 15000)",
            html,
        )
        self.assertIn("window.addEventListener('pagehide',", html)
        self.assertIn("clearInterval(refreshTimer)", html)
        self.assertIn("页面会自动重试", html)

    async def test_usage_header_feedback_stays_compact_without_overriding_errors(self):
        response = await self.client.get("/ui/shared.css")
        source = await self.page_source(response)
        css = source
        feedback = css.split("#account-feedback {", 1)[1].split("}", 1)[0]
        for rule in ("max-width: 100%", "margin: 0", "font-size: 12px"):
            self.assertIn(rule, feedback)
        self.assertNotIn("color:", feedback)
        self.assertIn("#account-feedback:not(.error) { color: var(--muted); }", css)
        self.assertIn(".account-panel .session-title { flex: 1; min-width: 0;", css)

    async def test_usage_actions_update_rows_without_rebuilding_the_table(self):
        response = await self.client.get("/ui/usage")
        source = await self.page_source(response)
        html = source
        self.assertNotIn("$('account-list').replaceChildren", html)
        for marker in (
            "item.dataset.accountId = row.id",
            "patchAccountNode(current.cells[column], cell)",
            "if (current.isEqualNode(next)) return",
            "pendingAccounts: new Map()",
            "accountQueue.then",
            "function renderAccountResult(payload, accountId)",
            "function accountFeedback(accountId, text",
            "focusedButton === lastActionFocus",
            "if (usageRenderKey === renderKey) return",
            "refreshAll({ silent: true })",
        ):
            self.assertIn(marker, html)
        selection = html.split("$('account-list').addEventListener('click'", 1)[
            1
        ].split("$('stats-window').addEventListener", 1)[0]
        self.assertNotIn("renderAccountList()", selection)
        self.assertIn("renderSelectedAccount()", selection)

    async def test_usage_background_query_is_silent_and_removal_is_linked(self):
        response = await self.client.get("/ui/usage")
        source = await self.page_source(response)
        self.assertIn(
            "await refreshAccounts([row.id], 'refresh', { silent: true })", source
        )
        self.assertIn("pending?.action === action && !pending.silent", source)
        self.assertNotIn("current.cells[column].replaceWith(cell)", source)
        self.assertNotIn("location.reload", source)
        self.assertIn("连接与设置页的同一账号也会移除", source)
        response = await self.client.get("/ui")
        source = await self.page_source(response)
        self.assertIn("用量与费用页的同一账号也会移除", source)
        self.assertNotIn("需在用量页单独移除", source)

    async def test_usage_page_reuses_shared_theme_and_compact_layout(self):
        response = await self.client.get("/ui/shared.css")
        source = await self.page_source(response)
        css = source
        self.assertNotIn(".usage-page { --bg:", css)
        self.assertNotIn(".usage-page main", css)
        self.assertNotIn("#4338ca", css)
        self.assertIn(".account-window { display: grid;", css)
        self.assertIn(".quota-summary { display: flex; flex-wrap: nowrap;", css)
        self.assertIn(".quota-tools { display: flex; flex-wrap: wrap;", css)
        self.assertNotIn(".quota-stats", css)
        self.assertIn(".account-table td { padding: 10px 16px;", css)

    async def test_usage_metric_labels_keep_small_inline_values_on_one_row(self):
        response = await self.client.get("/ui/shared.css")
        source = await self.page_source(response)
        css = source
        summary = css.split(".quota-summary {", 1)[1].split("}", 1)[0]
        label = css.split(".quota-summary > div {", 1)[1].split("}", 1)[0]
        value = css.split(".quota-summary dd {", 1)[1].split("}", 1)[0]
        for rule in (
            "display: flex",
            "flex-wrap: nowrap",
            "gap: 6px",
            "overflow-x: auto",
            "font-size: 11px",
            "color: var(--text)",
            "font-weight: 500",
            "line-height: 1.4",
        ):
            self.assertIn(rule, summary)
        for rule in (
            "display: inline-flex",
            "flex: 0 0 auto",
            "padding: 2px 6px",
            "background: var(--bg)",
        ):
            self.assertIn(rule, label)
        self.assertIn("margin: 0", value)
        self.assertIn("font-weight: 600", value)
        self.assertIn("white-space: nowrap", value)
        self.assertNotIn("font-size:", value)

    async def test_usage_quota_actions_are_compact_without_shrinking_row_actions(self):
        response = await self.client.get("/ui/shared.css")
        source = await self.page_source(response)
        css = source
        account_link = css.split(".account-table .account-link {", 1)[1].split("}", 1)[
            0
        ]
        quota_action = css.split(".account-table .quota-actions .account-link {", 1)[
            1
        ].split("}", 1)[0]
        focus = css.split(
            ".account-table .quota-actions .account-link:focus-visible {", 1
        )[1].split("}", 1)[0]
        self.assertIn("min-height: 28px", account_link)
        self.assertIn("font-size: 12px", account_link)
        for rule in (
            "min-height: 19.2px",
            "padding-block: 0",
            "font-size: 11px",
            "line-height: 1.4",
        ):
            self.assertIn(rule, quota_action)
        self.assertIn("outline-offset: 2px", focus)

    async def test_usage_account_columns_have_balanced_widths_and_vertical_centering(
        self,
    ):
        response = await self.client.get("/ui/shared.css")
        source = await self.page_source(response)
        css = source
        for column, width in enumerate((18, 12, 10, 46, 14), start=1):
            self.assertIn(
                f".account-table th:nth-child({column}) {{ width: {width}%; }}", css
            )
        self.assertIn(".account-table th { padding: 10px 16px;", css)
        self.assertIn(
            ".account-table td { padding: 10px 16px; vertical-align: middle; }", css
        )
        self.assertIn(
            ".account-row-actions .account-link + .account-link { margin-left: 16px; }",
            css,
        )

    async def test_usage_account_hover_uses_a_distinct_blue_gray_background(self):
        response = await self.client.get("/ui/shared.css")
        source = await self.page_source(response)
        css = source
        self.assertIn(".account-table tbody tr:hover { background: #f1f4f8; }", css)
        self.assertIn(".account-identity { background: inherit; }", css)

    async def test_usage_related_values_do_not_spread_to_far_edge(self):
        response = await self.client.get("/ui/shared.css")
        source = await self.page_source(response)
        css = source
        reset_time = css.split(".quota-reset-time {", 1)[1].split("}", 1)[0]
        tools = css.split(".quota-tools {", 1)[1].split("}", 1)[0]
        self.assertNotIn("margin-left: auto", reset_time)
        self.assertNotIn("space-between", tools)
        self.assertIn(".account-quota { width: max-content; max-width: 100%; }", css)

    async def test_usage_has_per_account_connection_test(self):
        html = await self.page_source(await self.client.get("/ui/usage"))
        self.assertIn("accountButton(row, 'test', '测试')", html)
        self.assertIn("`/api/account-balances/${row.id}/test`", html)
        self.assertIn("AbortSignal.timeout(35000)", html)
        self.assertIn("result.response_text", html)
        self.assertIn("error.status", html)
        self.assertIn("quotaContent.append(tools)", html)

    async def test_usage_windows_group_metrics_between_progress_and_supporting_details(
        self,
    ):
        response = await self.client.get("/ui/usage")
        source = await self.page_source(response)
        html = source
        window = html.split("function renderQuotaWindow(row, id, cycle)", 1)[1].split(
            "function accountButton", 1
        )[0]
        window = re.sub(r"([(\[])\s+", r"\1", window)
        self.assertLess(
            window.index("group.append(progress)"),
            window.index("group.append(summary)"),
        )
        self.assertLess(
            window.index("group.append(summary)"),
            window.index("group.append(textElement('p', 'quota-coverage'"),
        )
        self.assertEqual(window.count("group.append(summary)"), 1)
        for marker in (
            "metrics.push(['请求', number(usage.request_count)])",
            "metrics.push(['tokens',",
            "metrics.push(['估算费用',",
            "metrics.push(['预计总额度',",
            "['预计剩余',",
        ):
            self.assertIn(marker, window)
        for marker in (
            "document.createElement('dl')",
            "textElement('dt', '', label)",
            "textElement('dd', '', value)",
            "估算费用",
            "windowLabel(id)",
            "tools.className = 'quota-tools'",
            "meta.className = 'quota-account-meta'",
            "tools.append(controls)",
            "meta.append(credits)",
            "tools.append(meta)",
        ):
            self.assertIn(marker, html)
        self.assertNotIn("quota-stats", window)

    async def test_usage_window_omits_partial_pricing_badge(self):
        response = await self.client.get("/ui/usage")
        source = await self.page_source(response)
        window = source.split("function renderQuotaWindow(row, id, cycle)", 1)[1].split(
            "function accountButton", 1
        )[0]
        window = re.sub(r"([(\[])\s+", r"\1", window)
        self.assertNotIn("已计价", window)
        self.assertNotIn("可能偏低", window)
        self.assertNotIn("部分估算 ·", window)
        self.assertIn(
            "if (!estimated) group.append(textElement('p', 'quota-coverage'", window
        )

    async def test_usage_window_picker_only_contains_reported_windows(self):
        response = await self.client.get("/ui/usage")
        source = await self.page_source(response)
        self.assertIn("function availableWindows(row)", source)
        self.assertIn("row?.quota?.windows", source)
        self.assertIn("$('stats-window').replaceChildren", source)
        self.assertIn('id="stats-window-control"', source)
        self.assertNotIn('<option value="5h">', source)
        self.assertNotIn('<option value="7d">', source)

    async def test_usage_table_exposes_estimates_and_separate_count_refresh(self):
        response = await self.client.get("/ui/usage")
        source = await self.page_source(response)
        for marker in (
            'class="account-table"',
            '<tbody id="account-list">',
            "预计总额度",
            "预计剩余",
            "['refresh', 'refresh-cards'].includes(button.dataset.accountAction)",
            "刷新可用重置次数，不消耗重置卡",
            "canResetAccount(row)",
            'id="usage-details"',
            "额度更新后重新估算",
        ):
            self.assertIn(marker, source)
        self.assertNotIn('id="quota-windows"', source)
        self.assertNotIn('class="balance-metrics"', source)
        self.assertNotIn("等待本周期有效样本", source)
        self.assertIn("quota.unlimited || Number.isFinite(quota.balance)", source)

    async def test_existing_pages_check_usage_route_before_navigation(self):
        for path in ("/ui", "/ui/requests"):
            with self.subTest(path=path):
                response = await self.client.get(path)
                source = await self.page_source(response)
                self.assertIn('id="navigation-error"', source)
                self.assertIn("response.status === 404", source)
                self.assertIn("此页面需要重启服务才能使用", source)
                self.assertIn("window.location.assign(link.href)", source)
                self.assertIn("$('navigation-error').focus()", source)

    async def test_requests_page_retains_filter_paging_and_details(self):
        response = await self.client.get("/ui/requests")
        source = await self.page_source(response)
        self.assertEqual(response.status_code, 200)
        for marker in (
            'id="request-rows"',
            'id="request-filter"',
            'id="next-page"',
            'id="prompt-panel"',
            "/api/request-prompt/",
            'href="/ui"',
        ):
            self.assertIn(marker, source)
        self.assertNotIn('id="session-heading"', source)
        self.assertNotIn("/api/config/excel-session", source)

    async def test_requests_page_shows_first_token_timing(self):
        response = await self.client.get("/ui/requests")
        source = await self.page_source(response)
        self.assertEqual(response.status_code, 200)
        self.assertRegex(source, r'>\s*首字\s*</th>\s*<th scope="col">耗时</th>')
        self.assertIn('title="收到首段回复的等待时间，含排队和网络耗时', source)
        self.assertIn("正文、推理和工具调用均计入，心跳不计", source)
        self.assertIn("非流式请求统计完整回复到达时间", source)
        self.assertIn("无记录或无输出时显示 —", source)
        self.assertIn(
            "row.time_to_first_token_ms != null ? `${(row.time_to_first_token_ms / 1000).toFixed(1)}s` : '—'",
            source,
        )
        self.assertIn('colspan="9"', source)
        self.assertIn("td.colSpan = 9", source)

    async def test_requests_page_shows_safe_diagnosis_without_prompt_logging(self):
        response = await self.client.get("/ui/requests")
        source = await self.page_source(response)
        for marker in (
            'id="request-diagnosis"',
            "failure_diagnosis",
            "tool_call_recovery",
            "diagnosisText(row)",
            "工具调用格式错误",
            "已纠正",
        ):
            self.assertIn(marker, source)
        self.assertIn("$('request-diagnosis').textContent = diagnosisText(row)", source)

    async def test_pages_have_distinct_etags_and_revalidate(self):
        etags = set()
        for path in ("/ui", "/ui/requests", "/ui/usage"):
            response = await self.client.get(path)
            source = await self.page_source(response)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("content-encoding"), "gzip")
            etags.add(response.headers["etag"])
            cached = await self.client.get(
                path, headers={"If-None-Match": response.headers["etag"]}
            )
            self.assertEqual(cached.status_code, 304)
        self.assertEqual(len(etags), 3)

    async def test_connection_page_uses_readable_type_without_resizing_other_pages(
        self,
    ):
        response = await self.client.get("/ui/shared.css")
        source = await self.page_source(response)
        css = source
        self.assertIn(".connection-page { font-size: 16px; }", css)
        controls = css.split(".connection-page button, .connection-page select {", 1)[
            1
        ].split("}", 1)[0]
        for rule in ("min-height: 44px", "font-size: 15px"):
            self.assertIn(rule, controls)
        for selector in (
            ".connection-page .hint, .connection-page .feedback, .connection-page .field label",
            ".connection-workspace .field label",
            ".connection-workspace .endpoint span",
            ".connection-page .setting-row p",
            ".connection-page footer",
        ):
            with self.subTest(selector=selector):
                rule = css.split(selector + " {", 1)[1].split("}", 1)[0]
                self.assertIn("font-size: 14px", rule)
        self.assertIn("font: 14px/1.65", css.split("body {", 1)[1].split("}", 1)[0])

    async def test_pages_share_desktop_width_and_retain_narrow_layout(
        self,
    ):
        response = await self.client.get("/ui/shared.css")
        source = await self.page_source(response)
        css = source
        main = css.split("main {", 1)[1].split("}", 1)[0]
        self.assertIn("width: min(1600px, 100% - 48px)", main)
        narrow = css.split("@media (max-width: 720px) {", 1)[1]
        self.assertIn(
            ".connection-workspace { grid-template-columns: minmax(0, 1fr); }", narrow
        )

    async def test_shared_stylesheet_is_served(self):
        response = await self.client.get("/ui/shared.css")
        source = await self.page_source(response)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/css", response.headers["content-type"])
        self.assertIn("color-scheme: light", source)
        self.assertIn("--accent: #176b58", source)
