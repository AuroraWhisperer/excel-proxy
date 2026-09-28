# Repository Notes

## User Environment

The user does not have a mobile client.

## Repository Ownership

This is the independent repository `AuroraWhisperer/excel-proxy`.
Use only its `origin` remote for normal fetches and pushes. Its Git history
starts with the current Excel-only application snapshot.

## Layout and Windows Launch

The project lives at `D:\Work\ghcp_proxy`. Application modules and resources
are in `app/`, offline regressions in `tests/`, and maintenance entrypoints in
`tools/`. Windows users double-click `启动.vbs` to open the native dashboard
window. Closing its title-bar X shuts down the proxy normally. The launcher
uses the repository virtual environment and `app/windows_launcher.py`.
For console debugging, run `.venv/Scripts/python.exe -B app/proxy.py`.
Runtime settings and history remain in their existing AppData directories.

## Python Environment

Use the repo-local virtualenv for all Python tests and tooling:

```sh
./.venv/bin/pytest ...
```

Do not use bare `pytest` or `python3 -m pytest` unless you have first verified
they resolve inside `./.venv`. The system Python on this machine may not have
project dependencies such as `pytest`, `httpx`, or `fastapi` installed.

## Tests

Run the selected offline regressions through the repository virtualenv:

```sh
./.venv/bin/python -B tools/run-offline-tests.py
```

On Windows, use `./.venv/Scripts/python.exe -B tools/run-offline-tests.py`.
Optional unittest module/class/method names select a focused run. The runner
isolates config, state and cache directories and blocks real httpx transports.
Use syntax checks and targeted manual verification for changes outside this
suite; do not run broad pytest discovery.

The `mutants/` directory is a generated mutation-testing workspace that mirrors
the source tree. Do not include it in normal searches or manual edits unless
the task is specifically about mutation testing.

## Prompt Debugging

Runtime request traces live under:

```sh
~/Library/Application\ Support/ghcp_proxy/request-trace.jsonl
```

Full prompt and request-body traces are only written when
`debug_prompt_logging_enabled` is true in the client proxy settings. It defaults
to false.

For request prompt drill-down work, keep the scope narrow. Current `main`
already lazy-loads prompts from recent usage events; file-backed prompt archives
belong in `proxy.py`, `util.py`, constants/docs, and focused tests. Do not revive
older UI2 or safeguard-telemetry stash chunks unless that is the explicit task.
