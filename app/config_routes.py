"""Client configuration routes with settings and process ownership kept outside."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from codex_config import normalize_proxy_targets


def create_config_router(
    *,
    client_proxy_config_service,
    background_proxy_manager,
    save_settings,
    decorate_settings,
    parse_json_request,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/config/client-proxy")
    async def client_proxy_status_api():
        payload = client_proxy_config_service.proxy_client_status_payload()
        settings = payload.get("settings")
        if isinstance(settings, dict):
            payload["settings"] = decorate_settings(settings)
        return JSONResponse(content=payload)

    @router.post("/api/config/client-proxy/settings")
    async def client_proxy_settings_api(request: Request):
        payload = await parse_json_request(request)
        result = save_settings(payload)
        return JSONResponse(content=result)

    @router.post("/api/config/client-proxy")
    async def client_proxy_install_api(request: Request):
        payload = await parse_json_request(request)
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Request body must be an object"
            )
        targets = normalize_proxy_targets(payload)
        action = payload.get("action", "enable")
        if not isinstance(action, str):
            raise HTTPException(
                status_code=400, detail='Action must be "enable" or "disable".'
            )

        action = action.strip().lower()
        if action == "install":
            action = "enable"
        if action not in {"enable", "disable"}:
            raise HTTPException(
                status_code=400, detail='Unsupported action. Use "enable" or "disable".'
            )

        clients = {}

        for target in targets:
            try:
                if action == "disable":
                    clients[target] = client_proxy_config_service.disable_target(target)
                else:
                    clients[target] = client_proxy_config_service.enable_target(target)
            except Exception as exc:
                clients[target] = client_proxy_config_service.empty_proxy_status(target)
                clients[target]["error"] = str(exc)
                clients[target]["status_message"] = "failed to write config"

        return JSONResponse(
            content={
                "clients": clients,
                "message": (
                    "Connection enabled for: "
                    if action == "enable"
                    else "Connection disabled for: "
                )
                + (
                    ", ".join(
                        target
                        for target, payload in sorted(clients.items())
                        if not payload.get("error")
                    )
                    or "none"
                ),
            }
        )

    @router.get("/api/config/background-proxy")
    async def background_proxy_status_api():
        return JSONResponse(content=background_proxy_manager.status_payload())

    @router.post("/api/config/background-proxy")
    async def background_proxy_config_api(request: Request):
        payload = await parse_json_request(request)
        action = payload.get("action")
        try:
            if action == "enable_startup":
                result = background_proxy_manager.enable_startup()
                message = "Background startup enabled."
            elif action == "disable_startup":
                result = background_proxy_manager.disable_startup()
                message = "Background startup disabled."
            elif action == "install_shell_commands":
                result = background_proxy_manager.install_shell_commands()
                message = "Shell commands installed."
            elif action == "uninstall_shell_commands":
                result = background_proxy_manager.uninstall_shell_commands()
                message = "Shell commands removed."
            else:
                raise HTTPException(
                    status_code=400, detail="Unsupported background service action."
                )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to update background service setup: {exc}",
            ) from exc
        return JSONResponse(content={**result, "message": message})

    return router
