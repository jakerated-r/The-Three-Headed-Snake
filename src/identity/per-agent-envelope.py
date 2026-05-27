#!/usr/bin/env python3
"""
per-agent-envelope.py — Pillar 1 of Twin Towers Wave 2.

Per-agent HMAC verification keys. Each agent (Maestro, Codex, Gemini, Architect) gets
its own Keychain entry; verdicts (especially claim-verify pass/needs_work) MUST
be signed with the VERIFIER's key, not the shared transport token.

Defeats: self-verify spoofing (Codex calling claim-verify --verifier Maestro
to fake Maestro's pass on its own claim).

Drops in as the public per-agent signing helper
helper. The canonical envelope script continues to support the shared key for
transport-level envelopes. Verdict signing routes through THIS module.

Keychain entries:
    maestro-codex-bridge          (shared transport key)
    maestro-codex-bridge-Maestro  (per-agent verdict-signing key)
    maestro-codex-bridge-Codex    (per-agent verdict-signing key)
    maestro-codex-bridge-Gemini   (per-agent verdict-signing key)
    maestro-codex-bridge-Architect (rare; only for ratified directives)

Environment fallbacks (for non-interactive contexts):
    COOP_BRIDGE_HMAC_SECRET             (shared, transport)
    COOP_BRIDGE_HMAC_SECRET_MAESTRO     (Maestro verdict key)
    COOP_BRIDGE_HMAC_SECRET_CODEX       (Codex verdict key)
    COOP_BRIDGE_HMAC_SECRET_GEMINI      (Gemini verdict key)
    COOP_BRIDGE_HMAC_SECRET_ARCHITECT   (Architect ratification key)

File fallbacks (auto-created with 600):
    data/coop/broker/.hmac-secret-<agent>

Usage:
    per-agent-envelope.py sign-verdict --verifier Maestro --claim-id <uuid> \\
        --verdict pass --verification-json '{...}' --out verdict.json

    per-agent-envelope.py verify-verdict verdict.json

    per-agent-envelope.py mint-keychain --agent Maestro    # mint a fresh key

Canonized 2026-05-27 by Maestro under Architect "trust-first" directive.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets as _pysecrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BRAIN = Path(os.environ.get("THREE_HEADED_SNAKE_ROOT", str(Path(__file__).resolve().parents[2])))
SECRETS_DIR = Path(os.environ.get("COOP_ROOT", str(BRAIN / "data" / "coop"))) / "broker"
KEYCHAIN_ACCOUNT = "coop"
KEYCHAIN_SHARED_SERVICE = "maestro-codex-bridge"
VALID_AGENTS = {"Maestro", "Codex", "Gemini", "Architect"}


def _keychain_service(agent: str) -> str:
    if agent not in VALID_AGENTS:
        raise ValueError(f"unknown agent: {agent}")
    return f"{KEYCHAIN_SHARED_SERVICE}-{agent}"


def _env_var(agent: str) -> str:
    return f"COOP_BRIDGE_HMAC_SECRET_{agent.upper()}"


def _fallback_path(agent: str) -> Path:
    return SECRETS_DIR / f".hmac-secret-{agent}"


def get_agent_secret(agent: str) -> bytes:
    """Resolve per-agent HMAC key. Order: env var → file → Keychain → generate."""
    env_secret = os.environ.get(_env_var(agent), "").strip()
    if env_secret:
        return env_secret.encode("utf-8")
    fp = _fallback_path(agent)
    if fp.exists():
        secret = fp.read_text(encoding="utf-8").strip()
        if secret:
            return secret.encode("utf-8")
    try:
        out = subprocess.check_output(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a", KEYCHAIN_ACCOUNT,
                "-s", _keychain_service(agent),
                "-w",
            ],
            stderr=subprocess.STDOUT,
            timeout=2,
        )
        secret = out.strip()
        if secret:
            return secret
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass
    # Auto-generate + persist to fallback file (single-user 600)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    secret = _pysecrets.token_urlsafe(48)
    fp.write_text(secret + "\n", encoding="utf-8")
    os.chmod(fp, 0o600)
    return secret.encode("utf-8")


def canonical_verdict_for_signing(verdict: dict) -> bytes:
    """Canonical sign-over: stable-key JSON of (claim_id|verifier|verdict|verification_hash|ts)."""
    if "verification" in verdict and "verification_hash" not in verdict:
        h = hashlib.sha256(
            json.dumps(verdict["verification"], sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        verdict["verification_hash"] = h
    fields = {k: verdict[k] for k in ("claim_id", "verifier", "verdict", "verification_hash", "ts") if k in verdict}
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_verdict(verifier: str, claim_id: str, verdict: str, verification: dict) -> dict:
    if verifier not in VALID_AGENTS:
        raise ValueError(f"verifier must be one of {VALID_AGENTS}")
    if verdict not in {"pass", "needs_work", "abstain"}:
        raise ValueError("verdict must be pass, needs_work, or abstain")
    body = {
        "claim_id": claim_id,
        "verifier": verifier,
        "verdict": verdict,
        "verification": verification,
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    secret = get_agent_secret(verifier)
    sig = hmac.new(secret, canonical_verdict_for_signing(body), hashlib.sha256).hexdigest()
    body["signature"] = {
        "alg": "HMAC-SHA256",
        "hmac": sig,
        "key_id": _keychain_service(verifier),
    }
    return body


def verify_verdict(body: dict) -> tuple[bool, str]:
    sig = body.get("signature")
    if not sig:
        return False, "missing signature"
    expected_key_id = sig.get("key_id", "")
    verifier = body.get("verifier")
    if not verifier or _keychain_service(verifier) != expected_key_id:
        return False, f"key_id mismatch: verdict.verifier={verifier} sig.key_id={expected_key_id}"
    if sig.get("alg") != "HMAC-SHA256":
        return False, f"unsupported alg: {sig.get('alg')}"
    secret = get_agent_secret(verifier)
    expected = hmac.new(secret, canonical_verdict_for_signing(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig.get("hmac", "")):
        return False, "hmac mismatch (tampered or spoofed verifier)"
    return True, "ok"


def cmd_sign_verdict(args):
    verification = json.loads(args.verification_json) if args.verification_json else {}
    body = sign_verdict(args.verifier, args.claim_id, args.verdict, verification)
    out = json.dumps(body, indent=2, sort_keys=False)
    if args.out:
        Path(args.out).write_text(out + "\n")
        print(f"[OK] verdict signed by {args.verifier}: {args.out}", file=sys.stderr)
    else:
        print(out)


def cmd_verify_verdict(args):
    body = json.loads(Path(args.verdict_file).read_text())
    ok, reason = verify_verdict(body)
    print(json.dumps({"verdict_file": args.verdict_file, "ok": ok, "reason": reason}, indent=2))
    sys.exit(0 if ok else 1)


def cmd_mint_keychain(args):
    secret = _pysecrets.token_hex(32)
    # Delete old, add new
    subprocess.run(
        ["/usr/bin/security", "delete-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", _keychain_service(args.agent)],
        capture_output=True,
        check=False,
    )
    subprocess.check_call(
        ["/usr/bin/security", "add-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", _keychain_service(args.agent), "-w", secret, "-T", ""]
    )
    print(f"[OK] {args.agent} key minted: 64-hex in Keychain ({_keychain_service(args.agent)})")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("sign-verdict")
    pv.add_argument("--verifier", required=True, choices=sorted(VALID_AGENTS))
    pv.add_argument("--claim-id", required=True)
    pv.add_argument("--verdict", required=True, choices=["pass", "needs_work", "abstain"])
    pv.add_argument("--verification-json", default="{}")
    pv.add_argument("--out")
    pv.set_defaults(func=cmd_sign_verdict)

    pvv = sub.add_parser("verify-verdict")
    pvv.add_argument("verdict_file")
    pvv.set_defaults(func=cmd_verify_verdict)

    pm = sub.add_parser("mint-keychain")
    pm.add_argument("--agent", required=True, choices=sorted(VALID_AGENTS))
    pm.set_defaults(func=cmd_mint_keychain)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
