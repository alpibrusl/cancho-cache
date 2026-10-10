#!/usr/bin/env python3
"""The client compatibility matrix (issue #13, slice 1).

    python3 tests/clients_matrix.py [path/to/cache]

Runs each client's session battery against a fresh server and writes the result of every client that
was present, as the Markdown table the README shows, to stdout. A client whose runtime is not
installed is listed as "not run" here and reported as skipped by tests/session.py; this harness only
records what actually ran, so a table cannot claim a client that CI never executed.

Exit status is 0 even if a client fails: the harness is the record, and the per-client batteries
inside tests/session.py are the gate. CI compares this table to the README's and fails if the README
claims something the run did not produce.
"""

import os
import subprocess
import sys
import time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 6480


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def run_client(name, run):
    """run() starts its own server on port PORT and returns (status, detail)."""
    env = dict(os.environ)
    proc = subprocess.Popen([CACHE, str(PORT), "64", "100000", "allkeys-lru"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    import socket
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", PORT), 0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        return run()
    finally:
        proc.terminate()


def node_battery(script):
    def run():
        node = None
        import shutil
        node = shutil.which("node")
        if node is None:
            return ("not run", "node is not installed")
        r = subprocess.run([node, os.path.join(HERE, "clients", script), str(PORT)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return ("pass", "")
        return ("fail", (r.stdout + r.stderr).strip().splitlines()[-1] if (r.stdout + r.stderr).strip() else "exit %d" % r.returncode)
    return run


def redis_py_battery():
    def run():
        try:
            import redis
        except ImportError:
            return ("not run", "pip install redis")
        for proto in (2, 3, None):
            r = redis.Redis(port=PORT, decode_responses=True) if proto is None else redis.Redis(port=PORT, decode_responses=True, protocol=proto)
            tag = "redis-py RESP2" if proto == 2 else ("redis-py RESP3" if proto == 3 else "redis-py (default)")
            try:
                ok = (r.ping(), r.set("m:a", "1"), r.get("m:a"), r.incr("m:a")) == (True, True, "1", 2)
                pl = r.pipeline(transaction=False)
                pl.set("m:b", "x").mget("m:a", "m:b").expire("m:b", 100)
                ok = ok and pl.execute() == [True, ["2", "x"], True]
                r.close()
            except Exception as e:
                return ("fail", "%s: %s" % (tag, e))
            if not ok:
                return ("fail", tag)
        return ("pass", "")
    return run


def redis_rs_battery():
    def run():
        import shutil
        bin_path = os.path.join(HERE, "clients", "redis-rs", "target", "release", "redis-rs-session")
        if not os.path.isfile(bin_path):
            return ("not run", "cargo build --release in tests/clients/redis-rs")
        r = subprocess.run([bin_path, str(PORT)], capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return ("pass", "")
        out = (r.stdout + r.stderr).strip().splitlines()
        return ("fail", out[-1] if out else "exit %d" % r.returncode)
    return run


CLIENTS = [
    ("redis-py (pip install redis; RESP2, RESP3, default)", redis_py_battery()),
    ("ioredis 5, node (npm install ioredis in tests/clients)", node_battery("ioredis_session.js")),
    ("node-redis 4, node (npm install redis in tests/clients)", node_battery("node_redis_session.js")),
    ("redis-rs 1.7, rust (cargo build --release in tests/clients/redis-rs)", redis_rs_battery()),
]


def main():
    rows = []
    for name, battery in CLIENTS:
        status, detail = run_client(name, battery)
        rows.append((name, status, detail))
    print("| Client | Result | Notes |")
    print("|---|---|---|")
    for name, status, detail in rows:
        print("| %s | %s | %s |" % (name, status, detail.replace("|", "\\|")))

    # The README's matrix section must agree with what this run produced: a row this harness
    # generated may not be missing, and the README may not widen a row to a better result.
    readme = os.path.join(os.path.dirname(HERE), "README.md")
    if os.path.exists(readme):
        text = open(readme).read()
        bad = []
        for name, status, detail in rows:
            if ("| %s | pass |" % name) not in text:
                bad.append("README matrix does not record this run's pass: %s" % name)
            if status != "pass" and ("| %s | pass |" % name) in text:
                bad.append("README claims a pass this run did not produce: %s (%s)" % (name, status))
        if bad:
            print("\n".join(bad), file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    PORT = free_port()
    main()
