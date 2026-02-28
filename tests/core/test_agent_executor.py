from datetime import datetime, timezone

from holmes.core.agent_executor import DeterministicExecutor
from holmes.core.models import (
    PlanRiskLevel,
    RemediationActionV1,
    RemediationEvidenceV1,
    RemediationPlanV1,
)


def test_executor_uses_kubernetes_adapter_by_default():
    plan = RemediationPlanV1(
        plan_id="plan_1",
        provider_id="kepler",
        version="v1",
        summary="test",
        confidence=0.7,
        risk_level=PlanRiskLevel.MEDIUM,
        created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        actions=[
            RemediationActionV1(
                id="a1",
                title="test",
                description="test",
                target="system:all",
                action_type="propose_remediation",
                risk_level=PlanRiskLevel.MEDIUM,
            )
        ],
        evidence=[RemediationEvidenceV1(id="e1", source="test", summary="test")],
        metadata={},
    )
    executor = DeterministicExecutor()
    result = executor.execute(plan, dry_run=True)
    assert result.adapter == "kubernetes"
    assert result.dry_run is True
    assert result.executed is False
