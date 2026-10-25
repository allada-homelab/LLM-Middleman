"""Throwaway /v1/converse SSE stub for the converse-preset E2E matrix row.

Speaks the converse preset contract (docs/knowledge/03 §4,
docs/external-agent-handoff/implementation-brief.md §4): SSE frames with `text_delta`,
`done` (optionally `continue_conversation: true`), `error`, the optional `tool_activity`,
and `: keepalive` comments. Copy it into the disposable HA container and run it there so
the integration can reach it on the container's loopback; point a converse-type parent
entry at `http://localhost:<port>`, then exercise the cases below.
See scripts/e2e/README.md for the full recipe (copy-in, run, teardown).

Usage (inside the throwaway HA container):
    docker cp scripts/e2e/converse_sse_stub.py llmm-e2e-ha:/config/converse_sse_stub.py
    docker exec -d llmm-e2e-ha python3 /config/converse_sse_stub.py --port 8099
    # teardown: docker exec llmm-e2e-ha pkill -f converse_sse_stub.py

Behavior by user text (the `text` field of the POST body; the integration prefixes it
with `[YYYY-MM-DD HH:MM:SS TZ] `, so match on substrings):
    contains "follow"  -> stream a reply, then done with continue_conversation=true
    contains "boom"    -> one text_delta, then an `error` event with a `code` (fallback path)
    contains "cut"     -> two text_deltas, then the stream closes without `done` (truncation:
                          the partial reply is kept and the fallback is appended)
    contains "tool"    -> two `tool_activity` events (one ok, one error) before the reply text
                          (shown in the Assist chat as tool calls), then the reply and done
    contains "late"    -> the reply text, then a `tool_activity` (the spec example order: traced,
                          not shown, since text already started), then done
    contains "slow"    -> `: keepalive` comments for ~3 s before the reply (an agent busy on tools)
    contains "empty"   -> done with no text and no deltas (the fallback path)
    anything else      -> stream a short reply, done (continue_conversation absent)

Every request logs the received body so session-key forwarding is observable.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from aiohttp import web


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


async def _stream_words(resp: web.StreamResponse, reply: str) -> None:
    for word in reply.split(" "):
        await resp.write(_sse("text_delta", {"delta": word + " "}))
        await asyncio.sleep(0.15)  # slow enough to observe early-TTS start


async def converse(request: web.Request) -> web.StreamResponse:
    body = await request.json()
    print(f"REQUEST body={json.dumps(body)}", flush=True)
    text = str(body.get("text", ""))

    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)

    if "boom" in text:
        await resp.write(_sse("text_delta", {"delta": "Something is about to "}))
        await asyncio.sleep(0.3)
        await resp.write(_sse("error", {"code": "stub_error", "message": "stub-injected backend error"}))
        return resp

    if "cut" in text:
        await resp.write(_sse("text_delta", {"delta": "This reply is cut "}))
        await resp.write(_sse("text_delta", {"delta": "off mid-"}))
        return resp  # closes without `done`: a dropped connection, never a complete reply

    if "empty" in text:
        await resp.write(_sse("done", {"text": ""}))
        return resp

    if "slow" in text:
        for _ in range(3):
            await resp.write(b": keepalive\n\n")
            await asyncio.sleep(1)

    reply = "Hello from the converse stub, streaming one word at a time."
    if "follow" in text:
        reply = "Sure — should I turn on the lights as well?"
    elif "tool" in text or "late" in text:
        reply = "The kitchen lights are off now."

    if "tool" in text:
        await resp.write(_sse("tool_activity", {"tool": "HassTurnOff", "args": {"name": "kitchen"}, "status": "ok"}))
        await resp.write(_sse("tool_activity", {"tool": "GetWeather", "args": {}, "status": "error"}))

    await _stream_words(resp, reply)

    if "late" in text:
        await resp.write(_sse("tool_activity", {"tool": "HassTurnOff", "args": {"name": "kitchen"}, "status": "ok"}))

    done: dict = {"text": reply}
    if "follow" in text:
        done["continue_conversation"] = True
    await resp.write(_sse("done", done))
    return resp


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    app = web.Application()
    app.router.add_post("/v1/converse", converse)
    web.run_app(app, port=args.port)


if __name__ == "__main__":
    main()
