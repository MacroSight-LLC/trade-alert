"""Post-deploy Langfuse doctor for trade-alert.

Runs a compact set of end-to-end checks from the CUGA runtime context:
1. HTTP transport health
2. SDK authentication
3. Prompt availability (decision-system / decision-user)
4. Dataset availability (decision-runs)
5. Recent trace visibility for 15m / 1h sessions

Usage:
    python scripts/langfuse_doctor.py
    python scripts/langfuse_doctor.py --json
    python scripts/langfuse_doctor.py --strict
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import vault_env_loader  # noqa: F401, E402
from langfuse_client import get_langfuse_client, reset_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    critical: bool = False


def _transport_check(host: str) -> CheckResult:
    url = f"{host.rstrip('/')}/api/public/health"
    try:
        resp = httpx.get(url, timeout=5.0)
        if resp.status_code == 200:
            return CheckResult("transport", "ok", f"HTTP 200 from {url}", critical=True)
        return CheckResult("transport", "fail", f"HTTP {resp.status_code} from {url}", critical=True)
    except httpx.HTTPError as exc:
        return CheckResult("transport", "fail", f"transport error: {exc}", critical=True)


def _auth_check() -> tuple[CheckResult, Any | None]:
    reset_client()
    lf = get_langfuse_client()
    if lf is None:
        return CheckResult("auth", "fail", "Langfuse client unavailable", critical=True), None

    try:
        lf.fetch_traces(session_id="orchestrator-15m", limit=1, order_by="timestamp.DESC")
        return CheckResult("auth", "ok", "SDK auth succeeded", critical=True), lf
    except Exception as exc:  # noqa: BLE001
        return CheckResult("auth", "fail", f"SDK auth failed: {exc}", critical=True), None


def _prompt_check(lf: Any | None) -> list[CheckResult]:
    if lf is None:
        return [
            CheckResult("prompt:decision-system", "skip", "auth unavailable", critical=True),
            CheckResult("prompt:decision-user", "skip", "auth unavailable", critical=True),
        ]

    results: list[CheckResult] = []
    try:
        response = lf.api.prompts.list(label="production", page=1, limit=100)
        prompt_meta = getattr(response, "data", None) or []
        prompt_versions = {
            str(prompt.name): getattr(prompt, "version", "unknown")
            for prompt in prompt_meta
            if getattr(prompt, "name", None)
        }
    except Exception as exc:  # noqa: BLE001
        return [
            CheckResult(
                "prompt:decision-system",
                "fail",
                f"prompt list failed: {exc}",
                critical=True,
            ),
            CheckResult(
                "prompt:decision-user",
                "fail",
                f"prompt list failed: {exc}",
                critical=True,
            ),
        ]

    for prompt_name in ("decision-system", "decision-user"):
        if prompt_name in prompt_versions:
            results.append(
                CheckResult(
                    f"prompt:{prompt_name}",
                    "ok",
                    f"production label available (version={prompt_versions[prompt_name]})",
                    critical=True,
                )
            )
        else:
            results.append(
                CheckResult(
                    f"prompt:{prompt_name}",
                    "fail",
                    "prompt missing from production label",
                    critical=True,
                )
            )
    return results


def _dataset_check(lf: Any | None) -> CheckResult:
    if lf is None:
        return CheckResult("dataset:decision-runs", "skip", "auth unavailable")
    try:
        response = lf.api.datasets.list(page=1, limit=100)
        dataset_names = {
            str(dataset.name)
            for dataset in (getattr(response, "data", None) or [])
            if getattr(dataset, "name", None)
        }
        if "decision-runs" in dataset_names:
            return CheckResult("dataset:decision-runs", "ok", "dataset available")
        return CheckResult("dataset:decision-runs", "warn", "dataset not created yet")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("dataset:decision-runs", "warn", f"dataset lookup failed: {exc}")


def _recent_trace_check(lf: Any | None, timeframe: str) -> CheckResult:
    if lf is None:
        return CheckResult(f"trace:{timeframe}", "skip", "auth unavailable")
    session_id = f"orchestrator-{timeframe}"
    try:
        response = lf.fetch_traces(session_id=session_id, limit=1, order_by="timestamp.DESC")
        traces = response.data if getattr(response, "data", None) else []
        if not traces:
            return CheckResult(f"trace:{timeframe}", "warn", f"no traces found for {session_id}")
        trace = traces[0]
        return CheckResult(
            f"trace:{timeframe}",
            "ok",
            f"latest trace {getattr(trace, 'id', 'unknown')}",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(f"trace:{timeframe}", "warn", f"trace lookup failed: {exc}")


def run_doctor(*, strict: bool = False) -> tuple[list[CheckResult], int]:
    host = os.getenv("LANGFUSE_HOST", "http://langfuse:3000")
    results: list[CheckResult] = []

    results.append(_transport_check(host))
    auth_result, lf = _auth_check()
    results.append(auth_result)
    results.extend(_prompt_check(lf))
    results.append(_dataset_check(lf))
    results.append(_recent_trace_check(lf, "15m"))
    results.append(_recent_trace_check(lf, "1h"))

    failures = [result for result in results if result.status == "fail"]
    warnings = [result for result in results if result.status == "warn"]
    critical_failures = [result for result in failures if result.critical]

    exit_code = 0
    if critical_failures:
        exit_code = 2
    elif strict and warnings:
        exit_code = 1

    return results, exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Run post-deploy Langfuse checks for trade-alert")
    parser.add_argument("--json", action="store_true", help="Print results as JSON")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings (for example missing recent traces) as non-zero exit",
    )
    args = parser.parse_args()

    results, exit_code = run_doctor(strict=args.strict)

    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        print("Langfuse Doctor")
        print("=" * 40)
        for result in results:
            print(f"[{result.status.upper():>4}] {result.name}: {result.detail}")
        print("=" * 40)
        print(f"exit_code={exit_code}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())