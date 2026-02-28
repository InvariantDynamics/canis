import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

from holmes.core.models import AgentPlanStatus, PlanRiskLevel, RemediationPlanV1


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AgentPolicySettings:
    global_kill_switch: bool
    freeze_window_active: bool
    auto_execute_low_risk: bool

    @classmethod
    def from_env(cls) -> "AgentPolicySettings":
        return cls(
            global_kill_switch=_env_flag("AGENT_GLOBAL_KILL_SWITCH", False),
            freeze_window_active=_env_flag("AGENT_FREEZE_WINDOW_ACTIVE", False),
            auto_execute_low_risk=_env_flag("AGENT_AUTO_EXECUTE_LOW_RISK", False),
        )


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    status: AgentPlanStatus
    results: List[str]


def evaluate_simulation_policy(
    plan: RemediationPlanV1,
    stale_evidence_after_seconds: int,
    freeze_window_requested: bool = False,
) -> PolicyDecision:
    settings = AgentPolicySettings.from_env()
    results: List[str] = []

    if settings.global_kill_switch:
        return PolicyDecision(
            allowed=False,
            status=AgentPlanStatus.BLOCKED,
            results=["Global kill switch is enabled."],
        )

    if settings.freeze_window_active or freeze_window_requested:
        return PolicyDecision(
            allowed=False,
            status=AgentPlanStatus.BLOCKED,
            results=["Execution blocked by freeze window policy."],
        )

    plan_age_seconds = (
        datetime.now(timezone.utc) - datetime.fromisoformat(plan.created_at)
    ).total_seconds()
    if plan_age_seconds > stale_evidence_after_seconds:
        return PolicyDecision(
            allowed=False,
            status=AgentPlanStatus.BLOCKED,
            results=["Execution blocked due to stale evidence window."],
        )

    if plan.risk_level in {PlanRiskLevel.HIGH, PlanRiskLevel.CRITICAL}:
        results.append("High-risk plan requires explicit Observatory UI approval.")

    return PolicyDecision(
        allowed=True, status=AgentPlanStatus.SIMULATED, results=results
    )


def can_auto_execute(plan: RemediationPlanV1) -> bool:
    settings = AgentPolicySettings.from_env()
    if settings.global_kill_switch or settings.freeze_window_active:
        return False
    if not settings.auto_execute_low_risk:
        return False
    return plan.risk_level == PlanRiskLevel.LOW
