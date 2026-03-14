# KubeVela

This toolset provides access to KubeVela CLI commands for managing and troubleshooting applications built on the Open Application Model (OAM).

## Prerequisites

The KubeVela CLI (`vela`) must be installed and configured to access your cluster.

**Installation:**

```bash
# Install vela CLI
curl -fsSl https://kubevela.io/script/install.sh | bash

# Verify installation
vela version
```

## Configuration

=== "Canis CLI"

    Add the following to **~/.holmes/config.yaml**:

    <!-- markdownlint-disable-next-line MD046 -->
    ```yaml
    toolsets:
        kubevela/core:
            enabled: true
    ```

    --8<-- "snippets/toolset_refresh_warning.md"

    To test, run:

    ```bash
    canis ask "What is the status of my KubeVela applications?"
    ```

## Common Use Cases

```bash
canis ask "What KubeVela applications are unhealthy and why?"
```

```bash
canis ask "Show me the workflow status for my payment-service application"
```

```bash
canis ask "What components does my frontend application have and are they running correctly?"
```

```bash
canis ask "Check if there are any trait configuration issues in the user-api application"
```
