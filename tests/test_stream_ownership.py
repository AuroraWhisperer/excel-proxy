"""Stream extraction preserves cancellation ordering and registry ownership."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import responses_stream as streams
from upstream_request import UpstreamRequestPlan
from test_module_boundaries import imported_modules


def plan(request_id, lineage="task-a"):
    return UpstreamRequestPlan(
        request_id=request_id,
        upstream_url="https://example.test/responses",
        headers={"x-agent-task-id": lineage},
        body={},
        usage_event=None,
        requested_model=None,
        resolved_model=None,
        request_affinity="session-a",
        trace_context={"initiator_verdict": {"candidate_initiator": "user"}},
    )


class StreamOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(streams, "_ACTIVE_RESPONSES_STREAMS", {}))
        self.enterContext(
            patch.object(
                streams, "_responses_supersession_timeout_seconds", return_value=0.05
            )
        )

    async def prior_stream(self, request_plan, events, *, confirmed=True):
        ready = asyncio.Event()
        holder = []

        async def run():
            entry = streams._register_active_responses_stream(request_plan)
            holder.append(entry)
            entry.send_started = True
            entry.upstream = object()
            entry.response_ready.set()

            async def cancel_transport():
                events.append("transport")
                return "http2_rst_cancel" if confirmed else "unconfirmed", confirmed

            entry.stream_body = SimpleNamespace(
                request_transport_cancel=cancel_transport
            )
            ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("task")
                raise
            finally:
                streams._complete_active_responses_teardown(
                    entry, transport_cancel="test", confirmed=confirmed
                )

        task = asyncio.create_task(run())

        async def cleanup():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        await asyncio.wait_for(ready.wait(), 1)
        return task, holder[0]

    async def test_transport_is_cancelled_before_task_and_other_lineages_survive(self):
        events, other_events = [], []
        task, entry = await self.prior_stream(plan("old"), events)
        other, _ = await self.prior_stream(plan("other", "task-b"), other_events)
        result = await streams._supersede_active_responses_streams(plan("new"))
        self.assertEqual(events, ["transport", "task"])
        self.assertEqual([row["request_id"] for row in result], ["old"])
        self.assertTrue(entry.teardown_confirmed)
        self.assertTrue(task.done())
        self.assertFalse(other.done())
        self.assertEqual(other_events, [])

    async def test_unconfirmed_transport_blocks_supersession_without_task_cancel(self):
        events = []
        task, _ = await self.prior_stream(plan("old"), events, confirmed=False)
        with self.assertRaises(streams._ResponsesSupersessionBlocked) as error:
            await streams._supersede_active_responses_streams(plan("new"))
        self.assertEqual(events, ["transport"])
        self.assertFalse(task.done())
        self.assertEqual(
            error.exception.results[0]["blocked_reason"], "teardown_timeout"
        )

    async def test_finished_unconfirmed_entry_does_not_block_future_retry(self):
        entry = streams._register_active_responses_stream(plan("old"))
        streams._complete_active_responses_teardown(
            entry, transport_cancel="unconfirmed", confirmed=False
        )
        self.assertEqual(streams._ACTIVE_RESPONSES_STREAMS, {})
        self.assertEqual(
            await streams._supersede_active_responses_streams(plan("retry")), []
        )

    async def test_http2_reset_flushes_before_response_close(self):
        events = []
        upstream = SimpleNamespace(
            extensions={"http_version": b"HTTP/2"},
            aclose=AsyncMock(side_effect=lambda: events.append("close")),
        )
        reset = Mock(side_effect=lambda *args, **kwargs: events.append("reset"))
        flush = AsyncMock(side_effect=lambda *args: events.append("flush"))
        core = SimpleNamespace(
            _closed=False,
            _stream_id=7,
            _request=object(),
            _connection=SimpleNamespace(
                _h2_state=SimpleNamespace(reset_stream=reset),
                _write_outgoing_data=flush,
            ),
        )
        with patch.object(streams, "_httpcore_http2_stream", return_value=core):
            result = await streams._close_upstream_response(
                upstream, cancel_generation=True
            )
        self.assertEqual(result, "http2_rst_cancel")
        self.assertEqual(events, ["reset", "flush", "close"])
        reset.assert_called_once_with(7, error_code=8)
        flush.assert_awaited_once_with(core._request)

    def test_stream_owner_has_no_reverse_dependency(self):
        self.assertNotIn("proxy", imported_modules("responses_stream"))
