# Kepler + Orion + MIG (Platform-First Interop)

This example package treats Orion + MIG as the control-plane foundation and exposes **Kepler** as a provider (`provider_id=kepler`) on the same contract as partner AI-SRE vendors.

## What this includes

- [`canis-values.yaml`](./canis-values.yaml): Helm values to run Canis with Kepler-facing MIG patterns
- [`toolsets-cli.yaml`](./toolsets-cli.yaml): CLI profile for reading Orion + MIG data paths
- [`mig-cli.sh`](./mig-cli.sh): Terminal helper for MIG and agent lifecycle APIs
- [`orion-mig-contract.yaml`](./orion-mig-contract.yaml): Machine-readable partner contract

## Locked naming

- Provider ID: `kepler`
- Display name: `Kepler`
- Event prefix: `agent.kepler.*`
- Deprecated product naming: do not use `native_holmes` or `holmes-native`

## Contract surface

MIG partner APIs:

- `POST /v1/mig/providers/{provider_id}/evaluate`
- `GET /v1/mig/providers/{provider_id}/plans/{plan_id}`
- `POST /v1/mig/providers/{provider_id}/plans/{plan_id}/explain`
- `GET /v1/mig/capabilities`
- `POST /v1/mig/conformance/run`
- `GET /v1/mig/mcp/tools`
- `POST /v1/mig/mcp/tools/{tool_name}/invoke`

Orion agent APIs:

- `POST /v1/agent/evaluations`
- `GET /v1/agent/evaluations/{id}`
- `POST /v1/agent/evaluations/{id}/simulate`
- `POST /v1/agent/plans/{id}/approve`
- `POST /v1/agent/plans/{id}/reject`
- `POST /v1/agent/plans/{id}/execute`
- `GET /v1/agent/plans/{id}/timeline`
- `POST /v1/agent/shapes/{candidate_id}/promote`

Core Orion reads (for context ingestion):

- `GET /v1/mig/status`
- `GET /v1/orion/entities`
- `GET /v1/orion/edges`
- `GET /v1/dkm/overview?scope=...`
- `GET /v1/dkm/timeline?scope=...&limit=...`
- `GET /v1/incidents`

## 1) Create secrets

```bash
export ANTHROPIC_API_KEY="YOUR_ANTHROPIC_KEY"
export ORION_TOKEN="${ORION_TOKEN:-orion-dev-admin}"

kubectl create secret generic canis-mig-secrets \
  -n orion-platform \
  --from-literal=anthropic-api-key="${ANTHROPIC_API_KEY}" \
  --from-literal=orion-token="${ORION_TOKEN}" \
  --dry-run=client -o yaml | kubectl apply -f -
```

## 2) Deploy

```bash
helm repo add robusta https://robusta-charts.storage.googleapis.com
helm repo update

helm upgrade --install canis-mig robusta/canis \
  -n orion-platform \
  --create-namespace \
  -f examples/orion_mig/canis-values.yaml
```

## 3) Validate service and run Kepler evaluation

```bash
kubectl rollout status deploy/canis-mig-canis -n orion-platform
kubectl port-forward svc/canis-mig-canis 18080:80 -n orion-platform
```

```bash
curl -sS -X POST http://localhost:18080/v1/mig/providers/kepler/evaluate \
  -H "Content-Type: application/json" \
  -d '{
    "scope":"system:all",
    "question":"Map high-risk services and propose a guarded remediation plan",
    "trigger":{"type":"dkm_hotspot","severity":"high"},
    "context":{"tenant_id":"dev","service":"checkout"}
  }'
```

## 4) Run full lifecycle from CLI helper

```bash
chmod +x examples/orion_mig/mig-cli.sh

examples/orion_mig/mig-cli.sh capabilities
examples/orion_mig/mig-cli.sh evaluate kepler '{"scope":"system:all","trigger":{"type":"slo_burn","severity":"medium"}}'
examples/orion_mig/mig-cli.sh agent-evaluate '{"provider_id":"kepler","workload":"finops","scope":"org:all","trigger":{"type":"cost_pressure","severity":"high"},"context":{"dimension":"margin"}}'
```

## V1 safety posture

- Partners and Kepler are planner-only in v1 (read + propose).
- Execution authority remains in Orion control plane.
- Approval channel is Orion UI.
- `agent.kepler.*` timeline events are emitted for auditability and MCP/NATS fan-out.

## Optional NATS sensor bus

Enable NATS publishing from Kepler lifecycle events:

```bash
export KEPLER_NATS_ENABLED=true
export KEPLER_NATS_URL="nats://nats.orion-platform.svc.cluster.local:4222"
export KEPLER_NATS_SUBJECT_PREFIX=""
```

Optional auth variables:

- `KEPLER_NATS_TOKEN`
- `KEPLER_NATS_USER`
- `KEPLER_NATS_PASSWORD`
