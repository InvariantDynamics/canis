from dataclasses import dataclass
from typing import Dict, List

from holmes.core.models import PlanRiskLevel, RemediationPlanV1


@dataclass(frozen=True)
class ExecutionResult:
    adapter: str
    executed: bool
    dry_run: bool
    messages: List[str]


class BaseExecutionAdapter:
    name = "base"

    def execute(self, plan: RemediationPlanV1, dry_run: bool) -> ExecutionResult:
        raise NotImplementedError


class KubernetesExecutionAdapter(BaseExecutionAdapter):
    name = "kubernetes"

    def execute(self, plan: RemediationPlanV1, dry_run: bool) -> ExecutionResult:
        messages = [f"Kubernetes adapter prepared for plan {plan.plan_id}."]
        for action in plan.actions:
            messages.append(
                f"Action {action.id}: type={action.action_type}, target={action.target}, "
                f"risk={action.risk_level.value}"
            )
        messages.append("Dry-run only." if dry_run else "Execution requested.")
        return ExecutionResult(
            adapter=self.name, executed=not dry_run, dry_run=dry_run, messages=messages
        )


class AwsExecutionAdapter(BaseExecutionAdapter):
    name = "aws"

    def execute(self, plan: RemediationPlanV1, dry_run: bool) -> ExecutionResult:
        messages = [
            f"AWS adapter prepared for plan {plan.plan_id}.",
            "This adapter is approval-only in v1 and returns deterministic previews.",
        ]
        return ExecutionResult(
            adapter=self.name, executed=not dry_run, dry_run=dry_run, messages=messages
        )


class DeterministicExecutor:
    def __init__(self):
        self._adapters: Dict[str, BaseExecutionAdapter] = {
            "kubernetes": KubernetesExecutionAdapter(),
            "aws": AwsExecutionAdapter(),
        }

    def execute(
        self, plan: RemediationPlanV1, dry_run: bool = False, adapter_hint: str = ""
    ) -> ExecutionResult:
        hint = adapter_hint.strip().lower()
        adapter_name = hint if hint in self._adapters else self._choose_adapter(plan)
        adapter = self._adapters[adapter_name]
        return adapter.execute(plan, dry_run=dry_run)

    @staticmethod
    def _choose_adapter(plan: RemediationPlanV1) -> str:
        has_aws_target = any(
            "aws" in action.target.lower() or "lambda" in action.target.lower()
            for action in plan.actions
        )
        if has_aws_target:
            return "aws"
        if plan.risk_level == PlanRiskLevel.CRITICAL:
            return "kubernetes"
        return "kubernetes"
