"""A synthetic member-servicing console, standing in for a real legacy bank app.

The brief does not provide a real system and explicitly says not to obtain one, so
a stand-in is required. A locally-built app was chosen over a public demo site for
three reasons:

  - no third party's terms are being violated, and no real credentials exist
  - all data is synthetic, so nothing sensitive can leak into evidence
  - runtime errors can be triggered on demand, which is the only way to actually
    demonstrate the error handling the brief weights most heavily

It is deliberately hostile in the ways real legacy software is hostile:
table-based layout, no test ids, inconsistent naming between the label and the
form field, and a POST-then-redirect in the middle of the flow. That is the point.
A locator that works here has been tested against something; a locator that only
works on a clean modern DOM has not.

One honest caveat: this app emits machine-readable markers (`data-outcome`,
`data-error`) for its own error states. Plenty of real legacy apps do not, and
there you would have to match on display text instead, which is more brittle and
breaks on localisation. Detection strategy is a per-app-profile decision, not an
assumption baked into the engine.

Every record here is invented. Never point this at real member data.
"""

from __future__ import annotations

import asyncio
from html import escape

from fastapi import FastAPI, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI(title="Northstar CU Member Servicing (synthetic)")

# Synthetic members, chosen to produce three different endings for the same flow.
MEMBERS = {
    "12345": {"name": "Demo Member", "balance": "$4,281.73", "status": "Active"},
    "77777": {"name": "Restricted Demo", "balance": "$0.00", "status": "Restricted"},
    # Anything else produces MEMBER_NOT_FOUND.
}

# One-shot faults, armed by the test-control endpoint and consumed by the next
# member-detail render. A queue rather than a flag so a test can arm the same
# fault several times and watch a retry budget run out.
PENDING_FAULTS: list[str] = []

# Records what the irreversible flow actually wrote, so a test can assert that a
# blocked capability really did not create anything.
CREATED_SUBACCOUNTS: list[str] = []


def _take_fault() -> str | None:
    return PENDING_FAULTS.pop(0) if PENDING_FAULTS else None


def shell(title: str, content: str) -> str:
    """Page chrome. Ugly on purpose — 1990s enterprise, not a modern web app."""
    return f"""<!doctype html>
<html><head><title>{escape(title)}</title><style>
body{{font-family:Arial,sans-serif;background:#d8d8d8;margin:0;color:#111}}
.top{{background:#16355c;color:#fff;padding:10px 18px;font-weight:bold}}
.wrap{{width:820px;margin:22px auto;background:#f5f1df;border:2px ridge #888;padding:16px}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #777;padding:8px;text-align:left}}
label{{font-weight:bold}}input{{padding:6px;width:240px}}button{{padding:6px 18px}}
.error{{background:#ffe4e4;border:1px solid #a00;padding:12px;margin:12px 0}}
.notice{{background:#fff6c7;border:1px solid #9b7b00;padding:12px;margin:12px 0}}
.balance{{font-size:22px;font-weight:bold}}
</style></head><body><div class="top">Northstar CU - Member Servicing Console</div>
<div class="wrap">{content}</div></body></html>"""


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/legacy", status_code=302)


@app.get("/legacy", response_class=HTMLResponse)
async def search_page() -> str:
    """The entry point.

    Note the awkwardness, all of it intentional: the field is laid out inside a
    <table>, it has no test id, and the label's `for` attribute points at
    `memberNumber` while the form field is named `member_id`. An automation that
    guesses the field name gets it wrong; one that reads the visible label does not.
    """
    return shell(
        "Legacy Member Servicing",
        """
        <h2>Member Search</h2>
        <form method="post" action="/legacy/member/search">
          <table><tr><td><label for="memberNumber">Member ID</label></td>
          <td><input id="memberNumber" name="member_id" autocomplete="off"></td></tr></table>
          <p><button type="submit">Search</button></p>
        </form>
        <p class="notice">Training environment. Use synthetic member identifiers only.</p>
        """,
    )


@app.post("/legacy/member/search")
async def search_member(member_id: str = Form(...)) -> RedirectResponse:
    """POST then redirect to a GET — the classic pattern, and a real trap.

    Automation that assumes a click lands it on the results page will read the
    page mid-redirect and find nothing. The surface adapter has to wait for the
    navigation to settle.
    """
    safe_id = "".join(ch for ch in member_id if ch.isdigit())[:12]
    return RedirectResponse(f"/legacy/member/{safe_id}", status_code=303)


@app.get("/legacy/member/{member_id}", response_class=HTMLResponse)
async def member_detail(member_id: str, simulate: str | None = Query(default=None)) -> str:
    """The results page, and every alternative ending the flow can reach.

    Five distinct endings, which is the whole reason this app exists:

      details            the happy path
      MEMBER_NOT_FOUND   a legitimate answer — the member does not exist
      PERMISSION_DENIED  a legitimate answer — not authorised to view them
      SESSION_EXPIRED    recoverable — re-enter and replay the flow
      APPLICATION_ERROR  recoverable — a transient host failure

    An automation that only handles the first is not useful in production.
    """
    fault = simulate or _take_fault()

    if fault == "slow":
        # Transient slowness, not an error. Tests that the adapter waits rather
        # than declaring failure.
        await asyncio.sleep(2)
        fault = None

    if fault == "SESSION_EXPIRED":
        return shell(
            "Session Expired",
            '<div class="error" data-error="SESSION_EXPIRED">'
            "Session expired. Please sign in again.</div>",
        )

    if fault == "APPLICATION_ERROR":
        return shell(
            "Application Error",
            '<div class="error" data-error="APPLICATION_ERROR">'
            "The servicing host is temporarily unavailable.</div>",
        )

    if fault == "INTERSTITIAL_NOTICE":
        # A popup carrying no business decision. Safe to dismiss, unlike the two
        # above — which is exactly the distinction the artifact has to encode.
        return shell(
            "Scheduled Maintenance",
            '<div class="notice" data-interstitial="NOTICE">'
            "Scheduled maintenance this weekend.</div>"
            f'<form method="get" action="/legacy/member/{escape(member_id)}">'
            '<button type="submit">Acknowledge</button></form>',
        )

    member = MEMBERS.get(member_id)

    if not member:
        return shell(
            "Member Result",
            '<h2>Search Result</h2>'
            '<div class="notice" data-outcome="MEMBER_NOT_FOUND">'
            "No member matches that identifier.</div>"
            "<p><a href='/legacy'>Return to search</a></p>",
        )

    if member["status"] == "Restricted":
        return shell(
            "Permission Denied",
            '<div class="error" data-outcome="PERMISSION_DENIED">'
            "You do not have permission to view this member.</div>",
        )

    return shell(
        "Member Details",
        f"""
        <h2>Member Details</h2>
        <table><tr><th>Member</th><td>{escape(member['name'])}</td></tr>
        <tr><th>Status</th><td>{escape(member['status'])}</td></tr></table>
        <h3>Savings Account</h3>
        <table><tr><th>Current Balance</th>
        <td><span class="balance" data-field="savings-balance">{escape(member['balance'])}</span></td></tr></table>
        <p><a href="/legacy/member/{escape(member_id)}/subaccount/review">Open sub-account</a></p>
        """,
    )


@app.get("/legacy/member/{member_id}/subaccount/review", response_class=HTMLResponse)
async def subaccount_review(member_id: str) -> str:
    """The confirmation screen for an irreversible action.

    Reaching this page is safe — nothing has been written yet. Pressing the button
    is not. That distinction is what the risk classification exists to capture.
    """
    return shell(
        "Review Sub-account",
        f"""
        <h2>Review New Sub-account</h2>
        <div class="notice" data-risk="irreversible">Creating the account changes the
        system of record.</div>
        <p>Member: {escape(member_id)}</p>
        <form method="post" action="/legacy/member/{escape(member_id)}/subaccount/create">
          <button type="submit">Confirm and create</button>
        </form>
        """,
    )


@app.post("/legacy/member/{member_id}/subaccount/create", response_class=HTMLResponse)
async def subaccount_create(member_id: str) -> str:
    """The write. Deliberately observable so a test can prove it did not happen."""
    CREATED_SUBACCOUNTS.append(member_id)
    return shell(
        "Sub-account Created",
        '<h2>Sub-account Created</h2>'
        f'<div class="notice" data-field="subaccount-status">'
        f"Sub-account opened for member {escape(member_id)}.</div>",
    )


# ---------------------------------------------------------------------------
# Test-harness control surface
# ---------------------------------------------------------------------------
# Not part of the application under automation. It exists because the brief's
# interesting failures are runtime conditions, and those cannot be demonstrated
# in a real browser unless the target can be made to produce them on cue. A real
# integration would obviously have no such endpoint.


@app.post("/__control/fault", include_in_schema=False)
async def arm_fault(code: str = Form(...), count: int = Form(default=1)) -> JSONResponse:
    """Arm a fault for the next `count` member-detail renders."""
    PENDING_FAULTS.extend([code] * count)
    return JSONResponse({"armed": code, "count": count, "queue": len(PENDING_FAULTS)})


@app.post("/__control/reset", include_in_schema=False)
async def reset() -> JSONResponse:
    """Clear all armed faults and forget any sub-accounts created."""
    PENDING_FAULTS.clear()
    CREATED_SUBACCOUNTS.clear()
    return JSONResponse({"reset": True})


@app.get("/__control/state", include_in_schema=False)
async def state() -> JSONResponse:
    """Let a test assert on what the app believes happened."""
    return JSONResponse(
        {"pending_faults": list(PENDING_FAULTS), "subaccounts": list(CREATED_SUBACCOUNTS)}
    )
