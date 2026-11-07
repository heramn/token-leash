"""Token Leash: a live token meter + kill switch between any agent and the Belweave API.

Terminal 1 (PowerShell):
    $env:BW_KEY="bw-..."                      # used only if the client sends no key
    $env:NOTIFY_WEBHOOK="https://notify.belweave.ai/hooks/whk_..."
    $env:BUDGET_TOKENS="2000"                 # small for the demo
    python leash.py

Terminal 2: point your agent at http://127.0.0.1:8787/v1 (same key, same model), or run the demo:
    $env:BW_KEY="bw-..."
    python leash.py burn
"""
import collections
import hashlib
import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8787"))
UP_HOST = os.environ.get("UPSTREAM_HOST", "api.infer.belweave.ai")
UP_SCHEME = os.environ.get("UPSTREAM_SCHEME", "https")
BUDGET = int(os.environ.get("BUDGET_TOKENS", "20000"))
EXTEND = int(os.environ.get("EXTEND_TOKENS", "20000"))
MAX_RPM = int(os.environ.get("MAX_RPM", "55"))
LOOP_REPEATS = int(os.environ.get("LOOP_REPEATS", "4"))
ASK_SECONDS = int(os.environ.get("ASK_SECONDS", "300"))
HOOK = os.environ.get("NOTIFY_WEBHOOK", "").rstrip("/")


# ---------- Notify ----------
def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "Idempotency-Key": str(uuid.uuid4())},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:300]}") from None


def push(body, title="Token Leash"):
    try:
        call("POST", HOOK, {"title": title, "body": body[:2000]})
    except Exception as e:
        print("push failed:", e)


def live_start(title):
    try:
        # key + replace: each run ends its own previous card instead of leaking one (device cap is 5)
        r = call("POST", f"{HOOK}/live-activities",
                 {"title": title, "status": "Starting", "progress": 0, "key": "token-leash", "replace": True})
        return r.get("activityId") or r.get("id")
    except Exception as e:
        print("live start failed:", e)


def live_update(aid, status, progress):
    if not aid:
        return
    try:
        call("PATCH", f"{HOOK}/live-activities/{aid}", {"status": status[:60], "progress": round(max(0.0, min(progress, 0.99)), 2)})
    except Exception as e:
        print("live update failed:", e)


def live_end(aid, status):
    if not aid:
        return
    try:
        call("POST", f"{HOOK}/live-activities/{aid}/end", {"status": status[:60], "progress": 1})
    except Exception as e:
        print("live end failed:", e)


def ask_yes_no(text):
    """True only on an explicit yes. Expiry, no answer, or errors all mean False."""
    try:
        ev = call("POST", HOOK, {
            "title": "Token Leash",
            "body": text[:400],
            "response": {"type": "yes_no", "expiresInSeconds": ASK_SECONDS, "correlationId": str(uuid.uuid4())},
        })
        eid = ev["eventId"]
        end = time.time() + ASK_SECONDS + 3
        while time.time() < end:
            time.sleep(1)
            s = call("GET", f"{HOOK}/events/{eid}")
            resp = (s.get("event") or {}).get("response") or {}
            vals = {str(resp.get("status") or "").lower(), str(resp.get("action") or "").lower()}
            if vals & {"yes", "approved", "approve"}:
                return True
            if vals & {"no", "denied", "deny", "expired", "canceled"}:
                return False
    except Exception as e:
        print("ask failed (treating as NO):", e)
    return False


# ---------- State ----------
lock = threading.Lock()
ask_lock = threading.Lock()
state = {"used": 0, "budget": BUDGET, "paused": False, "reqs": 0}
recent_sigs = collections.deque(maxlen=8)
req_times = collections.deque()
MAX_INFLIGHT = int(os.environ.get("MAX_INFLIGHT", "2"))  # backend rejects >2 in flight with 429
inflight = threading.BoundedSemaphore(MAX_INFLIGHT)
activity = {"id": None}


tok_times = collections.deque()
dirty = threading.Event()


def add_tokens(n):
    dirty.set()
    with lock:
        state["used"] += max(0, int(n))
        state["reqs"] += 1
        tok_times.append((time.time(), max(0, int(n))))
        print(f"  tokens used: {state['used']:,} / {state['budget']:,}")


def live_add(n):
    """Add tokens mid-stream so the meter ticks up while the model is still writing."""
    n = int(n)
    if n == 0:
        return
    with lock:
        state["used"] += n
        if n > 0:
            tok_times.append((time.time(), n))
    dirty.set()


def tpm():
    """Tokens burned in the last 60 seconds."""
    now = time.time()
    with lock:
        while tok_times and now - tok_times[0][0] > 60:
            tok_times.popleft()
        return sum(t for _, t in tok_times)


def rpm():
    now = time.time()
    with lock:
        while req_times and now - req_times[0] > 60:
            req_times.popleft()
        return len(req_times)


def rate_wait():
    """Queue instead of letting the client hit 429s."""
    while True:
        now = time.time()
        with lock:
            while req_times and now - req_times[0] > 60:
                req_times.popleft()
            if len(req_times) < MAX_RPM:
                req_times.append(now)
                return
            wait = 60 - (now - req_times[0]) + 0.05
        time.sleep(max(wait, 0.1))


def gate(sig):
    """Return None to allow, or an error string to reject."""
    with ask_lock:
        with lock:
            over = state["used"] >= state["budget"]
            used, budget = state["used"], state["budget"]
        if over:
            print(f"BUDGET HIT {used}/{budget}. Asking phone...")
            state["paused"] = True
            live_update(activity["id"], "PAUSED: need your OK", 0.99)
            ok = ask_yes_no(f"Budget hit: {fmt(used)} of {fmt(budget)} tokens used. Extend by {fmt(EXTEND)}?")
            state["paused"] = False
            if not ok:
                push("Agent stays paused. Budget not extended.")
                return "token budget exhausted; not extended"
            with lock:
                state["budget"] += EXTEND
            print(f"  extended to {state['budget']:,}")
        if sig:
            recent_sigs.append(sig)
            if list(recent_sigs).count(sig) >= LOOP_REPEATS:
                print("LOOP SUSPECTED. Asking phone...")
                state["paused"] = True
                live_update(activity["id"], "PAUSED: possible loop", 0.99)
                ok = ask_yes_no(f"Agent sent the same request {LOOP_REPEATS} times. Looks stuck. Keep going?")
                state["paused"] = False
                recent_sigs.clear()
                if not ok:
                    push("Stopped a suspected loop.")
                    return "suspected loop; stopped by user"
    return None


def fmt(n):
    """1234 -> 1.2k, 2_500_000 -> 2.5M, 3e9 -> 3B"""
    n = float(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= div:
            v = n / div
            s = f"{v:.1f}" if v < 100 else f"{v:.0f}"
            if "." in s:
                s = s.rstrip("0").rstrip(".")
            return s + suf
    return str(int(n))


def meter_loop():
    last = 0.0
    while True:
        dirty.wait(timeout=2)  # wake on every token update, or every 2s
        dirty.clear()
        gap = time.time() - last
        min_gap = float(os.environ.get("LIVE_GAP", "2"))  # iOS throttles Live Activities updated too often
        if gap < min_gap:
            time.sleep(min_gap - gap)
        last = time.time()
        with lock:
            used, budget, paused = state["used"], state["budget"], state["paused"]
        if paused:
            continue
        if not activity["id"]:
            # Live Activity unavailable (device cap): fall back to a push every 20% of the budget
            b = int(used * 5 / max(budget, 1))
            if state.get("bud_seen") != budget:
                state["bud_seen"], state["bucket"] = budget, b
            elif b > state.get("bucket", 0):
                state["bucket"] = b
                push(
                    f"{fmt(used)}/{fmt(budget)} tokens ({int(used * 100 / max(budget, 1))}%) · ~{fmt(tpm())} tok/min",
                    title="Token Leash meter",
                )
            continue
        live_update(
            activity["id"],
            f"{fmt(used)}/{fmt(budget)} tok · {rpm()} req/min · ~{fmt(tpm())} tok/min",
            used / max(budget, 1),
        )


# ---------- Proxy ----------
HOP = {"transfer-encoding", "connection", "content-length", "keep-alive", "content-encoding"}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def send_json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self.proxy()

    def do_POST(self):
        self.proxy()

    def proxy(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        is_chat = self.command == "POST" and self.path.split("?")[0].endswith("/chat/completions")
        stream = False
        if is_chat:
            sig = None
            try:
                j = json.loads(body)
                stream = bool(j.get("stream"))
                sig = hashlib.md5((json.dumps((j.get("messages") or [])[-1:], sort_keys=True) + str(j.get("model"))).encode()).hexdigest()
            except Exception:
                pass
            err = gate(sig)
            if err:
                return self.send_json(429, {"error": {"message": "Token Leash: " + err, "type": "budget_exceeded"}})
            rate_wait()
            if os.environ.get("MOCK") == "1":
                # SIMULATED backend for demo/testing without an API key. Not real model output.
                time.sleep(0.8)
                toks = 80
                add_tokens(toks)
                return self.send_json(200, {
                    "id": "mock-" + uuid.uuid4().hex[:8], "object": "chat.completion", "model": "mock",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "[simulated response]"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 60, "total_tokens": toks}})

        headers = {"Accept-Encoding": "identity"}
        auth = self.headers.get("Authorization") or ("Bearer " + os.environ.get("BW_KEY", ""))
        headers["Authorization"] = auth
        if self.headers.get("Content-Type"):
            headers["Content-Type"] = self.headers["Content-Type"]

        if is_chat:
            inflight.acquire()
        try:
            C = http.client.HTTPSConnection if UP_SCHEME == "https" else http.client.HTTPConnection
            conn = C(UP_HOST, timeout=300)
            conn.request(self.command, self.path, body=body if body else None, headers=headers)
            resp = conn.getresponse()
            pre_body = None
            # backend says "too many in flight" -> wait and retry instead of failing the agent
            for _attempt in range(10):
                if resp.status != 429:
                    break
                pre_body = resp.read()
                if b"in_flight" not in pre_body and b"in flight" not in pre_body:
                    break
                print("  upstream busy (2 in flight), retrying...")
                conn.close()
                time.sleep(1.5)
                conn = C(UP_HOST, timeout=300)
                conn.request(self.command, self.path, body=body if body else None, headers=headers)
                resp = conn.getresponse()
                pre_body = None
            if stream and resp.status == 200:
                self.send_response(200)
                self.send_header("Content-Type", resp.getheader("Content-Type", "text/event-stream"))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                exact, chars = None, 0
                pre = len(body) // 4
                live_add(pre)
                counted = pre
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
                    if line.startswith(b"data:") and b"[DONE]" not in line:
                        try:
                            o = json.loads(line[5:].strip())
                            if o.get("usage") and o["usage"].get("total_tokens"):
                                exact = o["usage"]["total_tokens"]
                            for ch in o.get("choices") or []:
                                chars += len((ch.get("delta") or {}).get("content") or "")
                        except Exception:
                            pass
                        target = pre + chars // 4
                        if target > counted:
                            live_add(target - counted)
                            counted = target
                final = exact if exact is not None else pre + chars // 4
                live_add(final - counted)
                with lock:
                    state["reqs"] += 1
                    print(f"  tokens used: {state['used']:,} / {state['budget']:,}")
            else:
                data = pre_body if pre_body is not None else resp.read()
                total = None
                if is_chat and resp.status == 200:
                    try:
                        total = json.loads(data).get("usage", {}).get("total_tokens")
                    except Exception:
                        pass
                    add_tokens(total if total is not None else (len(data) + len(body)) // 4)
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in HOP:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            try:
                self.send_json(502, {"error": {"message": f"Token Leash upstream error: {e}"}})
            except Exception:
                pass
        finally:
            if is_chat:
                inflight.release()


# ---------- Demo client ----------
def burn():
    from openai import OpenAI
    c = OpenAI(base_url=f"http://127.0.0.1:{PORT}/v1", api_key=os.environ["BW_KEY"])
    topics = ["tides", "volcanoes", "bees", "jazz", "glaciers", "coffee", "chess", "satellites",
              "octopuses", "bridges", "monsoons", "origami", "lighthouses", "compilers", "fungi"]
    from concurrent.futures import ThreadPoolExecutor

    stop = threading.Event()

    def one(job):
        i, t = job
        if stop.is_set():
            return
        try:
            kw = dict(
                model=os.environ.get("MODEL", "Belweave/Kai-4"),
                messages=[{"role": "user", "content": f"Write about 120 words on {t}."}],
                max_tokens=int(os.environ.get("MAXTOK", "300")),
            )
            if os.environ.get("STREAM", "1") == "0":
                r = c.chat.completions.create(**kw)
                print(f"#{i} ok ({r.usage.total_tokens if r.usage else '?'} tokens)")
            else:
                n = 0
                for _chunk in c.chat.completions.create(stream=True, **kw):
                    n += 1
                print(f"#{i} ok (streamed {n} chunks)")
        except Exception as e:
            print(f"#{i} BLOCKED: {e}")
            stop.set()

    # several requests in flight at once = the meter climbs continuously (WORKERS=1 for sequential)
    workers = int(os.environ.get("WORKERS", "2"))
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, list(enumerate(topics * 3, 1))))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "burn":
        os.environ.setdefault("STREAM", "0")  # backend rejects streaming for this model
        burn()
        sys.exit(0)
    if not HOOK:
        sys.exit("Set NOTIFY_WEBHOOK first")
    activity["id"] = live_start("Token Leash")
    threading.Thread(target=meter_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.daemon_threads = True
    print(f"Token Leash on http://127.0.0.1:{PORT}/v1  (budget {BUDGET:,} tokens, extend {EXTEND:,})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        live_end(activity["id"], f"Stopped: {state['used']:,} tokens")
        print("stopped.")
