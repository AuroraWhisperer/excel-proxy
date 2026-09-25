# Close the desktop window to exit

**Goal:** Double-clicking the launcher opens one native Excel Proxy window. Closing its title-bar X ends the proxy normally and restores Codex configuration according to the existing setting.

**Approach:** Host the existing dashboard in pywebview using the installed WebView2 runtime. Keep the existing server, local stop event, and shutdown behavior. Hold a separate desktop-instance mutex for the window lifetime; repeated launches signal the existing window to restore and activate. Always stop the server when the GUI loop returns or fails.

**Scope:** `app/windows_launcher.py`, `app/background_proxy.py`, `requirements.txt`, Windows launch documentation, and focused tests. Remove the root stop script from the normal user flow. Keep the explicit headless CLI option for maintenance.

1. Add regressions for window close, failed initialization, repeated launch, and HTTP failure before showing a window. Verify they fail against the old browser launcher.
2. Implement the native window and lifetime management. Verify focused regressions and all selected offline contracts.
3. Run the real VBS launcher with isolated config, cache, state and Codex paths. Disable session discovery and upstream requests in the test fixture. Verify one window and one server, minimizing without exit, repeat launch restoring the window, native `FormClosing`/`FormClosed` on close, clean server shutdown, and unchanged real user configuration.
4. Inspect the actual WebView2 renderer using the persistent Playwright skill. Check the original window size, dashboard state, viewport fit, and screenshot; also check closing and reopening a fresh instance. Clean up all test processes and temporary data.

The window uses the existing dashboard styling. Page reloads and minimize operations must keep the proxy running. The test invokes the framework's native window close path, which is the same WinForms `FormClosing` path used by the title-bar X.

## Verification

- Added regressions reproduced the old launcher's missing window lifetime handling before implementation. All 151 selected offline regressions pass; the focused nine launcher tests also pass after final review.
- The actual VBS entry opened a visible native WebView2 window with no console. Playwright checked the owned renderer, `/ui` returned HTTP 200, and the dashboard showed the running state. Reloading and expanding settings worked without stopping the server; no horizontal overflow was present.
- Native capture confirmed the standard title-bar close, minimize, and maximize controls. The window was 1120 × 820 logical pixels (1680 × 1230 physical pixels at the current Windows scaling).
- The isolated native lifecycle check passed minimizing, restoring through a repeated launch, retaining one window/server, native `UserClosing`, normal server shutdown, removal of the PID file, and restoration of a seeded Codex config. A fresh launch and second close also passed. The final isolated run left the real user configuration hashes unchanged.
- No test Python processes or listener on port 8000 remained after verification. The Playwright renderer connection disconnected on native close.
- Automatic approval review blocked an explicit cleanup command for an earlier temporary QA directory and a temporary screenshot (`blocked by policy`). Those temporary artifacts remain outside version control; that deletion was not retried through another tool. The final successful fixture cleaned up its own isolated temporary directory normally.
