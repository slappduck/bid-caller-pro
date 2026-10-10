"""Monthly refresh of every state's going rates and bid results.

Runs each state's builder on its own (tools/build_state_prices.py and
tools/build_bid_results.py), so one DOT that changed its site or is down
never stops the rest. After each, the files it wrote are compared with the
committed ones, and last month's are put back when the new ones look worse:

  - the builder failed;
  - a rates file lost a year it had, or kept prices on far fewer items;
  - a results file came back with far fewer contracts, or an older newest
    letting.

Shrinking is how a parser broken by a layout change usually shows (a few
rows still match, most don't), and serving last month's good numbers beats
serving this month's half-read ones. Whatever was kept is listed in the
summary, so it gets looked at instead of silently going stale.

    python3 tools/refresh_state_data.py --summary refresh.md

Exits 1 if any state was kept back, after writing everything that passed.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)
SITE = os.path.join(ROOT, "curbcall_netlify_v4")
sys.path.insert(0, TOOLS)
import build_state_prices  # noqa: E402

# States whose results builder also writes their rates (from the bid tabs).
RESULTS_WITH_RATES = ("nc", "tx", "ky", "ks", "ia", "il")
RESULTS_ONLY = ("mo", "or")
MIN_ITEMS_KEPT = 0.7       # of the items that had prices
MIN_CONTRACTS_KEPT = 0.6   # of the contracts (a rolling window, so it moves)
TIMEOUT = 45 * 60


def jobs():
    """(name, command, files it writes) for every state, rates first."""
    out = []
    for st in build_state_prices.BUILDERS:
        out.append((f"{st.upper()} rates", ["build_state_prices.py", "--states", st],
                    [f"rates/{st}.json"]))
    for st in RESULTS_ONLY + RESULTS_WITH_RATES:
        files = [f"results/{st}.json"] + ([f"rates/{st}.json"] if st in RESULTS_WITH_RATES else [])
        out.append((f"{st.upper()} results", ["build_bid_results.py", "--states", st], files))
    return out


def committed(rel):
    """The file as committed at HEAD, or None if it isn't."""
    r = subprocess.run(["git", "show", f"HEAD:curbcall_netlify_v4/{rel}"], cwd=ROOT,
                       capture_output=True)
    return r.stdout if r.returncode == 0 else None


def load(raw):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def rates_shape(d):
    years = set(d.get("years") or [])
    priced = {c for c, by_d in (d.get("prices") or {}).items() if by_d}
    return years, priced


def worse(rel, old_raw, new_raw):
    """Why the new file is worse than the committed one, or None."""
    old, new = load(old_raw), load(new_raw)
    if new is None:
        return "not readable"
    if old is None:
        return None                     # a state added since the last commit
    if rel.startswith("rates/"):
        (oy, oi), (ny, ni) = rates_shape(old), rates_shape(new)
        if oy and ny and max(ny) < max(oy):
            return f"newest year went back from {max(oy)} to {max(ny)}"
        if len(ni) < MIN_ITEMS_KEPT * len(oi):
            return f"prices on {len(ni)} items, down from {len(oi)}"
        return None
    oc, nc = old.get("contracts") or [], new.get("contracts") or []
    if len(nc) < MIN_CONTRACTS_KEPT * len(oc):
        return f"{len(nc)} contracts, down from {len(oc)}"
    newest = lambda cs: max((c.get("date") or "" for c in cs), default="")
    if newest(nc) < newest(oc):
        return f"newest letting went back from {newest(oc)} to {newest(nc)}"
    return None


def describe(rel, old_raw, new_raw):
    old, new = load(old_raw), load(new_raw)
    if rel.startswith("rates/"):
        ys = new.get("years") or []
        span = f"{min(ys)}–{max(ys)}" if ys else "no years"
        if old and old.get("years") and max(old["years"]) < max(ys):
            span += f" (adds {', '.join(str(y) for y in ys if y > max(old['years']))})"
        return f"rates {span}"
    n = len(new.get("contracts") or [])
    was = len(old.get("contracts") or []) if old else 0
    return f"results {n} contracts" + (f" (was {was})" if old and was != n else "")


def restore(rel, old_raw):
    path = os.path.join(SITE, rel)
    if old_raw is None:
        if os.path.exists(path):
            os.remove(path)
    else:
        with open(path, "wb") as f:
            f.write(old_raw)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--summary", help="write a Markdown summary here")
    ap.add_argument("--only", help="comma-separated job names, e.g. 'IL results,AR rates'")
    ap.add_argument("--cache", default=None)
    args = ap.parse_args()
    cache = args.cache or tempfile.mkdtemp(prefix="state_refresh_")
    only = {x.strip().upper() for x in args.only.split(",")} if args.only else None
    rows, kept = [], []
    for name, cmd, files in jobs():
        if only and name.upper() not in only:
            continue
        before = {rel: committed(rel) for rel in files}
        print(f"── {name}", flush=True)
        try:
            r = subprocess.run([sys.executable, os.path.join(TOOLS, cmd[0]), *cmd[1:], "--cache", cache],
                               cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT)
            log, failed = (r.stdout + r.stderr).strip(), r.returncode != 0
        except subprocess.TimeoutExpired:
            log, failed = f"timed out after {TIMEOUT // 60} min", True
        print(log[-3000:], flush=True)
        notes = []
        for rel in files:
            path = os.path.join(SITE, rel)
            new_raw = open(path, "rb").read() if os.path.exists(path) else None
            why = "the build failed" if failed else worse(rel, before[rel], new_raw)
            if why:
                restore(rel, before[rel])
                kept.append(f"{name}: {why}")
                notes.append(f"kept last month's {rel.split('/')[0]} ({why})")
            elif new_raw == before[rel]:
                notes.append("no change")
            else:
                notes.append(describe(rel, before[rel], new_raw))
        if failed:
            tail = [x for x in log.splitlines() if x.strip()][-1:] or ["no output"]
            notes.append(f"last line: `{tail[0][:160]}`")
        rows.append((name, "; ".join(dict.fromkeys(notes))))
    build_state_prices.write_index()
    # The public price pages are built from the same files, so they follow them.
    import build_cost_pages
    build_cost_pages.main()
    lines = ["| Source | This month |", "|---|---|"] + [f"| {n} | {t} |" for n, t in rows]
    if kept:
        lines += ["", "**Kept last month's data** (look at these; the source may have changed):", ""]
        lines += [f"- {k}" for k in kept]
    text = "\n".join(lines) + "\n"
    print(text)
    if args.summary:
        with open(args.summary, "w", encoding="utf-8") as f:
            f.write(text)
    return 1 if kept else 0


if __name__ == "__main__":
    sys.exit(main())
