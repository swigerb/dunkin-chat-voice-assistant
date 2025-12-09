# Backlog

Use this lightweight backlog to seed GitHub Issues once the repository is published. Completed work from earlier iterations is hidden in HTML comments for reference.

## Must

- [ ] Create menu images for the `data/menu_images` directory and surface them in the menu UI.
- [ ] Add an optional "Show me what this drink looks like" feature that reveals the relevant menu image on request.
- [ ] Build a toggle that can switch between the Azure OpenAI Realtime stack and the STT ➜ LLM ➜ TTS services pipeline for latency comparisons.
- [x] Implement session tokens or audio tokenization research so every conversation round-trip has a durable identifier.
- [ ] Add the Azure AI Speech backend pathway, including documentation updates and an updated architecture diagram for that flow.

## Should

- [ ] Capture deployment automation (for example, a GitHub Action or Azure Developer CLI pipeline) so new contributors can redeploy safely.

## Could

- [ ] Integrate Bing Search or another web data source for seasonal beverage information.
- [ ] Create audio-triggered tools that open/close chat history or reveal the menu hands-free.

## Would

- [ ] Consider adding a lightweight text chat box beneath Transcript History for hybrid voice + text interactions.
