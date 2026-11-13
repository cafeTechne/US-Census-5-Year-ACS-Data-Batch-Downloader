# SPDX-License-Identifier: AGPL-3.0-or-later
"""Opt-in bounded local-model evaluation. No service calls on import or without --run-model.

Requires a reviewed operator request, an exact model ID, an operator-declared
runtime version, a loopback OpenAI-compatible URL, and a new output directory.
"""

import argparse
import asyncio
from datetime import timedelta
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from census.verification import BoundRequest, strict_json  # noqa: E402
from scripts.verification_eval import prepare, run_protocol, tool_schema_digest  # noqa: E402
from scripts.local_model_eval import _chat_payload, _tool_arguments  # noqa: E402

MAX_WIRE_BYTES = 1024 * 1024


def loopback_url(value):
    parts = urlsplit(value)
    host = parts.hostname
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
        port = parts.port
    except ValueError:
        local, port = False, None
    if (
        parts.scheme not in ("http", "https")
        or not local
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or port == 0
    ):
        raise ValueError("base URL must be HTTP(S) loopback without credentials, query or fragment")
    return value.rstrip("/")


async def exchange(client, directory, label, method, url, *, timeout, payload=None):
    """Retain bounded wire bodies before parsing, including failure/truncation."""
    request = b"" if payload is None else json.dumps(payload, allow_nan=False).encode("utf-8")
    if len(request) > MAX_WIRE_BYTES:
        raise ValueError("model request exceeds the 1 MiB wire limit")
    with open(directory / f"{label}.request.bin", "xb") as stream:
        stream.write(request)
    body, metadata = bytearray(), {"method": method, "url": url, "response_truncated": False}
    try:
        async with client.stream(
            method,
            url,
            content=request or None,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer local",
                "Accept-Encoding": "identity",
            },
            timeout=timeout,
        ) as response:
            metadata.update(status=response.status_code, headers=dict(response.headers))
            async for chunk in response.aiter_raw():
                room = MAX_WIRE_BYTES - len(body)
                body.extend(chunk[:room])
                if len(chunk) > room:
                    metadata["response_truncated"] = True
                    raise ValueError("model response exceeds the 1 MiB wire limit; retained prefix only")
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("compressed responses are unsupported; raw body retained without parsing")
        return _tool_arguments(bytes(body).decode("utf-8"))  # strict object JSON, finite ordinary metadata
    finally:
        metadata["response_bytes_retained"] = len(body)
        for suffix, data in (
            ("response.bin", bytes(body)),
            ("http.json", (json.dumps(metadata, indent=2) + "\n").encode("utf-8")),
        ):
            with open(directory / f"{label}.{suffix}", "xb") as stream:
                stream.write(data)


async def evaluate_session(bound, session, client, *, base_url, model, timeout, directory, json_final=False):
    """Wire an initialized verification MCP session and an async HTTP client."""
    base_url = loopback_url(base_url)
    started = asyncio.get_running_loop().time()
    case = prepare(bound)
    listed = (await session.list_tools()).tools
    tools = [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema,
            },
        }
        for tool in listed
    ]
    tool_schema_digest(tools, case["request_arguments"])
    catalog = await exchange(client, directory, "models", "GET", base_url + "/models", timeout=timeout)
    identifiers = [entry.get("id") for entry in catalog.get("data", []) if type(entry) is dict]
    if model not in identifiers:
        raise ValueError("requested exact model ID is absent from the runtime catalog")
    requests = []

    async def chat(*, messages, tools, timeout, final):
        label = "final" if final else "planning"
        response = await exchange(
            client,
            directory,
            label,
            "POST",
            base_url + "/chat/completions",
            timeout=timeout,
            payload=_chat_payload(model, messages, tools, final=final, json_final=json_final),
        )
        requests.append({"phase": label, "response_model": response.get("model")})
        if response.get("model") != model:
            raise ValueError("chat response model does not match the requested exact model ID")
        return response["choices"][0]["message"]

    async def invoke(*, name, arguments, timeout):
        result = await asyncio.wait_for(session.call_tool(name, arguments), timeout=timeout)
        raw = "".join(content.text for content in result.content if content.type == "text")
        with open(directory / "mcp-result.json", "x", encoding="utf-8") as stream:
            stream.write(raw)
        if result.isError:
            raise ValueError("MCP returned a tool error; see retained MCP result")
        return strict_json(raw)

    remaining = timeout - (asyncio.get_running_loop().time() - started)
    result = await run_protocol(bound, chat=chat, invoke=invoke, tools=tools, timeout=remaining)
    result["observed_runtime"] = {"catalog_model_ids": identifiers, "model_responses": requests}
    result["prepared_case"] = case
    return result


async def run_live(bound, request_path, directory, *, base_url, model, timeout, json_final=False):
    import httpx
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    environment = dict(os.environ, CENSUS_VERIFICATION_REQUEST=str(Path(request_path).resolve()))
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-B", str(ROOT / "mcp_server.py"), "--profile", "verification"],
        cwd=str(ROOT),
        env=environment,
    )
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                await session.initialize()
                return await evaluate_session(
                    bound,
                    session,
                    client,
                    base_url=base_url,
                    model=model,
                    timeout=timeout,
                    directory=directory,
                    json_final=json_final,
                )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-model", action="store_true", help="explicitly opt into model-service calls")
    parser.add_argument("--request", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True, help="exact runtime catalog ID; no automatic selection")
    parser.add_argument(
        "--runtime-version", required=True, help="operator-declared runtime/version, recorded as declared"
    )
    parser.add_argument("--out-dir", required=True, help="new directory for reports and bounded raw bodies")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument(
        "--json-final", action="store_true", help="request JSON-object response mode only in the final phase"
    )
    args = parser.parse_args(argv)
    if not args.run_model:
        parser.error("--run-model is required; use verification_eval.py for offline preparation/scoring")
    directory = Path(args.out_dir)
    try:
        base_url = loopback_url(args.base_url)
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600:
            raise ValueError("timeout must be finite and in (0, 3600] seconds")
        if not args.model.strip() or not args.runtime_version.strip():
            raise ValueError("model and declared runtime/version must be nonempty")
        bound = BoundRequest.from_file(args.request)
        prepare(bound)
        directory.mkdir(parents=True, exist_ok=False)
    except (ValueError, OSError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}))
        return 2
    try:
        result = asyncio.run(
            asyncio.wait_for(
                run_live(
                    bound,
                    args.request,
                    directory,
                    base_url=base_url,
                    model=args.model,
                    timeout=args.timeout,
                    json_final=args.json_final,
                ),
                args.timeout,
            )
        )
    except Exception as exc:
        result = {"protocol_passed": False, "failure": {"type": type(exc).__name__, "message": str(exc)}}
    result["declared_runtime"] = {
        "model": args.model,
        "version": args.runtime_version,
        "source": "operator_declared",
    }
    result["settings"] = {
        "temperature": 0,
        "max_tokens": 1500,
        "timeout_seconds": args.timeout,
        "base_url": base_url,
        "redirects": False,
        "environment_proxies": False,
        "response_format_final": {"type": "json_object"} if args.json_final else None,
    }
    result["files"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }
    with open(directory / "run.json", "x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
    print(json.dumps({"protocol_passed": result["protocol_passed"], "report": str(directory / "run.json")}))
    return 0 if result["protocol_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
