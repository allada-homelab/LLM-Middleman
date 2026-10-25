"""Owner-run Dify E2E helper (LLMM-021) — drives the REST-observable rows of the Dify checklist.

Stdlib only (urllib, curl-hook-safe like the rest of the rig). Needs a live HA with a Dify
agent configured, plus the Dify app's API for the rows that inspect or mutate Dify state:

    HA_URL=http://ha.local:8123  HA_TOKEN=<long-lived token>
    DIFY_BASE_URL=https://api.dify.ai/v1  DIFY_API_KEY=<app key>  [DIFY_USER=home-assistant]

    python3 scripts/e2e/dify_e2e.py turn   --agent conversation.jarvis "What time is it?"
    python3 scripts/e2e/dify_e2e.py nodup  --agent conversation.jarvis
    python3 scripts/e2e/dify_e2e.py recall --agent conversation.jarvis
    python3 scripts/e2e/dify_e2e.py recover --agent conversation.jarvis
    python3 scripts/e2e/dify_e2e.py inputs

Every command prints what it observed and exits non-zero on a failed check. Rows that need
eyes (Dify logs, the HA trace UI, voice hardware) stay manual — see ``scripts/e2e/README.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
import uuid
from typing import Any

# Must match const.ERROR_MESSAGE: the integration's spoken fallback.
FALLBACK = "Sorry, I could not reach the assistant right now. Please try again."


def _request(method: str, url: str, token: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 — operator-supplied URL
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        raw = resp.read()
    return json.loads(raw) if raw else None


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        sys.exit(f"missing env var {name}")
    return value


def turn(agent: str, text: str, conversation_id: str | None = None) -> tuple[str, str]:
    """Send one turn through HA's conversation API; return (speech, conversation_id)."""
    body: dict[str, Any] = {"text": text, "agent_id": agent}
    if conversation_id:
        body["conversation_id"] = conversation_id
    result = _request("POST", f"{_env('HA_URL')}/api/conversation/process", _env("HA_TOKEN"), body)
    speech = result["response"]["speech"]["plain"]["speech"]
    return speech, result["conversation_id"]


def _dify(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    return _request(method, f"{_env('DIFY_BASE_URL').rstrip('/')}{path}", _env("DIFY_API_KEY"), body)


def _check(ok: bool, label: str) -> bool:
    print(f"{'PASS' if ok else 'FAIL'}: {label}")
    return ok


def cmd_turn(args: argparse.Namespace) -> bool:
    speech, cid = turn(args.agent, args.text, args.conversation_id)
    print(f"conversation_id={cid}\n{speech}")
    return _check(speech != FALLBACK, "reply is not the fallback")


def cmd_nodup(args: argparse.Namespace) -> bool:
    # An Agent app's closing ``message`` repeats the full answer; it must not be appended.
    speech, _ = turn(args.agent, "Answer in exactly one short sentence: what is the capital of France?")
    print(speech)
    half = len(speech) // 2
    doubled = len(speech) > 1 and speech[:half].strip() == speech[half:].strip()
    return _check(speech != FALLBACK, "reply is not the fallback") and _check(not doubled, "answer not spoken twice")


def cmd_recall(args: argparse.Namespace) -> bool:
    token = f"BANANA{uuid.uuid4().hex[:4].upper()}"
    _, cid = turn(args.agent, f"Remember this secret word for later: {token}. Reply OK.")
    speech, _ = turn(args.agent, "What was the secret word I gave you?", cid)
    print(speech)
    return _check(token in speech.upper(), f"second turn recalls {token}")


def cmd_recover(args: argparse.Namespace) -> bool:
    user = _env("DIFY_USER", "home-assistant")
    _, cid = turn(args.agent, "Hello, start a conversation. Reply OK.")
    listing = _dify("GET", f"/conversations?{urllib.parse.urlencode({'user': user, 'limit': 1})}")
    latest = listing["data"][0]["id"]
    _dify("DELETE", f"/conversations/{latest}", {"user": user})  # same as deleting it in the console
    print(f"deleted Dify conversation {latest}")
    speech, _ = turn(args.agent, "Are you still there? Reply yes.", cid)
    print(speech)
    return _check(speech != FALLBACK, "turn after deletion recovers (new Dify conversation)")


def cmd_inputs(_args: argparse.Namespace) -> bool:
    params = _dify("GET", "/parameters")
    for item in params.get("user_input_form", []):
        for kind, control in item.items():
            required = " (required)" if control.get("required") else ""
            print(f"{control.get('variable')}: {kind}{required} max_length={control.get('max_length')}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_turn = sub.add_parser("turn", help="send one turn and print the reply")
    p_turn.add_argument("--agent", required=True)
    p_turn.add_argument("--conversation-id")
    p_turn.add_argument("text")
    p_turn.set_defaults(func=cmd_turn)
    for name, func in (("nodup", cmd_nodup), ("recall", cmd_recall), ("recover", cmd_recover)):
        p = sub.add_parser(name)
        p.add_argument("--agent", required=True)
        p.set_defaults(func=func)
    sub.add_parser("inputs", help="list the app's declared inputs").set_defaults(func=cmd_inputs)
    args = parser.parse_args()
    sys.exit(0 if args.func(args) else 1)


if __name__ == "__main__":
    main()
