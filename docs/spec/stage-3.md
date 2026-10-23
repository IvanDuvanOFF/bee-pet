# Stage 3 — BeeLLM client and mini-chat

Read `docs/spec/00-common.md` first. Stages 1–2 are done; read `mood.py`, `bee_window.py`,
`bubble.py`, `app.py`.

## llm.py

- `BEE_URL = os.environ.get("BEE_URL", "http://127.0.0.1:8081")`.
- `ask_bee(prompt: str, max_tokens: int = 64, timeout: float = 10.0) -> str` — POST
  `{BEE_URL}/v1/completions` with `{"prompt", "max_tokens", "temperature": 1.0}` via
  `urllib` (same shape as `docs/bee_mcp.py`), returns `choices[0].text.strip()`.
  Raises on network/format errors.
- `FALLBACK_BUZZ: list[str]` — canned Russian/emoji buzz lines (reuse the ones from
  `docs/bee_mcp.py`, add a few more).
- `clean_reply(text) -> str`: collapse whitespace, cut to ≤ 200 chars on a word
  boundary with «…», empty → random fallback.
- `BeeClient(QObject)`: `request(prompt: str, tag: object = None)` runs `ask_bee` in a
  worker (`QThreadPool` + `QRunnable` with a signals object, or `QThread`), and emits
  `reply_ready(tag, text: str, from_model: bool)` on the GUI thread. On any exception →
  fallback line with `from_model=False`. Remember whether the server is reachable
  (`online: bool | None`) and, after a failure, skip network calls for 30 s (answer with
  fallback immediately) so offline use stays snappy. Must be safe at app exit (no crash
  if a worker finishes after windows close).

## Wiring menu actions

`BeeWindow.action_performed(Action)` → `BeeClient.request(ACTION_PHRASES[action], tag=…)`.
The bubble first shows the local canned reaction immediately (stage 2 behavior), then,
when the reply arrives, shows «🐝 <reply>» in the bubble. If the reply is from fallback,
the canned reaction is enough — do not show a second fallback bubble for menu actions.

## chat.py — `ChatWindow(QWidget)`

- Opened from the «💬 Чат» menu item (`chat_requested`); a second open just raises it.
- Small tool window (`Qt.Tool | Qt.WindowStaysOnTopHint`), normal title bar is fine,
  title «Чат с пчелой 🐝», ~300×360, placed next to the bee (left of it if room, clamped
  to the screen).
- `QTextBrowser` history (user lines right/plain «Ты: …», bee lines «🐝 …», a small
  grey note «(пчела далеко, отвечает по памяти)» when `from_model` is False — at most once
  per offline streak), `QLineEdit` + «Отправить» button, Enter sends, input disabled while
  waiting, a «бзз…» typing indicator line replaced by the reply.
- Each user message: `classify(text)` → `BeeWindow.apply_action(action)` *without*
  re-emitting a model request for the menu path (the chat sends the user's own text to
  the model as the prompt). Add a parameter or separate method so chat-driven mood
  changes do not trigger the menu request path twice.
- The reply also appears in the bee's bubble.
- Closing the chat window hides it (keeps history for the session).

## Tests

`clean_reply` cases; `ask_bee` against a tiny local `http.server` stub in a thread
(success, bad JSON, connection refused → exception); `BeeClient` emits fallback when the
server is down and respects the 30 s backoff (inject a clock or make backoff configurable);
`ChatWindow` send flow with a fake client (no network): history updated, mood changed via
`classify`, input re-enabled after reply.

## Done when

Tests green, smoke run ok, report per common rules.
