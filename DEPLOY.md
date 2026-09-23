# Deployment Instructions

## What The Deployment Script Does

1. Loads environment variables from the specified .env file
2. Creates or updates Azure resources:
   - Resource Group
   - Azure Container Registry
   - App Service Plan
   - Web App
   - Application Insights
   - Log Analytics Workspace
3. Builds the Docker image locally using the specified Dockerfile and context
4. Pushes the image to Azure Container Registry
5. Configures the Web App with all environment variables

## Build and Test Locally with Docker

From the root directory, execute the following commands:

```bash
docker build -t coffee-chat-app -f ./app/Dockerfile ./app
docker run -p 8000:8000 --env-file ./app/backend/.env coffee-chat-app:latest
```

## Deploy the Application

After testing locally, deploy the application with:

```bash
./scripts/deploy.sh \
    --env-file ./app/backend/.env \
    --dockerfile ./app/Dockerfile \
    --context ./app \
    coffee-chat-assistant
```

## Deploy with azd

The recommended deployment method uses Azure Developer CLI:

```bash
azd up
```

This provisions all infrastructure (Container Apps, AI Search, OpenAI, Storage)
and deploys the application. The `postprovision` hook automatically sets up the
search index with menu embeddings.

### Post-deploy realtime smoke check

After `azd deploy` / `azd up`, the `postdeploy` hook runs `scripts/smoke_realtime.py`. The script
builds the exact `session.update` payloads the backend sends: the bootstrap, a relayed browser update
and the minimal fallback. It uses the app's own prompt, tool schemas and `config.yaml`. It sends them
to the live realtime deployment and checks that each comes back as `session.updated` with all three
tools (`search`, `update_order`, `get_order`) and `tool_choice: auto`. It also checks that guest
speech is transcribed with the configured transcription model.

The hook **never fails a deployment**. It is non-interactive and `continueOnError`, and the wrapper
always exits 0. It prints a loud warning instead. Right after a first provision, the OpenAI role
assignment can take a few minutes to apply, so "could not run" is expected then. Rerun it by hand:

```bash
python scripts/smoke_realtime.py                     # azd env values
python scripts/smoke_realtime.py --deployment gpt-realtime-1.5 --skip-transcription
azd env set DUNKIN_SKIP_REALTIME_SMOKE true          # turn the hook off
```

Exit codes: `0` passed, `1` a check failed (the model would run without its tools), `2` could not run.

## EasyAuth (Entra ID Authentication) — Optional

The template supports opt-in Entra ID authentication via Container Apps EasyAuth.
When enabled, only users in your tenant (or assigned app roles) can access the app.

### Prerequisites

1. Register an App Registration in your Entra ID tenant
2. Optionally set `appRoleAssignmentRequired = true` on the service principal to
   restrict access to named individuals (without this, any tenant member can
   sign in). **Warning:** enabling this disables user self-consent, so the first
   sign-in fails with "Need admin approval" until someone holding Application
   Administrator, Cloud Application Administrator or Global Administrator runs
   `az ad app permission admin-consent --id <app-id>`. Global *Reader* is not
   sufficient. If you have no admin account to hand, leave this off — the app is
   single-tenant, so sign-in is still limited to your own tenant.
3. Create a client secret and note the value

### Enable via azd env

```bash
azd env set AZURE_AUTH_ENABLED true
azd env set AZURE_AUTH_CLIENT_ID <YOUR-APP-CLIENT-ID>
azd env set AZURE_AUTH_TENANT_ID <YOUR-TENANT-ID>
azd env set AZURE_AUTH_CLIENT_SECRET <YOUR-CLIENT-SECRET>
```

Then run `azd up` to deploy with auth enabled.

### How it works

- `enableAuth` and `authClientId` parameters gate the auth module — both must be
  truthy for the `container-app-auth.bicep` module to deploy.
- The client secret is stored as a Container App secret named `aad-client-secret`
  and referenced by `clientSecretSettingName`. **No secret value appears in any
  template or source file.**
- If you redeploy with auth unset, ensure the `authClientSecret` parameter is
  still populated (via `azd env`) so the secret isn't deleted from the container
  app, which would break auth while it still appears enabled.

### Disabling auth

To disable, clear the variables and redeploy:

```bash
azd env set AZURE_AUTH_ENABLED false
azd up
```

## Semantic Ranker (Free SKU Note)

When deploying with `AZURE_SEARCH_SERVICE_SKU=free`, the free tier has **no
semantic ranker**. The Bicep template automatically detects this and sets
`AZURE_SEARCH_SEMANTIC_RANKER=disabled` in the container app environment.
The application code falls back to keyword + vector search when the ranker
is disabled, so menu lookups continue to work.

