"""The seam between perceiving a surface and the flow that was recorded on it.

This is the most load-bearing boundary in the project. Everything above it — the
discovery loop, the artifact, the replay engine, the guardrails, the handoff — is
written against the `Surface` protocol and never imports Playwright. A recorded
flow says "click the button named Search"; this layer decides what that means on
a particular kind of surface.

That split is what lets the same artifact run somewhere else later. A legacy-web
adapter walking framesets, or a desktop adapter over OS accessibility APIs,
implements these nine methods and the replay engine does not change. If the
artifact stored CSS selectors or pixel coordinates instead of intent, none of that
would be possible — the flow would be welded to one browser.

Two adapters ship here. `PlaywrightSurface` drives a real browser and is the one
that matters. `FakeSurface` models the target app's state machine in memory: it
exists so the orchestration above can be tested without a browser, and it doubles
as proof that the protocol is genuinely implementable by something that is not a
browser at all.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .models import ActionKind, Locator, LocatorStrategy, Observation, PlannedAction


class SurfaceError(RuntimeError):
    """The surface could not carry out the action as described."""


class TargetNotFound(SurfaceError):
    """The named control was not on the surface.

    Kept distinct from the general case because "the button isn't there" and "the
    click failed" have different causes, and a debugger benefits from knowing which.
    """


@runtime_checkable
class Surface(Protocol):
    """What any surface adapter must be able to do.

    Marked `runtime_checkable` so a test can assert that an adapter really does
    satisfy the protocol, rather than finding out mid-run that a method is missing.

    A Protocol rather than a base class: an adapter does not have to inherit from
    anything, it just has to have these methods. That keeps a future desktop
    adapter free of any dependency on this module.

    `current_url`, `reload` and `navigate` exist because recovery needs them — to
    dismiss an interstitial, retry a transient error, or re-enter after a dropped
    session. On a desktop adapter they map to a window identifier, a refresh, and
    reopening the app.
    """

    async def start(self, entry_point: str) -> None: ...
    async def observe(self, screenshot_path: str | None = None) -> Observation: ...
    async def execute(self, action: PlannedAction) -> str | None: ...
    async def checkpoint(self, locator: Locator, expected: str | None = None) -> bool: ...
    async def screenshot(self, path: str) -> None: ...
    async def current_url(self) -> str: ...
    async def reload(self) -> None: ...
    async def navigate(self, url: str) -> None: ...
    async def close(self) -> None: ...


# Implicit ARIA roles for the few tags a legacy enterprise app actually uses.
# Reported to the planner so it can choose role-based locators on markup that
# carries no explicit role attributes — which is most legacy markup.
_IMPLICIT_ROLES = """
    const implicitRole = (e) => {
      const explicit = e.getAttribute('role');
      if (explicit) return explicit;
      const tag = e.tagName.toLowerCase();
      if (tag === 'a') return e.hasAttribute('href') ? 'link' : null;
      if (tag === 'button') return 'button';
      if (tag === 'select') return 'combobox';
      if (tag === 'textarea') return 'textbox';
      if (tag === 'input') {
        const type = (e.getAttribute('type') || 'text').toLowerCase();
        if (['submit','button','reset'].includes(type)) return 'button';
        if (type === 'checkbox') return 'checkbox';
        if (type === 'radio') return 'radio';
        return 'textbox';
      }
      return null;
    };
"""


class PlaywrightSurface:
    """Web adapter. The only file in the project that knows a DOM exists."""

    def __init__(
        self,
        headless: bool = False,
        timeout_ms: int = 8_000,
        channel: str | None = None,
    ):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.entry_point = ""

        # Playwright's bundled Chromium fails to start on some Windows installs
        # ("side-by-side configuration is incorrect"). Setting PLAYWRIGHT_CHANNEL
        # to an installed browser such as msedge uses that instead. Unset, this
        # behaves exactly as stock Playwright.
        self.channel = channel or os.environ.get("PLAYWRIGHT_CHANNEL") or None

        # Pause between actions so a person can follow a headed run. Demo aid;
        # unset in normal operation, where deliberate waiting is pure waste.
        self.slow_mo_ms = int(os.environ.get("PLAYWRIGHT_SLOW_MO", "0"))

        self._playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None

    async def start(self, entry_point: str) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run `pip install -e '.[dev]'` "
                "and `playwright install chromium`."
            ) from exc

        self._playwright = await async_playwright().start()
        launch: dict[str, Any] = {"headless": self.headless}
        if self.channel:
            launch["channel"] = self.channel
        if self.slow_mo_ms:
            launch["slow_mo"] = self.slow_mo_ms

        self.browser = await self._playwright.chromium.launch(**launch)
        self.context = await self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = await self.context.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        self.entry_point = entry_point
        await self.page.goto(entry_point, wait_until="domcontentloaded")

    async def observe(self, screenshot_path: str | None = None) -> Observation:
        """Describe the current screen the way a human operator would read it.

        Deliberately not the raw DOM. The planner gets visible text plus a list of
        controls with their accessible role and name — roughly what a person sees.
        Feeding it raw markup would tempt it into brittle structural selectors, and
        would not translate to a desktop surface at all.
        """
        if screenshot_path:
            await self.screenshot(screenshot_path)

        controls = await self.page.locator("input, button, a, select, textarea").evaluate_all(
            _IMPLICIT_ROLES
            + """
            elements => elements.filter(e => {
                const r = e.getBoundingClientRect();
                return r.width > 0 && r.height > 0;   // visible controls only
            }).slice(0, 80).map(e => ({
                tag: e.tagName.toLowerCase(),
                role: implicitRole(e),
                name: (e.getAttribute('aria-label') || e.innerText || e.value || '').trim() || null,
                label: e.labels && e.labels.length ? e.labels[0].innerText.trim() : null,
                placeholder: e.getAttribute('placeholder'),
                type: e.getAttribute('type')
            }))"""
        )

        return Observation(
            url=self.page.url,
            title=await self.page.title(),
            visible_text=(await self.page.locator("body").inner_text())[:15_000],
            controls=controls,
            screenshot_path=screenshot_path,
        )

    def _resolve(self, target: Locator) -> Any:
        """Turn a locator's stated intent into a Playwright handle."""
        scope = self.page.frame_locator(target.frame) if target.frame else self.page
        if target.strategy == LocatorStrategy.ROLE:
            return scope.get_by_role(target.role or "button", name=target.value, exact=target.exact)
        if target.strategy == LocatorStrategy.LABEL:
            return scope.get_by_label(target.value, exact=target.exact)
        if target.strategy == LocatorStrategy.TEXT:
            return scope.get_by_text(target.value, exact=target.exact)
        if target.strategy == LocatorStrategy.PLACEHOLDER:
            return scope.get_by_placeholder(target.value, exact=target.exact)
        if target.strategy == LocatorStrategy.CSS:
            return scope.locator(target.value)
        raise SurfaceError(f"The web adapter cannot resolve strategy: {target.strategy}")

    async def execute(self, action: PlannedAction) -> str | None:
        if action.action == ActionKind.NAVIGATE:
            await self.page.goto(action.value, wait_until="domcontentloaded")
            return self.page.url
        if action.action == ActionKind.WAIT:
            await asyncio.sleep(float(action.value or "0.5"))
            return "waited"
        if action.action in {ActionKind.COMPLETE, ActionKind.ESCALATE}:
            return action.reasoning
        if not action.target:
            raise SurfaceError(f"{action.action} requires a target")

        locator = self._resolve(action.target).first
        try:
            if await locator.count() == 0:
                raise TargetNotFound(
                    f"{action.target.strategy}={action.target.value!r} matched nothing"
                )
            if action.action == ActionKind.CLICK:
                await locator.click()
                # The target app answers a search with POST -> 303 -> GET. Without
                # this wait, the next observation reads the page mid-redirect.
                await self.page.wait_for_load_state("domcontentloaded")
                return "clicked"
            if action.action == ActionKind.FILL:
                await locator.fill(action.value or "")
                return "filled"
            if action.action == ActionKind.READ:
                text = (await locator.inner_text()).strip()
                if text:
                    return text
                try:
                    return (await locator.input_value()).strip()
                except Exception:
                    raise TargetNotFound(
                        f"{action.target.value!r} is present but holds no readable value"
                    ) from None
        except SurfaceError:
            raise
        except Exception as exc:
            # Wrap anything Playwright throws so nothing above this file has to
            # know Playwright's exception types.
            raise SurfaceError(str(exc)) from exc

        raise SurfaceError(f"Unsupported action: {action.action}")

    async def checkpoint(self, locator: Locator, expected: str | None = None) -> bool:
        """Ask whether an expected state is present, without waiting for it to arrive.

        The short timeout is deliberate. A checkpoint answers "are we there now";
        waiting a long while turns a genuinely wrong state into a slow success.
        """
        try:
            target = self._resolve(locator).first
            await target.wait_for(state="visible", timeout=2_000)
            if expected is None:
                return True
            return expected in (await target.inner_text()).strip()
        except Exception:
            return False

    async def screenshot(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=path, full_page=True)

    async def current_url(self) -> str:
        return self.page.url

    async def reload(self) -> None:
        await self.page.reload(wait_until="domcontentloaded")

    async def navigate(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")

    async def close(self) -> None:
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()


@dataclass
class FakeSurface:
    """In-memory adapter modelling the target app's state machine.

    Not a mock of Playwright — a second, genuinely different implementation of the
    same protocol. That matters twice over: the orchestration can be tested in
    milliseconds without a browser, and the existence of a non-browser adapter is
    evidence that the seam is real rather than aspirational.

    It never produces browser evidence. Screenshots are a valid 1x1 PNG, so an
    offline run can never be mistaken for a real one.
    """

    member_exists: bool = True
    restricted: bool = False
    # Faults consumed one per settle, mirroring the target app's one-shot queue.
    faults: list[str] = field(default_factory=list)

    entry_point: str = "http://127.0.0.1:8000/legacy"
    state: str = "search"
    member_id: str = ""
    subaccount_created: bool = False
    reloads: int = 0
    navigations: int = 0

    _PAGES = {
        "search": ("Legacy Member Servicing", "Member Search Member ID Search"),
        "details": ("Member Details", "Member Details Savings Account Current Balance $4,281.73"),
        "review": ("Review Sub-account", "Review New Sub-account changes the system of record"),
        "created": ("Sub-account Created", "Sub-account Created Sub-account opened"),
        "MEMBER_NOT_FOUND": ("Member Result", "MEMBER_NOT_FOUND No member matches that identifier"),
        "PERMISSION_DENIED": ("Permission Denied", "PERMISSION_DENIED You do not have permission"),
        "SESSION_EXPIRED": ("Session Expired", "SESSION_EXPIRED Session expired. Please sign in"),
        "APPLICATION_ERROR": ("Application Error", "APPLICATION_ERROR host is temporarily unavailable"),
        "INTERSTITIAL_NOTICE": ("Scheduled Maintenance", "NOTICE Scheduled maintenance Acknowledge"),
    }

    async def start(self, entry_point: str) -> None:
        self.entry_point = entry_point
        self.state = "search"

    def _settle(self) -> None:
        """Decide which page a search lands on, applying any armed fault first."""
        if self.faults:
            self.state = self.faults.pop(0)
        elif self.restricted:
            self.state = "PERMISSION_DENIED"
        elif self.member_exists:
            self.state = "details"
        else:
            self.state = "MEMBER_NOT_FOUND"

    async def observe(self, screenshot_path: str | None = None) -> Observation:
        if screenshot_path:
            await self.screenshot(screenshot_path)
        title, text = self._PAGES[self.state]
        return Observation(
            url=await self.current_url(),
            title=title,
            visible_text=text,
            controls=[],
            screenshot_path=screenshot_path,
        )

    async def execute(self, action: PlannedAction) -> str | None:
        if action.action == ActionKind.FILL:
            self.member_id = action.value or ""
            return "filled"

        if action.action == ActionKind.CLICK:
            name = action.target.value if action.target else ""
            if name == "Acknowledge":
                self._settle()
            elif name == "Open sub-account":
                self.state = "review"
            elif name == "Confirm and create":
                self.subaccount_created = True
                self.state = "created"
            else:
                self._settle()
            return "clicked"

        if action.action == ActionKind.READ:
            if self.state != "details":
                raise TargetNotFound(action.target.value if action.target else "value")
            return "$4,281.73"

        if action.action == ActionKind.WAIT:
            return "waited"
        return action.reasoning

    async def checkpoint(self, locator: Locator, expected: str | None = None) -> bool:
        """Approximate the real adapter's matching, faithfully enough to be useful.

        `expected` wins when supplied, mirroring the real adapter, which asserts on
        the element's text rather than on the selector. Otherwise the marker value
        is pulled out of the selector — `[data-error='SESSION_EXPIRED']` is asking
        about SESSION_EXPIRED, and the fake's page text carries those markers.

        The first version compared the entire selector string against the page text
        and so matched nothing. It made every interstitial look like an unknown
        state, which is exactly the misclassification this whole design exists to
        avoid — worth keeping the note as a reminder that a fake which is subtly
        wrong is more dangerous than no fake at all.
        """
        observation = await self.observe()
        if expected:
            return expected in observation.visible_text
        if locator.strategy == LocatorStrategy.CSS:
            marker = re.search(r"'([^']+)'", locator.value)
            if marker:
                return marker.group(1) in observation.visible_text
        return locator.value in observation.visible_text

    async def screenshot(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
        )

    async def current_url(self) -> str:
        if self.state == "search":
            return self.entry_point
        if self.state in {"review", "created"}:
            return f"{self.entry_point}/member/{self.member_id}/subaccount"
        return f"{self.entry_point}/member/{self.member_id}"

    async def reload(self) -> None:
        self.reloads += 1
        self._settle()

    async def navigate(self, url: str) -> None:
        self.navigations += 1
        self.state = "search"

    async def close(self) -> None:
        return None
