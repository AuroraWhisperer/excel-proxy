import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx


spec = importlib.util.spec_from_file_location(
    "first_output_probe", Path(__file__).resolve().parents[1] / "tools" / "probe-first-output.py",
)
probe_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe_module)


def frame(event_type, **payload):
    return ("data: " + json.dumps({"type": event_type, **payload}) + "\n\n").encode()


class FirstOutputProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_is_first_byte_not_first_output(self):
        clock = [0.0]

        class TimedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                clock[0] = 0.1
                yield b": heartbeat\n\n"
                clock[0] = 0.2
                yield frame("response.created", response={"output": []})
                clock[0] = 0.7
                yield frame("response.output_text.delta", delta="O")
                clock[0] = 0.8
                yield frame("response.output_text.delta", delta="K")
                clock[0] = 1.0
                yield frame("response.completed", response={"usage": {"output_tokens": 2}})

        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=TimedStream()))
        async with httpx.AsyncClient(transport=transport) as client:
            with patch.object(probe_module.time, "perf_counter", side_effect=lambda: clock[0]):
                result = await probe_module.probe(client, "https://example.test/responses", {})
        self.assertEqual(result["first_byte_ms"], 100.0)
        self.assertEqual(result["first_output_ms"], 700.0)
        self.assertEqual(result["duration_ms"], 1000.0)
        self.assertEqual(result["terminal"], "response.completed")

    async def test_trace_and_http_errors_do_not_expose_secrets(self):
        async def respond(request):
            await request.extensions["trace"](
                "http11.send_request_body.complete", {"authorization": "test-secret"},
            )
            return httpx.Response(422, text="test-secret")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await probe_module.probe(client, "https://example.test/responses", {})
        self.assertEqual(result["status"], 422)
        self.assertNotIn("first_output_ms", result)
        self.assertNotIn("test-secret", json.dumps(result))
        self.assertEqual(result["transport_events"][0]["event"], "http11.send_request_body.complete")

    def test_live_requests_require_explicit_opt_in(self):
        with patch("sys.argv", ["probe-first-output.py"]), \
             patch.object(probe_module.asyncio, "run") as run, \
             patch("sys.stderr"):
            with self.assertRaises(SystemExit) as error:
                probe_module.main()
        self.assertEqual(error.exception.code, 2)
        run.assert_not_called()
