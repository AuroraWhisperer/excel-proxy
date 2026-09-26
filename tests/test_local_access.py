import unittest

import httpx

import proxy


class LocalAccessTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, path="/v1/models", *, peer="127.0.0.1", headers=None):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app, client=(peer, 43210)),
            base_url="http://127.0.0.1:8000",
        ) as client:
            return await client.get(path, headers=headers)

    async def test_native_clients_and_same_origin_dashboard_are_allowed(self):
        for headers in ({}, {"Origin": "http://127.0.0.1:8000"}, {
            "Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-origin",
        }, {"Host": "localhost:8000", "Origin": "http://localhost:8000"}):
            with self.subTest(headers=headers):
                response = await self.request(headers=headers)
                self.assertEqual(response.status_code, 200)

    async def test_nonlocal_peers_are_denied_even_with_forwarded_headers(self):
        for peer in ("192.168.1.20", "203.0.113.8"):
            response = await self.request(peer=peer, headers={"X-Forwarded-For": "127.0.0.1"})
            self.assertEqual(response.status_code, 403)

    async def test_external_hosts_and_browser_origins_are_denied_on_all_pages(self):
        for path in ("/v1/models", "/ui", "/ui/requests", "/api/config/client-proxy"):
            for headers in (
                {"Host": "example.com:8000"}, {"Origin": "https://example.com"},
                {"Origin": "null"}, {"Origin": "http://127.0.0.1:8001"},
                {"Sec-Fetch-Site": "cross-site"},
                {"Sec-Fetch-Site": "same-site"},
            ):
                with self.subTest(path=path, headers=headers):
                    response = await self.request(path, headers=headers)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["error"]["code"], "local_access_required")

    async def test_malformed_or_duplicate_authority_is_denied(self):
        for headers in (
            {"Host": "user@localhost:8000"}, {"Host": "localhost:8000/private"},
            {"Host": "localhost:bad-port"}, {"Host": "localhost:8000#fragment"},
            [("Host", "127.0.0.1:8000"), ("Host", "example.com")],
            [("Origin", "http://127.0.0.1:8000"), ("Origin", "https://example.com")],
        ):
            with self.subTest(headers=headers):
                self.assertEqual((await self.request(headers=headers)).status_code, 403)

    async def test_ipv6_loopback_is_allowed(self):
        response = await self.request(peer="::1", headers={
            "Host": "[::1]:8000", "Origin": "http://[::1]:8000",
        })
        self.assertEqual(response.status_code, 200)

    async def test_cross_origin_write_is_denied_before_reading_the_body(self):
        consumed = False

        async def body():
            nonlocal consumed
            consumed = True
            yield b"invalid JSON"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://127.0.0.1:8000",
        ) as client:
            response = await client.post("/api/config/client-proxy/settings", content=body(),
                                         headers={"Origin": "https://example.com"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(consumed)
