"""Separate dashboard pages retain their existing behaviors."""

import unittest

import httpx

import proxy


class DashboardPagesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1")
        self.addAsyncCleanup(self.client.aclose)

    async def test_home_has_navigation_but_no_request_table(self):
        response = await self.client.get("/ui")
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/ui/requests"', response.text)
        self.assertIn('id="session-heading"', response.text)
        self.assertNotIn('id="request-rows"', response.text)
        self.assertIn("/api/dashboard", response.text)

    async def test_home_replaces_subscription_quota_with_api_costs(self):
        response = await self.client.get("/ui")
        for marker in ('id="cost-heading"', 'id="cost-total"', 'id="cost-model-rows"', 'API 费用估算', '参考单价', '不代表实际扣费'):
            self.assertIn(marker, response.text)
        for marker in ('/api/account-quota', 'check-quota', 'quotaTimer', 'Codex 账号额度'):
            self.assertNotIn(marker, response.text)

    async def test_requests_page_retains_filter_paging_and_details(self):
        response = await self.client.get("/ui/requests")
        self.assertEqual(response.status_code, 200)
        for marker in ('id="request-rows"', 'id="request-filter"', 'id="next-page"', 'id="prompt-panel"', '/api/request-prompt/', 'href="/ui"'):
            self.assertIn(marker, response.text)
        self.assertNotIn('id="session-heading"', response.text)
        self.assertNotIn("/api/config/excel-session", response.text)

    async def test_requests_page_shows_first_token_timing(self):
        response = await self.client.get("/ui/requests")
        self.assertEqual(response.status_code, 200)
        self.assertIn('>首字</th><th scope="col">耗时</th>', response.text)
        self.assertIn('title="从代理开始计时到收到上游首个有效输出（正文、推理或工具调用）', response.text)
        self.assertIn('历史缺失或无输出显示 —', response.text)
        self.assertIn(
            "row.time_to_first_token_ms != null ? `${(row.time_to_first_token_ms / 1000).toFixed(1)}s` : '—'",
            response.text,
        )
        self.assertIn('colspan="9"', response.text)
        self.assertIn('td.colSpan = 9', response.text)

    async def test_pages_have_distinct_etags_and_revalidate(self):
        home = await self.client.get("/ui")
        requests = await self.client.get("/ui/requests")
        self.assertNotEqual(home.headers.get("etag"), requests.headers.get("etag"))
        for path, response in (("/ui", home), ("/ui/requests", requests)):
            cached = await self.client.get(path, headers={"If-None-Match": response.headers["etag"]})
            self.assertEqual(cached.status_code, 304)

    async def test_shared_stylesheet_is_served(self):
        response = await self.client.get("/ui/dashboard.css")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/css", response.headers["content-type"])
        self.assertIn("--accent: #ff7a3a", response.text)
