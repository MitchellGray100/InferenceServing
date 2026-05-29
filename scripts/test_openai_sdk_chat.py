"""Smoke test MiniTen with the official OpenAI Python SDK.

This script verifies that OpenAI-compatible clients can call MiniTen's
`/v1/chat/completions` route. It expects a running MiniTen API server and a
running model deployment.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
DEFAULT_PROMPT = "How does the learning rate affect gradient descent?"
DEFAULT_PRIOR_ANSWER = (
    "An optimization algorithm that iteratively adjusts model parameters by "
    "moving in the direction of steepest decrease in the loss function."
)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Call MiniTen chat completions through the OpenAI Python SDK.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("MINITEN_OPENAI_BASE_URL", DEFAULT_BASE_URL),
        help=f"OpenAI-compatible API base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("MINITEN_PROJECT_API_KEY"),
        help="MiniTen project API key. Defaults to MINITEN_PROJECT_API_KEY.",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("MINITEN_MODEL", "small-llm"),
        help="MiniTen deployment name, not the Hugging Face model ID.",
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="Final user prompt to send.",
    )
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--temperature", type=float, default=0)
    return parser.parse_args()


def import_openai_client() -> Any:
    """Import the OpenAI SDK with an actionable error if it is missing."""
    try:
        from openai import OpenAI
    except ModuleNotFoundError:
        print(
            "The OpenAI Python SDK is not installed. Install it with:\n"
            "  python -m poetry add openai\n"
            "or temporarily with:\n"
            "  python -m pip install openai",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    return OpenAI


def main() -> int:
    """Run the OpenAI SDK compatibility smoke test."""
    args = parse_args()
    if not args.api_key:
        print(
            "Missing API key. Set MINITEN_PROJECT_API_KEY or pass --api-key.",
            file=sys.stderr,
        )
        return 1

    OpenAI = import_openai_client()
    client = OpenAI(base_url=args.base_url, api_key=args.api_key)

    response = client.chat.completions.create(
        model=args.model,
        messages=[
            {"role": "system", "content": "You are a concise technical writer."},
            {"role": "user", "content": "What is gradient descent?"},
            {"role": "assistant", "content": DEFAULT_PRIOR_ANSWER},
            {"role": "user", "content": args.prompt},
        ],
        max_tokens=args.max_tokens,
        temperature=args.temperature,
    )

    print("OpenAI SDK request succeeded.")
    print(f"model: {args.model}")
    print(f"base_url: {args.base_url}")
    print("response:")
    print(response.choices[0].message.content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
