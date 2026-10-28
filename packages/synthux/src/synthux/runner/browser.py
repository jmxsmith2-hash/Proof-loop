"""Real browser via Playwright. The persona only ever receives screenshots from here.

DOM access exists in two places only, both used by code and never by a model:
`element_at` for the safety guard, and request routing for the network policy.
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import os
import time
from pathlib import Path
from typing import Any

from synthux.core.config import DeviceConfig, NetworkConfig, SafetyConfig, Settings
from synthux.core.logging import get_logger
from synthux.runner.types import Action, Timing
from synthux.safety.allowlist import SafetyError, check_url, is_internal
from synthux.safety.credentials import CredentialVault
from synthux.safety.guard import ELEMENT_PROBE_JS
from synthux.safety.network import RequestPolicy

log = get_logger(__name__)

# The person reads the open list and clicks the option they want; native lists can't be typed into reliably.
PICK_OPTION_JS = """
(text) => {
  const sel = document.activeElement;
  if (!sel || sel.tagName !== 'SELECT') return false;
  const want = text.trim().toLowerCase();
  const opts = Array.from(sel.options);
  const hit = opts.find(o => o.text.trim().toLowerCase() === want) ||
              opts.find(o => o.text.trim().toLowerCase().startsWith(want));
  if (!hit) return true;  // it was a dropdown; nothing matched, so the shown value stays as it is
  sel.value = hit.value;
  sel.dispatchEvent(new Event('input', {bubbles: true}));
  sel.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
}
"""

MAJOR_CHANGE = 0.01  # share of pixels that must change for a "real" screen response


def _small(png: bytes) -> Any:
    import io

    import numpy as np
    from PIL import Image

    return np.asarray(Image.open(io.BytesIO(png)).convert("L").resize((200, 125)), dtype=np.int16)


def _changed_pixels(a: bytes, b: bytes) -> int:
    """Full-resolution count of pixels that differ visibly (for changes too small to see when downscaled)."""
    import io

    import numpy as np
    from PIL import Image

    x = np.asarray(Image.open(io.BytesIO(a)).convert("L"), dtype=np.int16)
    y = np.asarray(Image.open(io.BytesIO(b)).convert("L"), dtype=np.int16)
    if x.shape != y.shape:
        return x.size
    return int((np.abs(x - y) > 10).sum())


def _diff(a: Any, b: Any) -> float:
    import numpy as np

    return float(np.mean(np.abs(a - b) > 10))


def find_chromium(settings: Settings) -> str | None:
    """Explicit setting, then $SYNTHUX_CHROMIUM, then a pre-installed Playwright Chromium."""
    if settings.browser.executable_path:
        return settings.browser.executable_path
    if os.environ.get("SYNTHUX_CHROMIUM"):
        return os.environ["SYNTHUX_CHROMIUM"]
    return None


def _fallback_chromium() -> str | None:
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""), "/opt/pw-browsers",
             str(Path.home() / ".cache/ms-playwright"),                      # Linux
             str(Path.home() / "Library/Caches/ms-playwright"),              # macOS
             str(Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "ms-playwright")]  # Windows
    for root in filter(None, roots):
        direct = Path(root) / "chromium"
        if direct.is_file() or (direct.is_symlink() and direct.exists()):
            return str(direct)
        for pattern in ("chromium-*/chrome-linux*/chrome", "chromium-*/chrome-win*/chrome.exe",
                        "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
                        "chromium_headless_shell-*/chrome-*/headless_shell",
                        "chromium_headless_shell-*/chrome-*/chrome-headless-shell",
                        "chromium_headless_shell-*/chrome-*/chrome-headless-shell.exe"):
            hits = sorted(glob.glob(str(Path(root) / pattern)))
            if hits:
                return hits[-1]
    return None


class BrowserPool:
    """One Chromium process shared by all sessions; each session gets its own context."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._pw = None
        self._browser = None

    async def __aenter__(self) -> "BrowserPool":
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        exe = find_chromium(self.settings)
        try:
            self._browser = await self._pw.chromium.launch(headless=self.settings.browser.headless,
                                                           executable_path=exe)
        except Exception as exc:  # the bundled browser for this Playwright version may be missing
            fallback = _fallback_chromium()
            if not fallback or exe == fallback:
                raise RuntimeError(f"could not start Chromium ({exc}); set browser.executable_path") from exc
            log.info("default Chromium not found; using %s", fallback)
            self._browser = await self._pw.chromium.launch(headless=self.settings.browser.headless,
                                                           executable_path=fallback)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def session(self, *, device: DeviceConfig, network: NetworkConfig, safety: SafetyConfig,
                      sandbox: bool, brand_mask: list[str] | None = None) -> "BrowserSession":
        assert self._browser is not None, "use `async with BrowserPool(...)`"
        context = await self._browser.new_context(
            viewport={"width": device.width, "height": device.height},
            device_scale_factor=device.scale, is_mobile=device.mobile, has_touch=device.mobile,
            locale="en-US", timezone_id="UTC",
        )
        if brand_mask:
            css = ", ".join(brand_mask) + " { filter: blur(9px) !important; }"
            await context.add_init_script(
                "document.addEventListener('DOMContentLoaded', () => {"
                f"const s = document.createElement('style'); s.textContent = {css!r};"
                "document.head.appendChild(s); });")
        page = await context.new_page()
        session = BrowserSession(context, page, RequestPolicy(safety, sandbox=sandbox), safety, device)
        session.quiet_exit_s = self.settings.browser.quiet_exit_seconds
        await session._install(network)
        return session


class BrowserSession:
    def __init__(self, context: Any, page: Any, policy: RequestPolicy, safety: SafetyConfig, device: DeviceConfig):
        self.context = context
        self.page = page
        self.policy = policy
        self.safety = safety
        self.device = device
        self.nav_pending = False
        self.errors: list[str] = []
        self.inflight = 0                      # network requests started and not yet finished
        self.last_net = time.monotonic()       # when a request last started or finished
        self.quiet_exit_s = 0.0

    @property
    def viewport(self) -> tuple[int, int]:
        return self.device.width, self.device.height

    async def _install(self, network: NetworkConfig) -> None:
        async def route(route: Any) -> None:
            request = route.request
            is_nav = request.is_navigation_request() and request.frame == self.page.main_frame
            decision = self.policy.decide(request.url, request.method, is_navigation=is_nav)
            if decision.allow:
                await route.continue_()
            else:
                await route.abort("blockedbyclient")

        await self.page.route("**/*", route)
        self.page.on("request", self._on_request)
        self.page.on("requestfinished", self._on_request_done)
        self.page.on("requestfailed", self._on_request_done)
        self.page.on("load", lambda *_: setattr(self, "nav_pending", False))
        self.page.on("pageerror", lambda err: self.errors.append(str(err)))
        self.page.on("crash", lambda *_: self.errors.append("page crashed"))
        if network.latency_ms or network.download_kbps:
            cdp = await self.context.new_cdp_session(self.page)
            await cdp.send("Network.enable")
            await cdp.send("Network.emulateNetworkConditions", {
                "offline": False, "latency": network.latency_ms,
                "downloadThroughput": network.download_kbps * 1024 / 8 if network.download_kbps else -1,
                "uploadThroughput": network.upload_kbps * 1024 / 8 if network.upload_kbps else -1,
            })

    def _on_request(self, request: Any) -> None:
        self.inflight += 1
        self.last_net = time.monotonic()
        if request.is_navigation_request() and request.frame == self.page.main_frame:
            self.nav_pending = True

    def _on_request_done(self, request: Any) -> None:
        self.inflight = max(0, self.inflight - 1)
        self.last_net = time.monotonic()

    def _quiet(self, elapsed: float) -> bool:
        """Nothing changed, nothing is loading and the network has been idle: waiting longer won't help."""
        if not self.quiet_exit_s or self.nav_pending or self.inflight:
            return False
        return elapsed >= self.quiet_exit_s and time.monotonic() - self.last_net >= min(1.0, self.quiet_exit_s)

    @property
    def url(self) -> str:
        return self.page.url

    async def goto(self, url: str) -> None:
        check_url(url, self.safety.allowlist)
        await self.page.goto(url, wait_until="load", timeout=45_000)

    async def screenshot(self) -> bytes:
        return await self.page.screenshot(type="png")

    async def element_at(self, x: int, y: int, for_enter: bool = False) -> dict[str, Any]:
        """Safety guard only: describe the element under the pointer. Never passed to a model."""
        try:
            return await self.page.evaluate(ELEMENT_PROBE_JS, [x, y, for_enter]) or {}
        except Exception as exc:  # page navigating
            log.debug("element probe failed: %s", exc)
            return {}

    async def _pick_option(self, text: str) -> bool:
        """If the click opened a dropdown, choose the option whose label matches `text` (a native list can't be typed into reliably)."""
        try:
            is_select = await self.page.evaluate("() => document.activeElement && document.activeElement.tagName === 'SELECT'")
            if not is_select:
                return False
            await self.page.keyboard.press("Escape")
            return bool(await self.page.evaluate(PICK_OPTION_JS, text))
        except Exception as exc:  # page navigating
            log.debug("dropdown pick failed: %s", exc)
            return False

    async def execute(self, action: Action, vault: CredentialVault) -> None:
        page = self.page
        if action.kind in ("click", "fill") and action.x is not None and action.y is not None:
            if self.device.mobile:
                await page.touchscreen.tap(action.x, action.y)
            else:
                await page.mouse.click(action.x, action.y)
        if action.kind in ("fill", "type") and action.text is not None:
            text = vault.resolve(action.text)  # secrets are resolved only here, right before typing
            if await self._pick_option(text):
                pass  # a dropdown: the person picks the option they read in the list
            else:
                await page.keyboard.press("ControlOrMeta+A")
                await page.keyboard.type(text, delay=8)
        elif action.kind == "press" and action.key:
            await page.keyboard.press(action.key)
        elif action.kind == "scroll":
            w, h = self.viewport
            await page.mouse.move(w // 2, h // 2)
            await page.mouse.wheel(0, action.dy)
        elif action.kind == "back":
            try:
                await page.go_back(wait_until="commit", timeout=15_000)
            except Exception as exc:
                log.debug("back navigation: %s", exc)
        current = page.url
        if not is_internal(current):
            try:
                check_url(current, self.safety.allowlist)
            except SafetyError:
                await page.goto("about:blank")
                raise

    async def wait_for_settle(self, before: bytes, *, max_wait: float, poll: float = 0.15) -> tuple[bytes, Timing]:
        """Poll screenshots until the screen is stable; measure when it really responded.

        Tiny changes (a focus ring) do not count as a response while the browser is
        still loading. A "major" change is >= 1% of the screen.
        """
        start = time.monotonic()
        before_small = _small(before)
        before_hash = hashlib.sha1(before).digest()
        last_hash, png = before_hash, before
        first_change: float | None = None
        major_change: float | None = None
        frames: list[tuple[float, Any]] = []
        stable = 0
        responded = False
        quiet = False
        while True:
            await asyncio.sleep(poll)
            # A screenshot blocks while a navigation is in flight; cap it at the persona's remaining patience.
            left = (max_wait - (time.monotonic() - start)) if not responded else 10.0
            try:
                shot = await self.page.screenshot(type="png", timeout=max(300.0, left * 1000))
            except Exception:  # still loading (or mid-navigation): the old screen is what a person sees
                shot = None
            elapsed = time.monotonic() - start  # measured after the frame is captured
            if shot is None:
                if not responded and elapsed >= max_wait:
                    break
                if elapsed >= max(max_wait * 2.5, 15.0):
                    break
                continue
            png = shot
            digest = hashlib.sha1(png).digest()
            if digest != last_hash:
                stable = 0
                small = _small(png)
                first_change = first_change if first_change is not None else elapsed
                if _diff(small, before_small) >= MAJOR_CHANGE:
                    major_change = elapsed
                frames.append((elapsed, small))
            else:
                stable += 1
            last_hash = digest
            responded = major_change is not None or (first_change is not None and not self.nav_pending)
            # Settled: unchanged for two polls with nothing loading (or for longer, if a connection stays open).
            if responded and not self.nav_pending and (stable >= 2 and not self.inflight or stable >= 5):
                break
            if not responded and elapsed >= max_wait:
                break
            if first_change is None and self._quiet(elapsed):
                quiet = True
                break
            if elapsed >= max(max_wait * 2.5, 15.0):  # keeps changing (animation); stop anyway
                break
        final_small = _small(png)
        intermediate = sum(1 for _, f in frames[:-1]
                           if _diff(f, before_small) >= MAJOR_CHANGE and _diff(f, final_small) >= MAJOR_CHANGE)
        # A change that is gone once the screen settles (a tap highlight flashing) is not a response;
        # one that stays (a ticked box, a new page) is.
        lasting = hashlib.sha1(png).digest() != before_hash and (
            _diff(final_small, before_small) > 0 or _changed_pixels(png, before) >= 20)
        responded = major_change is not None or (first_change is not None and lasting and not self.nav_pending)
        settled = time.monotonic() - start
        if quiet:
            settled = max(settled, max_wait)  # the persona would have waited this long for nothing
        return png, Timing(first_change_s=first_change, settled_s=settled,
                           intermediate_states=intermediate, major_change_s=major_change, responded=responded)

    async def close(self) -> None:
        await self.context.close()
