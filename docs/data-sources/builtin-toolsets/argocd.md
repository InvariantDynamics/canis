# ArgoCD

By enabling this toolset, Canis will be able to fetch the status, deployment history, and configuration of ArgoCD applications.

![Canis ArgoCD Demo](../../assets/Holmes_ArgoCD_demo.gif)

## Prerequisites

### Generating an ArgoCD token
This toolset requires an `ARGOCD_AUTH_TOKEN` environment variable. Generate an auth token by following [these steps](https://argo-cd.readthedocs.io/en/stable/user-guide/commands/argocd_account_generate-token/).

### Adding a Read-only Policy to ArgoCD
Canis requires specific permissions to access ArgoCD data. Add the permissions below to your ArgoCD RBAC configuration.

Edit the RBAC ConfigMap: `kubectl edit configmap argocd-rbac-cm -n argocd`

```yaml
# Add this to the data section of your argocd-rbac-cm configmap.
# Creates a 'canis' user with read-only permissions for troubleshooting.
data:
  policy.default: role:readonly
  policy.csv: |
    p, role:admin, *, *, *, allow
    p, role:admin, accounts, apiKey, *, allow
    p, canis, accounts, apiKey, canis, allow
    p, canis, projects, get, *, allow
    p, canis, applications, get, *, allow
    p, canis, repositories, get, *, allow
    p, canis, clusters, get, *, allow
    p, canis, applications, manifests, */*, allow
    p, canis, applications, resources, */*, allow
    g, admin, role:admin
```

## Configuration

In addition to setting permissions and generating an auth token, you will need to tell Canis how to connect to the server. This can be done two ways:

1. **Using port forwarding**. This is the recommended approach if your ArgoCD is inside your Kubernetes cluster.
2. **Setting the env var** `ARGOCD_SERVER`. This is the recommended approach if your ArgoCD is reachable through a public DNS.

### 1. Port Forwarding

This is the recommended approach if your ArgoCD is inside your Kubernetes cluster.

Canis needs permission to establish a port-forward to ArgoCD. The configuration below includes that authorization.

=== "Canis CLI"

    Set the following environment variables:

    ```bash
    export ARGOCD_AUTH_TOKEN="<your-argocd-token>"
    export ARGOCD_OPTS="--port-forward --port-forward-namespace <your_argocd_namespace> --server <your_server_address> --grpc-web"
    ```

    Then add the following to **~/.holmes/config.yaml**:

    ```yaml
    toolsets:
        argocd/core:
            enabled: true
    ```

    --8<-- "snippets/toolset_refresh_warning.md"

=== "Robusta Helm Chart"

    ```yaml
    holmes:
        customClusterRoleRules:
            - apiGroups: [""]
              resources: ["pods/portforward"]
              verbs: ["create"]
        additionalEnvVars:
            - name: ARGOCD_AUTH_TOKEN
              value: "<your-argocd-token>"
            - name: ARGOCD_OPTS
              value: "--port-forward --port-forward-namespace <your_argocd_namespace> --server <your_server_address> --grpc-web"
        toolsets:
            argocd/core:
                enabled: true
    ```

    --8<-- "snippets/helm_upgrade_command.md"

!!! note

    For in-cluster address, use the cluster DNS. For example: `--port-forward --port-forward-namespace argocd --server argocd-server.argocd.svc.cluster.local --insecure --grpc-web`

    - Add `--insecure` to work with self-signed certificates
    - Change the namespace `--port-forward-namespace <your_argocd_namespace>` to the namespace in which your ArgoCD service is deployed
    - The option `--grpc-web` in `ARGOCD_OPTS` prevents some connection errors from leaking into the tool responses and provides a cleaner output for Canis

### 2. Server URL

This is the recommended approach if your ArgoCD is reachable through a public DNS.

=== "Canis CLI"

    Set the following environment variables:

    ```bash
    export ARGOCD_AUTH_TOKEN="<your-argocd-token>"
    export ARGOCD_SERVER="argocd.example.com"
    ```

    Then add the following to **~/.holmes/config.yaml**:

    ```yaml
    toolsets:
        argocd/core:
            enabled: true
    ```

    --8<-- "snippets/toolset_refresh_warning.md"

    To test, run:

    ```bash
    canis ask "Which ArgoCD applications are failing and why?"
    ```

=== "Robusta Helm Chart"

    ```yaml
    holmes:
        additionalEnvVars:
            - name: ARGOCD_AUTH_TOKEN
              value: "<your-argocd-token>"
            - name: ARGOCD_SERVER
              value: "argocd.example.com"
        toolsets:
            argocd/core:
                enabled: true
    ```

    --8<-- "snippets/helm_upgrade_command.md"

!!! note

    In production, always use a Kubernetes secret instead of hardcoding the token value in your Helm values.

## Capabilities

--8<-- "snippets/toolset_capabilities_intro.md"

| Tool Name | Description |
|-----------|-------------|
| argocd_app_list | List the applications in ArgoCD |
| argocd_app_get | Retrieve information about an existing application, such as its status and configuration |
| argocd_app_manifests | Retrieve manifests for an application |
| argocd_app_resources | List resources of an application |
| argocd_app_diff | Display the differences between the current state of an application and the desired state specified in its Git repository |
| argocd_app_history | List the deployment history of an application in ArgoCD |
| argocd_repo_list | List all the Git repositories that ArgoCD is currently managing |
| argocd_proj_list | List all available projects |
| argocd_proj_get | Retrieve information about an existing project, such as its applications and policies |
| argocd_cluster_list | List all known clusters |
