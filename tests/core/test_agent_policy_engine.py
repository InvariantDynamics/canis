from datetime import datetime, timezone

from holmes.core.agent_policy_engine import can_auto_execute, evaluate_simulation_policy
from holmes.core.models import (
    PlanRiskLevel,
    RemediationActionV1,
    RemediationEvidenceV1,
    RemediationPlanV1,
)


def _plan(plan_id: str = "plan_1", risk_level: PlanRiskLevel = PlanRiskLevel.LOW):
    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return RemediationPlanV1(
        plan_id=plan_id,
        provider_id="kepler",
        version="v1",
        summary="test",
        confidence=0.8,
        risk_level=risk_level,
        created_at=created_at,
        actions=[
            RemediationActionV1(
                id=f"{plan_id}-a1",
                title="Read context",
                description="Read",
                target="system:all",
                action_type="read_context",
                risk_level=PlanRiskLevel.LOW,
            )
        ],
        evidence=[
            RemediationEvidenceV1(id=f"{plan_id}-e1", source="test", summary="summary")
        ],
        metadata={},
    )


def test_policy_blocks_with_kill_switch(monkeypatch):
    monkeypatch.setenv("AGENT_GLOBAL_KILL_SWITCH", "true")
    decision = evaluate_simulation_policy(_plan(), stale_evidence_after_seconds=3600)
    assert decision.allowed is False
    assert any("kill switch" in msg.lower() for msg in decision.results)


def test_policy_allows_and_flags_high_risk():
    decision = evaluate_simulation_policy(
        _plan(risk_level=PlanRiskLevel.HIGH), stale_evidence_after_seconds=3600
    )
    assert decision.allowed is True
    assert any("high-risk" in msg.lower() for msg in decision.results)


def test_auto_execute_low_risk(monkeypatch):
    monkeypatch.setenv("AGENT_AUTO_EXECUTE_LOW_RISK", "true")
    assert can_auto_execute(_plan(risk_level=PlanRiskLevel.LOW)) is True
    assert can_auto_execute(_plan(risk_level=PlanRiskLevel.HIGH)) is False
