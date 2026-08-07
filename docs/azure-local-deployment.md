# Azure Local Edge Deployment

This guide covers deploying the Dunkin Voice Chat Assistant to an **Azure Local** (formerly Azure Stack HCI) cluster running AKS Arc, using Kubernetes manifests and Flux GitOps. This is an alternative to the default [Azure Container Apps deployment](../README.md#deploying-to-azure) — both paths coexist and share the same application code.

## When to Choose Each Deployment Path

| | **Azure Container Apps** | **Azure Local (Edge)** |
|---|---|---|
| **Best for** | Cloud-first, rapid iteration, managed infra | On-prem, low-latency, data-sovereignty requirements |
| **Runs on** | Azure-managed platform | Your own AKS Arc cluster on Azure Local hardware |
| **GPU workloads** | Not applicable (uses Azure OpenAI) | Optional — enables on-prem model serving (Phi-4 Mini, Whisper, Piper TTS) |
| **GitOps** | `azd up` | Flux CD reconciles from your Git repo |
| **Networking** | Azure-managed FQDN + TLS | You manage ingress, TLS certs, DNS |
| **Scaling** | Auto-scale built in | Manual replica configuration |

Choose Azure Local when you need the assistant running at the edge (e.g., in-store at a franchise location) with low-latency audio, data locality, or offline-capable model serving.

## Prerequisites

- **Azure Local cluster** with AKS Arc provisioned and `kubectl` configured
- **Flux CD** bootstrapped on the cluster (v2)
- **Azure Container Registry (ACR)** — for hosting the app container image
- **Azure Key Vault** — for storing OpenAI API keys securely
- **Azure Key Vault Provider for Secrets Store CSI Driver** installed on the cluster
- **cert-manager** — installed via Flux (included in `flux/infrastructure/`)
- **NGINX Ingress Controller** — for TLS termination and WebSocket proxying
- **DNS record** pointing your chosen domain at the cluster's ingress IP
- **Azure OpenAI** resource with a `gpt-4o-realtime-preview` deployment
- **(Optional) GPU node(s)** — required only for on-prem model serving; see [GPU Requirements](#gpu-requirements)

## Repository Layout

```
flux/
├── apps/
│   ├── dunkin-voice/          # App deployment, service, ingress, configmap, netpol
│   ├── foundry-models/        # Phi-4 Mini GPU model deployment (requires GPU)
│   ├── piper-tts/             # On-prem text-to-speech service
│   ├── whisper-stt/           # On-prem speech-to-text service
│   └── kustomization.yaml     # Controls which components are enabled
├── clusters/
│   └── example-site/          # Per-site Flux source + kustomization (copy per location)
└── infrastructure/
    ├── cert-manager/           # HelmRelease
    ├── foundry-operator/       # HelmRelease (disabled by default — see note)
    ├── nvidia-device-plugin/   # DaemonSet for GPU nodes
    ├── sources/                # Helm repository definitions
    ├── trust-manager/          # HelmRelease
    ├── namespaces.yaml
    └── kustomization.yaml

k8s/                           # Standalone manifests for script-based deploy
scripts/
├── deploy-edge.ps1            # PowerShell deployment script
├── deploy-edge.sh             # Bash deployment script
├── smoke_test.sh              # Post-deploy smoke tests
└── resilience_test.sh         # Deployment resilience validation
```

## Placeholder Values

Every operator-specific value has been replaced with a placeholder. Search for `<your-` or `PLACEHOLDER` in the manifests. You **must** replace all of these before deploying.

| Placeholder | Where | What to Set | Example |
|---|---|---|---|
| `<your-acr>` | `flux/apps/dunkin-voice/deployment.yaml` | Your ACR login server name (without `.azurecr.io`) | `cadunkinacr` |
| `<your-domain>` | `flux/apps/dunkin-voice/ingress.yaml` (×2 in hosts + rules) | FQDN pointing at your cluster ingress | `dunkin.adaptivecloudlab.com` |
| `<your-org>/<your-repo>` | `flux/clusters/example-site/flux-source.yaml` | Your GitHub org and repo for Flux to reconcile | `mgodfre3/dunkin-chat-voice-assistant` |
| `PLACEHOLDER` (×3) | `flux/apps/dunkin-voice/secret-provider-class.yaml` | `userAssignedIdentityID`, `keyvaultName`, `tenantId` | (your Azure values) |
| `${AZURE_ACR_NAME}` | `k8s/deployment.yaml` | Set via `.env` file; substituted by `deploy-edge.sh` at deploy time | `cadunkinacr` |
| `${DOCKER_IMAGE_NAME}` | `k8s/deployment.yaml` | Set via `.env` file; defaults to `dunkin-voice-assistant` | `dunkin-voice-assistant` |
| `${DOCKER_IMAGE_TAG}` | `k8s/deployment.yaml` | Set via `.env` file; defaults to `latest` | `latest` |
| `${AKS_MANAGED_IDENTITY_CLIENT_ID}` | `k8s/secret-provider-class.yaml` | Managed identity client ID with Key Vault access | (GUID) |
| `${AZURE_KEYVAULT_NAME}` | `k8s/secret-provider-class.yaml` | Your Key Vault name | `mykeyvault` |
| `${AZURE_TENANT_ID}` | `k8s/secret-provider-class.yaml` | Your Entra ID tenant ID | (GUID) |

### `.env` File Variables (for `deploy-edge.sh` / `deploy-edge.ps1`)

The deploy scripts read from a `.env` file at the repo root. Required variables:

| Variable | Required | Description |
|---|---|---|
| `AZURE_SUBSCRIPTION_ID` | ✅ | Azure subscription ID |
| `AZURE_RESOURCE_GROUP` | ✅ | Resource group for ACR and Key Vault |
| `AZURE_LOCATION` | ✅ | Azure region (e.g., `eastus2`) |
| `AZURE_KEYVAULT_NAME` | ✅ | Key Vault name for secrets |
| `AZURE_ACR_NAME` | ✅ | ACR name (without `.azurecr.io`) |
| `AZURE_OPENAI_EASTUS2_ENDPOINT` | ✅ | Azure OpenAI endpoint URL |
| `AZURE_OPENAI_EASTUS2_API_KEY` | ✅ | Azure OpenAI API key |
| `DOCKER_IMAGE_NAME` | | Container image name (default: `dunkin-voice-assistant`) |
| `DOCKER_IMAGE_TAG` | | Image tag (default: `latest`) |
| `K8S_NAMESPACE` | | Kubernetes namespace (default: `dunkin-voice`) |
| `AKS_MANAGED_IDENTITY_CLIENT_ID` | | Managed identity for Key Vault CSI |
| `AZURE_TENANT_ID` | | Entra ID tenant ID |

## Deployment

### Option A: Script-Based Deploy (Recommended for First-Time Setup)

The deploy scripts handle ACR creation, image build/push, Key Vault provisioning, and Kubernetes deployment in one pass.

**Bash (Linux/macOS):**

```bash
# 1. Copy and populate .env
cp .env.template .env
# Edit .env with your values

# 2. Run the deploy script
chmod +x scripts/deploy-edge.sh
./scripts/deploy-edge.sh

# Skip individual steps if needed:
./scripts/deploy-edge.sh --skip-build --skip-keyvault
```

**PowerShell (Windows):**

```powershell
# 1. Copy and populate .env
Copy-Item .env.template .env
# Edit .env with your values

# 2. Run the deploy script
.\scripts\deploy-edge.ps1

# Skip individual steps if needed:
.\scripts\deploy-edge.ps1 -SkipBuild -SkipKeyVault
```

### Option B: Flux GitOps (Recommended for Ongoing Operations)

Once Flux is bootstrapped, it continuously reconciles the cluster state from your Git repository.

1. **Fork/clone this repo** and push to your Git hosting.

2. **Configure your site:** Copy `flux/clusters/example-site/` to a directory named for your site (e.g., `flux/clusters/store-042/`).

3. **Update `flux-source.yaml`** with your repository URL:
   ```yaml
   url: https://github.com/<your-org>/<your-repo>
   ```

4. **Update placeholders** in `flux/apps/dunkin-voice/` manifests (see table above).

5. **Enable model-serving components** by uncommenting lines in `flux/apps/kustomization.yaml` as needed.

6. **Bootstrap Flux** pointing at your site directory:
   ```bash
   flux bootstrap github \
     --owner=<your-org> \
     --repository=<your-repo> \
     --path=flux/clusters/<your-site> \
     --personal
   ```

7. Flux will reconcile infrastructure first (cert-manager, trust-manager, NVIDIA plugin), then apps.

## GPU Requirements

The edge topology optionally supports on-prem AI model serving. GPU requirements:

| Component | GPU Required? | Notes |
|---|---|---|
| **Phi-4 Mini** (`foundry-models/`) | ✅ Yes | Needs 1× NVIDIA GPU; uses Foundry Local operator |
| **NVIDIA Device Plugin** (`infrastructure/nvidia-device-plugin/`) | ✅ Yes | DaemonSet that exposes GPUs to Kubernetes; only schedules on nodes labelled `accelerator=nvidia` |
| **Whisper STT** (`whisper-stt/`) | ❌ No | Configured for CPU inference (`WHISPER__INFERENCE_DEVICE=cpu`); the CUDA container image is used for flexibility but runs in CPU mode |
| **Piper TTS** (`piper-tts/`) | ❌ No | CPU-only |
| **Dunkin Voice App** (`dunkin-voice/`) | ❌ No | Standard web workload |

**If your cluster has no GPU nodes:**
- Remove or comment out `nvidia-device-plugin/daemonset.yaml` from `flux/infrastructure/kustomization.yaml`
- Do not enable `foundry-models/phi4-mini-deployment.yaml` in `flux/apps/kustomization.yaml`
- Whisper STT and Piper TTS will still work (CPU mode)

**If your cluster has GPU nodes:**
- Label GPU nodes: `kubectl label node <node-name> accelerator=nvidia`
- The NVIDIA device plugin DaemonSet will schedule only on labelled nodes
- The Foundry Local Operator HelmRelease is currently commented out in `flux/infrastructure/kustomization.yaml` due to an upstream chart issue (duplicate label key) — install it manually or re-enable when the chart is fixed

## Verification

### Smoke Tests

After deployment, run the smoke test suite to validate all endpoints:

```bash
chmod +x scripts/smoke_test.sh
./scripts/smoke_test.sh https://<your-domain>
```

Tests cover: HTTP health checks, response content, WebSocket endpoint, API endpoints, security headers, static assets, and HTTPS.

### Resilience Tests

Validate self-healing, Flux reconciliation, resource limits, network policies, and secrets:

```bash
chmod +x scripts/resilience_test.sh
./scripts/resilience_test.sh https://<your-domain> dunkin-voice
```

This test **deletes a running pod** to verify self-healing — run it in a maintenance window or on a non-production cluster.

## Notes

- The **Foundry Local Operator HelmRelease** is commented out in `flux/infrastructure/kustomization.yaml` because the upstream chart has a YAML error (duplicate label key). Install the operator manually or re-enable when the chart is fixed.
- The **SecretProviderClass** in `flux/apps/dunkin-voice/` is commented out in the kustomization — re-enable it after installing the Secrets Store CSI Driver.
- **Model-serving manifests** (Whisper STT, Piper TTS, Phi-4 Mini) are included but commented out in `flux/apps/kustomization.yaml`. The backend code to use these local services is planned for Sprint 4. The manifests are provided now so operators can pre-stage the services.
- The `k8s/` directory contains standalone manifests for script-based deployment (used by `deploy-edge.sh`/`.ps1`). The `flux/` directory contains the GitOps-managed manifests. Both target the same application.
