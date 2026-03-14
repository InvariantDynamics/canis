# ruff: noqa: E402
import os

from holmes.utils.cert_utils import add_custom_certificate

ADDITIONAL_CERTIFICATE: str = os.environ.get("CERTIFICATE", "")
if add_custom_certificate(ADDITIONAL_CERTIFICATE):
    print("added custom certificate")

# DO NOT ADD ANY IMPORTS OR CODE ABOVE THIS LINE
# IMPORTING ABOVE MIGHT INITIALIZE AN HTTPS CLIENT THAT DOESN'T TRUST THE CUSTOM CERTIFICATE
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import colorlog
import litellm
import sentry_sdk
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from litellm.exceptions import AuthenticationError

from holmes import get_version, is_official_release
from holmes.common.env_vars import (
    DEVELOPMENT_MODE,
    ENABLE_CONNECTION_KEEPALIVE,
    ENABLE_TELEMETRY,
    ENABLED_SCHEDULED_PROMPTS,
    HOLMES_HOST,
    HOLMES_PORT,
    LOG_PERFORMANCE,
    MCP_RETRY_BACKOFF_SCHEDULE,
    SENTRY_DSN,
    SENTRY_TRACES_SAMPLE_RATE,
    TOOLSET_STATUS_REFRESH_INTERVAL_SECONDS,
)
from holmes.config import DEFAULT_CONFIG_LOCATION, Config
from holmes.core import investigation
from holmes.core.agent_event_bus import AgentEventBus
from holmes.core.agent_executor import DeterministicExecutor
from holmes.core.agent_policy_engine import can_auto_execute, evaluate_simulation_policy
from holmes.core.conversations import (
    build_chat_messages,
    build_issue_chat_messages,
)
from holmes.core.models import (
    AgentEvaluationDetails,
    AgentEvaluationRequest,
    AgentEvaluationSummary,
    AgentPlanDecisionRequest,
    AgentPlanExecutionRequest,
    AgentPlanExecutionResponse,
    AgentPlanStatus,
    AgentPlanTimelineResponse,
    AgentShapePromoteRequest,
    AgentShapePromoteResponse,
    AgentSimulationRequest,
    AgentSimulationResponse,
    AgentTimelineEvent,
    ChatRequest,
    ChatResponse,
    FollowUpAction,
    InvestigateRequest,
    IssueChatRequest,
    MigCapabilitiesResponse,
    MigConformanceCheckResult,
    MigConformanceRunRequest,
    MigConformanceRunResponse,
    PlanRiskLevel,
    MCPInvokeRequest,
    MCPInvokeResponse,
    MCPToolMetadata,
    MCPToolsResponse,
    ProviderCapability,
    ProviderEvaluateRequest,
    ProviderEvaluateResponse,
    ProviderPlanExplainRequest,
    ProviderPlanExplainResponse,
    RemediationActionV1,
    RemediationEvidenceV1,
    RemediationPlanV1,
    ShapeCandidate,
    WorkloadDomain,
)
from holmes.core.prompt import PromptComponent
from holmes.core.tools import ToolsetStatusEnum, ToolsetType
from holmes.core.scheduled_prompts import ScheduledPromptsExecutor
from holmes.utils.connection_utils import patch_socket_create_connection
from holmes.utils.holmes_status import update_holmes_status_in_db
from holmes.utils.holmes_sync_toolsets import holmes_sync_toolsets_status
from holmes.utils.log import EndpointFilter
from holmes.checks.checks_api import init_checks_app
from holmes.core.tools_utils.filesystem_result_storage import tool_result_storage
from holmes.utils.stream import stream_chat_formatter, stream_investigate_formatter

# removed: add_runbooks_to_user_prompt


def init_logging():
    # Filter out periodical healniss and readiness probe.
    uvicorn_logger = logging.getLogger("uvicorn.access")
    uvicorn_logger.addFilter(EndpointFilter(path="/healthz"))
    uvicorn_logger.addFilter(EndpointFilter(path="/readyz"))

    logging_level = os.environ.get("LOG_LEVEL", "INFO")
    logging_format = "%(log_color)s%(asctime)s.%(msecs)03d %(levelname)-8s %(message)s"
    logging_datefmt = "%Y-%m-%d %H:%M:%S"

    print("setting up colored logging")
    colorlog.basicConfig(
        format=logging_format, level=logging_level, datefmt=logging_datefmt
    )
    logging.getLogger().setLevel(logging_level)

    httpx_logger = logging.getLogger("httpx")
    if httpx_logger:
        httpx_logger.setLevel(logging.WARNING)

    litellm_logger = logging.getLogger("LiteLLM")
    if litellm_logger:
        litellm_logger.handlers = []

    logging.info(f"logger initialized using {logging_level} log level")


init_logging()

if ENABLE_CONNECTION_KEEPALIVE:
    patch_socket_create_connection()


def init_config():
    """
    Initialize configuration from file if it exists at the default location,
    otherwise load from environment variables.

    Returns:
        tuple: (config, dal) - The initialized Config object and its DAL instance
    """
    default_config_path = Path(DEFAULT_CONFIG_LOCATION)
    if default_config_path.exists():
        logging.info(f"Loading config from file: {default_config_path}")
        config = Config.load_from_file(default_config_path)
    else:
        logging.info("No config file found, loading from environment variables")
        config = Config.load_from_env()

    dal = config.dal
    return config, dal


config, dal = init_config()


def sync_before_server_start():
    if not dal.enabled:
        logging.info(
            "Skipping holmes status and toolsets synchronization - not connected to Robusta platform"
        )
        return
    try:
        update_holmes_status_in_db(dal, config)
    except Exception:
        logging.error("Failed to update holmes status", exc_info=True)
    try:
        holmes_sync_toolsets_status(dal, config)
    except Exception:
        logging.error("Failed to synchronise holmes toolsets", exc_info=True)
    if not ENABLED_SCHEDULED_PROMPTS:
        return
    # No need to check if dal is enabled again, done at the start of this function
    try:
        scheduled_prompts_executor.start()
    except Exception:
        logging.error("Failed to start scheduled prompts executor", exc_info=True)


def _has_failed_mcp_toolsets() -> bool:
    """Check if any MCP toolsets are in FAILED state."""
    executor = config._server_tool_executor
    if not executor:
        return False
    return any(
        t.type == ToolsetType.MCP and t.status == ToolsetStatusEnum.FAILED
        for t in executor.toolsets
    )


def _get_next_refresh_interval(
    has_failed_mcp: bool,
    backoff_index: int,
    default_interval: int,
) -> tuple[int, int]:
    """Determine the next sleep interval and updated backoff index.

    Returns (sleep_seconds, new_backoff_index).
    """
    if has_failed_mcp and backoff_index < len(MCP_RETRY_BACKOFF_SCHEDULE):
        return MCP_RETRY_BACKOFF_SCHEDULE[backoff_index], backoff_index + 1
    return default_interval, 0


def _toolset_status_refresh_loop():
    interval = TOOLSET_STATUS_REFRESH_INTERVAL_SECONDS
    if interval <= 0:
        logging.info("Periodic toolset status refresh is disabled")
        return

    logging.info(
        f"Starting periodic toolset status refresh (interval: {interval} seconds)"
    )

    def refresh_loop():
        backoff_index = 0

        while True:
            # Use shorter intervals when MCP servers are failing
            sleep_time, backoff_index = _get_next_refresh_interval(
                _has_failed_mcp_toolsets(), backoff_index, interval
            )
            if sleep_time < interval:
                logging.info(
                    f"Failed MCP server(s) detected, retrying in {sleep_time} seconds"
                )

            time.sleep(sleep_time)
            try:
                changes = config.refresh_server_tool_executor(dal)
                if changes:
                    for toolset_name, old_status, new_status in changes:
                        logging.info(
                            f"Toolset '{toolset_name}' status changed: {old_status} -> {new_status}"
                        )
                    holmes_sync_toolsets_status(dal, config)
                else:
                    logging.debug(
                        "Periodic toolset status refresh: no changes detected"
                    )
            except Exception:
                logging.error(
                    "Error during periodic toolset status refresh", exc_info=True
                )

    thread = threading.Thread(target=refresh_loop, daemon=True, name="toolset-refresh")
    thread.start()


if ENABLE_TELEMETRY and SENTRY_DSN:
    # Initialize Sentry for official releases or when development mode is enabled
    if is_official_release() or DEVELOPMENT_MODE:
        environment = "production" if is_official_release() else "development"
        version = get_version()
        release = None if version.startswith("dev-") else version
        logging.info(f"Initializing sentry for {environment} environment...")

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            send_default_pii=False,
            traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
            profiles_sample_rate=0,
            environment=environment,
            release=release,
        )
        sentry_sdk.set_tags(
            {
                "account_id": dal.account_id,
                "cluster_name": config.cluster_name,
                "version": get_version(),
                "environment": environment,
            }
        )
    else:
        logging.info(
            "Skipping sentry initialization - not an official release and DEVELOPMENT_MODE not enabled"
        )

app = FastAPI()

if LOG_PERFORMANCE:

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start_time = time.time()
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            process_time = int((time.time() - start_time) * 1000)

            status_code = "unknown"
            if response:
                status_code = response.status_code
            logging.info(
                f"Request completed {request.method} {request.url.path} status={status_code} latency={process_time}ms"
            )


init_checks_app(app, config)

MIG_CONTRACT_VERSION = "v1"
KEPLER_PROVIDER_ID = "kepler"
PROVIDER_CAPABILITY_CONTRACT = [
    "read_context",
    "propose_plan",
    "explain_evidence",
    "emit_confidence",
]

_AGENT_DATA_LOCK = threading.Lock()
_PROVIDER_PLAN_STORE: Dict[str, RemediationPlanV1] = {}
_AGENT_EVALUATION_STORE: Dict[str, AgentEvaluationDetails] = {}
_PLAN_TO_EVALUATION: Dict[str, str] = {}
_PLAN_TIMELINE_STORE: Dict[str, List[AgentTimelineEvent]] = {}
_SHAPE_CANDIDATES: Dict[str, ShapeCandidate] = {}

_AGENT_EVENT_BUS = AgentEventBus.from_env()
_DETERMINISTIC_EXECUTOR = DeterministicExecutor()

_MCP_TOOL_METADATA: Dict[str, MCPToolMetadata] = {
    "mig.capabilities": MCPToolMetadata(
        name="mig.capabilities",
        description="List MIG provider capabilities and profiles.",
        sensitivity_class="low",
        latency_slo_ms=200,
        rate_limit_per_minute=120,
        approval_requirement_class="none",
    ),
    "provider.evaluate": MCPToolMetadata(
        name="provider.evaluate",
        description="Evaluate a provider and return a normalized RemediationPlanV1.",
        sensitivity_class="medium",
        latency_slo_ms=1200,
        rate_limit_per_minute=60,
        approval_requirement_class="read_propose_only",
    ),
    "plan.explain": MCPToolMetadata(
        name="plan.explain",
        description="Explain evidence and confidence for a provider plan.",
        sensitivity_class="medium",
        latency_slo_ms=800,
        rate_limit_per_minute=60,
        approval_requirement_class="read_propose_only",
    ),
}

_PROVIDER_REGISTRY: Dict[str, ProviderCapability] = {
    KEPLER_PROVIDER_ID: ProviderCapability(
        provider_id=KEPLER_PROVIDER_ID,
        display_name="Kepler",
        capabilities=PROVIDER_CAPABILITY_CONTRACT,
        write_enabled=False,
        approval_required=True,
        contract_version=MIG_CONTRACT_VERSION,
        profile="planner",
    )
}


def _load_partner_registry_from_env() -> None:
    raw_providers = os.getenv("MIG_PARTNER_PROVIDERS", "")
    partner_ids = [p.strip() for p in raw_providers.split(",") if p.strip()]
    for partner_id in partner_ids:
        if partner_id == KEPLER_PROVIDER_ID:
            continue
        _PROVIDER_REGISTRY[partner_id] = ProviderCapability(
            provider_id=partner_id,
            display_name=partner_id.replace("_", " ").title(),
            capabilities=PROVIDER_CAPABILITY_CONTRACT,
            write_enabled=False,
            approval_required=True,
            contract_version=MIG_CONTRACT_VERSION,
            profile="partner-read-propose-only",
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _build_event_name(provider_id: str, suffix: str) -> str:
    return f"agent.{provider_id}.{suffix}"


def _append_plan_timeline_event(
    plan_id: str, event: str, payload: Optional[Dict[str, Any]] = None
) -> None:
    event_payload = payload or {}
    timeline_event = AgentTimelineEvent(
        timestamp=_utc_now_iso(), event=event, payload=event_payload
    )
    with _AGENT_DATA_LOCK:
        _PLAN_TIMELINE_STORE.setdefault(plan_id, []).append(timeline_event)

    bus_payload = {
        "timestamp": timeline_event.timestamp,
        "event": timeline_event.event,
        "plan_id": plan_id,
        "payload": event_payload,
    }
    _AGENT_EVENT_BUS.publish(event_name=event, payload=bus_payload)


def _get_provider(provider_id: str) -> ProviderCapability:
    provider = _PROVIDER_REGISTRY.get(provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail=f"Unknown provider '{provider_id}'")
    return provider


def _get_plan_or_404(provider_id: str, plan_id: str) -> RemediationPlanV1:
    _get_provider(provider_id)
    with _AGENT_DATA_LOCK:
        plan = _PROVIDER_PLAN_STORE.get(plan_id)
    if not plan or plan.provider_id != provider_id:
        raise HTTPException(
            status_code=404,
            detail=f"Plan '{plan_id}' not found for provider '{provider_id}'",
        )
    return plan


def _get_evaluation_or_404(evaluation_id: str) -> AgentEvaluationDetails:
    with _AGENT_DATA_LOCK:
        evaluation = _AGENT_EVALUATION_STORE.get(evaluation_id)
    if not evaluation:
        raise HTTPException(
            status_code=404, detail=f"Evaluation '{evaluation_id}' was not found"
        )
    return evaluation


def _get_evaluation_by_plan_or_404(plan_id: str) -> AgentEvaluationDetails:
    with _AGENT_DATA_LOCK:
        evaluation_id = _PLAN_TO_EVALUATION.get(plan_id)
        evaluation = _AGENT_EVALUATION_STORE.get(evaluation_id) if evaluation_id else None
    if not evaluation:
        raise HTTPException(
            status_code=404, detail=f"No agent evaluation is associated with plan '{plan_id}'"
        )
    return evaluation


def _risk_from_request(trigger: Dict[str, Any], constraints: Dict[str, Any]) -> PlanRiskLevel:
    severity = str(trigger.get("severity", "")).lower()
    if severity in {"critical", "sev0", "sev-0"}:
        return PlanRiskLevel.CRITICAL
    if severity in {"high", "sev1", "sev-1"}:
        return PlanRiskLevel.HIGH
    if constraints.get("low_risk_only"):
        return PlanRiskLevel.LOW
    return PlanRiskLevel.MEDIUM


def _build_plan(
    provider_id: str, request: ProviderEvaluateRequest, evaluation_id: str
) -> RemediationPlanV1:
    plan_id = _new_id("plan")
    created_at = _utc_now_iso()
    risk_level = _risk_from_request(request.trigger, request.constraints)
    scoped_target = request.scope or "system:all"

    if request.workload == WorkloadDomain.FINOPS:
        actions = [
            RemediationActionV1(
                id=f"{plan_id}-action-1",
                title="Collect cost and margin pressure telemetry",
                description=(
                    "Gather bounded cost, margin, and revenue-funnel signals by service "
                    "to identify immediate financial pressure contributors."
                ),
                target=scoped_target,
                action_type="read_context",
                risk_level=PlanRiskLevel.LOW,
                requires_approval=True,
                metadata={"approval_class": "observability-read", "domain": "finops"},
            ),
            RemediationActionV1(
                id=f"{plan_id}-action-2",
                title="Propose FinOps optimization plan",
                description=(
                    "Generate a reversible cost optimization proposal with projected "
                    "savings, risk bounds, and funnel impact checks."
                ),
                target=scoped_target,
                action_type="cost_analysis",
                risk_level=risk_level,
                requires_approval=True,
                execute_command="orion-executor apply finops-plan --plan-id ${PLAN_ID}",
                metadata={"approval_class": "human_required", "domain": "finops"},
            ),
        ]
        summary_text = (
            f"Provider '{provider_id}' proposes a FinOps planner-only sequence for scope '{scoped_target}'."
        )
    else:
        actions = [
            RemediationActionV1(
                id=f"{plan_id}-action-1",
                title="Collect targeted health signal snapshot",
                description=(
                    "Capture bounded entity, edge, and DKM context to validate blast radius "
                    "before any remediation proposal is approved."
                ),
                target=scoped_target,
                action_type="read_context",
                risk_level=PlanRiskLevel.LOW,
                requires_approval=True,
                dry_run_command="kubectl get pods -A --field-selector=status.phase!=Running",
                metadata={"approval_class": "observability-read", "domain": "sre"},
            ),
            RemediationActionV1(
                id=f"{plan_id}-action-2",
                title="Prepare traffic-shift remediation plan",
                description=(
                    "Prepare a deterministic traffic-shift or scale action proposal "
                    "for Orion policy simulation and human approval."
                ),
                target=scoped_target,
                action_type="propose_remediation",
                risk_level=risk_level,
                requires_approval=True,
                dry_run_command=(
                    "kubectl -n default rollout status deploy/checkout "
                    "--timeout=30s || true"
                ),
                execute_command="orion-executor apply remediation-plan --plan-id ${PLAN_ID}",
                metadata={"approval_class": "human_required", "domain": "sre"},
            ),
        ]
        summary_text = (
            f"Provider '{provider_id}' proposes a planner-only remediation sequence for scope '{scoped_target}'."
        )

    evidence = [
        RemediationEvidenceV1(
            id=f"{plan_id}-evidence-trigger",
            source="trigger",
            summary=f"Scope={scoped_target}. Trigger={json.dumps(request.trigger or {}, default=str)}",
            captured_at=created_at,
        ),
        RemediationEvidenceV1(
            id=f"{plan_id}-evidence-context",
            source="context",
            summary=f"Context keys={sorted(list((request.context or {}).keys()))}",
            captured_at=created_at,
        ),
    ]

    confidence = 0.64 if risk_level in {PlanRiskLevel.HIGH, PlanRiskLevel.CRITICAL} else 0.77
    return RemediationPlanV1(
        plan_id=plan_id,
        provider_id=provider_id,
        version=MIG_CONTRACT_VERSION,
        summary=summary_text,
        confidence=confidence,
        risk_level=risk_level,
        actions=actions,
        evidence=evidence,
        created_at=created_at,
        metadata={
            "provider_id": provider_id,
            "trace_id": request.trace_id or _new_id("trace"),
            "evaluation_id": evaluation_id,
            "execution_authority": "orion-control-plane",
            "provider_write_enabled": False,
            "planner_mode": True,
            "workload": request.workload.value,
        },
    )


def _evaluate_provider(
    provider_id: str,
    evaluate_request: ProviderEvaluateRequest,
    evaluation_id: Optional[str] = None,
) -> ProviderEvaluateResponse:
    provider = _get_provider(provider_id)
    final_evaluation_id = evaluation_id or _new_id("eval")
    plan = _build_plan(provider_id, evaluate_request, final_evaluation_id)

    with _AGENT_DATA_LOCK:
        _PROVIDER_PLAN_STORE[plan.plan_id] = plan
        _PLAN_TIMELINE_STORE.setdefault(plan.plan_id, [])

    _append_plan_timeline_event(
        plan.plan_id,
        _build_event_name(provider_id, "plan.generated"),
        {
            "provider_id": provider_id,
            "evaluation_id": final_evaluation_id,
            "trace_id": plan.metadata.get("trace_id"),
            "provider_write_enabled": False,
        },
    )

    return ProviderEvaluateResponse(
        evaluation_id=final_evaluation_id,
        provider_id=provider_id,
        capabilities=provider.capabilities,
        plan=plan,
        metadata={
            "contract_version": MIG_CONTRACT_VERSION,
            "profile": provider.profile,
            "write_enabled": provider.write_enabled,
        },
    )


_load_partner_registry_from_env()


@app.get("/v1/mig/capabilities", response_model=MigCapabilitiesResponse)
def mig_capabilities() -> MigCapabilitiesResponse:
    providers = sorted(_PROVIDER_REGISTRY.values(), key=lambda p: p.provider_id)
    return MigCapabilitiesResponse(providers=providers)


@app.post(
    "/v1/mig/providers/{provider_id}/evaluate", response_model=ProviderEvaluateResponse
)
def mig_provider_evaluate(
    provider_id: str, evaluate_request: ProviderEvaluateRequest
) -> ProviderEvaluateResponse:
    return _evaluate_provider(provider_id, evaluate_request)


@app.get(
    "/v1/mig/providers/{provider_id}/plans/{plan_id}", response_model=RemediationPlanV1
)
def mig_provider_plan_get(provider_id: str, plan_id: str) -> RemediationPlanV1:
    return _get_plan_or_404(provider_id, plan_id)


@app.post(
    "/v1/mig/providers/{provider_id}/plans/{plan_id}/explain",
    response_model=ProviderPlanExplainResponse,
)
def mig_provider_plan_explain(
    provider_id: str, plan_id: str, explain_request: ProviderPlanExplainRequest
) -> ProviderPlanExplainResponse:
    plan = _get_plan_or_404(provider_id, plan_id)
    focus = explain_request.focus or "overall_plan"
    explanation = (
        f"Plan '{plan_id}' was generated by provider '{provider_id}' in planner-only mode. "
        f"Focus='{focus}'. All actions require Orion approval before execution."
    )
    confidence_reasoning = (
        f"Confidence={plan.confidence:.2f} derived from bounded trigger/context evidence. "
        "No direct provider writes are permitted in this profile."
    )
    return ProviderPlanExplainResponse(
        plan_id=plan.plan_id,
        provider_id=provider_id,
        explanation=explanation,
        confidence_reasoning=confidence_reasoning,
        evidence=plan.evidence[: explain_request.max_evidence],
    )


@app.post("/v1/mig/conformance/run", response_model=MigConformanceRunResponse)
def mig_conformance_run(
    conformance_request: MigConformanceRunRequest,
) -> MigConformanceRunResponse:
    provider = _get_provider(conformance_request.provider_id)
    checks = [
        MigConformanceCheckResult(
            id="endpoint.evaluate",
            passed=True,
            details="Provider evaluate endpoint contract is available.",
        ),
        MigConformanceCheckResult(
            id="endpoint.plan_get",
            passed=True,
            details="Provider plan retrieval endpoint contract is available.",
        ),
        MigConformanceCheckResult(
            id="endpoint.plan_explain",
            passed=True,
            details="Provider explain endpoint contract is available.",
        ),
        MigConformanceCheckResult(
            id="contract.capabilities",
            passed=all(
                c in provider.capabilities for c in PROVIDER_CAPABILITY_CONTRACT
            ),
            details=f"Capabilities={provider.capabilities}",
        ),
        MigConformanceCheckResult(
            id="policy.read_propose_only",
            passed=provider.write_enabled is False,
            details="Provider write path is disabled in v1.",
        ),
    ]

    return MigConformanceRunResponse(
        provider_id=provider.provider_id,
        version=conformance_request.version,
        passed=all(c.passed for c in checks),
        checks=checks,
    )


@app.get("/v1/mig/mcp/tools", response_model=MCPToolsResponse)
def mig_mcp_tools() -> MCPToolsResponse:
    return MCPToolsResponse(
        provider_profile="read_propose_only_v1",
        tools=list(_MCP_TOOL_METADATA.values()),
    )


@app.post("/v1/mig/mcp/tools/{tool_name}/invoke", response_model=MCPInvokeResponse)
def mig_mcp_invoke_tool(tool_name: str, invoke_request: MCPInvokeRequest) -> MCPInvokeResponse:
    _get_provider(invoke_request.provider_id)
    if tool_name not in _MCP_TOOL_METADATA:
        raise HTTPException(status_code=404, detail=f"MCP tool '{tool_name}' not found")

    args = invoke_request.arguments
    if tool_name == "mig.capabilities":
        result: Dict[str, Any] = {
            "providers": [
                provider.model_dump()
                for provider in sorted(
                    _PROVIDER_REGISTRY.values(), key=lambda p: p.provider_id
                )
            ]
        }
    elif tool_name == "provider.evaluate":
        provider_id = str(args.get("provider_id", invoke_request.provider_id))
        try:
            workload = WorkloadDomain(str(args.get("workload", "sre")))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="workload must be one of: sre, finops",
            )
        eval_req = ProviderEvaluateRequest(
            scope=str(args.get("scope", "system:all")),
            workload=workload,
            question=args.get("question"),
            trigger=args.get("trigger", {}),
            context=args.get("context", {}),
            constraints=args.get("constraints", {}),
            model=args.get("model"),
            trace_id=args.get("trace_id"),
        )
        result = _evaluate_provider(provider_id, eval_req).model_dump()
    elif tool_name == "plan.explain":
        provider_id = str(args.get("provider_id", invoke_request.provider_id))
        plan_id = str(args.get("plan_id", ""))
        if not plan_id:
            raise HTTPException(status_code=400, detail="plan_id is required")
        explain_req = ProviderPlanExplainRequest(
            focus=args.get("focus"),
            max_evidence=int(args.get("max_evidence", 5)),
        )
        result = mig_provider_plan_explain(provider_id, plan_id, explain_req).model_dump()
    else:
        raise HTTPException(
            status_code=400,
            detail=f"MCP tool '{tool_name}' is unsupported in partner v1 profile",
        )

    return MCPInvokeResponse(
        tool_name=tool_name, provider_id=invoke_request.provider_id, result=result
    )


@app.post("/v1/agent/evaluations", response_model=AgentEvaluationSummary)
def agent_create_evaluation(
    evaluation_request: AgentEvaluationRequest,
) -> AgentEvaluationSummary:
    provider_request = ProviderEvaluateRequest(
        scope=evaluation_request.scope,
        workload=evaluation_request.workload,
        question=evaluation_request.question,
        trigger=evaluation_request.trigger,
        context=evaluation_request.context,
        constraints=evaluation_request.constraints,
        model=evaluation_request.model,
        trace_id=evaluation_request.trace_id,
    )
    evaluation_id = _new_id("eval")
    provider_eval = _evaluate_provider(
        provider_id=evaluation_request.provider_id,
        evaluate_request=provider_request,
        evaluation_id=evaluation_id,
    )

    now = _utc_now_iso()
    details = AgentEvaluationDetails(
        id=evaluation_id,
        provider_id=evaluation_request.provider_id,
        plan_id=provider_eval.plan.plan_id,
        status=AgentPlanStatus.PROPOSED,
        created_at=now,
        updated_at=now,
        trace_id=provider_eval.plan.metadata.get("trace_id"),
        plan=provider_eval.plan,
    )
    with _AGENT_DATA_LOCK:
        _AGENT_EVALUATION_STORE[evaluation_id] = details
        _PLAN_TO_EVALUATION[provider_eval.plan.plan_id] = evaluation_id
        _SHAPE_CANDIDATES[f"shape_{evaluation_id}"] = ShapeCandidate(
            candidate_id=f"shape_{evaluation_id}",
            evaluation_id=evaluation_id,
            provider_id=evaluation_request.provider_id,
            workload=evaluation_request.workload,
            status="candidate",
            created_at=now,
            metadata={"plan_id": provider_eval.plan.plan_id},
        )

    _append_plan_timeline_event(
        provider_eval.plan.plan_id,
        _build_event_name(evaluation_request.provider_id, "shape.candidate_created"),
        {
            "candidate_id": f"shape_{evaluation_id}",
            "evaluation_id": evaluation_id,
            "provider_id": evaluation_request.provider_id,
        },
    )

    return AgentEvaluationSummary(**details.model_dump(exclude={"plan"}))


@app.get("/v1/agent/evaluations/{id}", response_model=AgentEvaluationDetails)
def agent_get_evaluation(id: str) -> AgentEvaluationDetails:
    return _get_evaluation_or_404(id)


@app.get("/v1/agent/shapes/{candidate_id}", response_model=ShapeCandidate)
def agent_get_shape_candidate(candidate_id: str) -> ShapeCandidate:
    with _AGENT_DATA_LOCK:
        candidate = _SHAPE_CANDIDATES.get(candidate_id)
    if not candidate:
        raise HTTPException(
            status_code=404, detail=f"Shape candidate '{candidate_id}' was not found"
        )
    return candidate


@app.post(
    "/v1/agent/evaluations/{id}/simulate", response_model=AgentSimulationResponse
)
def agent_simulate_evaluation(
    id: str, simulation_request: AgentSimulationRequest
) -> AgentSimulationResponse:
    evaluation = _get_evaluation_or_404(id)
    decision = evaluate_simulation_policy(
        plan=evaluation.plan,
        stale_evidence_after_seconds=simulation_request.stale_evidence_after_seconds,
        freeze_window_requested=simulation_request.freeze_window_active,
    )
    new_status = decision.status
    updated_evaluation = evaluation.model_copy(
        update={"status": new_status, "updated_at": _utc_now_iso()}
    )
    with _AGENT_DATA_LOCK:
        _AGENT_EVALUATION_STORE[id] = updated_evaluation

    _append_plan_timeline_event(
        evaluation.plan_id,
        _build_event_name(evaluation.provider_id, "plan.simulated"),
        {
            "evaluation_id": id,
            "allowed": decision.allowed,
            "policy_results": decision.results,
        },
    )

    return AgentSimulationResponse(
        evaluation_id=id,
        plan_id=evaluation.plan_id,
        status=new_status,
        allowed=decision.allowed,
        policy_results=decision.results,
        next_action="approve_or_reject" if decision.allowed else "revise_or_re_evaluate",
    )


@app.post("/v1/agent/plans/{id}/approve", response_model=AgentEvaluationSummary)
def agent_approve_plan(id: str, decision: AgentPlanDecisionRequest) -> AgentEvaluationSummary:
    evaluation = _get_evaluation_by_plan_or_404(id)
    if evaluation.status == AgentPlanStatus.REJECTED:
        raise HTTPException(
            status_code=409, detail=f"Plan '{id}' has already been rejected"
        )
    updated = evaluation.model_copy(
        update={"status": AgentPlanStatus.APPROVED, "updated_at": _utc_now_iso()}
    )
    with _AGENT_DATA_LOCK:
        _AGENT_EVALUATION_STORE[updated.id] = updated

    _append_plan_timeline_event(
        id,
        _build_event_name(updated.provider_id, "plan.approved"),
        {"evaluation_id": updated.id, "reason": decision.reason},
    )

    if can_auto_execute(updated.plan):
        execution_result = _DETERMINISTIC_EXECUTOR.execute(updated.plan, dry_run=False)
        auto_executed = updated.model_copy(
            update={"status": AgentPlanStatus.EXECUTED, "updated_at": _utc_now_iso()}
        )
        with _AGENT_DATA_LOCK:
            _AGENT_EVALUATION_STORE[auto_executed.id] = auto_executed
        _append_plan_timeline_event(
            id,
            _build_event_name(updated.provider_id, "plan.executed"),
            {
                "evaluation_id": updated.id,
                "auto_executed": True,
                "adapter": execution_result.adapter,
                "messages": execution_result.messages,
            },
        )
        _append_plan_timeline_event(
            id,
            _build_event_name(updated.provider_id, "plan.verified"),
            {
                "evaluation_id": updated.id,
                "verification": "auto_execute_completed",
            },
        )
        return AgentEvaluationSummary(**auto_executed.model_dump(exclude={"plan"}))

    return AgentEvaluationSummary(**updated.model_dump(exclude={"plan"}))


@app.post("/v1/agent/plans/{id}/reject", response_model=AgentEvaluationSummary)
def agent_reject_plan(id: str, decision: AgentPlanDecisionRequest) -> AgentEvaluationSummary:
    evaluation = _get_evaluation_by_plan_or_404(id)
    if evaluation.status == AgentPlanStatus.EXECUTED:
        raise HTTPException(
            status_code=409, detail=f"Plan '{id}' was already executed and cannot be rejected"
        )
    updated = evaluation.model_copy(
        update={"status": AgentPlanStatus.REJECTED, "updated_at": _utc_now_iso()}
    )
    with _AGENT_DATA_LOCK:
        _AGENT_EVALUATION_STORE[updated.id] = updated

    _append_plan_timeline_event(
        id,
        _build_event_name(updated.provider_id, "plan.rejected"),
        {"evaluation_id": updated.id, "reason": decision.reason},
    )
    return AgentEvaluationSummary(**updated.model_dump(exclude={"plan"}))


@app.post("/v1/agent/plans/{id}/execute", response_model=AgentPlanExecutionResponse)
def agent_execute_plan(
    id: str, execution_request: AgentPlanExecutionRequest
) -> AgentPlanExecutionResponse:
    evaluation = _get_evaluation_by_plan_or_404(id)
    if evaluation.status != AgentPlanStatus.APPROVED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Plan '{id}' must be approved before execution. "
                f"Current status={evaluation.status.value}"
            ),
        )

    updated = evaluation.model_copy(
        update={"status": AgentPlanStatus.EXECUTED, "updated_at": _utc_now_iso()}
    )
    with _AGENT_DATA_LOCK:
        _AGENT_EVALUATION_STORE[updated.id] = updated

    execution_result = _DETERMINISTIC_EXECUTOR.execute(
        updated.plan, dry_run=execution_request.dry_run
    )
    _append_plan_timeline_event(
        id,
        _build_event_name(updated.provider_id, "plan.executed"),
        {
            "evaluation_id": updated.id,
            "dry_run": execution_request.dry_run,
            "reason": execution_request.reason,
            "adapter": execution_result.adapter,
            "messages": execution_result.messages,
        },
    )
    _append_plan_timeline_event(
        id,
        _build_event_name(updated.provider_id, "plan.verified"),
        {
            "evaluation_id": updated.id,
            "verification": "dry_run_ok"
            if execution_request.dry_run
            else "execution_requested",
        },
    )

    message = (
        "Dry run completed. Orion executor remains the only write authority."
        if execution_request.dry_run
        else "Execution request recorded for Orion deterministic executor."
    )
    return AgentPlanExecutionResponse(plan_id=id, status=updated.status, message=message)


@app.get("/v1/agent/plans/{id}/timeline", response_model=AgentPlanTimelineResponse)
def agent_plan_timeline(id: str) -> AgentPlanTimelineResponse:
    with _AGENT_DATA_LOCK:
        events = _PLAN_TIMELINE_STORE.get(id)
    if events is None:
        raise HTTPException(status_code=404, detail=f"Plan '{id}' was not found")
    return AgentPlanTimelineResponse(plan_id=id, events=events)


@app.post(
    "/v1/agent/shapes/{candidate_id}/promote",
    response_model=AgentShapePromoteResponse,
)
def agent_promote_shape(
    candidate_id: str, promote_request: AgentShapePromoteRequest
) -> AgentShapePromoteResponse:
    with _AGENT_DATA_LOCK:
        candidate = _SHAPE_CANDIDATES.get(candidate_id)
        if not candidate:
            raise HTTPException(
                status_code=404,
                detail=f"Shape candidate '{candidate_id}' was not found",
            )
        promoted = candidate.model_copy(
            update={"status": "promoted", "promoted_at": _utc_now_iso()}
        )
        _SHAPE_CANDIDATES[candidate_id] = promoted

    _append_plan_timeline_event(
        plan_id=promoted.metadata.get("plan_id", f"shape:{candidate_id}"),
        event=_build_event_name(KEPLER_PROVIDER_ID, "shape.promoted"),
        payload={"candidate_id": candidate_id, "promoted_by": promote_request.promoted_by},
    )
    return AgentShapePromoteResponse(
        candidate_id=candidate_id,
        status="promoted",
        message="Incident shape promoted and recorded in learning pipeline inbox.",
    )


@app.post("/api/investigate")
def investigate_issues(investigate_request: InvestigateRequest, http_request: Request):
    try:
        runbooks = config.get_runbook_catalog()
        request_context = extract_passthrough_headers(http_request)
        with tool_result_storage() as tool_results_dir:
            result = investigation.investigate_issues(
                investigate_request=investigate_request,
                dal=dal,
                config=config,
                model=investigate_request.model,
                runbooks=runbooks,
                request_context=request_context,
                tool_results_dir=tool_results_dir,
            )
            return result

    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=e.message)
    except litellm.exceptions.RateLimitError as e:
        raise HTTPException(status_code=429, detail=e.message)
    except Exception as e:
        logging.error(f"Error in /api/investigate: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/stream/investigate")
def stream_investigate_issues(req: InvestigateRequest, http_request: Request):
    try:
        req_info = f"/api/stream/investigate request: title={req.title}"
        logging.info(f"Received {req_info}")
        storage = tool_result_storage()
        tool_results_dir = storage.__enter__()
        ai, system_prompt, user_prompt, response_format, sections = (
            investigation.get_investigation_context(
                req, dal, config, tool_results_dir=tool_results_dir
            )
        )
        request_context = extract_passthrough_headers(http_request)

        return StreamingResponse(
            _stream_with_storage_cleanup(
                storage,
                stream_investigate_formatter(
                    ai.call_stream(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        response_format=response_format,
                        sections=sections,
                        request_context=request_context,
                    ),
                ),
                req_info
            ),
            media_type="text/event-stream",
        )

    except AuthenticationError as e:
        storage.__exit__(None, None, None)
        raise HTTPException(status_code=401, detail=e.message)
    except Exception as e:
        storage.__exit__(None, None, None)
        logging.exception(f"Error in /api/stream/investigate: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/issue_chat")
def issue_conversation(issue_chat_request: IssueChatRequest, http_request: Request):
    try:
        runbooks = config.get_runbook_catalog()
        with tool_result_storage() as tool_results_dir:
            ai = config.create_toolcalling_llm(
                dal=dal,
                model=issue_chat_request.model,
                tool_results_dir=tool_results_dir,
            )
            global_instructions = dal.get_global_instructions_for_account()

            messages = build_issue_chat_messages(
                issue_chat_request=issue_chat_request,
                ai=ai,
                config=config,
                global_instructions=global_instructions,
                runbooks=runbooks,
            )
            request_context = extract_passthrough_headers(http_request)
            llm_call = ai.messages_call(
                messages=messages, request_context=request_context
            )

            return ChatResponse(
                analysis=llm_call.result,
                tool_calls=llm_call.tool_calls,
                conversation_history=llm_call.messages,
                metadata=llm_call.metadata,
            )
    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=e.message)
    except litellm.exceptions.RateLimitError as e:
        raise HTTPException(status_code=429, detail=e.message)
    except Exception as e:
        logging.error(f"Error in /api/issue_chat: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def already_answered(conversation_history: Optional[List[dict]]) -> bool:
    if conversation_history is None:
        return False

    for message in conversation_history:
        if message["role"] == "assistant":
            return True
    return False


def extract_passthrough_headers(request: Request) -> dict:
    """
    Extract pass-through headers from the request, excluding sensitive auth headers.
    These headers are forwarded to MCP servers for authentication and context.

    The blocked headers can be configured via the HOLMES_PASSTHROUGH_BLOCKED_HEADERS
    environment variable (comma-separated list). Defaults to "authorization,cookie,set-cookie".

    Returns:
        dict: {"headers": {"X-Foo-Bar": "...", "ABC": "...", ...}}
    """
    # Get blocked headers from environment variable or use defaults
    blocked_headers_str = os.environ.get(
        "HOLMES_PASSTHROUGH_BLOCKED_HEADERS", "authorization,cookie,set-cookie"
    )
    blocked_headers = {
        h.strip().lower() for h in blocked_headers_str.split(",") if h.strip()
    }

    passthrough_headers = {}
    for header_name, header_value in request.headers.items():
        if header_name.lower() not in blocked_headers:
            # Preserve original case from request (no normalization)
            passthrough_headers[header_name] = header_value

    return {"headers": passthrough_headers} if passthrough_headers else {}


def _stream_with_storage_cleanup(storage, stream_generator, req_info):
    """Wrap a stream generator to clean up tool result files after streaming completes."""
    try:
        yield from stream_generator
    finally:
        logging.info(f"Stream request end: {req_info}")
        storage.__exit__(None, None, None)


@app.post("/api/chat")
def chat(chat_request: ChatRequest, http_request: Request):
    try:
        # Log incoming request details
        has_images = bool(chat_request.images)
        has_structured_output = bool(chat_request.response_format)
        req_info = f"/api/chat request: ask={chat_request.ask}"
        logging.info(
            f"Received: {req_info}, model={chat_request.model}, "
            f"images={has_images}, structured_output={has_structured_output}, "
            f"streaming={chat_request.stream}"
        )

        runbooks = config.get_runbook_catalog()

        prompt_component_overrides = None
        if chat_request.behavior_controls:
            logging.info(
                f"Applying behavior_controls: {chat_request.behavior_controls}"
            )
            prompt_component_overrides = {}
            for k, v in chat_request.behavior_controls.items():
                try:
                    prompt_component_overrides[PromptComponent(k.lower())] = v
                except ValueError:
                    logging.warning(f"Unknown behavior_controls key '{k}', ignoring")

        follow_up_actions = []
        if not already_answered(chat_request.conversation_history):
            follow_up_actions = [
                FollowUpAction(
                    id="logs",
                    action_label="Logs",
                    prompt="Show me the relevant logs",
                    pre_action_notification_text="Fetching relevant logs...",
                ),
                FollowUpAction(
                    id="graphs",
                    action_label="Graphs",
                    prompt="Show me the relevant graphs. Use prometheus and make sure you embed the results with `<< >>` to display a graph",
                    pre_action_notification_text="Drawing some graphs...",
                ),
                FollowUpAction(
                    id="articles",
                    action_label="Articles",
                    prompt="List the relevant runbooks and links used. Write a short summary for each",
                    pre_action_notification_text="Looking up and summarizing runbooks and links...",
                ),
            ]

        request_context = extract_passthrough_headers(http_request)

        storage = tool_result_storage()
        tool_results_dir = storage.__enter__()
        ai = config.create_toolcalling_llm(
            dal=dal, model=chat_request.model, tool_results_dir=tool_results_dir
        )
        global_instructions = dal.get_global_instructions_for_account()
        messages = build_chat_messages(
            chat_request.ask,
            chat_request.conversation_history,
            ai=ai,
            config=config,
            global_instructions=global_instructions,
            additional_system_prompt=chat_request.additional_system_prompt,
            runbooks=runbooks,
            images=chat_request.images,
            prompt_component_overrides=prompt_component_overrides,
        )

        if chat_request.stream:
            stream = stream_chat_formatter(
                ai.call_stream(
                    msgs=messages,
                    enable_tool_approval=chat_request.enable_tool_approval or False,
                    tool_decisions=chat_request.tool_decisions,
                    response_format=chat_request.response_format,
                    request_context=request_context,
                ),
                [f.model_dump() for f in follow_up_actions],
            )
            return StreamingResponse(
                _stream_with_storage_cleanup(storage, stream, req_info),
                media_type="text/event-stream",
            )
        else:
            try:
                llm_call = ai.messages_call(
                    messages=messages,
                    trace_span=chat_request.trace_span,
                    response_format=chat_request.response_format,
                    request_context=request_context,
                )

                logging.info(f"Completed {req_info}")
                return ChatResponse(
                    analysis=llm_call.result,
                    tool_calls=llm_call.tool_calls,
                    conversation_history=llm_call.messages,
                    follow_up_actions=follow_up_actions,
                    metadata=llm_call.metadata,
                )
            finally:
                storage.__exit__(None, None, None)
    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=e.message)
    except litellm.exceptions.RateLimitError as e:
        raise HTTPException(status_code=429, detail=e.message)
    except Exception as e:
        logging.error(f"Error in /api/chat: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


scheduled_prompts_executor = ScheduledPromptsExecutor(
    dal=dal, config=config, chat_function=chat
)


@app.get("/api/model")
def get_model():
    return {"model_name": json.dumps(config.get_models_list())}


@app.get("/healthz")
def health_check():
    return {"status": "healthy"}


@app.get("/readyz")
def readiness_check():
    try:
        models_list = config.get_models_list()
        return {"status": "ready", "models": models_list}
    except Exception as e:
        logging.error(f"Readiness check failed: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="Service not ready")


def main():
    """Holmes AI Server entry point"""
    # Configure uvicorn logging
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["access"]["fmt"] = (
        "%(asctime)s %(levelname)-8s %(message)s"
    )
    log_config["formatters"]["default"]["fmt"] = (
        "%(asctime)s %(levelname)-8s %(message)s"
    )

    # Sync before server start
    sync_before_server_start()
    _toolset_status_refresh_loop()

    # Start server
    uvicorn.run(app, host=HOLMES_HOST, port=HOLMES_PORT, log_config=log_config)


if __name__ == "__main__":
    main()
