# Windows relocation and desktop launch implementation plan

> **For agentic workers:** Implement these tasks inline in the authorized session. Preserve all existing uncommitted work.

**Goal:** Move the complete project to `D:\Work\ghcp_proxy`, organize its files, and provide double-click start and stop controls without a terminal.

**Architecture:** Keep the existing executable Python application and its module imports together in `app/`. Put regressions in `tests/`, maintenance scripts in `tools/`, and keep documentation in `docs/`. Windows Script Host starts the repository's Python GUI interpreter; the launcher manages a hidden server and opens the existing dashboard.

**Tech Stack:** Python 3.12 virtual environment, FastAPI/Uvicorn, Windows Script Host and Win32 synchronization objects; no new dependencies.

## Global constraints

- Preserve `.git`, uncommitted changes, the virtual environment, and existing temporary files.
- Keep runtime settings and history in their existing AppData directories.
- Use `.venv/Scripts/python.exe` for Python tooling and selected offline regressions; no broad pytest discovery.
- Do not search or edit `mutants/`.
- Run live checks with isolated runtime directories and no real upstream calls.
- Use native PowerShell moves with verified absolute source and destination paths.

### Task 1: Organize the executable application

**Files:** Move root application modules into `app/`, root `test_*.py` into `tests/`, `dashboard.html` into `app/static/`, `prompts/` into `app/prompts/`, and `install_macos.sh` into `tools/`. Update `app/constants.py`, `app/background_proxy.py`, test/resource lookups, and tool bootstraps.

- [x] Run the existing offline suite as a baseline: 141 tests passed.
- [x] Move files without changing existing application behavior.
- [x] Point the test runner at both source and test directories and pass the source path to subprocess regressions:

```python
sys.path[:0] = [str(repo / "app"), str(repo / "tests")]
# Add PYTHONPATH=str(repo / "app") to the runner's isolated environment.
```

- [x] Resolve the dashboard from `app/static/dashboard.html`, retain prompts beside application modules, and update the macOS installer to use its parent as the repository root.
- [x] Verify all selected offline tests still pass after relocation of modules.

### Task 2: Add desktop lifecycle controls

**Files:** Create `app/windows_launcher.py`, `启动.vbs`, `停止.vbs`, and `tests/test_windows_launcher.py`; update the entrypoint in `app/proxy.py`, startup/profile generation in `app/background_proxy.py`, and the test runner.

**Interfaces:** `start_proxy()` launches or reuses the application. `stop_proxy()` requests graceful shutdown. `shutdown_listener(server)` connects a Windows named event to Uvicorn's `server.should_exit` flag. The server identity check consumes the existing `/api/config/background-proxy` endpoint and its `pid_file` field.

- [x] Check server identity before reusing port 8000 and check `/ui` returns HTTP 200 before opening the browser.
- [x] Serialize desktop lifecycle actions with a Windows mutex; use a named event for graceful shutdown so existing Codex config restoration executes.
- [x] Start `.venv/Scripts/python.exe -B app/proxy.py` with `CREATE_NO_WINDOW`, redirected logs, and an explicit working directory.
- [x] Display actionable launch failures in a native message box, including the log path.
- [x] Verify reuse of an existing server, refusal of an unrelated server, startup failures, and graceful shutdown through focused regressions and an isolated Windows process check.
- [x] Make existing login startup and PowerShell controls use the same launcher.

### Task 3: Move and verify the complete project

**Files:** Update `readme.md`, `AGENTS.md`, and `docs/excel-codex-tools.md`; move the repository to `D:\Work\ghcp_proxy`.

- [x] Document double-click operation, directory responsibilities, log locations, and developer commands.
- [x] Verify the source resolves to `D:\Tools\ghcp_proxy`, the destination resolves beneath `D:\Work`, and the destination does not already exist; move using `Move-Item -LiteralPath`.
- [x] Repair virtual-environment activation paths and installed console entrypoints for the new location without changing installed package versions.
- [x] Run offline regressions at the destination and a live isolated start/reopen/stop check through the actual VBS entries.
- [x] Verify the local dashboard route and inspect it with the current CUA browser API.
- [x] Remove only this task's temporary test artifacts and verify no test processes remain.

## Completion evidence

- Final offline regressions: 148 tests passed in the relocated repository.
- Python syntax: all 37 source, test, and tool modules parsed successfully.
- Native Windows check: actual `WScript.exe` launches of the VBS entries passed concurrent start, repeat start, and graceful stop. Three launches produced one server; all recorded Python processes had no console window.
- HTTP: `/ui`, `/api/config/background-proxy`, and `/v1/models` returned 200. CUA confirmed the dashboard displayed “代理运行中”.
- The browser-open callback was recorded in the isolated check; the dashboard itself was verified in the in-app browser. No real upstream transport was allowed.
- Shutdown completed normally, removed its PID file, left no test server, and preserved the hashes of the user's Codex and proxy configuration files.
- Virtualenv: fixed `pyvenv.cfg`, activation paths, and nine installed entrypoints. `pip.exe --version` and `uvicorn.exe --version` resolve under the new path; no old repository paths remain in virtualenv scripts. Package versions were preserved.
- Generated PowerShell controls parsed successfully. The registered `.vbs` double-click association points to Windows Script Host.
- All project files, Git metadata, existing temporary data, and uncommitted work now live at `D:\Work\ghcp_proxy`. A lock prevented moving the root directory itself, so its contents were moved with native PowerShell commands.
- The old `D:\Tools\ghcp_proxy` and its `.git` subdirectory contain no files. Automatic approval review rejected the command that also removed the old empty directory (`blocked by policy`); directory deletion was omitted and these empty directories remain.
