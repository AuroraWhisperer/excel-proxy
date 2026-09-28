"""Opt-in, quota-consuming first-output diagnostics; never restart the proxy."""

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
import uuid

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from usage_tracking import SSEUsageCapture


async def probe(client, url, body, headers=None):
    started = time.perf_counter()
    result = {"transport_events": []}
    capture = SSEUsageCapture("responses")

    def elapsed():
        return round((time.perf_counter() - started) * 1000, 1)

    async def trace(name, info):
        # Trace info can contain authorization headers; retain event names only.
        if any(
            phase in name
            for phase in (
                "connect_tcp",
                "start_tls",
                "send_request_body",
                "receive_response_headers",
            )
        ):
            result["transport_events"].append({"event": name, "ms": elapsed()})

    try:
        async with client.stream(
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
            else:
                await response.aread()
        result.update(usage=capture.usage, terminal=capture.terminal_event_type)
    except httpx.RequestError as error:
        result["error"] = type(error).__name__
    result["duration_ms"] = elapsed()
    return result


async def run(args):
    session = "latency-probe-" + uuid.uuid4().hex
    headers = None
    url = "http://127.0.0.1:8000/v1/responses"
    if args.direct:
        import excel_upstream

        # Load existing credentials without persisting, recapturing, or logging them.
        store = excel_upstream.ExcelSessionStore(excel_upstream.SESSION_FILE)
        store.load()
        headers = store.request_headers(stream=True)
        url = excel_upstream.RESPONSES_URL
    async with httpx.AsyncClient(
        trust_env=args.direct,
        http2=False,
        timeout=90,
        limits=httpx.Limits(
            max_connections=8, max_keepalive_connections=4, keepalive_expiry=300
        ),
    ) as client:
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
                body = excel_upstream.prepare_responses_body(
                    body, tools_version_id=store.tools_version_id()
                )
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
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
