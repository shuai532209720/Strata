"""The GPU lease (opt-in, serve/gpu_lease.py, docs/GPU_LEASE.md) over the real HTTP API with a test engine: the lease
waits for the running request and goes before queued ones, saves the conversation and unloads, holds the queue (with
keep-alives) until release or expiry, restores; the control routes, the reject mode, the parallel refusal, the lease
key, the loopback and proxy checks, the config rules and the key file."""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import serve.server as server_mod
from serve import gpu_lease as lease_api
from serve.frontend import ChatTemplate
from serve.server import ByteTokenizer, EngineStuck, Service, serve
from serve.test_lifecycle import ResidentEngine

KEY = "k" * 40
CHAT = {"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 16, "reasoning_effort": "none"}


def quiet(*args, **kwargs):
    pass


class LeaseEngine(ResidentEngine):
    """The resident test engine, loaded, with session files and an optional gate that holds a generation."""

    def __init__(self, tok):
        super().__init__(tok)
        self.loaded, self.unloaded = True, False
        self.sessions = []
        self.gate = None                 # a threading.Event: generate() waits for it (a long-running request)
        self.generating = threading.Event()
        self.unload_error = None
        self.vram_calls = []

    def session_file(self, action, path):
        self.sessions.append((action, path))
        return {"tokens": 123, "bytes": 4560, "ms": 7}

    def unload(self):
        if self.unload_error is not None:
            raise self.unload_error
        super().unload()

    def generate(self, *args, **kwargs):
        self.generating.set()
        if self.gate is not None:
            self.gate.wait(10)
        yield from super().generate(*args, **kwargs)


class ElasticEngine(LeaseEngine):
    def __init__(self, tok):
        super().__init__(tok)
        self.info = {"vram_elastic": True}

    def vram(self, reserve_mib, timeout=120.0):
        self.vram_calls.append(reserve_mib)
        return {"reserve_mib": reserve_mib, "vram_free_mib": reserve_mib or 700, "expert_cache_mib": 1000,
                "expert_cache_full_mib": 6000}


class Base(unittest.TestCase):
    engine_cls = LeaseEngine

    def setUp(self):
        tok = ByteTokenizer()
        self.engine = self.engine_cls(tok)
        self.svc = Service(self.engine, tok, ChatTemplate(Path(__file__).parent / "chat_template.jinja"))
        self.httpd = serve(self.svc, port=0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        if self.svc.gpu_lease is not None:
            self.svc.gpu_lease.shutdown()
        if self.engine.gate is not None:
            self.engine.gate.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def lease_on(self, **kw):
        kw.setdefault("log", quiet)
        self.svc.gpu_lease = lease_api.LeaseManager(self.svc, **kw)
        return self.svc.gpu_lease

    def request(self, path, body=None, headers=None, method=None, timeout=20):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read().decode()
            if "event-stream" in response.headers.get("Content-Type", ""):
                return response.status, raw, response.headers
            return response.status, (json.loads(raw) if raw else None), response.headers

    def acquire(self, body=None, headers=None):
        return self.request("/v1/gpu/lease", {"holder": "comfyui", **(body or {})}, headers)

    def in_thread(self, fn, *args, **kwargs):
        out = {}

        def run():
            try:
                out["result"] = fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - the test reads it
                out["error"] = e
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t, out

    def wait_for(self, cond, timeout=10.0, what="the condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for {what}")


class Disabled(Base):
    def test_off_by_default_and_the_other_routes_unchanged(self):              # T1
        code, body, _ = self.acquire()
        self.assertEqual(code, 404)
        self.assertEqual(body["error"]["code"], "gpu_lease_disabled")
        self.assertEqual(self.request("/v1/gpu/lease")[0], 404)
        self.assertNotIn("gpu_lease", self.request("/v1/status")[1])
        self.assertNotIn("gpu_lease", self.request("/metrics")[1])
        code, answer, _ = self.request("/v1/chat/completions", CHAT)
        self.assertEqual((code, answer["choices"][0]["message"]["content"]), (200, "Hello."))
        self.assertEqual(self.request("/unload", {})[1]["status"], "unloaded")

    def test_the_reason_it_stayed_off_is_said(self):
        self.svc.gpu_lease_off = "the GPU lease is off: no key"
        self.assertEqual(self.acquire()[1]["error"]["message"], "the GPU lease is off: no key")


class Grant(Base):
    def test_acquire_saves_unloads_holds_and_release_restores(self):          # T2
        self.lease_on()
        code, out, _ = self.acquire()
        self.assertEqual(code, 200, out)
        self.assertTrue(out["lease_id"].startswith("gl_"))
        self.assertEqual(out["policy"], "unload")
        self.assertEqual(out["conversation_saved"], {"tokens": 123, "bytes": 4560})
        self.assertEqual(self.engine.sessions[0][0], "save")
        self.assertFalse(self.engine.loaded)
        view = self.request("/v1/gpu/lease")[1]
        self.assertEqual((view["state"], view["holder"], view["policy"]), ("held", "comfyui", "unload"))
        self.assertNotIn(out["lease_id"], json.dumps(view))                      # the id is never shown
        self.assertNotIn(out["lease_id"], json.dumps(self.request("/metrics")[1]))
        self.assertEqual(self.request("/v1/status")[1]["gpu_lease"]["state"], "held")
        code, rel, _ = self.request(f"/v1/gpu/lease/{out['lease_id']}/release", {})
        self.assertEqual((code, rel["status"], rel["reload"]), (200, "released", "started"))
        self.wait_for(lambda: not self.svc.gpu_lease.active(), what="the lease to end")
        self.assertTrue(self.engine.loaded)                                      # eager: started again
        self.assertEqual(self.engine.sessions[-1][0], "restore")
        self.assertEqual(self.engine.sessions[-1][1], self.engine.sessions[0][1])
        self.assertEqual(self.request(f"/v1/gpu/lease/{out['lease_id']}/release", {})[0], 404)   # once only
        self.assertEqual(self.request("/v1/gpu/lease")[1]["last"]["ended"], "released")

    def test_lazy_release_loads_with_the_next_request(self):
        self.lease_on(reload="lazy")
        lid = self.acquire()[1]["lease_id"]
        self.assertEqual(self.request(f"/v1/gpu/lease/{lid}/release", {})[1]["reload"], "lazy")
        self.wait_for(lambda: not self.svc.gpu_lease.active())
        self.assertFalse(self.engine.loaded)
        self.assertEqual(self.request("/v1/chat/completions", CHAT)[0], 200)
        self.assertTrue(self.engine.loaded)
        self.assertEqual(self.engine.sessions[-1][0], "restore")

    def test_a_client_that_leaves_while_waiting_holds_nothing(self):
        mgr = self.lease_on()
        self.engine.gate = threading.Event()
        chat, _ = self.in_thread(self.request, "/v1/chat/completions", CHAT)
        self.assertTrue(self.engine.generating.wait(5))
        with self.assertRaises(Exception):                       # the client gives up (socket timeout) and closes
            self.request("/v1/gpu/lease", {"holder": "gone", "wait_s": 30}, timeout=0.5)
        self.wait_for(lambda: not mgr.active(), 5, "the abandoned lease to go")
        self.engine.gate.set()
        chat.join(10)
        time.sleep(0.3)
        self.assertTrue(self.engine.loaded)                     # nothing was unloaded for it
        self.assertEqual(mgr.totals["leases"], 0)

    def test_a_running_request_finishes_first_and_a_busy_one_gives_409(self):  # T3
        self.lease_on()
        self.engine.gate = threading.Event()
        chat, chat_out = self.in_thread(self.request, "/v1/chat/completions", CHAT)
        self.assertTrue(self.engine.generating.wait(5))
        code, body, headers = self.acquire({"wait_s": 0.5})
        self.assertEqual((code, body["error"]["code"]), (409, "model_busy"))
        self.assertTrue(headers.get("Retry-After"))
        self.assertFalse(self.svc.gpu_lease.active())
        lease, lease_out = self.in_thread(self.acquire, {"wait_s": 10})
        self.wait_for(lambda: self.svc.gpu_lease.pending(), what="the lease to queue")
        time.sleep(0.3)
        self.engine.gate.set()
        lease.join(10)
        chat.join(10)
        self.assertEqual(chat_out["result"][0], 200)
        self.assertEqual(lease_out["result"][0], 200)
        self.assertGreater(lease_out["result"][1]["waited_s"], 0)

    def test_queued_requests_let_a_waiting_lease_go_first_and_run_after(self):  # T4, T9
        self.lease_on()
        self.engine.gate = threading.Event()
        first, first_out = self.in_thread(self.request, "/v1/chat/completions", CHAT)
        self.assertTrue(self.engine.generating.wait(5))
        lease, lease_out = self.in_thread(self.acquire, {"wait_s": 20})
        self.wait_for(lambda: self.svc.gpu_lease.pending(), what="the lease to queue")
        later = [self.in_thread(self.request, "/v1/chat/completions", CHAT) for _ in range(3)]
        time.sleep(0.3)
        self.engine.generating.clear()
        self.engine.gate.set()
        lease.join(10)
        first.join(10)
        self.assertEqual(lease_out["result"][0], 200)
        self.assertFalse(self.engine.loaded)                                     # the lease got the turn
        time.sleep(0.3)
        self.assertTrue(all(t.is_alive() for t, _ in later))                     # they wait behind it
        lid = lease_out["result"][1]["lease_id"]
        self.request(f"/v1/gpu/lease/{lid}/release", {})
        for t, out in later:
            t.join(15)
            self.assertEqual(out["result"][0], 200)
            self.assertEqual(out["result"][1]["choices"][0]["message"]["content"], "Hello.")

    def test_a_streamed_request_gets_keep_alives_while_the_lease_is_held(self):  # T5
        self.lease_on()
        lid = self.acquire()[1]["lease_id"]
        with mock.patch.object(server_mod, "QUEUE_BEAT_S", 0.2):
            req = urllib.request.Request(self.base + "/v1/chat/completions",
                                         data=json.dumps({**CHAT, "stream": True}).encode(),
                                         headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=10)           # the headers come at once: load() did not wait
            self.assertIn("event-stream", resp.headers.get("Content-Type"))
            beats = 0
            while beats < 3:
                line = resp.readline()
                if line.startswith(b": keep-alive"):
                    beats += 1
            self.assertFalse(self.engine.loaded)
            self.request(f"/v1/gpu/lease/{lid}/release", {})
            rest = resp.read().decode()
            resp.close()
        self.assertIn('"content": "H"', rest)
        self.assertIn("data: [DONE]", rest)
        self.assertTrue(self.engine.loaded)


class Expiry(Base):
    def test_expiry_renew_cap_and_unknown_ids(self):                          # T6
        mgr = self.lease_on(ttl_s=0.6, max_s=30)
        with mock.patch.object(lease_api, "TTL_MIN_S", 0.1):
            lid = self.acquire({"ttl_s": 0.6})[1]["lease_id"]
            for _ in range(3):                                   # renewed: it outlives its first ttl
                time.sleep(0.3)
                code, r, _ = self.request(f"/v1/gpu/lease/{lid}/renew", {"ttl_s": 0.6})
                self.assertEqual((code, r["status"]), (200, "renewed"))
            self.assertTrue(mgr.active())
            self.assertEqual(self.request("/v1/gpu/lease/gl_nope/renew", {})[0], 404)
            self.wait_for(lambda: not mgr.active(), 5, "the lease to expire")
        self.assertEqual(mgr.totals["expired"], 1)
        self.assertEqual(mgr.last["ended"], "expired")
        self.assertTrue(self.engine.loaded)                     # taken back and started again
        self.assertEqual(self.request(f"/v1/gpu/lease/{lid}/renew", {})[0], 404)

    def test_the_total_time_is_capped(self):
        mgr = self.lease_on(max_s=0.8)
        with mock.patch.object(lease_api, "TTL_MIN_S", 0.1):
            lid = self.acquire({"ttl_s": 30})[1]["lease_id"]
            r = self.request(f"/v1/gpu/lease/{lid}/renew", {"ttl_s": 30})[1]
            self.assertTrue(r["capped"])
            self.wait_for(lambda: not mgr.active(), 5, "the cap")
        self.assertEqual(mgr.last["ended"], "expired")

    def test_shutdown_ends_the_lease_without_a_restart(self):                 # T17
        mgr = self.lease_on()
        self.acquire()
        starts = self.engine.starts
        mgr.shutdown()
        self.wait_for(lambda: not mgr.active())
        self.assertEqual(self.engine.starts, starts)
        self.assertFalse(self.engine.loaded)


class Queueing(Base):
    def test_a_second_lease_waits_then_gets_it_or_409(self):                  # T7
        mgr = self.lease_on()
        lid = self.acquire()[1]["lease_id"]
        code, body, _ = self.acquire({"holder": "second", "wait_s": 0.4})
        self.assertEqual((code, body["error"]["code"]), (409, "lease_busy"))
        second, out = self.in_thread(self.acquire, {"holder": "second", "wait_s": 10})
        self.wait_for(lambda: len(mgr.leases) == 2, what="the second lease to queue")
        self.assertEqual(self.request("/v1/gpu/lease")[1]["queued_leases"], 1)
        self.request(f"/v1/gpu/lease/{lid}/release", {})
        second.join(10)
        self.assertEqual(out["result"][0], 200)
        self.assertEqual(self.request("/v1/gpu/lease")[1]["holder"], "second")

    def test_control_routes_refuse_while_a_lease_is_active(self):              # T8
        self.lease_on()
        self.acquire()
        for path in ("/unload", "/load", "/v1/unload", "/v1/load", "/v1/vram", "/slots/0?action=save"):
            code, body, _ = self.request(path, {"reserve_mib": 1} if path == "/v1/vram" else {"filename": "a.bin"})
            self.assertEqual((code, body["error"]["code"]), (409, "gpu_leased"), path)

    def test_reject_mode_answers_503_with_retry_after(self):                  # T16
        self.lease_on(queue="reject")
        lid = self.acquire({"ttl_s": 120})[1]["lease_id"]
        code, body, headers = self.request("/v1/chat/completions", CHAT)
        self.assertEqual((code, body["error"]["code"]), (503, "gpu_leased"))
        self.assertGreaterEqual(int(headers["Retry-After"]), 100)
        self.request(f"/v1/gpu/lease/{lid}/release", {})
        self.wait_for(lambda: not self.svc.gpu_lease.active())
        self.assertEqual(self.request("/v1/chat/completions", CHAT)[0], 200)


class Policies(Base):
    def test_parallel_is_refused(self):                                         # T12
        self.lease_on()
        self.engine.batch = 2
        code, body, _ = self.acquire()
        self.assertEqual((code, body["error"]["code"]), (400, "gpu_lease_parallel"))
        self.assertFalse(self.svc.gpu_lease.active())

    def test_a_failed_unload_gives_503_and_frees_the_turn(self):              # T13
        self.lease_on()
        self.engine.unload_error = EngineStuck("Strata is still releasing GPU/RAM")
        code, body, _ = self.acquire()
        self.assertEqual((code, body["error"]["code"]), (503, "lease_make_room_failed"))
        self.assertFalse(self.svc.gpu_lease.active())
        self.assertTrue(self.svc.fifo.acquire(blocking=False))
        self.svc.fifo.release()
        self.engine.unload_error = None
        self.assertEqual(self.request("/v1/chat/completions", CHAT)[0], 200)

    def test_an_eager_start_waits_for_vram_then_falls_back_to_lazy(self):     # T14
        mgr = self.lease_on(reload_wait_s=0.3)
        self.svc.min_free_vram_mib = 8000
        lid = self.acquire()[1]["lease_id"]
        with mock.patch.object(self.svc, "free_vram_mib", return_value=1000):
            self.request(f"/v1/gpu/lease/{lid}/release", {})
            self.wait_for(lambda: not mgr.active())
        self.assertFalse(self.engine.loaded)                     # not started into 1000 MiB
        with mock.patch.object(self.svc, "free_vram_mib", return_value=12000):
            self.assertEqual(self.request("/v1/chat/completions", CHAT)[0], 200)
        self.assertTrue(self.engine.loaded)

    def test_bad_fields(self):
        self.lease_on()
        for body in ({"holder": "x" * 65}, {"holder": "a\nb"}, {"policy": "maybe"}, {"ttl_s": 99999},
                     {"wait_s": -1}, {"reload": "now"}, {"reserve_mib": -5}, {"policy": "shrink"}):
            self.assertEqual(self.acquire(body)[0], 400, body)
        self.assertEqual(self.request("/v1/gpu/lease", ["not", "an", "object"])[0], 400)
        self.assertEqual(self.request("/v1/gpu/lease/x/what", {})[0], 404)
        self.assertEqual(self.request("/v1/gpu/leases", {})[0], 404)
        self.assertEqual(self.request("/v1/gpu/lease/a/b/release", {})[0], 404)
        self.assertFalse(self.svc.gpu_lease.active())

    def test_a_finished_run_leaves_the_turn_free(self):                       # T15 (D4: MCP tools run between rounds)
        self.lease_on()
        ids = self.svc.tok.encode("Hello")
        for _ in self.svc.run(ids, False, None, 4, {}, threading.Event()):
            pass
        self.assertTrue(self.svc.fifo.acquire(blocking=False))   # what run_with_mcp relies on before a tool call
        self.svc.fifo.release()


class Shrink(Base):
    engine_cls = ElasticEngine

    def test_shrink_gives_vram_and_takes_it_back(self):                       # T11
        self.lease_on()
        code, out, _ = self.acquire({"policy": "shrink", "reserve_mib": 9000})
        self.assertEqual((code, out["policy"]), (200, "shrink"), out)
        self.assertEqual(self.engine.vram_calls, [9000])
        self.assertTrue(self.engine.loaded)                      # the conversation stays
        self.request(f"/v1/gpu/lease/{out['lease_id']}/release", {})
        self.wait_for(lambda: not self.svc.gpu_lease.active())
        self.assertEqual(self.engine.vram_calls, [9000, None])

    def test_shrink_needs_an_elastic_engine(self):
        self.engine.info = {}
        self.lease_on()
        self.assertEqual(self.acquire({"policy": "shrink", "reserve_mib": 9000})[0], 400)


class Access(Base):
    def test_api_key_origin_and_content_type(self):                           # T10
        self.lease_on(key=KEY)
        self.svc.api_key = "chat-key"
        self.assertEqual(self.acquire(headers={lease_api.KEY_HEADER: KEY})[0], 401)
        auth = {"Authorization": "Bearer chat-key", lease_api.KEY_HEADER: KEY}
        self.assertEqual(self.acquire(headers={**auth, "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.acquire(headers={**auth, "Content-Type": "text/plain"})[0], 415)
        self.assertFalse(self.svc.gpu_lease.active())
        self.assertEqual(self.acquire(headers=auth)[0], 200)

    def test_the_lease_key(self):                                              # T18
        self.lease_on(key=KEY)
        self.assertEqual(self.acquire()[0], 403)
        self.assertEqual(self.acquire(headers={lease_api.KEY_HEADER: "wrong" * 10})[0], 403)
        self.assertEqual(self.request("/v1/gpu/lease")[0], 403)
        self.assertFalse(self.svc.gpu_lease.active())
        self.assertEqual(self.request("/v1/chat/completions", CHAT)[0], 200)   # chat needs no lease key
        code, out, _ = self.acquire(headers={lease_api.KEY_HEADER: KEY})
        self.assertEqual(code, 200)
        self.assertEqual(self.request(f"/v1/gpu/lease/{out['lease_id']}/release", {})[0], 403)
        self.assertEqual(self.request(f"/v1/gpu/lease/{out['lease_id']}/release", {},
                                      {lease_api.KEY_HEADER: KEY})[0], 200)

    def test_proxy_headers_are_refused(self):                                  # T19
        self.lease_on()
        for h in ("CF-Connecting-IP", "CF-Ray", "X-Forwarded-For", "X-Real-IP", "Forwarded"):
            code, body, _ = self.acquire(headers={h: "203.0.113.9"})
            self.assertEqual((code, body["error"]["code"]), (403, "gpu_lease_forbidden"), h)
        self.svc.gpu_lease.loopback_only = False
        self.assertEqual(self.acquire(headers={"X-Forwarded-For": "203.0.113.9"})[0], 200)

    def test_loopback_peers(self):
        for ip, ok in (("127.0.0.1", True), ("127.1.2.3", True), ("::1", True), ("::ffff:127.0.0.1", True),
                       ("192.168.1.5", False), ("10.0.0.1", False), ("100.64.0.1", False), ("", False)):
            self.assertEqual(lease_api.loopback_peer(ip), ok, ip)


class Config(unittest.TestCase):                                               # T20
    class Svc:
        api_key = ""

    def cfg(self, **kw):
        return {"gpu_lease": True, **kw}

    def test_off_unless_asked(self):
        self.assertEqual(lease_api.from_config(self.Svc(), {}, "127.0.0.1", {}), (None, ""))
        self.assertEqual(lease_api.from_config(self.Svc(), {"gpu_lease": False}, "127.0.0.1", {}), (None, ""))

    def test_local_needs_no_key(self):
        mgr, why = lease_api.from_config(self.Svc(), self.cfg(), "127.0.0.1", {}, log=quiet)
        self.assertIsNotNone(mgr)
        self.assertIsNone(mgr.key)
        self.assertTrue(mgr.loopback_only)
        self.assertEqual((mgr.ttl_s, mgr.max_s, mgr.wait_s, mgr.queue_mode, mgr.reload),
                         (600.0, 14400.0, 900.0, "wait", "eager"))

    def test_shared_without_a_key_stays_off(self):
        for host, key in (("0.0.0.0", ""), ("192.168.1.9", ""), ("127.0.0.1", "chat-key")):
            svc = self.Svc()
            svc.api_key = key
            mgr, why = lease_api.from_config(svc, self.cfg(), host, {})
            self.assertIsNone(mgr, host)
            self.assertIn("needs its own key", why)
        svc = self.Svc()
        svc.api_key = "chat-key"
        mgr, _ = lease_api.from_config(svc, self.cfg(gpu_lease_key_env="LK"), "0.0.0.0", {"LK": KEY}, log=quiet)
        self.assertEqual(mgr.key, KEY)

    def test_bad_keys_keep_it_off(self):
        d = tempfile.mkdtemp()
        short = Path(d, "short.env")
        short.write_text("STRATA_LEASE_KEY=abc\n")
        empty = Path(d, "empty.env")
        empty.write_text("# nothing\n")
        for cfg, env in ((self.cfg(gpu_lease_key_file=str(short)), {}),
                         (self.cfg(gpu_lease_key_file=str(empty)), {}),
                         (self.cfg(gpu_lease_key_file=str(Path(d, "missing.env"))), {}),
                         (self.cfg(gpu_lease_key_env="LK"), {})):
            mgr, why = lease_api.from_config(self.Svc(), cfg, "127.0.0.1", env)
            self.assertIsNone(mgr, cfg)
            self.assertTrue(why)
            self.assertNotIn("abc", why)

    def test_wrong_types_stop_the_start(self):
        for cfg in ({"gpu_lease": "yes"}, self.cfg(gpu_lease_ttl_s="10"), self.cfg(gpu_lease_ttl_s=99999),
                    self.cfg(gpu_lease_queue="drop"), self.cfg(gpu_lease_reload="soon"),
                    self.cfg(gpu_lease_loopback_only="no"), self.cfg(gpu_lease_wait_s=True)):
            with self.assertRaises(SystemExit, msg=cfg):
                lease_api.from_config(self.Svc(), cfg, "127.0.0.1", {})


class Keys(unittest.TestCase):                                                 # T21
    def test_parse_env_matches_character_refs(self):
        # the cases of shuohao-skills character-refs' selftest (parseEnv) and the line forms around them
        self.assertEqual(lease_api.parse_env('# x\nCOMFY_URL=http://h:1/\nCOMFY_PASS="p w"\n')["COMFY_PASS"], "p w")
        env = lease_api.parse_env("A=1\r\n  B = two \r\n#C=3\nD='q'\n=bad\n9X=1\nE=\n")
        self.assertEqual(env, {"A": "1", "B": "two ", "D": "q", "E": ""})

    def test_env_first_then_file_and_the_key_never_printed(self):
        d = tempfile.mkdtemp()
        f = Path(d, "lease.env")
        f.write_text(f"# the lease key\nSTRATA_LEASE_KEY={KEY}\r\nSTRATA_API_KEY=chat\n")
        if os.name != "nt":
            os.chmod(f, 0o600)
        key, source = lease_api.read_key(str(f), None, {})
        self.assertEqual(key, KEY)
        self.assertNotIn(KEY, source)
        other = "e" * 40
        self.assertEqual(lease_api.read_key(str(f), "LK", {"LK": other})[0], other)
        self.assertEqual(lease_api.read_key(str(f), "LK", {})[0], KEY)            # the variable unset: the file
        self.assertEqual(lease_api.read_key("lease.env", None, {}, base=d)[0], KEY)   # relative to the config
        self.assertEqual(lease_api.read_key(None, None, {}), (None, ""))
        printed = []
        mgr, _ = lease_api.from_config(type("S", (), {"api_key": ""})(), {"gpu_lease": True, "gpu_lease_key_file": str(f)},
                                       "127.0.0.1", {}, log=lambda *a, **k: printed.append(a))
        self.assertEqual(mgr.key, KEY)
        self.assertNotIn(KEY, repr(printed) + mgr.key_source)

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_a_file_others_can_read_is_noted(self):
        d = tempfile.mkdtemp()
        f = Path(d, "lease.env")
        f.write_text(f"STRATA_LEASE_KEY={KEY}\n")
        os.chmod(f, 0o644)
        self.assertIn("chmod 600", lease_api.read_key(str(f), None, {})[1])

    def test_key_matches(self):
        self.assertTrue(lease_api.key_matches(f" {KEY} ", KEY))
        self.assertFalse(lease_api.key_matches("", KEY))
        self.assertFalse(lease_api.key_matches(KEY[:-1], KEY))


if __name__ == "__main__":
    unittest.main()
