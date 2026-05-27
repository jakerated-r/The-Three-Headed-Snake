#!/usr/bin/env python3
"""CLI for the local Coop Broker."""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(os.environ.get("THREE_HEADED_SNAKE_ROOT", str(Path(__file__).resolve().parents[2])))
COOP_DIR = Path(os.environ.get("COOP_ROOT", str(REPO_ROOT / "data" / "coop")))
TOKEN_FILE = COOP_DIR / "broker" / ".token"
DEFAULT_URL = "http://127.0.0.1:17874"
ENVELOPE_SCRIPT = COOP_DIR / "scripts" / "cross-fire-envelope.py"
IDENTITY_SCRIPT = REPO_ROOT / "src" / "identity" / "per-agent-envelope.py"
SECRET_SCANNER = COOP_DIR / "scripts" / "secret-scanner.sh"
ENVELOPE_KIND_MAP = {
    "auto-engage": "task",
    "broadcast-notice": "status",
    "cross-fire-reply": "receipt",
    "cross-fire-request": "request",
    "handshake": "heartbeat",
    "health-ping": "health",
    "health-pong": "health",
    "help-queue-ask": "task",
    "help-queue-resolution": "receipt",
}


def token() -> str:
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def request(method: str, path: str, payload: dict[str, Any] | None = None,
            base_url: str = DEFAULT_URL, authed: bool = True) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method)
    req.add_header("Accept", "application/json")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    if authed:
        req.add_header("X-Coop-Token", token())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"ok": False, "error": body}
        parsed["status"] = exc.code
        return parsed


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def cmd_health(args: argparse.Namespace) -> int:
    print_json(request("GET", "/health", base_url=args.url, authed=False))
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    body: Any
    if args.body_json:
        body = json.loads(args.body_json)
    elif args.body_file:
        body = json.loads(Path(args.body_file).read_text(encoding="utf-8"))
    else:
        body = args.body
    payload = {
        "from_agent": args.from_agent,
        "to_agent": args.to_agent,
        "kind": args.kind,
        "priority": args.priority,
        "target": args.target or "",
        "body": body,
    }
    print_json(request("POST", "/v1/messages", payload, base_url=args.url))
    return 0


def load_envelope_module() -> Any:
    spec = importlib.util.spec_from_file_location("cross_fire_envelope", ENVELOPE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load envelope helper: {ENVELOPE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_identity_module() -> Any:
    spec = importlib.util.spec_from_file_location("per_agent_envelope", IDENTITY_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load identity helper: {IDENTITY_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scan_envelope(path: Path) -> tuple[bool, str]:
    proc = subprocess.run([str(SECRET_SCANNER), str(path)], capture_output=True, text=True, check=False)
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


def cmd_send_envelope(args: argparse.Namespace) -> int:
    envelope_path = Path(args.envelope)
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    clean, scan_result = scan_envelope(envelope_path)
    if not clean:
        print_json({"ok": False, "error": "secret scan failed", "detail": scan_result})
        return 1
    if not args.skip_verify:
        envelope_module = load_envelope_module()
        schema_ok, schema_reason = envelope_module.validate_schema(envelope)
        sig_ok, sig_reason = envelope_module.verify_envelope(envelope)
        if not schema_ok or not sig_ok:
            print_json({
                "ok": False,
                "error": "envelope verification failed",
                "schema": {"ok": schema_ok, "reason": schema_reason},
                "signature": {"ok": sig_ok, "reason": sig_reason},
            })
            return 1
    from_agent = envelope.get("from")
    if from_agent == "Claude":
        from_agent = "Maestro"
    agents = ("Codex", "Maestro", "Gemini")
    if from_agent not in set(agents):
        print_json({"ok": False, "error": "broker envelopes must be from Codex, Maestro, or Gemini"})
        return 1
    target_to = envelope.get("to")
    if target_to == "broadcast":
        recipients = [agent for agent in agents if agent != from_agent]
    elif target_to in set(agents):
        recipients = [target_to]
    else:
        print_json({"ok": False, "error": "broker envelope to must be Codex, Maestro, Gemini, or broadcast"})
        return 1
    kind = ENVELOPE_KIND_MAP.get(str(envelope.get("type")), "note")
    responses = []
    for recipient in recipients:
        responses.append(request("POST", "/v1/messages", {
            "from_agent": from_agent,
            "to_agent": recipient,
            "kind": kind,
            "priority": args.priority,
            "target": args.target or envelope.get("thread", ""),
            "body": {"envelope": envelope},
        }, base_url=args.url))
    print_json({"ok": all(item.get("ok") for item in responses), "responses": responses})
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    params = {"to": args.to_agent, "limit": str(args.limit)}
    if args.status:
        params["status"] = args.status
    if args.since_id:
        params["since_id"] = str(args.since_id)
    query = urllib.parse.urlencode(params)
    print_json(request("GET", f"/v1/messages?{query}", base_url=args.url))
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    print_json(request("POST", "/v1/ack", {"message_id": args.message_id, "ack_by": args.by}, base_url=args.url))
    return 0


def cmd_reserve(args: argparse.Namespace) -> int:
    payload = {
        "path": args.path,
        "agent": args.agent,
        "reason": args.reason,
        "ttl_seconds": args.ttl_seconds,
    }
    print_json(request("POST", "/v1/reservations", payload, base_url=args.url))
    return 0


def cmd_reservations(args: argparse.Namespace) -> int:
    print_json(request("GET", "/v1/reservations", base_url=args.url))
    return 0


def parse_json_or_text(value: str) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"notes": value}


def cmd_claim_start(args: argparse.Namespace) -> int:
    paths = args.paths
    payload = {
        "agent": args.agent,
        "peer_agent": args.peer,
        "project": args.project,
        "goal": args.goal,
        "paths": paths,
        "ttl_seconds": args.ttl_seconds,
    }
    print_json(request("POST", "/v1/claims/start", payload, base_url=args.url))
    return 0


def cmd_claims(args: argparse.Namespace) -> int:
    params = {"limit": str(args.limit)}
    if args.status:
        params["status"] = args.status
    if args.agent:
        params["agent"] = args.agent
    if args.project:
        params["project"] = args.project
    print_json(request("GET", f"/v1/claims?{urllib.parse.urlencode(params)}", base_url=args.url))
    return 0


def cmd_claim_complete(args: argparse.Namespace) -> int:
    evidence: Any
    if args.evidence_file:
        evidence = json.loads(Path(args.evidence_file).read_text(encoding="utf-8"))
    else:
        evidence = parse_json_or_text(args.evidence)
    payload = {"claim_id": args.claim_id, "agent": args.agent, "evidence": evidence}
    print_json(request("POST", "/v1/claims/complete", payload, base_url=args.url))
    return 0


def cmd_claim_verify(args: argparse.Namespace) -> int:
    verification: Any
    if args.verification_file:
        verification = json.loads(Path(args.verification_file).read_text(encoding="utf-8"))
    else:
        verification = parse_json_or_text(args.verification)
    if not isinstance(verification, dict):
        verification = {"notes": verification}
    payload = {
        "claim_id": args.claim_id,
        "verifier": args.verifier,
        "verdict": args.verdict,
        "verification": verification,
    }
    payload["signed_verdict"] = load_identity_module().sign_verdict(
        args.verifier,
        args.claim_id,
        args.verdict,
        verification,
    )
    print_json(request("POST", "/v1/claims/verify", payload, base_url=args.url))
    return 0


def cmd_claim_cancel(args: argparse.Namespace) -> int:
    payload = {"claim_id": args.claim_id, "agent": args.agent, "reason": args.reason}
    print_json(request("POST", "/v1/claims/cancel", payload, base_url=args.url))
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    deadline = time.time() + args.timeout
    last_seen = args.since_id
    while time.time() < deadline:
        params = {"to": args.to_agent, "since_id": str(last_seen), "limit": "25"}
        if args.status:
            params["status"] = args.status
        response = request("GET", f"/v1/messages?{urllib.parse.urlencode(params)}", base_url=args.url)
        messages = response.get("messages", []) if response.get("ok") else []
        if messages:
            print_json(response)
            return 0
        time.sleep(1)
    print_json({"ok": False, "error": "timeout", "to_agent": args.to_agent})
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coop Broker control CLI")
    parser.add_argument("--url", default=DEFAULT_URL)
    sub = parser.add_subparsers(dest="cmd", required=True)

    health = sub.add_parser("health")
    health.set_defaults(func=cmd_health)

    send = sub.add_parser("send")
    send.add_argument("--from", dest="from_agent", required=True)
    send.add_argument("--to", dest="to_agent", required=True)
    send.add_argument("--kind", default="note")
    send.add_argument("--priority", type=int, default=5)
    send.add_argument("--target", default="")
    send.add_argument("--body", default="")
    send.add_argument("--body-json", default="")
    send.add_argument("--body-file", default="")
    send.set_defaults(func=cmd_send)

    send_env = sub.add_parser("send-envelope")
    send_env.add_argument("envelope")
    send_env.add_argument("--priority", type=int, default=7)
    send_env.add_argument("--target", default="")
    send_env.add_argument("--skip-verify", action="store_true")
    send_env.set_defaults(func=cmd_send_envelope)

    inbox = sub.add_parser("inbox")
    inbox.add_argument("--to", dest="to_agent", required=True)
    inbox.add_argument("--status", default="")
    inbox.add_argument("--since-id", type=int, default=0)
    inbox.add_argument("--limit", type=int, default=50)
    inbox.set_defaults(func=cmd_inbox)

    ack = sub.add_parser("ack")
    ack.add_argument("--message-id", required=True)
    ack.add_argument("--by", required=True)
    ack.set_defaults(func=cmd_ack)

    reserve = sub.add_parser("reserve")
    reserve.add_argument("--path", required=True)
    reserve.add_argument("--agent", required=True)
    reserve.add_argument("--reason", required=True)
    reserve.add_argument("--ttl-seconds", type=int, default=3600)
    reserve.set_defaults(func=cmd_reserve)

    reservations = sub.add_parser("reservations")
    reservations.set_defaults(func=cmd_reservations)

    claim_start = sub.add_parser("claim-start")
    claim_start.add_argument("--agent", required=True)
    claim_start.add_argument("--peer", default="")
    claim_start.add_argument("--project", required=True)
    claim_start.add_argument("--goal", required=True)
    claim_start.add_argument("--ttl-seconds", type=int, default=4 * 3600)
    claim_start.add_argument("paths", nargs="+")
    claim_start.set_defaults(func=cmd_claim_start)

    claims = sub.add_parser("claims")
    claims.add_argument("--status", default="")
    claims.add_argument("--agent", default="")
    claims.add_argument("--project", default="")
    claims.add_argument("--limit", type=int, default=50)
    claims.set_defaults(func=cmd_claims)

    claim_complete = sub.add_parser("claim-complete")
    claim_complete.add_argument("--claim-id", required=True)
    claim_complete.add_argument("--agent", required=True)
    claim_complete.add_argument("--evidence", default="")
    claim_complete.add_argument("--evidence-file", default="")
    claim_complete.set_defaults(func=cmd_claim_complete)

    claim_verify = sub.add_parser("claim-verify")
    claim_verify.add_argument("--claim-id", required=True)
    claim_verify.add_argument("--verifier", required=True)
    claim_verify.add_argument("--verdict", required=True, choices=["pass", "needs_work"])
    claim_verify.add_argument("--verification", default="")
    claim_verify.add_argument("--verification-file", default="")
    claim_verify.set_defaults(func=cmd_claim_verify)

    claim_cancel = sub.add_parser("claim-cancel")
    claim_cancel.add_argument("--claim-id", required=True)
    claim_cancel.add_argument("--agent", required=True)
    claim_cancel.add_argument("--reason", default="")
    claim_cancel.set_defaults(func=cmd_claim_cancel)

    wait = sub.add_parser("wait")
    wait.add_argument("--to", dest="to_agent", required=True)
    wait.add_argument("--status", default="")
    wait.add_argument("--since-id", type=int, default=0)
    wait.add_argument("--timeout", type=int, default=60)
    wait.set_defaults(func=cmd_wait)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
