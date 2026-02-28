#!/usr/bin/env bash
set -euo pipefail

READ_BASE_URL="${OBSERVATORY_BASE_URL:-http://localhost:30090}"
AGENT_BASE_URL="${KEPLER_BASE_URL:-http://localhost:18080}"
TOKEN="${OBSERVATORY_TOKEN:-observatory-dev-admin}"

usage() {
  cat <<'EOF'
Usage:
  mig-cli.sh status
  mig-cli.sh entities
  mig-cli.sh edges
  mig-cli.sh incidents
  mig-cli.sh dkm-overview [scope]
  mig-cli.sh dkm-timeline [scope] [limit]
  mig-cli.sh call-tool <tool_name> [json_payload]
  mig-cli.sh capabilities
  mig-cli.sh mcp-tools
  mig-cli.sh mcp-invoke <tool_name> [json_payload]
  mig-cli.sh conformance [provider_id]
  mig-cli.sh evaluate [provider_id] [json_payload]
  mig-cli.sh plan <provider_id> <plan_id>
  mig-cli.sh explain <provider_id> <plan_id> [json_payload]
  mig-cli.sh agent-evaluate [json_payload]
  mig-cli.sh agent-get <evaluation_id>
  mig-cli.sh agent-simulate <evaluation_id> [json_payload]
  mig-cli.sh approve <plan_id> [reason]
  mig-cli.sh reject <plan_id> [reason]
  mig-cli.sh execute <plan_id> [dry_run:true|false]
  mig-cli.sh timeline <plan_id>

Environment:
  OBSERVATORY_BASE_URL   Read APIs base URL (default: http://localhost:30090)
  KEPLER_BASE_URL        Kepler/MIG APIs base URL (default: http://localhost:18080)
  OBSERVATORY_TOKEN      Bearer token (default: observatory-dev-admin)

Examples:
  mig-cli.sh status
  mig-cli.sh capabilities
  mig-cli.sh evaluate kepler '{"scope":"system:all","trigger":{"type":"slo_burn","severity":"high"}}'
EOF
}

request() {
  local base_url="$1"
  local method="$2"
  local path="$3"
  local body="${4:-}"

  if [[ -n "${body}" ]]; then
    curl -sS -X "${method}" "${base_url}${path}" \
      -H "Authorization: Bearer ${TOKEN}" \
      -H "Content-Type: application/json" \
      --data "${body}"
  else
    curl -sS -X "${method}" "${base_url}${path}" \
      -H "Authorization: Bearer ${TOKEN}" \
      -H "Content-Type: application/json"
  fi
}

pretty_print() {
  if command -v jq >/dev/null 2>&1; then
    jq .
  else
    cat
  fi
}

cmd="${1:-help}"
shift || true

case "${cmd}" in
  status)
    request "${READ_BASE_URL}" GET "/v1/mig/status" | pretty_print
    ;;
  entities)
    request "${READ_BASE_URL}" GET "/v1/observatory/entities" | pretty_print
    ;;
  edges)
    request "${READ_BASE_URL}" GET "/v1/observatory/edges" | pretty_print
    ;;
  incidents)
    request "${READ_BASE_URL}" GET "/v1/incidents" | pretty_print
    ;;
  dkm-overview)
    scope="${1:-system:all}"
    request "${READ_BASE_URL}" GET "/v1/dkm/overview?scope=${scope}" | pretty_print
    ;;
  dkm-timeline)
    scope="${1:-system:all}"
    limit="${2:-120}"
    request "${READ_BASE_URL}" GET "/v1/dkm/timeline?scope=${scope}&limit=${limit}" | pretty_print
    ;;
  call-tool)
    tool="${1:-}"
    payload="${2:-{}}"
    if [[ -z "${tool}" ]]; then
      echo "error: tool_name is required" >&2
      usage
      exit 1
    fi
    request "${READ_BASE_URL}" POST "/v1/mig/tools/${tool}" "${payload}" | pretty_print
    ;;
  capabilities)
    request "${AGENT_BASE_URL}" GET "/v1/mig/capabilities" | pretty_print
    ;;
  mcp-tools)
    request "${AGENT_BASE_URL}" GET "/v1/mig/mcp/tools" | pretty_print
    ;;
  mcp-invoke)
    tool_name="${1:-}"
    payload="${2:-{\"provider_id\":\"kepler\",\"arguments\":{}}}"
    if [[ -z "${tool_name}" ]]; then
      echo "error: tool_name is required" >&2
      usage
      exit 1
    fi
    request "${AGENT_BASE_URL}" POST "/v1/mig/mcp/tools/${tool_name}/invoke" "${payload}" | pretty_print
    ;;
  conformance)
    provider="${1:-kepler}"
    request "${AGENT_BASE_URL}" POST "/v1/mig/conformance/run" "{\"provider_id\":\"${provider}\"}" | pretty_print
    ;;
  evaluate)
    provider="${1:-kepler}"
    payload="${2:-{\"scope\":\"system:all\",\"trigger\":{\"type\":\"manual\"}}}"
    request "${AGENT_BASE_URL}" POST "/v1/mig/providers/${provider}/evaluate" "${payload}" | pretty_print
    ;;
  plan)
    provider="${1:-}"
    plan_id="${2:-}"
    if [[ -z "${provider}" || -z "${plan_id}" ]]; then
      echo "error: provider_id and plan_id are required" >&2
      usage
      exit 1
    fi
    request "${AGENT_BASE_URL}" GET "/v1/mig/providers/${provider}/plans/${plan_id}" | pretty_print
    ;;
  explain)
    provider="${1:-}"
    plan_id="${2:-}"
    payload="${3:-{\"focus\":\"overall_plan\",\"max_evidence\":5}}"
    if [[ -z "${provider}" || -z "${plan_id}" ]]; then
      echo "error: provider_id and plan_id are required" >&2
      usage
      exit 1
    fi
    request "${AGENT_BASE_URL}" POST "/v1/mig/providers/${provider}/plans/${plan_id}/explain" "${payload}" | pretty_print
    ;;
  agent-evaluate)
    payload="${1:-{\"provider_id\":\"kepler\",\"scope\":\"system:all\",\"trigger\":{\"type\":\"manual\"}}}"
    request "${AGENT_BASE_URL}" POST "/v1/agent/evaluations" "${payload}" | pretty_print
    ;;
  agent-get)
    evaluation_id="${1:-}"
    if [[ -z "${evaluation_id}" ]]; then
      echo "error: evaluation_id is required" >&2
      usage
      exit 1
    fi
    request "${AGENT_BASE_URL}" GET "/v1/agent/evaluations/${evaluation_id}" | pretty_print
    ;;
  agent-simulate)
    evaluation_id="${1:-}"
    payload="${2:-{\"freeze_window_active\":false,\"stale_evidence_after_seconds\":900}}"
    if [[ -z "${evaluation_id}" ]]; then
      echo "error: evaluation_id is required" >&2
      usage
      exit 1
    fi
    request "${AGENT_BASE_URL}" POST "/v1/agent/evaluations/${evaluation_id}/simulate" "${payload}" | pretty_print
    ;;
  approve)
    plan_id="${1:-}"
    reason="${2:-approved via cli}"
    if [[ -z "${plan_id}" ]]; then
      echo "error: plan_id is required" >&2
      usage
      exit 1
    fi
    request "${AGENT_BASE_URL}" POST "/v1/agent/plans/${plan_id}/approve" "{\"reason\":\"${reason}\"}" | pretty_print
    ;;
  reject)
    plan_id="${1:-}"
    reason="${2:-rejected via cli}"
    if [[ -z "${plan_id}" ]]; then
      echo "error: plan_id is required" >&2
      usage
      exit 1
    fi
    request "${AGENT_BASE_URL}" POST "/v1/agent/plans/${plan_id}/reject" "{\"reason\":\"${reason}\"}" | pretty_print
    ;;
  execute)
    plan_id="${1:-}"
    dry_run="${2:-true}"
    if [[ -z "${plan_id}" ]]; then
      echo "error: plan_id is required" >&2
      usage
      exit 1
    fi
    request "${AGENT_BASE_URL}" POST "/v1/agent/plans/${plan_id}/execute" "{\"dry_run\":${dry_run}}" | pretty_print
    ;;
  timeline)
    plan_id="${1:-}"
    if [[ -z "${plan_id}" ]]; then
      echo "error: plan_id is required" >&2
      usage
      exit 1
    fi
    request "${AGENT_BASE_URL}" GET "/v1/agent/plans/${plan_id}/timeline" | pretty_print
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "error: unknown command '${cmd}'" >&2
    usage
    exit 1
    ;;
esac
