# Lending the GPU to another program (GPU lease, opt-in)

This page is for people who run Strata and another GPU program on the same PC, for example ComfyUI for pictures or
video, and want them to take turns. It is off unless you turn it on. Without it nothing changes.

Strata normally keeps the graphics card and a large part of the RAM full: the expert cache fills the free VRAM, and the
experts sit in RAM. A second program that needs the card then runs out of memory. With a **GPU lease** the other
program asks Strata for the card, uses it, and gives it back:

1. **Ask.** The program sends `POST /v1/gpu/lease`. Strata lets the running request finish, saves the conversation to a
   session file and stops the engine. The VRAM and the engine's RAM go back to the system.
2. **Use.** The card belongs to the program. Chat requests that arrive meanwhile wait in Strata's queue. A streamed
   request gets a keep-alive every 10 s, so its client does not give up. The engine is not started again.
3. **Renew.** A long job sends `POST /v1/gpu/lease/<id>/renew` from time to time.
4. **Give back.** The program frees its own memory, then sends `POST /v1/gpu/lease/<id>/release`. Strata starts the
   engine, restores the conversation and answers the requests that waited.

A lease that is not renewed ends by itself when its time is up (10 minutes by default). A program that crashed never
leaves the model unloaded for good.

This page is written from `serve/gpu_lease.py` and `serve/server.py`, and the cases `serve/test_gpu_lease.py` pins. It
has not been measured on real hardware yet: how long saving, unloading and loading take on your PC is unknown.

## Installing this fork's server on Windows

The release's Terminal does not have the lease. This fork's installer puts the changed server files into a copy of the official
v0.2.9 Terminal (Control 0.1.55, built from commit `c62d5e3a`, the commit these changes start from):

```powershell
strata launch --stop
irm https://raw.githubusercontent.com/shuai532209720/Strata/main/packaging/install-gpulease.ps1 | iex
```

- It uses the official Terminal already installed, or installs it first with the official v0.2.9 installer (its signed feed and checksum).
- It downloads `serve/server.py`, `serve/images.py`, `serve/gpu_lease.py` and the lease test from commit `040afb0e` of this fork,
  checks each SHA-256 against the value in the script, copies the official Terminal to `terminal\0.1.55-gpulease-040afb0e`, puts them
  in, checks that it starts, and points `terminal\current` at it.
- Models, engines, presets and run configs live outside the Terminal folder: nothing is downloaded again.
- Back to the official Terminal: `$env:STRATA_GPULEASE_ROLLBACK='1'`, then the same `irm ... | iex`.
- `strata update` to a later release replaces it; the lease then needs a new installer for that release.
- From a file instead (no internet on that PC): `packaging/patch_release_terminal.py` builds the archive, `packaging/install-local.ps1`
  installs it.

Not run on a Windows PC yet: the script was only parsed with PowerShell 7 and its downloads checked.

## Turning it on

Add one key to the model's run config (`strata-<model>.json`) and restart:

```json
"gpu_lease": true
```

That is enough when Strata serves only this PC (the default, `127.0.0.1`, no API key). The startup log says
`GPU lease on: ...`.

Recommended together with it:

| Key | Why |
| --- | --- |
| `"min_free_vram_mib": 12000` | The expert cache takes the VRAM that is free when the engine starts. This makes Strata wait until the other program has freed the card, so the cache is not started small. Pick a number a bit below what is free with nothing else running. |
| `"slot_save_path": "D:/strata-sessions"` | Where the conversation is saved. Without it, a temporary folder is used. A long conversation makes a large file (about 1.2 GB per 63K tokens), so pick a fast drive with room. |

Strata must answer one request at a time. With `"parallel"` above 1 the conversations cannot be saved, and every lease
request is refused (400).

## Sharing Strata with other devices: the lease key

When Strata is reachable beyond this PC (`--access lan`, `tailscale`, `cloudflare`, a `"host"` other than
`127.0.0.1`) or has an API key, the lease needs **its own key**. Without one the lease stays off and the startup log says
why. The chat key and the lease key are separate:

| | Chat key (`"api_key"`) | Lease key |
| --- | --- | --- |
| Who has it | Every app that chats, on any device | Only the program on this PC that borrows the GPU |
| Header | `Authorization: Bearer ...` (as now) | `X-Strata-Lease-Key: ...` |
| Opens | The chat API, as now | Only `/v1/gpu/lease` |

A lease request needs both when the server has a chat key: the chat key passes the usual check, then the lease key is
checked.

Put the key in a file and name the file in the config. The same file can be read by the program that borrows the GPU:

```
# C:\Users\<you>\.strata\lease.env  (only you should be able to read it)
STRATA_LEASE_KEY=<at least 32 random characters>
```

```json
"gpu_lease_key_file": "C:/Users/<you>/.strata/lease.env"
```

Or name an environment variable instead: `"gpu_lease_key_env": "STRATA_LEASE_KEY"`. When both are set, the variable
wins. A relative file path is relative to the config's folder. The file is read once at start: restart after changing
it. The key is never printed. Make one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

The file format is one `NAME=value` per line, `#` for comments, quotes around a value allowed.

### Only programs on this PC

By default (`"gpu_lease_loopback_only": true`) the lease also refuses:

- a connection from another address than this PC (`127.0.0.1`, `::1`);
- a request that carries a proxy's header (`CF-Connecting-IP`, `CF-Ray`, `CF-Visitor`, `X-Forwarded-For`,
  `X-Forwarded-Host`, `X-Real-IP`, `Forwarded`). A Cloudflare tunnel delivers its requests from `127.0.0.1`, so only
  its headers tell them apart.

The header check only knows the proxies that add such headers. The lease key is what keeps others out; the other two
checks are a second line.

## The requests

Every lease request needs `Content-Type: application/json` and is refused from a web page of another site, as
`/v1/vram` is.

### `POST /v1/gpu/lease`

```json
{"holder": "comfyui", "policy": "unload", "ttl_s": 600, "wait_s": 900, "reload": "eager"}
```

| Field | Default | Meaning |
| --- | --- | --- |
| `holder` | `"client"` | A name for the log and the status, 1 to 64 printable ASCII characters |
| `policy` | `"unload"` | `unload`: save the conversation and stop the engine (VRAM and RAM freed). `shrink`: only the expert cache gives VRAM back, the conversation stays, RAM is not freed; needs `"vram_elastic": true` and `reserve_mib`. `auto`: `shrink` when `reserve_mib` is given and the engine is elastic, else `unload` |
| `reserve_mib` | none | `shrink` only: the VRAM to keep free |
| `ttl_s` | `gpu_lease_ttl_s` | Seconds until the lease ends without a renewal, 10 to 3600 |
| `wait_s` | `gpu_lease_wait_s` | How long to wait for the running request and for other leases, 0 to 3600 |
| `reload` | `gpu_lease_reload` | After the release: `eager` starts the engine at once, `lazy` with the next request |

The answer comes when the card is free:

```json
{"lease_id": "gl_...", "policy": "unload", "holder": "comfyui", "expires_in_s": 600, "max_s": 14400,
 "conversation_saved": {"tokens": 183422, "bytes": 3586000000}, "vram_free_mib": 15620,
 "waited_s": 12.4, "make_room_s": 21.7}
```

`conversation_saved` is `null` when nothing was saved (no conversation, or the engine could not write it). The lease is
granted anyway; the next request then reads its prompt again.

| Answer | When |
| --- | --- |
| 404 `gpu_lease_disabled` | The lease is off. The message says why |
| 403 `gpu_lease_forbidden` | Wrong or missing lease key, another device, or a proxy |
| 409 `model_busy` | A request was still running after `wait_s` |
| 409 `lease_busy` | Another program held the lease for all of `wait_s` |
| 400 `gpu_lease_parallel` | `"parallel"` is above 1 |
| 400 | A wrong field |
| 501 `gpu_lease_unsupported` | This engine cannot be unloaded |
| 503 `lease_make_room_failed` | The engine could not be stopped; it keeps the card and keeps serving |

Several programs can ask: they get the card one after the other, in order.

### `POST /v1/gpu/lease/<id>/renew` and `/release`

`renew` takes `{"ttl_s": 600}` and answers the new `expires_in_s`; `"capped": true` means the total limit
(`gpu_lease_max_s`, 4 hours by default) is reached. `release` takes `{"reload": "eager"|"lazy"}` (optional) and answers
at once; the engine starts in the background. An unknown or ended lease is 404.

**Free the other program's memory before the release.** For ComfyUI: `POST /free` with
`{"unload_models": true, "free_memory": true}`, then wait until `nvidia-smi` shows the VRAM free. With
`min_free_vram_mib` set, an eager start waits up to `gpu_lease_reload_wait_s` (120 s) for that VRAM, then leaves the start
to the next request.

### `GET /v1/gpu/lease`

The state: `idle`, `queued`, `making_room`, `held` or `releasing`, the holder, the time left, the requests and leases
waiting, and totals. It never shows the lease id. `/v1/status` and `/metrics` carry the same under `"gpu_lease"`, only
when the lease is on.

## While a lease is held

| What | What happens |
| --- | --- |
| A chat request (streamed) | Waits in the queue with a keep-alive every 10 s, then runs |
| A chat request (not streamed) | Waits too, but receives nothing until it runs: its client's own timeout decides how long it waits |
| A chat request with pictures encoded before (an agent resends its history's pictures every turn) | As one without pictures: the encoded pictures come from the cache, without waiting |
| A chat request with a new picture (streamed) | The stream starts at once with keep-alives; the picture is encoded when the lease has ended. An error after that (a picture that cannot be read) is the stream's last event, not an HTTP status |
| A new lease | Waits for this one, up to its `wait_s` |
| `/unload`, `/load`, `/v1/load`, `/v1/unload`, `/v1/vram`, `/slots/...` | 409 `gpu_leased` |
| `/v1/images/generations` | Waits its turn |
| The server stops | The lease ends; the engine is not started again |

`"gpu_lease_queue": "reject"` answers chat requests 503 with `Retry-After` instead of letting them wait.

A request that is already in Strata's queue when a lease asks lets the lease go first.

Only the conversation the engine holds is saved. Other conversations parked in RAM (other agents, sub-agents) are lost
when the engine stops; their next request reads its prompt again.

## All keys

| Key | Default | |
| --- | --- | --- |
| `gpu_lease` | `false` | Turns it on |
| `gpu_lease_key_file` / `gpu_lease_key_env` | none | The lease key, see above |
| `gpu_lease_loopback_only` | `true` | Only programs on this PC, no proxies |
| `gpu_lease_ttl_s` | `600` | Default `ttl_s` (10 to 3600) |
| `gpu_lease_max_s` | `14400` | A lease ends after this long, renewed or not |
| `gpu_lease_wait_s` | `900` | Default `wait_s` (0 to 3600) |
| `gpu_lease_queue` | `"wait"` | `wait` or `reject` |
| `gpu_lease_reload` | `"eager"` | Default `reload` |
| `gpu_lease_reload_wait_s` | `120` | How long an eager start waits for `min_free_vram_mib` |

A key of the wrong type stops the start with a message.

## Not covered

- Measured times on real hardware (save, unload, load, restore) on any card.
- Session files on Windows: the engine's own session save was run on Linux and CUDA only
  ([DETAILS.md](DETAILS.md#session-files-disk)). If a restore fails, the next request reads its prompt again.
- `"parallel"` above 1, a layer split, replicas and split serving.
