"""GPU lease (opt-in, the config's "gpu_lease": true): another program on this PC borrows the GPU and the RAM.

A program that needs the graphics card for a while (a local ComfyUI run that makes pictures or video) asks this server
for the GPU.  The server waits for the running request, saves the conversation to a session file, stops the engine
(its VRAM and pinned RAM go back to the system) and answers with a lease id.  While the lease is held every chat request
waits in the queue (a streamed one gets its keep-alive) instead of starting the engine again beside the other program.
The program renews the lease while it works and releases it when done; the engine then starts again and the
conversation is restored.  A lease that is not renewed ends by itself when its time is up, so a program that crashed
never leaves the model unloaded for good.

    POST /v1/gpu/lease                 {"holder", "policy": "unload"|"shrink"|"auto", "reserve_mib", "ttl_s",
                                        "wait_s", "reload": "eager"|"lazy"}  -> {"lease_id", ...}
    POST /v1/gpu/lease/<id>/renew      {"ttl_s"}
    POST /v1/gpu/lease/<id>/release    {"reload"}
    GET  /v1/gpu/lease                 the state (never the lease id)

It is the image job's turn (serve/images.py gpu_turn) held across HTTP calls: one thread per lease takes the service's
FIFO and keeps it until release or expiry, and make_room / give_back are the image job's own.  Only with parallel 1
(one request at a time): with batch slots the conversations cannot be saved, so a lease is refused there.

Who may ask (docs/GPU_LEASE.md): the server's API key as for every route, then - this module's own checks - the
lease key in X-Strata-Lease-Key when one is configured, a connection from this PC only and no proxy's forwarding
header (gpu_lease_loopback_only, on by default: a Cloudflare tunnel forwards from 127.0.0.1, its headers give it
away).  A server reachable from other devices (a host other than loopback, or an API key) needs a lease key, or the
lease stays off.
"""
from __future__ import annotations

import collections
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

from serve import images as images_api

POLICIES = ("unload", "shrink", "auto")
RELOADS = ("eager", "lazy")
QUEUES = ("wait", "reject")
KEY_VAR = "STRATA_LEASE_KEY"            # the variable a key file holds (the skill reads the same file)
KEY_HEADER = "X-Strata-Lease-Key"
KEY_MIN_CHARS = 32
HOLDER_MAX_CHARS = 64
TTL_MIN_S, TTL_MAX_S = 10, 3600
WAIT_MAX_S = 3600
MAKE_ROOM_LIMIT_S = 1800                # saving a long conversation and ending the engine: at most this long
# a request that came through a proxy or tunnel: Cloudflare's own headers and the usual reverse-proxy ones
FORWARD_HEADERS = ("CF-Connecting-IP", "CF-Ray", "CF-Visitor", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP",
                   "Forwarded")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
_HOLDER_OK = re.compile(r"[\x20-\x7e]{1,64}")


class LeaseError(Exception):
    """An answer other than 200: the HTTP status, an error code and a plain message."""

    def __init__(self, status: int, code: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after

    def body(self) -> dict:
        kind = "invalid_request_error" if self.status in (400, 404, 415) else \
            "forbidden" if self.status == 403 else "model_busy" if self.status == 409 else "server_error"
        return {"error": {"type": kind, "code": self.code, "message": self.message}}


# --------------------------------------------------------------------------- keys

_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\r\n\u2028\u2029]*)\s*$")


def parse_env(text: str) -> dict:
    """KEY=VALUE lines, the form shuohao-skills' character-refs reads (its parseEnv), so both sides read one file
    alike: lines split at \\n, '#' lines skipped, one quote at either end of a value dropped."""
    env = {}
    for line in text.split("\n"):
        m = _ENV_LINE.match(line)
        if m and not line.strip().startswith("#"):
            env[m.group(1)] = re.sub(r"^[\"']|[\"']$", "", m.group(2))
    return env


def read_key(file_value, env_name, env, base: str | None = None) -> tuple[str | None, str]:
    """The lease key and where it came from: the environment variable the config names (gpu_lease_key_env) first,
    then STRATA_LEASE_KEY in the key file (gpu_lease_key_file).  -> (None, "") when neither is configured.
    ValueError with a plain reason when one is configured but gives no usable key.  The key is never printed."""
    if env_name not in (None, ""):
        if not isinstance(env_name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
            raise ValueError(f'"gpu_lease_key_env" must be an environment variable name, not {env_name!r}')
        value = (env.get(env_name) or "").strip()
        if value:
            return _checked(value, f"the environment variable {env_name}")
        if file_value in (None, ""):
            raise ValueError(f"the environment variable {env_name} (gpu_lease_key_env) is not set or empty")
    if file_value in (None, ""):
        return None, ""
    if not isinstance(file_value, str):
        raise ValueError('"gpu_lease_key_file" must be a file path')
    path = Path(os.path.expanduser(file_value))
    if not path.is_absolute() and base:
        path = Path(base) / path
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ValueError(f"the lease key file {path} cannot be read ({type(e).__name__})") from None
    value = (parse_env(text).get(KEY_VAR) or "").strip()
    if not value:
        raise ValueError(f"the lease key file {path} has no {KEY_VAR}=... line")
    note = ""
    if os.name != "nt":
        try:
            if path.stat().st_mode & 0o077:
                note = f" (other users can read it: chmod 600 {path})"
        except OSError:
            pass
    key, source = _checked(value, f"the file {path}")
    return key, source + note


def _checked(value: str, source: str) -> tuple[str, str]:
    if len(value) < KEY_MIN_CHARS:
        raise ValueError(f"the lease key in {source} is shorter than {KEY_MIN_CHARS} characters")
    return value, source


def key_matches(given: str, key: str) -> bool:
    import hmac
    return hmac.compare_digest((given or "").strip().encode("latin-1", "replace"), key.encode("latin-1", "replace"))


def loopback_peer(ip: str) -> bool:
    ip = (ip or "").strip().lower()
    return ip == "::1" or ip.startswith("127.") or ip.startswith("::ffff:127.")


def forwarded(headers) -> str | None:
    """The first proxy or tunnel header the request carries (it came from elsewhere through this PC), else None."""
    for h in FORWARD_HEADERS:
        if headers.get(h) is not None:
            return h
    return None


# --------------------------------------------------------------------------- the lease

class Lease:
    def __init__(self, holder: str, policy: str, reserve_mib: int | None, ttl_s: float, wait_s: float, reload: str,
                 max_s: float):
        self.id = "gl_" + secrets.token_urlsafe(16)
        self.holder, self.policy, self.reserve_mib = holder, policy, reserve_mib
        self.ttl_s, self.wait_s, self.reload = ttl_s, wait_s, reload
        self.state = "queued"           # queued -> making_room -> held -> releasing -> ended
        self.created = time.monotonic()
        self.created_wall = time.time()
        self.expires = self.created + ttl_s
        self.max_until = self.created + max_s
        self.ready = threading.Event()      # making room ended (result or error is set)
        self.released = threading.Event()   # release asked for (or the server stops)
        self.result: dict | None = None
        self.error: LeaseError | None = None
        self.used_policy: str | None = None
        self.held_at: float | None = None
        self.ended_reason: str | None = None
        self.abandoned = False              # the asking client left before its answer: give it back at once
        self.shutting_down = False

    def until(self) -> float:
        return min(self.expires, self.max_until)


class LeaseManager:
    """The leases of one Service: at most one past its queue at a time, the others wait in order."""

    def __init__(self, svc, *, ttl_s=600.0, max_s=14400.0, wait_s=900.0, queue="wait", reload="eager",
                 reload_wait_s=120.0, key: str | None = None, key_source: str = "", loopback_only: bool = True,
                 session_dir: str | None = None, log=print):
        self.svc = svc
        self.ttl_s, self.max_s, self.wait_s = float(ttl_s), float(max_s), float(wait_s)
        self.queue_mode, self.reload, self.reload_wait_s = queue, reload, float(reload_wait_s)
        self.key, self.key_source, self.loopback_only = key, key_source, bool(loopback_only)
        self.session_dir = session_dir
        self.log = log
        self.cond = threading.Condition()
        self.leases: collections.deque[Lease] = collections.deque()   # in order; [0] is the one with the turn
        self.totals = {"leases": 0, "expired": 0, "make_room_failed": 0, "abandoned": 0}
        self.last: dict | None = None

    # ---- what the request paths ask

    def active(self) -> bool:
        """A lease waits for the GPU, holds it or is giving it back."""
        with self.cond:
            return bool(self.leases)

    def pending(self) -> bool:
        """A lease waits for the service's turn: new chat requests let it go first."""
        with self.cond:
            return any(x.state == "queued" for x in self.leases)

    def wait_not_pending(self, timeout: float) -> bool:
        """Wait up to `timeout` s until no lease waits for the turn; -> True when none does."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self.cond:
            while any(x.state == "queued" for x in self.leases):
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.cond.wait(left)
            return True

    def rejecting(self) -> float | None:
        """gpu_lease_queue "reject": the seconds a chat request is told to wait (Retry-After) while a lease is active,
        else None (requests queue)."""
        if self.queue_mode != "reject":
            return None
        with self.cond:
            if not self.leases:
                return None
            head = self.leases[0]
            return max(1.0, head.until() - time.monotonic()) if head.state == "held" else float(self.ttl_s)

    # ---- the routes

    def check_request(self, body: dict) -> dict:
        """The request's fields, checked and with this server's defaults: ValueError (400) for a wrong one."""
        if not isinstance(body, dict):
            raise ValueError("send a JSON object")
        holder = body.get("holder", "client")
        if not isinstance(holder, str) or not _HOLDER_OK.fullmatch(holder):
            raise ValueError(f"holder: 1 to {HOLDER_MAX_CHARS} printable ASCII characters")
        policy = body.get("policy", "unload")
        if policy not in POLICIES:
            raise ValueError(f"policy: one of {', '.join(POLICIES)}")
        reserve = body.get("reserve_mib")
        if reserve is not None and (isinstance(reserve, bool) or not isinstance(reserve, int) or reserve < 0):
            raise ValueError("reserve_mib: a whole number of MiB (0 or more)")
        if policy == "shrink" and reserve is None:
            raise ValueError("policy shrink needs reserve_mib: how much VRAM to keep free")
        ttl = _number(body.get("ttl_s", self.ttl_s), "ttl_s", TTL_MIN_S, TTL_MAX_S)
        wait = _number(body.get("wait_s", self.wait_s), "wait_s", 0, WAIT_MAX_S)
        reload = body.get("reload", self.reload)
        if reload not in RELOADS:
            raise ValueError(f"reload: one of {', '.join(RELOADS)}")
        return {"holder": holder, "policy": policy, "reserve_mib": reserve, "ttl_s": ttl, "wait_s": wait,
                "reload": reload}

    def acquire(self, body: dict, cancel: threading.Event | None = None) -> dict:
        """POST /v1/gpu/lease: wait for the turn, make room, -> the lease (LeaseError otherwise)."""
        opts = self.check_request(body)
        eng = self.svc.engine
        if getattr(eng, "batch", 0):
            raise LeaseError(400, "gpu_lease_parallel", 'the GPU lease needs one request at a time: "parallel" is '
                                                         f'{eng.batch} in this configuration')
        if opts["policy"] in ("unload", "auto") and not hasattr(eng, "unload"):
            raise LeaseError(501, "gpu_lease_unsupported", "this engine cannot be unloaded")
        if opts["policy"] == "shrink" and not images_api.engine_elastic(eng):
            raise ValueError('policy shrink needs an engine started with --vram-elastic ("vram_elastic": true)')
        lease = Lease(opts["holder"], opts["policy"], opts["reserve_mib"], opts["ttl_s"], opts["wait_s"],
                      opts["reload"], self.max_s)
        with self.cond:
            self.leases.append(lease)
            self.cond.notify_all()
        threading.Thread(target=self._run, args=(lease,), daemon=True, name="strata-gpu-lease").start()
        deadline = time.monotonic() + lease.wait_s + MAKE_ROOM_LIMIT_S
        while not lease.ready.wait(0.25):
            if cancel is not None and cancel.is_set():
                lease.abandoned = True
                lease.released.set()
                raise LeaseError(499, "client_gone", "the client left before the lease was granted")
            if time.monotonic() > deadline:
                lease.abandoned = True
                lease.released.set()
                raise LeaseError(503, "lease_make_room_failed", "making room took too long; the lease was given up")
        if lease.error is not None:
            raise lease.error
        if cancel is not None and cancel.is_set():
            lease.abandoned = True
            lease.released.set()
            raise LeaseError(499, "client_gone", "the client left before the lease was granted")
        return dict(lease.result or {})

    def _find(self, lease_id) -> Lease:
        with self.cond:
            for x in self.leases:
                if x.id == lease_id and x.state == "held":
                    return x
        raise LeaseError(404, "lease_not_found", "no such lease held (it ended, expired or was never granted)")

    def renew(self, lease_id: str, body: dict) -> dict:
        lease = self._find(lease_id)
        ttl = _number((body or {}).get("ttl_s", lease.ttl_s), "ttl_s", TTL_MIN_S, TTL_MAX_S)
        with self.cond:
            now = time.monotonic()
            lease.expires = now + ttl
            capped = lease.expires >= lease.max_until
            self.cond.notify_all()
        return {"status": "renewed", "expires_in_s": round(lease.until() - now, 1), "capped": capped}

    def release(self, lease_id: str, body: dict) -> dict:
        lease = self._find(lease_id)
        reload = (body or {}).get("reload", lease.reload)
        if reload not in RELOADS:
            raise ValueError(f"reload: one of {', '.join(RELOADS)}")
        with self.cond:
            lease.reload = reload
            held = time.monotonic() - (lease.held_at or lease.created)
            lease.ended_reason = lease.ended_reason or "released"
            lease.released.set()
            self.cond.notify_all()
        restarts = lease.used_policy == "unload" and reload == "eager"
        return {"status": "released", "reload": "started" if restarts else "lazy" if lease.used_policy == "unload"
                else "not needed", "held_s": round(held, 1)}

    def view(self) -> dict:
        """GET /v1/gpu/lease, /metrics, /v1/status: the state, never a lease id."""
        svc = self.svc
        with self.cond:
            head = self.leases[0] if self.leases else None
            now = time.monotonic()
            out = {"enabled": True, "state": head.state if head else "idle",
                   "queued_leases": max(0, len(self.leases) - 1), "totals": dict(self.totals),
                   "key_required": self.key is not None, "loopback_only": self.loopback_only,
                   "queue": self.queue_mode}
            if head is not None:
                out.update(holder=head.holder, policy=head.used_policy or head.policy,
                           since_s=round(now - head.created, 1))
                if head.state == "held":
                    out.update(held_s=round(now - (head.held_at or now), 1),
                               expires_in_s=round(max(0.0, head.until() - now), 1))
            if self.last is not None:
                out["last"] = dict(self.last)
        with svc.status_lock:
            out["queued_requests"] = int(svc.status.get("queued") or 0)
        return out

    def shutdown(self) -> None:
        """The server stops: every lease ends now, without starting the engine again."""
        with self.cond:
            for x in self.leases:
                x.shutting_down = True
                x.ended_reason = x.ended_reason or "server stopping"
                x.released.set()
            self.cond.notify_all()

    # ---- the lease's own thread

    def _say(self, text: str) -> None:
        self.log(f"[strata] {text}", flush=True)

    def _fail(self, lease: Lease, err: LeaseError) -> None:
        lease.error = err
        lease.ready.set()

    def _resolve(self, lease: Lease) -> str:
        if lease.policy != "auto":
            return lease.policy
        if lease.reserve_mib is not None and images_api.engine_elastic(self.svc.engine):
            return "shrink"
        return "unload"

    def _run(self, lease: Lease) -> None:
        svc = self.svc
        deadline = lease.created + lease.wait_s
        took = False
        try:
            with self.cond:                          # the leases before this one first
                while self.leases and self.leases[0] is not lease:
                    left = deadline - time.monotonic()
                    if left <= 0 or lease.released.is_set():
                        self._fail(lease, LeaseError(409, "lease_busy", "another program holds the GPU lease; "
                                                     "try again when it has finished", retry_after=30))
                        return
                    self.cond.wait(min(left, 0.5))
            while not took:                          # then the running request (new ones let it go first)
                if lease.released.is_set():
                    self._fail(lease, LeaseError(409, "lease_cancelled", "the lease was given up while waiting"))
                    return
                left = deadline - time.monotonic()
                if left <= 0:
                    self._fail(lease, LeaseError(409, "model_busy", "a request is still running; try again when it "
                                                 "has finished", retry_after=30))
                    return
                took = svc.fifo.acquire(timeout=min(0.25, left))
            with self.cond:
                lease.state = "making_room"
                self.cond.notify_all()
            t0 = time.monotonic()
            policy = lease.used_policy = self._resolve(lease)
            info: dict = {}
            save_dir = self.session_dir or svc.slot_save_path or _private_temp()
            self._say(f"GPU lease for {lease.holder}: making room ({policy}) ...")
            try:
                restore = images_api.make_room(svc, policy, lease.reserve_mib, save_dir, self._say, "GPU lease", info)
            except Exception as e:                   # noqa: BLE001 - EngineStuck and the like: nothing was given
                self.totals["make_room_failed"] += 1
                self._say(f"GPU lease for {lease.holder}: the GPU could not be given ({e}); the model keeps it")
                self._fail(lease, LeaseError(503, "lease_make_room_failed", f"the GPU could not be given: {e}",
                                             retry_after=30))
                return
            if policy == "shrink" and restore is None:
                self.totals["make_room_failed"] += 1
                self._fail(lease, LeaseError(503, "lease_make_room_failed", "the expert cache could not give VRAM "
                                             "back", retry_after=30))
                return
            now = time.monotonic()
            with self.cond:
                lease.state, lease.held_at = "held", now
                lease.expires = now + lease.ttl_s     # the time counts from when the GPU is the holder's
                self.totals["leases"] += 1
                self.cond.notify_all()
            free = svc.free_vram_mib() if hasattr(svc, "free_vram_mib") else None
            lease.result = {"lease_id": lease.id, "policy": policy, "holder": lease.holder,
                            "expires_in_s": round(lease.ttl_s, 1), "max_s": round(lease.max_until - now, 1),
                            "conversation_saved": ({"tokens": info.get("tokens"), "bytes": info.get("bytes")}
                                                   if info.get("tokens") is not None else None),
                            "vram_free_mib": free, "waited_s": round(t0 - lease.created, 1),
                            "make_room_s": round(now - t0, 1)}
            if lease.result["conversation_saved"] is None and policy == "unload":
                lease.result["note"] = ("no conversation was saved (none held, or the engine could not write it): "
                                        "the next request reads its prompt again")
            self._say(f"GPU lease for {lease.holder}: granted ({policy}, {lease.ttl_s:g} s, renewable)")
            lease.ready.set()
            while True:                              # held: until release, expiry or the server's stop
                left = lease.until() - time.monotonic()
                if lease.released.wait(max(0.0, min(left, 1.0))):
                    break
                if time.monotonic() >= lease.until():
                    with self.cond:
                        lease.ended_reason = "expired"
                        self.totals["expired"] += 1
                    self._say(f"GPU lease for {lease.holder}: not renewed in time - taking the GPU back")
                    break
            if lease.abandoned:
                self.totals["abandoned"] += 1
                lease.ended_reason = "abandoned"
            with self.cond:
                lease.state = "releasing"
                self.cond.notify_all()
            reload = "lazy" if lease.shutting_down else lease.reload
            if reload == "eager" and policy == "unload":
                reload = self._wait_vram(lease)
            if not lease.shutting_down:
                images_api.give_back(svc, policy, restore, reload, self._say, "GPU lease")
            held = time.monotonic() - (lease.held_at or now)
            self.last = {"holder": lease.holder, "policy": policy, "held_s": round(held, 1),
                         "ended": lease.ended_reason or "released", "at": time.time()}
            self._say(f"GPU lease for {lease.holder}: ended ({lease.ended_reason or 'released'}, held "
                      f"{held:.0f} s){'; the model loads again' if reload == 'eager' and policy == 'unload' else ''}")
        except Exception as e:                       # noqa: BLE001 - never leave the turn taken
            if not lease.ready.is_set():
                self._fail(lease, LeaseError(503, "lease_make_room_failed", str(e)))
            self._say(f"GPU lease for {lease.holder}: {type(e).__name__}: {e}")
        finally:
            if took:
                svc.fifo.release()
            with self.cond:
                lease.state = "ended"
                with_lease = [x for x in self.leases if x is not lease]
                self.leases.clear()
                self.leases.extend(with_lease)
                self.cond.notify_all()

    def _wait_vram(self, lease: Lease) -> str:
        """Before an eager start: the expert cache is sized by the VRAM free at the start, so give the other program
        up to reload_wait_s to free the card (min_free_vram_mib).  -> "eager", or "lazy" when it is still short."""
        svc = self.svc
        need = int(getattr(svc, "min_free_vram_mib", 0) or 0)
        if not need or not hasattr(svc, "free_vram_mib"):
            return "eager"
        deadline = time.monotonic() + self.reload_wait_s
        free = svc.free_vram_mib()
        while free is not None and free < need and time.monotonic() < deadline and not lease.shutting_down:
            time.sleep(0.5)
            free = svc.free_vram_mib()
        if free is not None and free < need:
            self._say(f"GPU lease for {lease.holder}: only {free} MiB of VRAM free (min_free_vram_mib {need}); the "
                      "model loads with the next request instead")
            return "lazy"
        return "eager"


def _number(value, name: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise ValueError(f"{name}: a number of seconds from {lo:g} to {hi:g}")
    return float(value)


def _private_temp() -> str:
    path = os.path.join(tempfile.gettempdir(), "strata-gpu-lease")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


# --------------------------------------------------------------------------- the config

def from_config(svc, cfg: dict, host: str, env, base: str | None = None, log=print) -> tuple[LeaseManager | None, str]:
    """The run config's gpu_lease keys -> (the manager, "") or (None, why it stays off).  SystemExit for a key of the
    wrong type, as the other config keys."""
    on = cfg.get("gpu_lease", False)
    if not isinstance(on, bool):
        raise SystemExit(f'[strata] config "gpu_lease" must be true or false, not {on!r}')
    if not on:
        return None, ""

    def num(key, default, lo, hi):
        v = cfg.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise SystemExit(f'[strata] config "{key}" must be a number from {lo:g} to {hi:g}, not {v!r}')
        return float(v)

    def choice(key, default, options):
        v = cfg.get(key, default)
        if v not in options:
            raise SystemExit(f'[strata] config "{key}" must be one of {", ".join(options)}, not {v!r}')
        return v

    ttl = num("gpu_lease_ttl_s", 600, TTL_MIN_S, TTL_MAX_S)
    max_s = num("gpu_lease_max_s", 14400, TTL_MIN_S, 86400)
    wait = num("gpu_lease_wait_s", 900, 0, WAIT_MAX_S)
    reload_wait = num("gpu_lease_reload_wait_s", 120, 0, 3600)
    queue = choice("gpu_lease_queue", "wait", QUEUES)
    reload = choice("gpu_lease_reload", "eager", RELOADS)
    loopback_only = cfg.get("gpu_lease_loopback_only", True)
    if not isinstance(loopback_only, bool):
        raise SystemExit(f'[strata] config "gpu_lease_loopback_only" must be true or false, not {loopback_only!r}')
    try:
        key, source = read_key(cfg.get("gpu_lease_key_file"), cfg.get("gpu_lease_key_env"), env, base)
    except ValueError as e:
        return None, f"{e}"
    shared = (host or "127.0.0.1") not in LOOPBACK_HOSTS or bool(getattr(svc, "api_key", ""))
    if shared and key is None:
        return None, ("this server is reachable beyond this PC or has an API key, so the GPU lease needs its own key: "
                      'set "gpu_lease_key_file" (a file with STRATA_LEASE_KEY=...) or "gpu_lease_key_env"')
    mgr = LeaseManager(svc, ttl_s=ttl, max_s=max_s, wait_s=wait, queue=queue, reload=reload,
                       reload_wait_s=reload_wait, key=key, key_source=source, loopback_only=loopback_only, log=log)
    return mgr, ""
