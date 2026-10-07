"""The machinery of the mutation tests (`tests/txn_mutants.py`, `tests/string_mutants.py`): each mutant is a deliberately wrong cache, and the
harnesses must fail on it.

Each mutant changes one place in a copy of `src/`, builds it, and runs the harness meant to catch it: `("diff", substring)` is
`tests/differential.py` on the cases whose names contain the substring, `("two",)` is `tests/transactions.py`, `("py", name)` another script of `tests/`, `("unit",)` is the store's unit
tests against the changed `store.cho` and `("globunit",)` the matcher's against the changed `glob.cho`. A mutant the harness does not fail is a hole in the harness, and the run exits 1.
"""
import os, shutil, subprocess, sys, tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CANCHO = os.environ.get("CANCHO")
FILES = ["cache", "resp", "store", "commands", "reply", "session", "txn", "glob"]


def run(args, env=None, timeout=600):
    try:
        return subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        # A harness that hangs on a wrong cache has not passed it.
        return subprocess.CompletedProcess(args, 124, "", "timed out")


def run_mutants(mutants, only=""):
    if not CANCHO:
        raise SystemExit("set CANCHO to the compiler")
    holes = []
    for name, fname, old, new, how in mutants:
        if only not in name:
            continue
        tmp = tempfile.mkdtemp(prefix="mutant-")
        try:
            shutil.copytree(os.path.join(ROOT, "src"), os.path.join(tmp, "src"))
            path = os.path.join(tmp, "src", fname)
            text = open(path).read()
            if text.count(old) != 1:
                print("BROKEN mutant (its text is not in %s exactly once): %s" % (fname, name))
                holes.append(name)
                continue
            open(path, "w").write(text.replace(old, new))
            binary = os.path.join(tmp, "cache")
            built = run([CANCHO, "build", "--std"] + [os.path.join(tmp, "src", f + ".cho") for f in FILES] + ["-o", binary])
            if built.returncode != 0:
                print("BROKEN mutant (does not build): %s\n%s" % (name, built.stderr[-300:]))
                holes.append(name)
                continue
            if how[0] == "diff":
                result = run([sys.executable, os.path.join(ROOT, "tests", "differential.py"), binary], env={**os.environ, "DIFF_ONLY": how[1]})
            elif how[0] == "globunit":
                result = run([CANCHO, "test", os.path.join(ROOT, "tests", "glob_test.cho"), os.path.join(tmp, "src", "glob.cho"), "--std"])
            elif how[0] == "py":
                result = run([sys.executable, os.path.join(ROOT, "tests", how[1]), binary])
            elif how[0] == "unit":
                result = run([CANCHO, "test", os.path.join(ROOT, "tests", "store_test.cho"), os.path.join(tmp, "src", "store.cho"), "--std"])
            else:
                result = run([sys.executable, os.path.join(ROOT, "tests", "transactions.py"), binary])
            if result.returncode == 0:
                print("SURVIVED  %s" % name)
                holes.append(name)
            else:
                print("caught    %s" % name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if holes:
        print("%d mutant(s) not caught" % len(holes))
        sys.exit(1)
    print("every mutant was caught")
