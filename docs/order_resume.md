# Order resume

A guest's order survives a short transport drop, such as a Wi-Fi blip, a load-balancer reset or a backgrounded tab.
If the browser reconnects within the hold, it gets the same session and order back. The crew member carries on
without greeting the guest again, and the crew dashboard keeps showing the same car and order card.

Two pairs of files implement the protocol below:
- backend: `app/backend/rtmt.py` and `app/backend/session_manager.py`;
- browser: `app/frontend/src/hooks/useRealtime.tsx` and `app/frontend/src/App.tsx`.

Resume applies to the **cloud realtime path** (`rtmt.py`). That includes the edge cluster in `flux/`, whose configmap
sets `USE_LOCAL_PIPELINE: "false"`. Resume does **not** apply to the fully local pipeline (`USE_LOCAL_PIPELINE=true`,
`rtmt_local.py`). That pipeline has no idle close, no resume and no dashboard publishing: a dropped socket ends its
conversation, as before.

## What the guest sees

| Situation | What happens |
| --- | --- |
| Connection drops mid-order | The mic pauses and the status line reads "Connection dropped. Reconnecting — your order is safe…". The ticket stays on screen. |
| Reconnected within the hold | "Reconnected — your order is still here." The mic restarts by itself. The crew member stays quiet until the guest speaks; after 30 s of silence it asks once whether they need anything else. |
| Browser won't restart the mic without a tap | "Reconnected — your order is still here. Tap the mic to continue." One tap carries on with the same order: no greeting, and no wait for one. |
| Guest taps while it is still reconnecting | The tap is held until the connection is back and carries on with the same order; the mic starts as soon as the order is restored. |
| Page reloaded in the same tab | The ticket comes back with "Tap the mic to continue". |
| Hold expired, or the order couldn't be restored | "We couldn't restore your order. Tap the mic to start a new one." The ticket is cleared. |
| 5 minutes with no guest activity, connected or not | "Your session ended after 5 minutes of inactivity…". No reconnect happens; the next tap starts fresh. |
| Same order resumed in another window | "This order continued in another window. Tap the mic to start again." |
| Guest presses **Start a new order** (shown once the ticket has items) | The order is ended on the server and the ticket clears. A fresh session is ready for the next tap. |

Resume is per tab (`sessionStorage`), so a new tab or window always starts a new order.

## Lifecycle

| Event | What the backend does |
| --- | --- |
| Transport close (1001/1002/1006/1011, handler exit, upstream connect failure) | The session is **detached**. Its order, transcript and resume credential are held for `min(resume.grace_seconds, remaining idle budget)`. |
| Idle close (4000 `idle_timeout`) | The session is **ended** before the socket closes, so it can't be resumed. |
| `extension.end_session` from the browser | The session is ended and the socket closes with 1000 `session_ended`. |
| Hold expires, or the `resume.max_detached` cap evicts the oldest hold | The session is ended. Checked every `security.idle_check_interval_seconds` and again at resume time. |
| Socket closes before the session was ever announced (no resume id issued) | The session is ended at once; there is nothing the browser could resume it with. |

The idle clock (`security.idle_timeout_seconds`, 300 s) runs from the guest's last activity. It **keeps running while
the guest is disconnected**, so a drop never extends the 5 minutes.

- **Guest activity:** client text frames other than `input_audio_buffer.append`, upstream
  `input_audio_buffer.speech_started`, and `conversation.item.input_audio_transcription.completed`.
- **Not guest activity:** a continuously streaming mic, the `extension.resume` frame itself, and the crew member's own
  speech (including the resume nudge).

The browser socket has no server heartbeat. A half-open socket (the old one, after the browser has already
reconnected) is closed with 4002 when the new socket resumes the session, or by the idle close.

Resume only works within one backend process. That's why the Dockerfiles run a single worker and the ingress uses
sticky affinity; see [customizing_deploy.md](customizing_deploy.md#scaling-session-affinity-and-secrets-order-resume).

## Resume credential

- It's a 256-bit `secrets.token_urlsafe(32)` string (43 chars).
- It's delivered **only over the websocket**: never in a URL, a cookie or a log. The server stores only its SHA-256,
  and logs show just `sha256(id)[:8]`.
- It's single-use. A successful resume consumes it and returns a new one; re-announcing a session also rotates it.
- It isn't bound to the signed-in user.

## Wire protocol

All messages are JSON text frames on `/realtime`.

### 1. Server → browser: `extension.session_metadata` (fresh session)

```json
{"type": "extension.session_metadata",
 "sessionToken": "…", "roundTripIndex": 0, "roundTripToken": "…",
 "resumeId": "<43-char id>"}
```

The server sends this once it has decided the connection is fresh **and** the upstream `session.created` has arrived.
The connection counts as fresh when either:
- the browser's first frame is anything other than `extension.resume`, or
- no frame arrives within `resume.first_frame_timeout_seconds` (2 s).

`resumeId` is omitted when `resume.enabled` is false.

**Browser:** store `resumeId` in `sessionStorage` under `dunkin.resumeId`.

### 2. Browser → server: `extension.resume` (must be the FIRST frame)

```json
{"type": "extension.resume", "resume_id": "<stored id>"}
```

Send it as the very first frame after `open`, before `extension.set_voice`, `session.update`, audio or anything queued.
The server honours it only as the first frame, and only before the first-frame deadline. It is never forwarded to the
model.

**Browser (as built):** `useRealtime` never uses react-use-websocket's own queue; every send is `keep=false`. While
the socket is closed, the hook keeps `extension.set_voice` and `session.update` in its own queue and drops audio and
buffer clears. `onOpen` sends `extension.resume` first (when an id is stored), then the queue. So a guest who taps
while the socket is still reconnecting still produces `extension.resume` → `extension.set_voice` →
`session.update`, in that order. The direct-AOAI mode never sends a resume frame.

### 3a. Server → browser: `extension.session_resumed` (accepted)

```json
{"type": "extension.session_resumed",
 "order_summary": {"items": [...], "total": 3.49, "tax": 0.28, "finalTotal": 3.77},
 "session_token": "…", "round_trip_index": 3, "round_trip_token": "…",
 "resume_id": "<rotated id>"}
```

`order_summary` has the same shape as an `update_order` tool result. This frame takes the place of
`extension.session_metadata`; no metadata follows.

**Browser (as built):**
1. It replaces the stored id with `resume_id`, restores the ticket and shows the identifiers.
2. If the guest was mid-conversation at the drop, the app re-sends `extension.set_voice` and `session.update`. That
   restores the voice and the browser's VAD settings; the server suppresses the greeting. The app then restarts the mic.
3. `Recorder.start()` resolves `false` if the AudioContext is still suspended after 1.5 s. The app treats that, or a
   `getUserMedia` rejection, as "needs a gesture": it releases the mic and shows "Tap the mic to continue". That tap
   starts the mic at once, because no greeting is coming.
4. If the guest wasn't talking at the drop (or after a reload), the ticket is restored and the mic stays off until a tap.

The crew member stays **silent** until the guest speaks; there is no "welcome back". If the guest hasn't spoken within
`resume.nudge_after_seconds` (30 s), the crew member asks once, briefly, whether they need anything else. It arrives
as a normal response (audio and transcript events). The nudge is skipped while a rate-limit retry is pending.

### 3b. Server → browser: `extension.resume_rejected`

```json
{"type": "extension.resume_rejected", "reason": "unknown"}
```

| `reason` | Meaning |
| --- | --- |
| `unknown` | Wrong or already-used id; the session was ended or evicted; or the socket tried to resume its own session. |
| `expired` | The grace hold or the idle budget ran out. |
| `malformed` | Missing id, or not a 32–128 char string. |
| `disabled` | `resume.enabled` is false. |
| `not_first_frame` | `extension.resume` arrived after another frame or after the first-frame deadline. The socket's current session continues. |

The server **always** follows a rejection with `extension.session_metadata` for the socket's fresh (or current)
session, carrying a new `resumeId`.

**Browser:** it drops the old id, clears the ticket and stores the new id from the metadata that follows. The app shows
"We couldn't restore your order…", unless the guest had already tapped during the reconnect. In that case the tap
carries straight on into the fresh session and its greeting.

### 4. Browser → server: `extension.end_session`

```json
{"type": "extension.end_session"}
```

The server ends the session, deleting the order at once, and closes the socket with 1000 `session_ended`.

**Browser (as built):** the **Start a new order** button sends this, clears the ticket and the id, and stops the mic.
The 1000 `session_ended` close is final for that session: no background reconnect and no resume. The hook then opens a
**fresh** socket by itself, as a page load would. Frames sent between `endSession()` and that close (a quick tap) are
held for the fresh session and never reach the ending one.

### Upstream ordering on a resume (reference)

The new model connection receives, in order:

1. The bootstrap `session.update` (instructions, voice, tools).
2. **One** system `conversation.item.create`, holding the current order JSON plus the last `resume.history_turns`
   guest/crew turns, capped at `resume.history_chars` (newest kept).
3. Only then: guest audio and the browser's `session.update`.

No greeting and no `response.create` are sent until the guest speaks or the nudge fires. The nudge waits for
`session.updated`, as the greeting does.

If the drop came before the conversation started (the session was never greeted), no rehydration item is sent and the
normal greeting runs.

## Close codes

| Code | Reason | Resumable? | Browser action |
| --- | --- | --- | --- |
| 1001/1002/1006/1011, plain 1000, etc. | transport | **Yes**, within the hold | Reconnect with 1 s, 2 s, 4 s … 30 s backoff (10 attempts, longer than the hold), send `extension.resume` first, and restart the mic on `session_resumed`. |
| 4000 | `idle_timeout` | No; the session has already ended | Don't reconnect. Clear the id, show the idle notice, and reconnect on the next tap. |
| 4002 | `superseded` | n/a | Another socket, usually this tab's own reconnect, took over. Don't reconnect and don't clear the id. |
| 1000 | `session_ended` | No | This is the reply to `extension.end_session`. Clear the id and open a fresh socket; don't resume. |

A 4002 normally lands on a socket the tab has already abandoned. A live 4002 means a duplicated tab (which copies
`sessionStorage`) resumed the order. This tab keeps its id, so its next tap presents that id, gets `resume_rejected`,
and starts fresh.

## Crew dashboard

The crew dashboard (`/crew`) only receives server pushes. A cloud voice session appears there when its conversation
starts (the greeting), not when a socket opens, so provisional and resuming sockets never create a car. Updates are
published under the session id:
- `dashboard.order_update` on every `update_order`;
- `session.ended` when the session ends.

On the dashboard itself:
- **Resume:** the session keeps its id, so the car and order card stay put. The dashboard keeps one order card per
  session, so a repeated update replaces the card instead of adding a new one.
- **End:** a New order, an idle close, or an expired or evicted hold all remove the car and its order card.

The local pipeline is not wired to the dashboard.

## Tests

- **Backend** (part of `pytest app/backend/tests`):
  - `test_order_resume.py`: hold, handshake, rehydration, nudge;
  - `test_dashboard_resume.py`: dashboard identity across a resume;
  - `test_idle_timeout.py`;
  - `test_infra_resume.py`: one worker, sticky ingress, secret preservation.
- **Frontend** (vitest, `npm test`):
  - `src/hooks/__tests__/useRealtime.test.tsx`: protocol;
  - `src/__tests__/App.resume.test.tsx`: UI;
  - `src/components/audio/__tests__/recorder.test.ts`: gesture timeout;
  - `src/components/ui/__tests__/status-message.test.tsx`: notices;
  - `src/locales/__tests__/locales.test.ts`: all four locales.
- **Real browser:** `scripts/e2e_order_resume.py` runs the built frontend and crew dashboard in headless Edge (`--channel chromium|chrome` also work),
  against the real middle tier and a fake realtime upstream. It needs no Azure and uses localhost only. It isn't part
  of the default test run:

  ```powershell
  .\.venv\Scripts\python.exe -m pip install playwright   # dev-only; install through your package proxy
  .\.venv\Scripts\python.exe -m playwright install chromium   # only for --channel chromium
  cd app/frontend; npm run build; cd ../employee-dashboard; npm run build; cd ../..
  .\.venv\Scripts\python.exe scripts/e2e_order_resume.py
  ```

  It covers: a 1011 drop with auto-reconnect, mic auto-restart, rehydration and one nudge; the tap-to-continue
  fallback; a tap while reconnecting; a reload; **Start a new order**; the idle close; a strict autoplay policy;
  and resume ids never appearing in URLs or logs. On the crew dashboard it checks one car and one order card for
  the session across every drop and reload, both cleared by **Start a new order** and by the idle close.

## Configuration (`app/backend/config.yaml`)

| Key | Default | Meaning |
| --- | --- | --- |
| `security.idle_timeout_seconds` | 300 | Idle budget from the guest's last activity. Keeps running while disconnected. |
| `security.idle_check_interval_seconds` | 15 | How often idle closes and hold expiry are checked. |
| `resume.enabled` | true | When false, transport closes end the session and no `resumeId` is issued. |
| `resume.grace_seconds` | 120 | Longest hold after a transport drop. The actual hold is `min(grace, remaining idle budget)`. |
| `resume.max_detached` | 20 | Cap on held sessions per process; the oldest hold is ended first. |
| `resume.first_frame_timeout_seconds` | 2 | How long to wait for `extension.resume` before announcing a fresh session. |
| `resume.history_turns` | 6 | Transcript turns replayed into the new upstream; 0 disables the transcript. |
| `resume.history_chars` | 2000 | Character cap on the replayed transcript. |
| `resume.nudge_after_seconds` | 30 | Silent-guest nudge after a resume; 0 disables it. |
