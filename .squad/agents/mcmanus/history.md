# McManus — History

## Project Context
- **Project:** Dunkin Voice Chat Assistant — React 18 + TypeScript + Vite frontend
- **Stack:** Radix UI + Tailwind CSS + shadcn/ui, react-use-websocket, i18next, Framer Motion
- **User:** Brian Swiger
- **Key files:** app/frontend/src/App.tsx, app/frontend/src/hooks/useRealtime.tsx, app/frontend/src/types.ts
- **Build output:** app/backend/static/ (Vite builds to backend static dir)

## Learnings

### Frontend Cleanup Pass (code quality, no visual changes)
- **Double ThemeProvider removed:** `index.tsx` wrapped App in `<ThemeProvider>` but `RootApp` in `App.tsx` also wraps with `<ThemeProvider>`. Removed the outer duplicate from `index.tsx`.
- **Dead code removed from App.tsx:** Commented-out `ImageDialog` import and JSX block cleaned out.
- **`any` types eliminated in useAzureSpeech.tsx:** Replaced all `any` callback params with proper types (`ExtensionMiddleTierToolResponse`, `{ transcript: string }`, `unknown` for errors). Also fixed matching callback signatures in `App.tsx`.
- **`useMemo` for static data:** `dummyTranscripts` was in `useState` with setter never used; converted to `useMemo` with `[]` deps. `transcripts` initializer simplified from `(() => [])` to `([])`.
- **menu-panel.tsx simplified:** Removed unnecessary `useEffect`/`useState` for static JSON import — data is now a module-level const. Also removed an empty `<p>` element.
- **grounding-files.tsx bug fixed:** `isAnimating` (a `useRef`) was used directly in a className conditional instead of `isAnimating.current`, causing `overflow-hidden` to always be applied.
- **history-panel.tsx:** Moved `memo(GroundingFile)` from inside the component render body to module scope to avoid re-creating the memo wrapper every render.
- **dummy-data-context.tsx modernized:** Replaced `React.FC<>` pattern with direct function component + named `ReactNode` import.
- **Unused import removed:** `render, screen` import cleaned from `calculate-order-summary.test.tsx` (pure logic test, no DOM rendering).
- **Files modified:** `index.tsx`, `App.tsx`, `useAzureSpeech.tsx`, `dummy-data-context.tsx`, `menu-panel.tsx`, `grounding-files.tsx`, `history-panel.tsx`, `calculate-order-summary.test.tsx`
- **Build:** `tsc -b && vite build` passes. **Tests:** All 13 tests pass.

## Team Feedback (2026-02-25 Cleanup Sprint)
- **Keaton (Lead):** Identified critical ref bug in grounding-files.tsx and comprehensive cleanup roadmap.
- **Fenster (Backend):** Modernized backend to Python 3.11+. All 56 tests pass. No behavioral changes.
- **Hockney (Tester):** Expanded frontend test suite 4→13 tests. All passing. Comprehensive component coverage.

## Stage 1: Frontend Modernization (2026-08-06)
- **React 18→19**, Vite 5→6, Vitest 1→2, Tailwind 3→4, TypeScript 5.5→5.8, jsdom 24→29, i18next 23→26, @testing-library/react 14→16
- **Tailwind 4 migration:** Deleted tailwind.config.js, rewrote index.css with @import 'tailwindcss' + @theme + @plugin, switched postcss to @tailwindcss/postcss, removed autoprefixer
- **Class renames:** shadow-sm→shadow-xs, bg-gradient-to-→bg-linear-to-, outline-none→outline-hidden, flex-grow→grow, backdrop-blur-sm→backdrop-blur-xs across 8 files
- **lucide-react 1.x:** Github icon removed — swapped to FaGithub from react-icons
- **react-draggable:** Removed (unused dependency, findDOMNode removed in React 19)
- **useRef() React 19:** Added null initial values in useAudioPlayer.tsx and useAudioRecorder.tsx
- **Brand preserved:** All Dunkin brand vars (brand-orange, brand-pink, brand-cream, brand-brown, Fredoka font) confirmed in built CSS
- **CSS size:** 33,972→47,932 bytes (TW4 generates more utilities by default; acceptable for demo)
- **Build:** tsc + vite build clean. **Tests:** 13 pass (5 files, unchanged count)

## Sonic parity port — `feat/sonic-parity` (2026-09-23)
- Item 3:
  - `lib/voices.ts` is the single source of the 10 voices; marin/cedar marked recommended, `DEFAULT_VOICE=marin`.
  - `resolveVoice()` drops unknown stored voices; Settings renders from `VOICE_OPTIONS`.
  - Docs corrected: a voice change applies from the next conversation, not live.
- Item 1: App.tsx sends the voice before `startSession()`.
- Item 6:
  - `useRealtime` exposes `onConnectionLost({code, reason})`, `isConnected`, `reconnect()`; parks on `onReconnectStop`; audio append/clear use `keep=false`.
  - App ends the conversation on loss, shows `status.connectionLost` (en/es/fr/ja), clears the order on the next tap, and never auto-restarts the mic.
- Frontend tests 13 → 31. Lockfiles unchanged.

## Round 3 (2026-09-23)
- R3: es/fr/ja "not recording" copy had Contoso template wording; rewritten per locale. Guard test: no template leftovers + key parity with en.
- R1 frontend: `extension.rate_limited` → `lib/apology-clip.ts` plays `/audio/apology-<lng>.wav` (UI language, fallback en), mic sending muted while it plays, `StatusMessage` shows `status.rateLimitRetrying` / `status.rateLimitFinal`. Cleared on the answer's first audio, on guest speech, or on stop.
- Clips: 24 kHz mono PCM16, 2.4–2.9 s, committed under `app/frontend/public/audio/`.

## Order resume port (2026-09-24)
- **`useRealtime`:**
  - Resume id in sessionStorage (`dunkin.resumeId`, per tab); `extension.resume` is the literal first frame.
  - The hook owns the queue; react-use-websocket is only ever called with keep=false.
  - `classifyClose` sorts closes into idle / superseded / ended / transport. Only transport reconnects: 10 tries, 1 s doubling to 30 s, plus jitter.
  - ended (1000 `session_ended`) re-opens a fresh socket at once. Dunkin has no token endpoint, so there is no token wait.
- **App:**
  - Notices: reconnecting / resumed / tap-to-continue / resume rejected / superseded.
  - Mic auto-restarts after a resume; `recorder.start()` reports false after a 1.5 s suspended-context timeout.
  - "Start a new order" button.
- **Delta from Sonic:** a tap made while still reconnecting now starts the mic as soon as the resume lands. Sonic waited for the 5 s greeting safety timer.
- **Tests:** frontend 65 → 132.
