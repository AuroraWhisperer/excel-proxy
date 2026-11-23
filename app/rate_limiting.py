"""Rate limiting for upstream Excel API requests."""

import asyncio
import os
import secrets
import sys
import time
from collections import deque
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field

import httpx

from constants import UPSTREAM_REQUESTS_PER_WINDOW, UPSTREAM_REQUEST_WINDOW_SECONDS
from excel_task_identity import account_key, task_identity_store
from upstream_errors import ExcelResponseError, certificate_verification_failed


def _limit_setting(name, default, minimum, maximum):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
        if minimum <= value <= maximum:
            return value
    except ValueError:
        pass
    print(f"Ignoring invalid {name}. Expected {minimum}-{maximum}, using {default}.", file=sys.stderr)
    return default


_upstream_rate_limit_lock = asyncio.Lock()
_upstream_request_timestamps = deque()
_upstream_starts_per_window = _limit_setting(
    "GHCP_UPSTREAM_STARTS_PER_SECOND", 0, 0, UPSTREAM_REQUESTS_PER_WINDOW
)
_TRANSIENT_UPSTREAM_RETRY_ATTEMPTS = 2
_TRANSIENT_UPSTREAM_RETRY_DELAY_SECONDS = 0.1

# Only retry failures that happen while establishing the connection. Read,
# write, close, and protocol failures can occur after a non-idempotent model
# request was accepted; automatically replaying those POSTs can generate and
# bill the same turn twice.
_TRANSIENT_UPSTREAM_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
)


class GenerationQueueError(ExcelResponseError):
    def __init__(self, code, message):
        super().__init__(code, message, status_code=429, headers={"Retry-After": "1"})


@dataclass
class _AccountSlots:
    semaphore: asyncio.Semaphore
    users: int = 0
    occupied: set[int] = field(default_factory=set)


class GenerationAdmission:
    """Bound live generation transports per account and queued work per process."""

    def __init__(self, per_account=4, max_pending=64, wait_seconds=30):
        self.per_account = per_account
        self.max_pending = max_pending
        self.wait_seconds = wait_seconds
        self._accounts = {}
        self.pending = 0

    def snapshot(self):
        return {
            "active": sum(pool.users for pool in self._accounts.values()) - self.pending,
            "pending": self.pending,
            "max_active_per_account": self.per_account,
            "max_pending": self.max_pending,
            "wait_seconds": self.wait_seconds,
            "max_starts_per_second": _upstream_starts_per_window,
        }

    @asynccontextmanager
    async def slot(self, account):
        pool = self._accounts.get(account)
        if pool is None:
            pool = self._accounts[account] = _AccountSlots(asyncio.Semaphore(self.per_account))
        pool.users += 1
        try:
            if pool.semaphore.locked():
                if self.pending >= self.max_pending:
                    raise GenerationQueueError("excel_queue_full", "The local generation queue is full.")
                self.pending += 1
                try:
                    await pool.semaphore.acquire()
                finally:
                    self.pending -= 1
            else:
                await pool.semaphore.acquire()
            slot_number = next(slot for slot in range(1, self.per_account + 1) if slot not in pool.occupied)
            pool.occupied.add(slot_number)
            try:
                yield slot_number
            finally:
                pool.occupied.remove(slot_number)
                pool.semaphore.release()
        finally:
            pool.users -= 1
            if not pool.users:
                del self._accounts[account]


generation_admission = GenerationAdmission(
    per_account=_limit_setting("GHCP_GENERATION_CONCURRENCY", 4, 1, 20),
    max_pending=_limit_setting("GHCP_GENERATION_MAX_PENDING", 64, 0, 64),
    wait_seconds=_limit_setting("GHCP_GENERATION_WAIT_SECONDS", 30, 1, 300),
)


class AdmittedStream(httpx.AsyncByteStream):
    """Keep the slot until httpx closes the body, including before first read."""

    def __init__(self, inner, slot):
        self.inner = inner
        self.slot = slot

    def __aiter__(self):
        return self.inner.__aiter__()

    async def aclose(self):
        try:
            await self.inner.aclose()
        finally:
            await self.slot.aclose()


def _prune_upstream_request_timestamps(now: float):
    cutoff = now - UPSTREAM_REQUEST_WINDOW_SECONDS
    while _upstream_request_timestamps and _upstream_request_timestamps[0] <= cutoff:
        _upstream_request_timestamps.popleft()


async def throttle_upstream_request(now: float | None = None):
    if not _upstream_starts_per_window:
        return
    while True:
        async with _upstream_rate_limit_lock:
            current_time = time.monotonic() if now is None else now
            _prune_upstream_request_timestamps(current_time)

            if len(_upstream_request_timestamps) < _upstream_starts_per_window:
                _upstream_request_timestamps.append(current_time)
                return

            oldest = _upstream_request_timestamps[0]
            delay = max(0.0, (oldest + UPSTREAM_REQUEST_WINDOW_SECONDS) - current_time)

        await asyncio.sleep(delay)


async def wait_for_downstream_disconnect(request):
    """Observe the ASGI channel after the handler has consumed the request body."""
    while (await request.receive())["type"] != "http.disconnect":
        pass


async def _wait_for_local_start(start, downstream_request):
    if downstream_request is None:
        return await start
    ready = asyncio.create_task(start)
    disconnected = asyncio.create_task(wait_for_downstream_disconnect(downstream_request))
    try:
        done, _ = await asyncio.wait({ready, disconnected}, return_when=asyncio.FIRST_COMPLETED)
        if disconnected in done:
            disconnected.result()
            raise ExcelResponseError(
                "client_disconnected", "The client disconnected before generation started.", status_code=499
            )
        return ready.result()
    finally:
        for task in (ready, disconnected):
            task.cancel()
        await asyncio.gather(ready, disconnected, return_exceptions=True)


async def throttled_client_send(
    client: httpx.AsyncClient, request: httpx.Request, *, on_send_state=None, downstream_request=None, **kwargs
) -> httpx.Response:
    account = account_key(request.headers)
    deadline = asyncio.get_running_loop().time() + generation_admission.wait_seconds
    async with AsyncExitStack() as lifetime:
        assigned_slot = None

        async def wait_to_start(attempt):
            nonlocal assigned_slot
            if attempt == 0:
                assigned_slot = await lifetime.enter_async_context(generation_admission.slot(account))
            else:
                await asyncio.sleep(_TRANSIENT_UPSTREAM_RETRY_DELAY_SECONDS)
            await throttle_upstream_request()

        for attempt in range(_TRANSIENT_UPSTREAM_RETRY_ATTEMPTS):
            try:
                async with asyncio.timeout_at(deadline):
                    await _wait_for_local_start(wait_to_start(attempt), downstream_request)
            except TimeoutError:
                raise GenerationQueueError(
                    "excel_queue_timeout", "Timed out waiting to start generation locally."
                ) from None
            body = request.extensions.get("excel_body")
            if attempt == 0 and isinstance(body, dict):
                body["metadata"] = {
                    **body.get("metadata", {}),
                    "task_id": task_identity_store.task_id(account, assigned_slot),
                    "turn_id": secrets.token_hex(32),
                }
                headers = request.headers.copy()
                headers.pop("content-length", None)
                request = httpx.Request(
                    request.method, request.url, headers=headers, json=body,
                    extensions=request.extensions,
                )
            if on_send_state is not None:
                on_send_state(True)
            try:
                response = await client.send(request, **kwargs)
            except _TRANSIENT_UPSTREAM_ERRORS as exc:
                if on_send_state is not None:
                    on_send_state(False)
                if (
                    attempt + 1 >= _TRANSIENT_UPSTREAM_RETRY_ATTEMPTS
                    or certificate_verification_failed(exc)
                    or asyncio.get_running_loop().time() >= deadline
                ):
                    raise
                continue
            if not response.is_closed:
                response.stream = AdmittedStream(response.stream, lifetime.pop_all())
            return response
