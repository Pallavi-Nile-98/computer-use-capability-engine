"""What is true of an application, as opposed to what is true of one recorded flow.

These are different kinds of knowledge and they change at different rates, so they
are stored separately.

  The flow      "type the id, click Search, read the balance"
                specific to one capability, recorded once

  The app       "this vendor product reports a missing member in a data-outcome
                attribute, and a dropped session in a data-error one"
                true of every capability recorded against the product, and of every
                institution running it

  The tenant    "this credit union renamed the button to Find Member"
                a handful of narrow deviations from the base recording

Collapsing these into one file is the obvious thing to do and it is the mistake.
If each capability restated the app's error vocabulary, then twenty capabilities
would hold twenty copies of the same rules, drifting apart independently — and the
day the vendor renames an error code, you would be editing twenty files and
missing one. Worse, adding an institution would mean re-recording every flow,
which is precisely the outcome the brief calls out as unacceptable.

With the split, adding a tenant that runs the same product is a small override
file, and fixing the vendor's renamed error code is one edit in one place.

Drift detection is designed here but not built: the report describes running the
approved capabilities against each tenant on a schedule and treating a checkpoint
failure as a per-tenant quarantine signal rather than a global regression. What is
built is the part that would make that possible — outcomes keyed by app family, and
overrides keyed by tenant.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    BusinessOutcomeRule,
    CapabilityArtifact,
    Locator,
    LocatorStrategy,
    RecoveryAction,
    RecoveryRule,
)


@dataclass(frozen=True)
class AppProfile:
    """One vendor product's vocabulary of endings.

    Keyed by the product, never by the institution. That is the whole point: a
    hundred credit unions running the same servicing software share this file.
    """

    app_family: str
    surface_type: str
    business_outcomes: list[BusinessOutcomeRule]
    recovery_rules: list[RecoveryRule]


@dataclass(frozen=True)
class TenantOverride:
    """A named institution's narrow deviations from the base recording.

    Deliberately not a place to put a different flow. If a tenant needs different
    *steps*, that is a different capability and should be recorded as one — an
    override that can restructure a flow is just a second copy of the flow with
    extra steps to review.
    """

    tenant_id: str
    entry_point: str | None = None
    # step id -> replacement locator, for the handful of controls a tenant renamed
    step_targets: dict[str, Locator] = field(default_factory=dict)


def _outcome(code: str, description: str, expected: str) -> BusinessOutcomeRule:
    """Business outcomes in this product family share a detection shape."""
    return BusinessOutcomeRule(
        code=code,
        description=description,
        locator=Locator(
            strategy=LocatorStrategy.CSS,
            value=f"[data-outcome='{code}']",
            rationale=(
                "The application emits a machine-readable outcome attribute. Keying "
                "on it rather than on display text survives copy edits and "
                "localisation, both of which vary by institution."
            ),
        ),
        expected=expected,
    )


def _error_detector(code: str) -> Locator:
    return Locator(
        strategy=LocatorStrategy.CSS,
        value=f"[data-error='{code}']",
        rationale="Application-emitted error code, stable across releases.",
    )


NORTHSTAR_SERVICING = AppProfile(
    app_family="demo-legacy-member-servicing",
    surface_type="legacy_web",
    business_outcomes=[
        # Both of these are answers the caller asked for, not failures. The system
        # reached the application, the application responded, and the response is
        # the information the caller needs.
        _outcome(
            "MEMBER_NOT_FOUND",
            "No member matches the supplied identifier.",
            "No member matches",
        ),
        _outcome(
            "PERMISSION_DENIED",
            "The member exists, but this service account may not view them.",
            "do not have permission",
        ),
    ],
    recovery_rules=[
        RecoveryRule(
            code="SESSION_EXPIRED",
            description=(
                "The servicing host dropped the session. Re-entering at the entry "
                "point re-establishes it, after which the flow replays from the start."
            ),
            detector=_error_detector("SESSION_EXPIRED"),
            action=RecoveryAction.RENAVIGATE,
            # Once. A session that drops twice in one run is an outage, not a blip,
            # and hammering a sick host is the wrong response to it.
            max_attempts=1,
        ),
        RecoveryRule(
            code="APPLICATION_ERROR",
            description=(
                "Transient host error. A reload distinguishes a blip from a real "
                "outage without masking a sustained failure."
            ),
            detector=_error_detector("APPLICATION_ERROR"),
            action=RecoveryAction.RELOAD,
            max_attempts=2,
        ),
        RecoveryRule(
            code="INTERSTITIAL_NOTICE",
            description=(
                "A maintenance notice occasionally covers the page. Safe to dismiss "
                "because it carries no business decision — unlike an error, which "
                "is telling you something you must not click past."
            ),
            detector=Locator(
                strategy=LocatorStrategy.CSS,
                value="[data-interstitial='NOTICE']",
                rationale="Dedicated interstitial marker, distinct from error markers.",
            ),
            action=RecoveryAction.DISMISS,
            dismiss_target=Locator(
                strategy=LocatorStrategy.ROLE,
                role="button",
                value="Acknowledge",
                rationale="Accessible role and name; resilient to layout changes.",
            ),
            max_attempts=1,
        ),
    ],
)


PROFILES: dict[str, AppProfile] = {NORTHSTAR_SERVICING.app_family: NORTHSTAR_SERVICING}


def profile_for(app_family: str) -> AppProfile:
    """Look up an app's vocabulary, refusing to guess if it is not registered.

    Failing loudly here is deliberate. A capability compiled without its app's
    outcome rules would look fine and would misreport every business outcome as a
    failure — a silent wrong answer, which is worse than a loud missing one.
    """
    if app_family not in PROFILES:
        raise KeyError(
            f"No app profile registered for {app_family!r}. Register the "
            "application's outcome and recovery vocabulary before recording "
            "capabilities against it."
        )
    return PROFILES[app_family]


def apply_tenant_override(
    artifact: CapabilityArtifact, override: TenantOverride
) -> CapabilityArtifact:
    """Specialise a base capability for one institution, without re-recording it.

    Returns a new artifact; the base is never mutated. The specialised copy records
    which tenant it was built for in `tenant_overrides`, so a failure can be
    attributed to the override rather than to the base recording — the difference
    between quarantining one institution and rolling back a capability for everyone.

    Note what this cannot do: change the steps, the contract, or the checkpoint.
    A tenant that needs a different flow needs its own recording.
    """
    steps = [
        step.model_copy(update={"target": override.step_targets[step.id]})
        if step.id in override.step_targets
        else step
        for step in artifact.steps
    ]

    return artifact.model_copy(
        update={
            "steps": steps,
            "entry_point": override.entry_point or artifact.entry_point,
            "tenant_overrides": {
                override.tenant_id: {
                    "entry_point": override.entry_point,
                    "step_targets": {
                        step_id: locator.model_dump(mode="json")
                        for step_id, locator in override.step_targets.items()
                    },
                }
            },
            # A specialised copy is a different thing from the reviewed base, so it
            # goes back to draft. Approval does not transfer across an edit.
            "approval_state": "draft",
            "approved_by": None,
            "approved_at": None,
        }
    )
