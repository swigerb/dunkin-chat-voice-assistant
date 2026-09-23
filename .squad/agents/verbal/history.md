# Verbal — History

## Context

- **Project:** Dunkin Voice Chat Assistant — AI-powered drive-thru ordering experience

## Sprint 2 — Azure Local Edge Deployment Layer (2026-08-07)

### What was done
Ported 33 files from `pr-2` (Michael Godfrey's hybrid-edge branch) to `sprint/azure-local-edge`:
- **flux/** — 22 files: app manifests (dunkin-voice: deployment, service, configmap, ingress, networkpolicy, nginx-tls-config, secret-provider-class), model-serving (foundry-models/phi4-mini-deployment, piper-tts/deployment+service, whisper-stt/deployment+service), kustomization, cluster config (example-site: flux-source, kustomization), infrastructure (cert-manager, foundry-operator, trust-manager HelmReleases, nvidia-device-plugin DaemonSet, namespaces, helm-repos, kustomization)
- **k8s/** — 5 files: configmap, deployment, namespace, nginx-tls-config, secret-provider-class
- **scripts/** — 4 files: deploy-edge.ps1, deploy-edge.sh, smoke_test.sh, resilience_test.sh
- **docs/azure-local-deployment.md** — full deployment guide
- **README.md** — updated ToC + new "Deploying to Azure Local (Edge)" section

### Placeholder convention
Used `<your-xxx>` angle-bracket placeholders in YAML and `PLACEHOLDER` for the SecretProviderClass fields. Chose inline placeholders over kustomize overlays because: (a) the repo doesn't use kustomize for the existing Container Apps path, (b) inline placeholders are grep-able and self-documenting, (c) operators can sed/replace in one pass.

### Contributor values generalised
| Original | Replacement |
|---|---|
| `cadunkinacr.azurecr.io` | `<your-acr>.azurecr.io` |
| `dunkin.adaptivecloudlab.com` | `<your-domain>` |
| `https://github.com/mgodfre3/dunkin-chat-voice-assistant` | `https://github.com/<your-org>/<your-repo>` |
| `flux/clusters/california/` | `flux/clusters/example-site/` |
| `smoke_test.sh` default URL `http://dunkin.adaptivecloudlab.com` | `http://localhost:8000` |
| `resilience_test.sh` default URL `http://dunkin.adaptivecloudlab.com` | `http://localhost:8000` |

### GPU/hardware assumptions documented
- **Phi-4 Mini** requires 1× NVIDIA GPU + foundry-local-operator + nvidia-device-plugin DaemonSet (nodes must be labelled `accelerator=nvidia`)
- **Whisper STT** runs in CPU mode (`WHISPER__INFERENCE_DEVICE=cpu`) despite using CUDA container image
- **Piper TTS** is CPU-only
- Clusters without GPUs: omit nvidia-device-plugin and phi4-mini-deployment
- Foundry operator HelmRelease commented out upstream due to chart YAML error

### Validation
- ✅ 27 YAML files parsed via `yaml.safe_load_all` — all clean
- ✅ `kubectl --dry-run=client` attempted — cluster unreachable (configured kubeconfig points at offline AKS), so dry-run could not validate against API schema. YAML parse is the authoritative check.
- ✅ `grep mgodfre3|cadunkinacr|adaptivecloudlab` across flux/, k8s/, scripts/ — **zero matches** (values appear only in docs Example column)
- ✅ `bash -n` all 3 shell scripts — SYNTAX OK
- ✅ PowerShell parse check deploy-edge.ps1 — PARSE OK
- ✅ `python -m pytest app/backend -q` → **110 passed**
- ✅ `ruff check .` → All checks passed
- ✅ `npm run build` → built in 5s
- ✅ `npm test` → **13 tests passed**
- ✅ `git status --short` → only intended files

### Deferred to Sprint 4
- `app/backend/rtmt_local.py` — local voice pipeline backend code
- ChromaDB integration / local search
- Backend `USE_LOCAL_PIPELINE=true` code path
- Uncommenting model-serving resources in `flux/apps/kustomization.yaml` (manifests are present but commented; the backend code to talk to them is not yet ported)

## Sonic parity port — `feat/sonic-parity` (2026-09-23)
- Item 2: bicep deployment gpt-realtime-2.1 (2026-07-07, GlobalStandard). Optional reasoning/transcription params set container env only when non-empty.
- Item 3: marin in `main.parameters.json`, `.env-sample`, and the flux + k8s configmaps.
- Item 7: `webAppExists` read `SERVICE_WEB_RESOURCE_EXISTS`, but the service is `backend`. So every provision would have reset the image to helloworld, although the env had `SERVICE_BACKEND_RESOURCE_EXISTS=true`. Fixed; `test_azd_service_wiring.py` added.
- azd env `dunkin-demo` (local, gitignored): `AZURE_OPENAI_REALTIME_DEPLOYMENT=gpt-realtime-2.1`, `..._VOICE_CHOICE=marin`. No provision or deploy run.
- `az bicep build` clean. `role.bicep` guid is deterministic, so re-provisioning against the shared Sonic OpenAI RG is idempotent.
- **Edge follow-up:** AOAI account `acx-dunkin-edge-openai` needs its own gpt-realtime-2.1 deployment before the configmap change is rolled out.
