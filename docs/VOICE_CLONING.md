# Voice cloning policy

The Violet API exposes zero-shot cloning at
`POST /v1/audio/speech/clone/upload`. This capability carries elevated abuse
risk.

## Miner defaults

- Cloning requires the same bearer token as other inference routes when
  `MINER_ACCESS_TOKEN` is set.
- Reference uploads are capped by `MINER_MAX_CLONE_REFERENCE_BYTES` (default 10 MiB).
- Upstream TTS must remain bound to localhost; only the sidecar port is public.

## Operator policy

1. **Consent** — Only clone voices you have rights to use.
2. **Logging** — Retain request metadata for abuse investigations; do not store
   reference audio longer than needed for inference unless legally required.
3. **Rate limits** — Put a reverse proxy or WAF in front of production miners
   if cloning is enabled.
4. **Disable** — TTS-only miners may omit clone routes by using an upstream that
   does not expose cloning, or by blocking the path at the proxy.

## Validator stance

Qualification does not yet perform biometric speaker verification. Cloning
quality is measured only at the signal/text-fidelity level. Governance may add
clone-specific probes in a future phase.
