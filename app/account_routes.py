"""Local account and session routes with explicit request-orchestration dependencies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

import account_balances
import account_login
import codex_quota
import dashboard as dashboard_module
import excel_models
import excel_session
import excel_session_capture
import responses_protocol
import proxy_accounts
from codex_config import ProxyClientConfigService
from usage_tracking import UsageTracker


@dataclass
class AccountRouteDependencies:
    usage_tracker: UsageTracker
    proxy_login_service: account_login.AccountLoginService
    client_proxy_config_service: ProxyClientConfigService
    activation_lock: asyncio.Lock
    parse_json_request: Callable[[Request], Awaitable[dict]]
    dispatch_response: Callable[..., Awaitable[Response]]
    run_connection_test: Callable[..., Awaitable[Response]]


def create_account_router(dependencies: AccountRouteDependencies) -> APIRouter:
    router = APIRouter()

    def _require_local_quota_request(request: Request):
        origin = request.headers.get("origin")
        local_hosts = {"127.0.0.1", "localhost", "::1"}
        if (
            request.url.hostname not in local_hosts
            or (request.client and request.client.host not in local_hosts)
            or (origin and origin != f"{request.url.scheme}://{request.url.netloc}")
        ):
            raise HTTPException(
                status_code=403, detail="Open quota checking from the local dashboard."
            )

    @router.get("/api/account-quota")
    async def account_quota_status_api(request: Request):
        _require_local_quota_request(request)
        return JSONResponse(
            codex_quota.quota_service.snapshot(), headers={"Cache-Control": "no-store"}
        )

    @router.post("/api/account-quota")
    async def account_quota_refresh_api(request: Request):
        _require_local_quota_request(request)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(
                status_code=415, detail="Use application/json for quota checks."
            )
        await dependencies.parse_json_request(request)
        payload = await asyncio.to_thread(codex_quota.quota_service.refresh)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    async def _balance_json(request: Request):
        _require_local_quota_request(request)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(
                status_code=415, detail="请使用 application/json 导入账号。"
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > account_balances.MAX_BYTES:
                raise HTTPException(status_code=413, detail="账号 JSON 不能超过 1 MB。")
        try:
            return json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(
                status_code=400, detail="无法解析 JSON，请检查文件格式。"
            ) from None

    async def _balance_response(operation, *args):
        try:
            payload = await asyncio.to_thread(operation, *args)
            payload["login"] = account_login.login_service.snapshot()
            if payload.get("accounts"):
                payload = await asyncio.to_thread(
                    dashboard_module.attach_account_cycle_estimates,
                    payload,
                    dependencies.usage_tracker.snapshot_all_usage_events(),
                )
        except account_balances.BalanceError as exc:
            return JSONResponse(
                {"detail": str(exc)},
                status_code=exc.status_code,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @router.get("/api/account-balances")
    async def account_balances_api(request: Request):
        _require_local_quota_request(request)
        return await _balance_response(account_balances.balance_store.snapshot)

    @router.get("/api/account-balances/login")
    async def account_login_status_api(request: Request):
        _require_local_quota_request(request)
        return JSONResponse(
            account_login.login_service.snapshot(),
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/account-balances/login/{action}")
    async def account_login_action_api(action: str, request: Request):
        payload = await _balance_json(request)
        operation, args = _account_login_operation(
            account_login.login_service, action, payload
        )
        try:
            result = await asyncio.to_thread(operation, *args)
        except account_balances.BalanceError as exc:
            return JSONResponse(
                {"detail": str(exc)},
                status_code=exc.status_code,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    def _account_login_operation(service, action, payload):
        if action == "start":
            if payload == {}:
                return service.start, ()
            if (
                isinstance(payload, dict)
                and set(payload) == {"credentials"}
                and isinstance(payload["credentials"], str)
            ):
                return service.start, (payload["credentials"],)
        if action == "cancel" and payload == {}:
            return service.cancel, ()
        raise HTTPException(
            status_code=400, detail="请选择手动登录，或提交一行账号----密码---2FA密钥。"
        )

    @router.post("/api/account-balances/import")
    async def account_balances_import_api(request: Request):
        payload = await _balance_json(request)
        return await _balance_response(
            account_balances.balance_store.import_accounts, payload
        )

    @router.post("/api/account-balances/rate")
    async def account_balances_rate_api(request: Request):
        payload = await _balance_json(request)
        if not isinstance(payload, dict) or "credit_unit_usd" not in payload:
            raise HTTPException(status_code=400, detail="请提供美元折算单价。")
        return await _balance_response(
            account_balances.balance_store.set_rate, payload["credit_unit_usd"]
        )

    @router.post("/api/account-balances/{record_id}/refresh")
    async def account_balances_refresh_api(record_id: str, request: Request):
        await _balance_json(request)
        return await _balance_response(
            account_balances.balance_store.refresh, record_id
        )

    @router.post("/api/account-balances/{record_id}/test")
    async def account_balances_test_api(record_id: str, request: Request):
        payload = await _balance_json(request)
        if not isinstance(payload, dict) or payload:
            raise HTTPException(status_code=400, detail="连接测试无需额外参数。")
        try:
            headers = await asyncio.to_thread(
                account_balances.balance_store.headers_for, record_id
            )
        except account_balances.BalanceError as exc:
            return JSONResponse(
                {"detail": str(exc)},
                status_code=exc.status_code,
                headers={"Cache-Control": "no-store"},
            )
        return await dependencies.run_connection_test(
            request, excel_models.MODEL_ID, session_headers=headers
        )

    @router.post("/api/account-balances/{record_id}/remove")
    async def account_balances_remove_api(record_id: str, request: Request):
        await _balance_json(request)
        if dependencies.activation_lock.locked():
            raise HTTPException(
                status_code=409, detail="正在验证并切换账号，请稍后再移除。"
            )
        async with dependencies.activation_lock:
            return await _balance_response(_remove_local_account, record_id, True)

    @router.post("/api/account-balances/{record_id}/reset")
    async def account_balances_reset_api(record_id: str, request: Request):
        payload = await _balance_json(request)
        if (
            not isinstance(payload, dict)
            or payload.get("confirm") is not True
            or "checked_at" not in payload
        ):
            raise HTTPException(status_code=400, detail="使用重置卡需要明确确认。")
        return await _balance_response(
            account_balances.balance_store.reset_credit,
            record_id,
            payload["checked_at"],
        )

    def _remove_local_account(record_id, from_balances=False):
        """Remove the same local identity from both lists without selecting another."""
        login_store = proxy_accounts.proxy_account_store
        balance_store = account_balances.balance_store
        login_ids = {row["id"] for row in login_store.snapshot()["accounts"]}
        balance_ids = {row["id"] for row in balance_store.snapshot()["accounts"]}
        if record_id not in login_ids | balance_ids:
            raise account_balances.BalanceError("账号不存在，请刷新页面。", 404)
        # Respect an in-flight reset before removing credentials used for routing.
        if record_id in balance_ids:
            balance_store.remove(record_id)
        if record_id in login_ids:
            login_store.remove(record_id)
        return balance_store.snapshot() if from_balances else login_store.snapshot()

    async def _proxy_accounts_response(operation, *args):
        try:
            result = await asyncio.to_thread(operation, *args)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except account_balances.BalanceError as exc:
            return JSONResponse(
                {"detail": str(exc)},
                status_code=exc.status_code,
                headers={"Cache-Control": "no-store"},
            )

    @router.get("/api/proxy-accounts")
    async def proxy_accounts_api(request: Request):
        _require_local_quota_request(request)
        return await _proxy_accounts_response(
            proxy_accounts.proxy_account_store.snapshot
        )

    @router.get("/api/proxy-accounts/login")
    async def proxy_login_status_api(request: Request):
        _require_local_quota_request(request)
        return JSONResponse(
            dependencies.proxy_login_service.snapshot(),
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/proxy-accounts/login/{action}")
    async def proxy_login_action_api(action: str, request: Request):
        payload = await _balance_json(request)
        operation, args = _account_login_operation(
            dependencies.proxy_login_service, action, payload
        )
        return await _proxy_accounts_response(operation, *args)

    @router.delete("/api/proxy-accounts/{record_id}")
    async def proxy_account_remove_api(record_id: str, request: Request):
        _require_local_quota_request(request)
        if dependencies.activation_lock.locked():
            raise HTTPException(
                status_code=409, detail="正在验证并切换账号，请稍后再移除。"
            )
        async with dependencies.activation_lock:
            return await _proxy_accounts_response(_remove_local_account, record_id)

    @router.post("/api/proxy-accounts/{record_id}/activate")
    async def proxy_account_activate_api(record_id: str, request: Request):
        payload = await _balance_json(request)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"model"}
            or excel_models.excel_model_id(payload["model"]) is None
        ):
            raise HTTPException(status_code=400, detail="请选择要验证的 Excel 模型。")
        if dependencies.activation_lock.locked():
            raise HTTPException(
                status_code=409, detail="已有账号正在验证，请稍后再试。"
            )
        async with dependencies.activation_lock:
            try:
                selection = await asyncio.to_thread(
                    proxy_accounts.proxy_account_store.snapshot
                )
                current = next(
                    (row for row in selection["accounts"] if row["id"] == record_id),
                    None,
                )
                if (
                    current
                    and current["active"]
                    and not current["pending"]
                    and not current["expired"]
                ):
                    return JSONResponse(
                        {
                            **selection,
                            "message": "该账号已启用，未重复发送验证请求。",
                            "warnings": [],
                        },
                        headers={"Cache-Control": "no-store"},
                    )
                headers = await asyncio.to_thread(
                    proxy_accounts.proxy_account_store.headers_for, record_id
                )
                async with asyncio.timeout(30):
                    response = await dependencies.dispatch_response(
                        request,
                        {
                            "model": payload["model"],
                            "stream": False,
                            "input": "Reply with exactly OK.",
                            "tool_choice": "none",
                            "reasoning": {"effort": "medium"},
                        },
                        session_headers=headers,
                    )
                result = json.loads(response.body)
                if (
                    response.status_code != 200
                    or result.get("status") != "completed"
                    or not (
                        responses_protocol.extract_response_output_text(result) or ""
                    ).strip()
                ):
                    messages = {
                        401: "该账号登录已失效，请重新登录。",
                        403: "该账号不能使用所选 Excel/BPS 模型。",
                        429: "该账号暂时受限，请稍后重试或手动选择其他账号。",
                    }
                    raise account_balances.BalanceError(
                        messages.get(
                            response.status_code,
                            "未获得完整模型回复，请检查网络或在高级设置中更换测试模型。",
                        )
                        + " 原连接账号保持不变。",
                        response.status_code if response.status_code >= 400 else 502,
                    )
                state = await asyncio.to_thread(
                    proxy_accounts.proxy_account_store.activate,
                    record_id,
                    headers["authorization"],
                )
            except TimeoutError:
                return JSONResponse(
                    {"detail": "验证超时，原连接账号保持不变。请稍后重试。"},
                    status_code=504,
                )
            except account_balances.BalanceError as exc:
                return JSONResponse(
                    {"detail": str(exc)},
                    status_code=exc.status_code,
                    headers={"Cache-Control": "no-store"},
                )
            warnings = []
            # Balance queries only receive access credentials, never refresh tokens.
            try:
                await asyncio.to_thread(
                    account_balances.balance_store.import_accounts,
                    {
                        "access_token": headers["authorization"][7:],
                        "account_id": headers["chatgpt-account-id"],
                    },
                )
            except account_balances.BalanceError:
                warnings.append("服务已启用，但余额查询账号未能同步。")
            try:
                client = await asyncio.to_thread(
                    dependencies.client_proxy_config_service.enable_target, "codex"
                )
                if client.get("error") or not client.get("configured"):
                    warnings.append(
                        "服务已启用，但 Codex 配置未完成，请在高级设置中重试接入。"
                    )
            except Exception:
                warnings.append(
                    "服务已启用，但 Codex 配置未完成，请在高级设置中重试接入。"
                )
            return JSONResponse(
                {
                    **state,
                    "message": "账号验证通过，已启用连接。首次接入请重启 Codex。",
                    "warnings": warnings,
                },
                headers={"Cache-Control": "no-store"},
            )

    @router.get("/api/config/excel-session")
    async def excel_session_status_api():
        try:
            selection = await asyncio.to_thread(
                proxy_accounts.proxy_account_store.snapshot
            )
        except account_balances.BalanceError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)
        if selection["source"] != "excel":
            active = next((row for row in selection["accounts"] if row["active"]), None)
            return JSONResponse(
                {
                    "source": selection["source"],
                    "configured": active is not None,
                    "expired": bool(
                        active and active["expired"] and not active["renewable"]
                    ),
                    "expires_at": active["expires_at"] if active else None,
                    "account_name": active["name"] if active else None,
                    "renewable": bool(active and active["renewable"]),
                    "persisted": True,
                    "models": [
                        {
                            "id": key,
                            "display_name": excel_models.LOCAL_MODEL_CAPABILITIES[key][
                                "display_name"
                            ],
                        }
                        for key in excel_models.MODEL_IDS
                    ],
                    "default_model": excel_models.MODEL_ID,
                },
                headers={"Cache-Control": "no-store"},
            )
        excel_session_capture.refresh_macos_excel_session(
            excel_session.excel_session_store,
        )
        excel_session_capture.refresh_windows_excel_session(
            excel_session.excel_session_store,
        )
        return JSONResponse(
            content={
                **excel_session.excel_session_store.status(),
                "source": "excel",
                "capture": excel_session_capture.cached_session_reader_status(),
                "models": [
                    {
                        "id": model_id,
                        "display_name": excel_models.LOCAL_MODEL_CAPABILITIES[model_id][
                            "display_name"
                        ],
                    }
                    for model_id in excel_models.MODEL_IDS
                ],
                "default_model": excel_models.MODEL_ID,
            }
        )

    @router.post("/api/config/excel-session/test")
    async def excel_session_test_api(request: Request):
        # This manual, quota-consuming action is available only to the local UI
        # and JSON API clients, never cross-site browser forms or DNS rebinding.
        origin = request.headers.get("origin")
        if request.url.hostname not in {"127.0.0.1", "localhost", "::1"} or (
            origin and origin != f"{request.url.scheme}://{request.url.netloc}"
        ):
            raise HTTPException(
                status_code=403,
                detail="Open the connection test from the local dashboard.",
            )
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(
                status_code=415, detail="Use application/json for connection tests."
            )
        payload = await dependencies.parse_json_request(request)
        model = excel_models.excel_model_id(payload.get("model"))
        if model is None:
            raise HTTPException(status_code=400, detail="Select a listed Excel model.")
        return await dependencies.run_connection_test(request, model)

    @router.post("/api/config/excel-session")
    async def excel_session_config_api(request: Request):
        payload = await dependencies.parse_json_request(request)
        action = str(payload.get("action") or "").strip().lower()
        if action in {"cancel_capture", "cancel_read"}:
            return JSONResponse(
                content={
                    **excel_session.excel_session_store.status(),
                    "capture": excel_session_capture.cached_session_reader_status(),
                }
            )
        if action in {"capture", "read_cached"}:
            if dependencies.activation_lock.locked():
                raise HTTPException(
                    status_code=409,
                    detail="正在验证并切换账号，请稍后再读取 Excel 会话。",
                )
            async with dependencies.activation_lock:
                for refresh in (
                    excel_session_capture.refresh_macos_excel_session,
                    excel_session_capture.refresh_windows_excel_session,
                ):
                    await asyncio.to_thread(
                        refresh, excel_session.excel_session_store, force=True
                    )
                status = excel_session.excel_session_store.status()
                if status.get("configured") and not status.get("expired"):
                    try:
                        await asyncio.to_thread(
                            proxy_accounts.proxy_account_store.use_excel
                        )
                    except account_balances.BalanceError as exc:
                        return JSONResponse(
                            {"detail": str(exc)}, status_code=exc.status_code
                        )
            return JSONResponse(
                content={
                    **excel_session.excel_session_store.status(),
                    "capture": excel_session_capture.cached_session_reader_status(),
                }
            )
        try:
            status = excel_session.excel_session_store.configure(
                payload.get("headers"),
                tools_version_id=payload.get("tools_version_id"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(
            content={
                **status,
                "capture": excel_session_capture.cached_session_reader_status(),
            }
        )

    @router.delete("/api/config/excel-session")
    async def excel_session_clear_api():
        return JSONResponse(
            content={
                **excel_session.excel_session_store.clear(),
                "capture": excel_session_capture.cached_session_reader_status(),
            }
        )

    return router
