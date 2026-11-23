"""Opt-in, quota-consuming first-output diagnostics; never restart the proxy."""

import argparse
import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
import time
import uuid

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from usage_tracking import SSEUsageCapture


PROBE_DEADLINE_SECONDS = 120
TRACE_EVENTS = {
    f"{phase}.{outcome}"
    for phase in (
        "connection.connect_tcp",
        "connection.start_tls",
        "http11.send_request_body",
        "http11.receive_response_headers",
    )
    for outcome in ("started", "complete", "failed")
}


async def probe(client, url, body, headers=None):
    started = time.perf_counter()
    result = {"transport_events": []}
    capture = SSEUsageCapture("responses")

    def elapsed():
        return round((time.perf_counter() - started) * 1000, 1)

    async def trace(name, info):
        # Trace info can contain authorization headers; retain event names only.
        if name in TRACE_EVENTS:
            result["transport_events"].append({"event": name, "ms": elapsed()})

    try:
        async with asyncio.timeout(PROBE_DEADLINE_SECONDS), client.stream(
            "POST",
            url,
            json=body,
            headers=headers,
            extensions={"trace": trace},
        ) as response:
            result.update(status=response.status_code, response_headers_ms=elapsed())
            if response.status_code == 200:
                async for chunk in response.aiter_bytes():
                    if chunk:
                        result.setdefault("first_byte_ms", elapsed())
                    if capture.feed(chunk):
                        result.setdefault("first_output_ms", elapsed())
                    if capture.terminal_event_seen:
                        break
        result.update(usage=capture.usage, terminal=capture.terminal_event_type)
    except TimeoutError:
        result["error"] = "probe_deadline"
    except httpx.RequestError as error:
        result["error"] = type(error).__name__
    result["duration_ms"] = elapsed()
    return result


async def run(args, output):
    session = "latency-probe-" + uuid.uuid4().hex
    headers = None
    url = "http://127.0.0.1:8000/v1/responses"
    if args.direct:
        import excel_upstream
        from excel_websocket import websocket_mounts
        import proxy_accounts
        from upstream_client import _build_upstream_client

        # Load the active saved connection account without logging credentials.
        selection = proxy_accounts.proxy_account_store.snapshot()
        if selection["source"] != "oauth" or not selection["active_id"]:
            raise RuntimeError("No active connection account is configured.")
        headers = proxy_accounts.proxy_account_store.headers_for(
            selection["active_id"], stream=True, active=True
        )
        url = excel_upstream.RESPONSES_URL
        client = _build_upstream_client(http2_override=False, mounts_for=websocket_mounts)
    else:
        client = httpx.AsyncClient(trust_env=False, timeout=90)
    async with client:
        if not args.direct:
            response = await client.get("http://127.0.0.1:8000/v1/models")
            response.raise_for_status()
        for index, effort in enumerate(args.efforts, 1):
            body = {
                "model": "gpt-6-astra-excel",
                "stream": True,
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Reply with exactly OK. Do not call tools.",
                            }
                        ],
                    }
                ],
                "reasoning": {"effort": effort, "summary": "auto"},
                "tool_choice": "none",
                "prompt_cache_key": session,
            }
            if args.direct:
                body = excel_upstream.prepare_responses_body(body)
            result = await probe(client, url, body, headers)
            print(
                json.dumps(
                    {
                        "probe": index,
                        "route": "upstream" if args.direct else "proxy",
                        "effort": effort,
                        "session": session,
                        **result,
                    }
                ),
                file=output,
                flush=True,
            )
            if (
                result.get("status") != 200
                or result.get("terminal") != "response.completed"
            ):
                return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow real model requests that consume quota.",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Measure the configured upstream using the saved session, not the local proxy.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write JSON Lines to a new report file instead of stdout.",
    )
    parser.add_argument(
        "--efforts",
        nargs="+",
        choices=("medium", "xhigh"),
        default=["medium", "xhigh", "xhigh", "medium"],
    )
    args = parser.parse_args()
    if not args.live:
        parser.error("Live probes consume quota; explicitly pass --live to run them.")
    if len(args.efforts) > 8:
        parser.error("Limit each run to at most eight short diagnostic requests.")
    destination = nullcontext(sys.stdout)
    if args.output is not None:
        try:
            destination = open(
                args.output, "x", encoding="utf-8",
                opener=lambda path, flags: os.open(path, flags, 0o600),
            )
        except OSError:
            parser.error("Cannot create report. Choose a new file in a writable directory.")
    with destination as output:
        return asyncio.run(run(args, output))


if __name__ == "__main__":
    raise SystemExit(main())
