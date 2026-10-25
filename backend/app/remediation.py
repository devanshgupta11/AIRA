"""The action layer: what AIRA is permitted to do to a running system.

This is the most dangerous code in the project, so it is written to be safe **on its own**, before
any agent is allowed near it. Nothing here imports an agent, a model or a prompt. An agent's only
influence is to name one of the actions below and one target; it never constructs a command, and
there is no path from model output to a shell.

Four properties, each enforced in code and covered by a test:

1. **A fixed enumeration, not free text.** `ACTIONS` is the complete set. An unknown name is
   refused, so a model that invents an action achieves nothing.
2. **Targets are allowlisted, not denylisted.** `RESTARTABLE` and the known flags are explicit
   permits. A denylist fails open for anything added to the system later; an allowlist fails
   closed. `FORBIDDEN_SERVICES` is then a *second, independent* check, so even a mistaken entry in
   an allowlist is still caught if it names something that must never be touched.
3. **No shell, ever.** Every subprocess call passes an argument list with `shell=False` (the
   default), and every argument is either a literal or a value that has already been matched
   against an allowlist. String interpolation into a command does not happen anywhere in this file.
4. **Verify at the source.** After acting, the result is confirmed by asking the system itself -
   flagd what it is now serving, Docker what the container's state and start time now are - rather
   than trusting that the command worked. This is the pattern `scripts/flagctl.py` already uses.

Reversal is recorded for every action. For `reset_feature_flag` it is a real inverse: the previous
variant, captured before acting. For `restart_container` there is none, and the record says so
explicitly rather than leaving it blank - a restart returns a container to its declared state, so
there is nothing to undo, unlike a flag change which leaves the system in a state someone chose.
"""

import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .change_tool import FLAG_SERVICE

log = logging.getLogger("aira.remediation")

# `scripts/` is not a package; flagctl.py is reused rather than reimplemented because it already
# writes a flag through the same API the UI uses and then confirms it with flagd directly.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _flagctl():
    """The flag tool, imported on use rather than at module load.

    It talks to flagd, which only exists where the monitored stack does. An archive deployment
    imports this module for its allowlist and its action names - that is what makes the console
    able to describe what was done - but it will never call flagd, and a module-level import of a
    file under scripts/ meant the whole backend refused to start in a container that quite
    reasonably did not ship the scripts directory.
    """
    import flagctl  # noqa: PLC0415 - deliberately deferred; see above

    return flagctl


# ---------------------------------------------------------------- what must never be touched

# Required: acting on these blinds AIRA instead of fixing anything.
MONITORING_SERVICES = frozenset({
    "prometheus", "alertmanager", "otel-collector", "grafana", "jaeger", "opensearch",
})

# AIRA's own control plane. A flag reset is performed *through* flagd-ui and confirmed with flagd,
# so restarting either would break the action mid-flight and blind the Change agent.
CONTROL_PLANE_SERVICES = frozenset({"flagd", "flagd-ui"})

# Every endpoint AIRA polls - Prometheus, Jaeger, Grafana, flagd-ui, the shop itself - is routed
# through the proxy on :8080. Restarting it blinds AIRA's own verification step.
INGRESS_SERVICES = frozenset({"frontend-proxy"})

# Restarting these can lose in-flight orders or cart contents, which is destructive rather than
# reversible. Their feature flags remain resettable; only the restart is forbidden.
STATEFUL_SERVICES = frozenset({"kafka", "valkey-cart", "astronomy-db"})

FORBIDDEN_SERVICES = (MONITORING_SERVICES | CONTROL_PLANE_SERVICES
                      | INGRESS_SERVICES | STATEFUL_SERVICES)

# The stateless application services of the Astronomy Shop. An explicit permit list: a service that
# appears in this environment later is not restartable until someone adds it deliberately.
RESTARTABLE = frozenset({
    "accounting", "ad", "cart", "checkout", "currency", "email", "fraud-detection",
    "frontend", "image-provider", "load-generator", "payment", "product-catalog",
    "quote", "recommendation", "shipping",
})

ActionName = Literal["reset_feature_flag", "restart_container"]
ACTION_NAMES: tuple[str, ...] = ("reset_feature_flag", "restart_container")

RISK = {"reset_feature_flag": "low", "restart_container": "medium"}


class ActionRefused(Exception):
    """The action is not permitted, or its parameters are not valid. Nothing was executed."""


class ActionFailed(Exception):
    """The action was permitted and attempted, but did not do what it was supposed to."""


@dataclass
class Proposal:
    """A validated, executable action. Only `validate()` may construct one of these."""

    action: str
    target_service: str
    params: dict = field(default_factory=dict)
    risk: str = "medium"
    reversal: str = ""
    before: dict = field(default_factory=dict)

    def describe(self) -> str:
        if self.action == "reset_feature_flag":
            return (f"Set the feature flag '{self.params['flag']}' to "
                    f"'{self.params['to_variant']}' (currently "
                    f"'{self.before.get('variant')}'), which affects {self.target_service}")
        return f"Restart the {self.target_service} container"


@dataclass
class Outcome:
    """What actually happened, including how it was independently confirmed."""

    ok: bool
    detail: str
    verified_by: str
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    reversal: str = ""
    duration_s: float = 0.0


# ---------------------------------------------------------------- shared checks

def _refuse_forbidden(service: str) -> None:
    """The second, independent check. Runs for every action regardless of its own allowlist."""
    if service in FORBIDDEN_SERVICES:
        reason = ("it is part of the monitoring stack" if service in MONITORING_SERVICES else
                  "it is part of AIRA's own control plane" if service in CONTROL_PLANE_SERVICES else
                  "it is the shared ingress AIRA itself depends on" if service in INGRESS_SERVICES
                  else "restarting it can lose in-flight data, so it is not reversible")
        raise ActionRefused(f"'{service}' may never be acted on: {reason}")


def _docker(args: list[str], timeout: int = 60) -> str:
    """Run one docker command. Argument list, never a shell string."""
    out = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise ActionFailed(f"docker {' '.join(args)} failed: {(out.stderr or '').strip()[:200]}")
    return out.stdout.strip()


def container_state(service: str) -> dict:
    """What Docker says about a container right now. Used to confirm a restart really happened."""
    raw = _docker(["inspect", service, "--format",
                   "{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}"])
    status, started_at, restarts = raw.split("|", 2)
    return {"status": status, "started_at": started_at, "restart_count": int(restarts or 0)}


# ---------------------------------------------------------------- validation

def validate(action: str, target_service: str, params: dict | None = None,
             incident_services: set[str] | None = None) -> Proposal:
    """Turn a named action and target into an executable Proposal, or refuse it.

    `incident_services` is defence in depth: the agent layer also checks that a proposal matches
    the incident's findings, but acting on a service the investigation never looked at is incoherent
    whatever proposed it, so it is refused here too.
    """
    params = dict(params or {})
    if action not in ACTION_NAMES:
        raise ActionRefused(
            f"'{action}' is not an action AIRA can take. The only actions are: "
            f"{', '.join(ACTION_NAMES)}")
    if not target_service or not isinstance(target_service, str):
        raise ActionRefused("no target service was given")

    _refuse_forbidden(target_service)

    if incident_services is not None and target_service not in incident_services:
        raise ActionRefused(
            f"'{target_service}' does not appear in this incident's findings "
            f"(the investigation looked at: {', '.join(sorted(incident_services)) or 'nothing'})")

    if action == "reset_feature_flag":
        return _validate_flag_reset(target_service, params)
    return _validate_restart(target_service)


def _validate_flag_reset(target_service: str, params: dict) -> Proposal:
    flag = params.get("flag")
    to_variant = params.get("to_variant", "off")
    if flag not in FLAG_SERVICE:
        raise ActionRefused(
            f"'{flag}' is not a feature flag this system has. Known flags: "
            f"{', '.join(sorted(FLAG_SERVICE))}")

    affects = FLAG_SERVICE[flag]
    # The flag's own service is checked as well as the named target: a flag affecting a monitoring
    # service must be refused even if the proposal names an application service as the target.
    _refuse_forbidden(affects)
    if affects != target_service:
        raise ActionRefused(
            f"flag '{flag}' affects '{affects}', not '{target_service}'. Reset it against the "
            f"service it actually affects")
    if to_variant != "off":
        # Turning a flag *on* is fault injection, not remediation. The agent may only turn one off.
        raise ActionRefused(
            f"a flag may only be reset to 'off' as a remediation, not to '{to_variant}'")

    current = _flagctl().read_flags().get(flag)
    if current is None:
        raise ActionRefused(f"flagd does not currently define a flag called '{flag}'")
    variant = _flagctl().configured_variant(current)
    if variant == "off":
        # Not a safety refusal but an honesty one: executing this would change nothing and the
        # verification step would then "confirm" a success that did nothing.
        raise ActionRefused(
            f"flag '{flag}' is already off, so resetting it would change nothing")

    return Proposal(
        action="reset_feature_flag", target_service=target_service,
        params={"flag": flag, "to_variant": "off"},
        risk=RISK["reset_feature_flag"],
        reversal=f"set the flag '{flag}' back to '{variant}'",
        before={"variant": variant},
    )


def _validate_restart(target_service: str) -> Proposal:
    if target_service not in RESTARTABLE:
        raise ActionRefused(
            f"'{target_service}' is not in the set of services AIRA may restart. Restartable: "
            f"{', '.join(sorted(RESTARTABLE))}")
    state = container_state(target_service)
    return Proposal(
        action="restart_container", target_service=target_service,
        params={}, risk=RISK["restart_container"],
        # Recorded explicitly rather than left blank: a restart returns the container to its
        # declared state, so there is nothing to undo.
        reversal="none required: a restart returns the container to its declared state",
        before=state,
    )


# ---------------------------------------------------------------- execution

def execute(proposal: Proposal) -> Outcome:
    """Carry out a validated Proposal and confirm it at the source.

    Re-validates first. A Proposal may have been produced, stored, shown to a human and approved
    minutes later, and the system can have changed in between - a flag may already be off, a
    container may already have been restarted - so permission is checked again at the moment of
    acting, not only at the moment of proposing.
    """
    t0 = time.perf_counter()
    fresh = validate(proposal.action, proposal.target_service, proposal.params)
    if proposal.action == "reset_feature_flag":
        out = _execute_flag_reset(fresh)
    else:
        out = _execute_restart(fresh)
    out.duration_s = round(time.perf_counter() - t0, 2)
    log.info("remediation %s on %s: %s (%s)", proposal.action, proposal.target_service,
             "ok" if out.ok else "FAILED", out.detail[:120])
    return out


def _execute_flag_reset(p: Proposal) -> Outcome:
    flag = p.params["flag"]
    previous = p.before["variant"]

    flags = _flagctl().read_flags()
    _flagctl().apply_variant(flags[flag], "off")
    _flagctl().write_flags(flags)

    # Confirmed by asking flagd what it is now serving, not by trusting the write.
    ok, result = _flagctl().wait_for(flag, "off", timeout=20)
    served = result.get("variant")
    if not ok:
        raise ActionFailed(
            f"the flag file was written, but flagd is still serving '{served}' for '{flag}' "
            f"after 20s")
    return Outcome(
        ok=True,
        detail=f"flag '{flag}' set to off (was '{previous}')",
        verified_by=f"flagd evaluates '{flag}' as variant '{served}'",
        before={"variant": previous}, after={"variant": served},
        reversal=p.reversal,
    )


def _execute_restart(p: Proposal) -> Outcome:
    svc = p.target_service
    before = p.before
    _docker(["restart", svc])

    # Confirmed by Docker's own view: running, and started more recently than before. A restart
    # that silently did nothing would otherwise read as a success.
    after = {}
    for _ in range(20):
        after = container_state(svc)
        if after["status"] == "running" and after["started_at"] != before.get("started_at"):
            break
        time.sleep(1)
    else:
        raise ActionFailed(
            f"'{svc}' is '{after.get('status')}' and its start time did not change after a restart")
    return Outcome(
        ok=True,
        detail=f"container '{svc}' restarted (was '{before.get('status')}')",
        verified_by=(f"docker reports '{svc}' running, started at {after['started_at']} "
                     f"(previously {before.get('started_at')})"),
        before=before, after=after, reversal=p.reversal,
    )


def reverse(proposal: Proposal) -> Outcome:
    """Undo an executed action, where an undo exists.

    Only the flag reset has a real inverse. This is deliberately a separate, explicit call: nothing
    reverses automatically, because an automatic rollback on a system a human just approved a change
    to is its own kind of surprise.
    """
    if proposal.action != "reset_feature_flag":
        raise ActionRefused(f"'{proposal.action}' has no reversal: {proposal.reversal}")
    flag = proposal.params["flag"]
    previous = proposal.before.get("variant")
    if not previous or previous == "off":
        raise ActionRefused(f"no previous variant was recorded for '{flag}', so it cannot be reversed")

    flags = _flagctl().read_flags()
    if flag not in flags:
        raise ActionFailed(f"flagd no longer defines a flag called '{flag}'")
    _flagctl().apply_variant(flags[flag], previous)
    _flagctl().write_flags(flags)
    ok, result = _flagctl().wait_for(flag, previous, timeout=20)
    if not ok:
        raise ActionFailed(f"flagd is serving '{result.get('variant')}' for '{flag}', not '{previous}'")
    return Outcome(
        ok=True, detail=f"flag '{flag}' restored to '{previous}'",
        verified_by=f"flagd evaluates '{flag}' as variant '{result.get('variant')}'",
        before={"variant": "off"}, after={"variant": previous},
        reversal=f"set the flag '{flag}' back to 'off'",
    )
