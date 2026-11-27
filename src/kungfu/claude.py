# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""Talking to Claude. A working Anthropic API connection is required to export or import.

    preflight(model)            fails early, with setup instructions, if the SDK, credentials or model
                                aren't available (one free models.retrieve call, no tokens used)
    ask_json(...)               one request whose answer must match a JSON schema

Requests use server-side fallback (``fallbacks="default"``): if the model declines a request on a
policy ground, the API retries it on a fallback model within the same call.
"""

from __future__ import annotations

import json

FALLBACK_BETA = "server-side-fallback-2026-07-01"

SETUP = (
    "I Know Kung-Fu needs the Anthropic API to write and examine lessons.\n"
    "  1. Run it as ./kungfu from its folder: it installs the SDK into its own environment.\n"
    "  2. Give it a key:     export ANTHROPIC_API_KEY=…   (or: ant auth login)\n"
    "Then run this command again."
)


class Unavailable(Exception):
    """Claude can't be used right now; the message says why and what to do."""


class Refused(Exception):
    pass


def _sdk():
    try:
        import anthropic
    except ImportError:
        raise Unavailable("The Anthropic SDK isn't installed.\n" + SETUP)
    return anthropic


def client():
    anthropic = _sdk()
    try:
        return anthropic.Anthropic()
    except Exception as exc:  # no credentials resolvable
        raise Unavailable(f"No Anthropic credentials found ({exc}).\n" + SETUP)


def preflight(model: str, c=None) -> None:
    """Check the SDK, the credentials and the model with a free metadata call."""
    anthropic = _sdk()
    c = c or client()
    try:
        c.models.retrieve(model)
    except TypeError as exc:  # the SDK found no API key, token or profile to send
        if "authentication" in str(exc).lower():
            raise Unavailable("No Anthropic credentials found.\n" + SETUP)
        raise
    except anthropic.AuthenticationError:
        raise Unavailable("Anthropic rejected the credentials.\n" + SETUP)
    except anthropic.PermissionDeniedError as exc:
        raise Unavailable(f"This Anthropic account can't use the API: {exc.message}")
    except anthropic.NotFoundError:
        raise Unavailable(f"Model {model!r} isn't available to this account. Set [distill] model in the config.")
    except anthropic.APIConnectionError:
        raise Unavailable("Couldn't reach the Anthropic API. Check the network and try again.")
    except anthropic.APIStatusError as exc:
        raise Unavailable(f"The Anthropic API returned an error ({exc.status_code}). Try again shortly.")


def ask_json(c, model: str, system: str, user: str, schema: dict, max_tokens: int = 16000) -> dict:
    """Send one request; return the parsed JSON answer. Raises Refused if every model declined,
    and lets API errors propagate (the SDK has already retried transient ones)."""
    response = c.beta.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise Refused("declined")
    text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), None)
    if text is None:
        raise Refused("no answer")
    if response.stop_reason == "max_tokens":
        raise Refused("answer was cut off")
    return json.loads(text)


def api_errors() -> tuple[type, ...]:
    """The SDK's API error classes, for callers that want to catch them (empty without the SDK)."""
    try:
        import anthropic
    except ImportError:
        return ()
    return (anthropic.APIStatusError, anthropic.APIConnectionError)
