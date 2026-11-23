import importlib.util
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx


spec = importlib.util.spec_from_file_location(
    "first_output_probe",
    Path(__file__).resolve().parents[1] / "tools" / "probe-first-output.py",
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
                yield frame(
                    "response.completed", response={"usage": {"output_tokens": 2}}
                )

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, stream=TimedStream())
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with patch.object(
                probe_module.time, "perf_counter", side_effect=lambda: clock[0]
            ):
                result = await probe_module.probe(
                    client, "https://example.test/responses", {}
                )
        self.assertEqual(result["first_byte_ms"], 100.0)
        self.assertEqual(result["first_output_ms"], 700.0)
        self.assertEqual(result["duration_ms"], 1000.0)
        self.assertEqual(result["terminal"], "response.completed")

    async def test_trace_and_http_errors_do_not_expose_secrets(self):
        async def respond(request):
            await request.extensions["trace"](
                "http11.send_request_body.complete",
                {"authorization": "test-secret"},
            )
            await request.extensions["trace"]("private.start_tls.test-secret", {})
            return httpx.Response(422, text="test-secret")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await probe_module.probe(
                client, "https://example.test/responses", {}
            )
        self.assertEqual(result["status"], 422)
        self.assertNotIn("first_output_ms", result)
        self.assertNotIn("test-secret", json.dumps(result))
        self.assertEqual(
            result["transport_events"][0]["event"], "http11.send_request_body.complete"
        )

    async def test_terminal_event_finishes_without_waiting_for_eof(self):
        class OpenAfterTerminal(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield frame("response.completed", response={"status": "completed"})
                raise AssertionError("read past the terminal event")

        transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=OpenAfterTerminal()))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await probe_module.probe(client, "https://example.test", {})
        self.assertEqual(result["terminal"], "response.completed")

    async def test_probe_deadline_bounds_heartbeat_only_stream(self):
        class NoTerminal(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b": heartbeat\n\n"
                await asyncio.Event().wait()

        transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=NoTerminal()))
        async with httpx.AsyncClient(transport=transport) as client:
            with patch.object(probe_module, "PROBE_DEADLINE_SECONDS", 0.02):
                result = await probe_module.probe(client, "https://example.test", {})
        self.assertEqual(result["error"], "probe_deadline")

    def test_report_created_privately_and_never_overwritten(self):
        def write_report(args, output):
            output.write('{"terminal":"response.completed"}\n')
            return 0

        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.jsonl"
            with (
                patch("sys.argv", ["probe", "--live", "--output", str(report)]),
                patch.object(probe_module, "run", side_effect=write_report) as run,
                patch("sys.stderr"),
            ):
                self.assertEqual(probe_module.main(), 0)
                saved = report.read_bytes()
                self.assertEqual(json.loads(saved)["terminal"], "response.completed")
                if os.name != "nt":
                    self.assertEqual(report.stat().st_mode & 0o077, 0)
                with self.assertRaises(SystemExit):
                    probe_module.main()
                self.assertEqual(report.read_bytes(), saved)
                run.assert_awaited_once()

    def test_live_requests_require_explicit_opt_in(self):
        with (
            patch("sys.argv", ["probe-first-output.py"]),
            patch.object(probe_module.asyncio, "run") as run,
            patch("sys.stderr"),
        ):
            with self.assertRaises(SystemExit) as error:
                probe_module.main()
        self.assertEqual(error.exception.code, 2)
        run.assert_not_called()
