# Multi-miner pilot checklist (Phase 3)

Use this before opening compensated testnet traffic to multiple operators.

## Network

- [ ] At least **3 independent miners** registered with distinct coldkeys
- [ ] Each miner passes full qualification including **identity challenge** and
      **progressive streaming**
- [ ] Public endpoints use **HTTPS** (`MINER_PUBLIC_ENDPOINT=https://…`)
- [ ] Upstream ASR/TTS bound to **127.0.0.1**; only miner port published

## Validators

- [ ] `VALIDATOR_EVALSET_PATH` points at the **SALT standard corpus** (`./data/evalset/salt` after `scripts/build_salt_evalset.py`) — see [EVALSET.md](./EVALSET.md)
- [ ] Evalset is **not** `synthetic_only` (dashboard / startup logs show real audio)
- [ ] Holdout kept offline (`data/evalset/salt-holdout`)
- [ ] `VIOLET_RELEASE_MANIFEST_PATH` pins allowed image digests
- [ ] `VIOLET_WORK_REPORT_HMAC_SECRET` differs from bearer token
- [ ] Qualification TTL enforced in scoring (`qualification_is_fresh`)

## Router / product

- [ ] `VIOLET_ROUTER_ENABLED=true` with **empty** `VIOLET_STATIC_MINERS`
- [ ] Receipt DB on persistent volume; spot-check signed work reports
- [ ] Registry rejects hotkey header mismatches on `/health`

## Evidence to publish

- [ ] Dashboard export: qualification pass rate, mean WER/TTS quality
- [ ] Adversarial run log: replayed work report rejected, bad image digest rejected
- [ ] Public latency samples for batch + streaming ASR/TTS
