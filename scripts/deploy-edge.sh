#!/usr/bin/env bash
set -euo pipefail
################################################################################
# deploy-edge.sh — Deploy Dunkin Voice Chat Assistant to Azure Local (AKS Arc)
#
# Prerequisites:
#   - Azure CLI with extensions: aks-preview, connectedk8s, k8s-extension
#   - kubectl configured for your AKS Arc cluster
#   - Docker (for building the container image)
#   - .env file populated from .env.template
#
# Usage:
#   chmod +x scripts/deploy-edge.sh
#   ./scripts/deploy-edge.sh [--skip-build] [--skip-keyvault] [--skip-deploy]
################################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${ROOT_DIR}/.env"

# Parse flags
SKIP_BUILD=false
SKIP_KEYVAULT=false
SKIP_DEPLOY=false
for arg in "$@"; do
    case $arg in
        --skip-build)    SKIP_BUILD=true ;;
        --skip-keyvault) SKIP_KEYVAULT=true ;;
        --skip-deploy)   SKIP_DEPLOY=true ;;
        --help|-h)
            echo "Usage: $0 [--skip-build] [--skip-keyvault] [--skip-deploy]"
            exit 0 ;;
    esac
done

# --------------------------------------------------------------------------- #
# Load environment
# --------------------------------------------------------------------------- #
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: .env file not found. Copy .env.template to .env and fill in values."
    exit 1
fi
set -a
source "$ENV_FILE"
set +a

# Validate required variables
REQUIRED_VARS=(
    AZURE_SUBSCRIPTION_ID AZURE_RESOURCE_GROUP AZURE_LOCATION
    AZURE_KEYVAULT_NAME AZURE_ACR_NAME
    AZURE_OPENAI_EASTUS2_ENDPOINT AZURE_OPENAI_EASTUS2_API_KEY
)
for var in "${REQUIRED_VARS[@]}"; do
    if [[ -z "${!var:-}" ]]; then
        echo "ERROR: Required variable $var is not set in .env"
        exit 1
    fi
done

# Defaults
AZURE_KEYVAULT_RG="${AZURE_KEYVAULT_RG:-$AZURE_RESOURCE_GROUP}"
AZURE_KEYVAULT_SKU="${AZURE_KEYVAULT_SKU:-standard}"
AZURE_ACR_RG="${AZURE_ACR_RG:-$AZURE_RESOURCE_GROUP}"
AZURE_ACR_SKU="${AZURE_ACR_SKU:-Standard}"
DOCKER_IMAGE_NAME="${DOCKER_IMAGE_NAME:-dunkin-voice-assistant}"
DOCKER_IMAGE_TAG="${DOCKER_IMAGE_TAG:-latest}"
K8S_NAMESPACE="${K8S_NAMESPACE:-dunkin-voice}"

echo "============================================================"
echo "Dunkin Voice Chat Assistant — Azure Local Edge Deployment"
echo "============================================================"
echo "Subscription:  $AZURE_SUBSCRIPTION_ID"
echo "Resource Group: $AZURE_RESOURCE_GROUP"
echo "Location:       $AZURE_LOCATION"
echo "Key Vault:      $AZURE_KEYVAULT_NAME"
echo "ACR:            $AZURE_ACR_NAME"
echo "Image:          $DOCKER_IMAGE_NAME:$DOCKER_IMAGE_TAG"
echo "K8s Namespace:  $K8S_NAMESPACE"
echo "============================================================"

az account set --subscription "$AZURE_SUBSCRIPTION_ID"

# --------------------------------------------------------------------------- #
# Step 1: Ensure Resource Group exists
# --------------------------------------------------------------------------- #
echo ""
echo ">>> Step 1: Ensuring resource group '$AZURE_RESOURCE_GROUP' exists..."
az group create \
    --name "$AZURE_RESOURCE_GROUP" \
    --location "$AZURE_LOCATION" \
    --output none 2>/dev/null || true

# --------------------------------------------------------------------------- #
# Step 2: Create / configure Azure Key Vault
# --------------------------------------------------------------------------- #
if [[ "$SKIP_KEYVAULT" == "false" ]]; then
    echo ""
    echo ">>> Step 2: Creating Key Vault '$AZURE_KEYVAULT_NAME'..."
    az keyvault create \
        --name "$AZURE_KEYVAULT_NAME" \
        --resource-group "$AZURE_KEYVAULT_RG" \
        --location "$AZURE_LOCATION" \
        --sku "$AZURE_KEYVAULT_SKU" \
        --enable-rbac-authorization true \
        --output none 2>/dev/null || echo "   Key Vault already exists or creation skipped."

    echo "   Storing secrets in Key Vault..."
    az keyvault secret set \
        --vault-name "$AZURE_KEYVAULT_NAME" \
        --name "azure-openai-endpoint" \
        --value "$AZURE_OPENAI_EASTUS2_ENDPOINT" \
        --output none

    az keyvault secret set \
        --vault-name "$AZURE_KEYVAULT_NAME" \
        --name "azure-openai-api-key" \
        --value "$AZURE_OPENAI_EASTUS2_API_KEY" \
        --output none

    echo "   ✓ Secrets stored in Key Vault."
else
    echo ""
    echo ">>> Step 2: Skipping Key Vault setup (--skip-keyvault)"
fi

# --------------------------------------------------------------------------- #
# Step 3: Create / configure Azure Container Registry
# --------------------------------------------------------------------------- #
if [[ "$SKIP_BUILD" == "false" ]]; then
    echo ""
    echo ">>> Step 3: Ensuring ACR '$AZURE_ACR_NAME' exists..."
    az acr create \
        --name "$AZURE_ACR_NAME" \
        --resource-group "$AZURE_ACR_RG" \
        --sku "$AZURE_ACR_SKU" \
        --output none 2>/dev/null || echo "   ACR already exists."

    echo "   Logging in to ACR..."
    az acr login --name "$AZURE_ACR_NAME"

    echo "   Building and pushing Docker image..."
    FULL_IMAGE="${AZURE_ACR_NAME}.azurecr.io/${DOCKER_IMAGE_NAME}:${DOCKER_IMAGE_TAG}"
    docker build -t "$FULL_IMAGE" -f "$ROOT_DIR/app/Dockerfile" "$ROOT_DIR/app"
    docker push "$FULL_IMAGE"
    echo "   ✓ Image pushed: $FULL_IMAGE"
else
    echo ""
    echo ">>> Step 3: Skipping build (--skip-build)"
    FULL_IMAGE="${AZURE_ACR_NAME}.azurecr.io/${DOCKER_IMAGE_NAME}:${DOCKER_IMAGE_TAG}"
fi

# --------------------------------------------------------------------------- #
# Step 4: Deploy to AKS Arc (Kubernetes)
# --------------------------------------------------------------------------- #
if [[ "$SKIP_DEPLOY" == "false" ]]; then
    echo ""
    echo ">>> Step 4: Deploying to Kubernetes..."

    # Create namespace
    kubectl create namespace "$K8S_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

    # Apply configmap
    kubectl apply -f "$ROOT_DIR/k8s/configmap.yaml"

    # Template and apply SecretProviderClass (substitute env vars)
    envsubst < "$ROOT_DIR/k8s/secret-provider-class.yaml" | kubectl apply -f -

    # Template and apply deployment (substitute image references)
    envsubst < "$ROOT_DIR/k8s/deployment.yaml" | kubectl apply -f -

    echo ""
    echo "   Waiting for deployment rollout..."
    kubectl rollout status deployment/dunkin-voice-assistant -n "$K8S_NAMESPACE" --timeout=120s

    echo ""
    echo "   ✓ Deployment complete!"
    echo ""
    echo "   Service endpoint:"
    kubectl get svc dunkin-voice-service -n "$K8S_NAMESPACE" -o wide
else
    echo ""
    echo ">>> Step 4: Skipping deploy (--skip-deploy)"
fi

echo ""
echo "============================================================"
echo "Done! Next steps:"
echo "  1. If Key Vault CSI driver is not installed, run:"
echo "     az k8s-extension create --cluster-name \$AKS_CLUSTER_NAME \\"
echo "       --resource-group \$AZURE_RESOURCE_GROUP \\"
echo "       --cluster-type connectedClusters \\"
echo "       --extension-type Microsoft.AzureKeyVaultSecretsProvider \\"
echo "       --name akvsecretsprovider"
echo ""
echo "  2. Grant the AKS managed identity 'Key Vault Secrets User' role:"
echo "     az role assignment create --role 'Key Vault Secrets User' \\"
echo "       --assignee <managed-identity-client-id> \\"
echo "       --scope /subscriptions/\$AZURE_SUBSCRIPTION_ID/resourceGroups/\$AZURE_KEYVAULT_RG/providers/Microsoft.KeyVault/vaults/\$AZURE_KEYVAULT_NAME"
echo ""
echo "  3. Access the app at the LoadBalancer IP shown above."
echo "============================================================"
