# Wiring Violet into ASRAPI

The subnet replaces two hardcoded single-host servers in the Avoices backend:

| What | Where it is today | Replaced by |
|---|---|---|
| ASR batch | `API_URL = "http://31.56.109.67:9000/transcribe"` — `ASRAPI/utils/utils.py:64` | miner pool |
| ASR streaming | `WS_API_URL = "wss://31.56.109.67:9000/realtime/transcribe"` — `ASRAPI/utils/utils.py:65` | miner pool |
| TTS | `TTS_API_URL` (ngrok) — `ASRAPI/main.py:1186` | miner pool |
| TTS streaming | `TTS_WS_URL` — `ASRAPI/main.py:1187` | miner pool |

Miners speak **exactly these contracts**, so integration is additive. No request
or response shape changes anywhere in ASRAPI.

## Ground rules

1. **Off by default.** Everything below is inert until `VIOLET_ROUTER_ENABLED=true`.
2. **The legacy hosts stay configured.** They become the fallback. Point
   `VIOLET_FALLBACK_ASR_URL` / `VIOLET_FALLBACK_TTS_URL` at them.
3. **Every helper returns `None` on failure**, and `None` means "use the existing
   code path". A subnet outage degrades Avoices to exactly today's behaviour.

## Step 1 — install

```bash
cd ASRAPI
pip install -e ../violet-subnet          # or: pip install violet-subnet
cp ../violet-subnet/integration/asrapi/violet_integration.py utils/
```

Add to `ASRAPI/requirements.txt`:

```
violet-subnet>=1.4.0
```

## Step 2 — environment

```bash
# Off until you flip this.
VIOLET_ROUTER_ENABLED=false

VIOLET_NETUID=<your netuid>
BT_NETWORK=finney

# Fallbacks: the servers Avoices uses today.
VIOLET_FALLBACK_ASR_URL=http://31.56.109.67:9000
VIOLET_FALLBACK_TTS_URL=https://laurine-unappropriable-unvolcanically.ngrok-free.app

# Shared with validators so they can verify work reports.
VIOLET_WORK_REPORT_SIGNING_KEY=<long random secret>
VIOLET_RECEIPTS_DB_PATH=/var/lib/avoices/violet_receipts.sqlite3

# Optional: only route to miners with at least this on-chain incentive.
VIOLET_ROUTER_MIN_INCENTIVE=0.0
```

`VIOLET_RECEIPTS_DB_PATH` must be on a **persistent** volume. On Render, that
means a mounted disk — a receipts file on ephemeral storage loses the work
history that miners are paid on every deploy.

## Step 3 — lifespan

In `ASRAPI/main.py`, inside the existing `lifespan` (line ~1333):

```python
from utils.violet_integration import get_router, shutdown_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... existing startup ...
    await get_router()          # no-op when disabled
    yield
    await shutdown_router()
    # ... existing shutdown ...
```

## Step 4 — batch ASR

In `ASRAPI/utils/utils.py`, at the top of `transcribe_single_file` (line 342),
after `api_lang` is resolved:

```python
from utils.violet_integration import transcribe_via_violet

routed = await transcribe_via_violet(
    audio_file_path, api_lang, response_format,
    audio_seconds=_audio_duration_seconds(audio_file_path),
)
if routed is not None:
    result = routed
else:
    # existing aiohttp POST to API_URL, unchanged
    ...
```

Everything downstream — the flat-`text` handling, segment normalisation, the
`time_offset` adjustment — is untouched, because a miner returns the same shape.

## Step 5 — TTS

In `ASRAPI/utils/tts_synthesis.py`, inside `_fetch_stream_chunk` (line ~122):

```python
from utils.violet_integration import synthesize_via_violet

routed = await synthesize_via_violet(text, speaker_id)
if routed is not None:
    pcm, meta = routed
    return pcm, meta, None, 200
# existing ngrok POST, unchanged
```

The retry, sub-chunking and `TTS_MIN_BYTES` logic above it still applies, and
now covers miner failures too.

## Step 6 — streaming proxies

`ASRAPI/main.py` already proxies both streams. Only the target URL changes.

Realtime ASR (line ~2628):

```python
from utils.violet_integration import asr_stream_url, finish_stream

session_id = str(uuid.uuid4())
routed = await asr_stream_url(session_id, language)
if routed:
    uri, miner = routed
else:
    uri, miner = f"{WS_API_URL}?language={language}", None

# ... existing proxy loop, unchanged ...

finish_stream(session_id, miner, service="asr", seconds=audio_seconds_forwarded, ok=True)
```

`finish_stream` is what credits the miner's Work score, so do not skip it — and
call it in a `finally` so an aborted stream still records what was served.

TTS streaming (line ~5539) follows the same pattern with `tts_stream_url`.

## Step 7 — expose the work report

Validators pull signed work counters from here. Without it the Work component of
every miner's score is zero.

```python
from utils.violet_integration import build_work_report, router_status

@app.get("/violet/work-report")
async def violet_work_report(since: float = 0, authorization: str = Header(None)):
    expected = os.getenv("VIOLET_WORK_REPORT_TOKEN")
    if expected and authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="unauthorized")
    window = (time.time() - since) if since else 7 * 86400
    return await build_work_report(window)

@app.get("/violet/status")
async def violet_status():
    return await router_status()
```

Give validators the URL and the token. `VIOLET_WORK_REPORT_TOKEN` is the bearer
token; `VIOLET_WORK_REPORT_SIGNING_KEY` is the HMAC secret — validators need both,
and they may be the same value only if you accept that anyone who can read the
report can also forge one.

## Step 8 — rollout

1. Deploy with `VIOLET_ROUTER_ENABLED=false`. Confirm nothing changed.
2. Set `true` on one instance. Watch `/violet/status` — `healthy` should be
   non-zero, and Avoices latency should be unchanged or better.
3. Check the validator dashboard: miners should show non-zero Work within a
   round.
4. Roll out to the rest.

To roll back, set `VIOLET_ROUTER_ENABLED=false` and restart. There is no
migration and no state to unwind.

## What to watch

| Signal | Where | Meaning if bad |
|---|---|---|
| `healthy` = 0 | `/violet/status` | every request is falling back; check discovery |
| high `fallback` rate | router logs | miners failing or saturated |
| `work_24h.failed` climbing | `/violet/status` | a miner is accepting then failing |
| validator `Work` all zero | validator dashboard | the work report is unreachable or its signature is rejected |
