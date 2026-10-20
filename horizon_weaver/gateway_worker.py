"""Private, cancellable HTTP worker. Secrets remain in inherited environment."""
from __future__ import annotations

import json
import sys

import httpx

from .core import DecisionInput
from .policy import GatewayError, ModelGateway


def main() -> None:
    data = json.load(sys.stdin)
    with httpx.Client() as client:
        gateway = ModelGateway(data["model"], data["base_url"], data["temperature"], client=client,
                               structured_output=data.get("structured_output", "json_schema"))
        try:
            result = gateway.complete(DecisionInput.model_validate(data["request"]), data["timeout_s"])
            print(result.model_dump_json())
        except GatewayError as error:
            print(json.dumps({"error": str(error), "usage": error.usage.model_dump(mode="json"),
                              "retryable": error.retryable}))


if __name__ == "__main__":
    main()
