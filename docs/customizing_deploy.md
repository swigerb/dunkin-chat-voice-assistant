# Customizing the VoiceRAG deployment

This guide shows you how to customize the [VoiceRAG](../README.md#deploying-the-app) deployment to specify different options.
If your goal is to reuse existing services (OpenAI or Search), see the [existing services guide](./existing_services.md) instead.

## Customizing the real-time voice choice

Run this command to set the voice choice for the real-time deployment:

```bash
azd env set AZURE_OPENAI_REALTIME_VOICE_CHOICE <marin, cedar, alloy, ash, ballad, coral, echo, sage, shimmer, or verse>
```

The default voice choice is `marin`. All ten built-in gpt-realtime-2.1 voices are available; OpenAI recommends
`marin` and `cedar` for best quality. Guests can also switch voices in the settings dialog. A change made
mid-conversation (after the assistant has spoken) applies from the next conversation (next page load), because the realtime
service locks the voice once assistant audio exists.

Once you have set the voice choice, run `azd up` to apply the changes to the deployed app.
If you've already run `azd up` and want to first preview the voice with the development server, then update your local `.env` file by running `./scripts/write_env.sh` or `pwsh ./scripts/write_env.ps1`, and then restart the development server.

## Customizing the realtime model and reasoning effort

The default realtime deployment is `gpt-realtime-2.1` (version `2026-07-07`, GlobalStandard). It is a
reasoning model, so every `session.update` carries `reasoning: {effort: "low"}` from
`app/backend/config.yaml` (`model.reasoning_effort`). `low` is the tested setting: at `none`/`minimal`
the model often calls a tool before speaking, leaving the guest in silence.

| azd env / app setting | config.yaml key | Values |
| --- | --- | --- |
| `AZURE_OPENAI_REALTIME_REASONING_EFFORT` | `model.reasoning_effort` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `off` / empty to omit `reasoning` |
| `AZURE_OPENAI_REALTIME_REASONING_MODEL` | `model.reasoning_model` | `auto` (infer from the deployment name), `true`, `false` |
| `AZURE_OPENAI_REALTIME_TRANSCRIPTION_MODEL` | `model.transcription_model` | `whisper-1` (default; needs no extra deployment) or a transcription deployment name |

An unset azd value means the `config.yaml` value applies. Rolling back to `gpt-realtime-1.5` is safe
with `reasoning_model: auto`: 1.5 rejects `reasoning` and `parallel_tool_calls` (and the whole
`session.update` with them, tools included), so the backend never sends them to a 1.5 deployment. If
you use a custom deployment name for 1.5, set `AZURE_OPENAI_REALTIME_REASONING_MODEL=false`.

The deployment name is configurable: `azd env set AZURE_OPENAI_REALTIME_DEPLOYMENT <name>`. Any name that
isn't recognised as gpt-4o / gpt-realtime-1.x is treated as a reasoning model, so suffixed names such as
`gpt-realtime-2.1-dz` (a DataZoneStandard deployment of the same model) get `reasoning` without any
extra setting.

### Rejected `session.update` fallback

The realtime service rejects a `session.update` as a whole if any single field is unsupported, and it
reports this only as an `error` event. That means one bad field silently drops every tool. So every
`session.update` the backend sends carries an `event_id`. When a rejection correlates to one of them
(by `error.event_id`, or as the oldest unacknowledged update when the error has no id), the backend
sends **one** minimal update with only `instructions`, `tools` and `tool_choice`, and does not
forward the error to the browser. If the rejected update carried `reasoning` / `parallel_tool_calls`,
the backend stops sending those for the rest of the process. The browser only sees the error if the
fallback is also rejected, and there is never a second fallback. Unrelated errors pass through
unchanged. Look for `Upstream REJECTED session.update` in the logs.
