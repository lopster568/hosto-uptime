#!/usr/bin/env python3
"""
hosto-uptime — external, $0 uptime + content watchdog for the Hosto customer sites.

Successor to hosto-infra/scripts/failover-monitor.sh, which ran ON the VPS and so died
WITH it (2026-08-04 outage went silent). This runs on GitHub Actions — outside pve-2,
the VPS, and Heroku — so it survives any single piece of our infra dying. Alerts go to
the existing Telegram group via the Bot API (no paid SaaS tier, no webhook bridge).

Design carried over verbatim from failover-monitor.sh, now PER-TARGET:
  - Silence == healthy. A Telegram message only ever means a state change or a live outage.
  - In-run blip filter: PROBES attempts PROBE_GAP seconds apart; any success = up.
  - Debounce: FAIL_THRESHOLD consecutive failed RUNS before the first DOWN alert (so a
    single flaky GitHub runner can't page you).
  - Re-alert every RENOTIFY_SECONDS while a target stays down (an outage must not scroll away).
  - Recovery notice carries the downtime duration.
  - Loud send failures: a non-200 Telegram response does NOT advance alert state (so the
    next run retries instead of suppressing), and makes the whole run exit non-zero — which
    turns the Action red and (if you enable it) emails you: a second, independent alert path.
  - Cross-run memory in state.json; the workflow commits it back ONLY when it changes, so a
    healthy platform produces zero commits and zero noise.

Improvements over the original: many public targets instead of one internal TCP port, an
optional content keyword per target (catches a 200 that serves the wrong/broken page), and a
single batched message per run instead of N separate pings when everything drops at once.

stdlib only — no pip install, so runs start instantly.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor

STATE_PATH = os.environ.get("STATE_PATH", "state.json")
TARGETS_PATH = os.environ.get("TARGETS_PATH", "targets.json")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID")

# Runs are ~10 min apart (GitHub cron, best-effort). Two consecutive failed runs before we
# page → a single bad runner recovers on the next run and stays silent; a real outage persists.
FAIL_THRESHOLD = int(os.environ.get("FAIL_THRESHOLD", "2"))
RENOTIFY_SECONDS = int(os.environ.get("RENOTIFY_SECONDS", "1800"))  # re-alert every 30 min while down
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "10"))
PROBES = int(os.environ.get("PROBES", "3"))          # in-run attempts to filter a momentary blip
PROBE_GAP = float(os.environ.get("PROBE_GAP", "3"))  # seconds between in-run attempts

UA = "hosto-uptime/1 (+github-actions)"


def now() -> int:
    return int(time.time())


def fmt_duration(s: int) -> str:
    s = max(0, int(s))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    out = ""
    if h:
        out += f"{h}h "
    if m:
        out += f"{m}m "
    return out + f"{s}s"


def probe_once(url: str, keyword):
    """Return (ok, detail). Up = 2xx/3xx/401/403 AND keyword present (if set)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            code = r.status
            body = r.read(65536).decode("utf-8", "replace") if keyword else ""
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            body = e.read(65536).decode("utf-8", "replace") if keyword else ""
        except Exception:
            body = ""
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"
    # A responding site (incl. auth-gated) is "up"; 5xx / errors are down.
    if code >= 500:
        return False, f"HTTP {code}"
    if keyword and keyword not in body:
        return False, f"HTTP {code} but missing keyword '{keyword}'"
    return True, f"HTTP {code}"


def probe(url: str, keyword):
    """In-run blip filter: any of PROBES attempts succeeding = up."""
    detail = ""
    for i in range(PROBES):
        ok, detail = probe_once(url, keyword)
        if ok:
            return True, detail
        if i < PROBES - 1:
            time.sleep(PROBE_GAP)
    return False, detail


def send_telegram(text: str) -> bool:
    """Plain text (no parse_mode) so a stray char can't make Telegram reject the one
    message that matters. Returns True only on HTTP 200."""
    if not TG_TOKEN or not TG_CHAT:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return False
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            if r.status == 200:
                return True
            print(f"ERROR: telegram http={r.status}", file=sys.stderr)
            return False
    except urllib.error.HTTPError as e:
        print(f"ERROR: telegram http={e.code} body={e.read()[:200]!r}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"ERROR: telegram send failed: {e}", file=sys.stderr)
        return False


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def main() -> int:
    targets = load_json(TARGETS_PATH, [])
    if not targets:
        print(f"no targets in {TARGETS_PATH}", file=sys.stderr)
        return 2
    state = load_json(STATE_PATH, {})
    before = json.dumps(state, sort_keys=True)
    t = now()

    # Vantage sanity: if the runner itself can't reach the open internet, EVERY probe would look
    # "down" and we'd falsely cry PLATFORM DOWN. Require a control host first; bail quietly if not.
    if not any(probe_once(u, None)[0] for u in
               ("https://www.google.com/generate_204", "https://api.github.com")):
        print("monitor vantage has no network — skipping run (no reading, no false alarms)")
        return 0

    # Probe every target concurrently so one run doesn't take sum(timeouts) when several
    # targets are down — it takes about as long as the single slowest target.
    with ThreadPoolExecutor(max_workers=min(16, len(targets))) as ex:
        probed = dict(ex.map(
            lambda tg: (tg["name"], probe(tg["url"], tg.get("keyword"))), targets))

    # This run's up/down per (site, role), so a down apex can say whether its www still serves.
    up_now = {(tg.get("site"), tg.get("role")): probed[tg["name"]][0] for tg in targets}
    n_customer = sum(1 for tg in targets if tg.get("role") != "canary")

    def annotate(tg):
        """(is_full_down, context_suffix) for a down customer target, sibling-aware."""
        site, role = tg.get("site"), tg.get("role")
        if role == "apex" and up_now.get((site, "www")):
            return False, " — apex down, www is UP (store still reachable)"
        if role == "www" and up_now.get((site, "apex")):
            return False, " — www down, apex is UP"
        return True, ""

    # Same per-target state machine as failover-monitor.sh. The canary (tunnel-test.hosto.app)
    # is the PLATFORM probe: it rides CF → tunnel → Caddy on app-01t exactly like every customer
    # site, so if pve-2 / app-01t / cloudflared is down, IT is down. When the platform is down
    # that ONE alert dominates and the downstream per-site list is SUPPRESSED — "the machine is
    # down", not a list of every website (correlated / root-cause alerting).
    site_events = []     # (kind, line): kind in {"full", "partial", "up"}
    plat_event = None    # (kind, line): kind in {"down", "up"}
    canary_st = None     # the canary's state dict (advance only ITS alert-state on platform-down)
    pending_down = []

    for tg in targets:
        name = tg["name"]
        is_canary = tg.get("role") == "canary"
        st = state.get(name) or {"fails": 0, "first_fail": 0, "alerted": 0, "last_alert": 0}
        if is_canary:
            canary_st = st
        ok, detail = probed[name]

        if ok:
            down_for = t - st["first_fail"] if st["first_fail"] else 0
            if st["alerted"] or st["fails"] >= FAIL_THRESHOLD:
                tail = "" if st["alerted"] else " (DOWN alerts during the outage could not be delivered)"
                if is_canary:
                    plat_event = ("up", f"🟢 PLATFORM RECOVERED: app-01t / cloudflared tunnel is back UP.\n\n"
                                        f"Tunnel canary reachable again. Downtime: {fmt_duration(down_for)}.{tail}")
                else:
                    site_events.append(("up", f"🟢 RECOVERED: {name} back UP after {fmt_duration(down_for)}{tail} ({detail})."))
            state[name] = {"fails": 0, "first_fail": 0, "alerted": 0, "last_alert": 0}
            continue

        # Down this run.
        if st["fails"] == 0:
            st["first_fail"] = t
        st["fails"] += 1
        if st["fails"] >= FAIL_THRESHOLD:
            dur = fmt_duration(t - st["first_fail"])
            if is_canary:
                if not st["alerted"]:
                    plat_event = ("down", f"🚨 PLATFORM DOWN: app-01t / cloudflared tunnel unreachable\n\n"
                                          f"Production is OFFLINE — the tunnel canary failed {st['fails']} consecutive checks. "
                                          f"Every tunnel-served customer site is affected. Investigate pve-2 / app-01t / cloudflared now.")
                    pending_down.append(st)
                elif t - st["last_alert"] >= RENOTIFY_SECONDS:
                    plat_event = ("down", f"🚨 PLATFORM STILL DOWN: app-01t / tunnel unreachable for {dur}. "
                                          f"Every tunnel-served site affected — manual intervention required (pve-2 / app-01t).")
                    pending_down.append(st)
            else:
                full, ctx = annotate(tg)
                dot, label = ("🔴", "DOWN") if full else ("🟠", "PARTIAL")
                kind = "full" if full else "partial"
                if not st["alerted"]:
                    site_events.append((kind, f"{dot} {label}: {name}{ctx} ({detail}, {st['fails']} consecutive checks)."))
                    pending_down.append(st)
                elif t - st["last_alert"] >= RENOTIFY_SECONDS:
                    site_events.append((kind, f"{dot} STILL {label}: {name}{ctx} — down for {dur} ({detail})."))
                    pending_down.append(st)
        state[name] = st

    # Correlate: if the platform (canary) is down, that single alert dominates and the per-site
    # list is suppressed. Sites still tick their counters silently and re-evaluate once it's back.
    canary_down = canary_st is not None and canary_st["fails"] >= FAIL_THRESHOLD

    exit_code = 0
    if canary_down:
        if plat_event and plat_event[0] == "down":
            n_down = sum(1 for k, _ in site_events if k in ("full", "partial"))
            msg = plat_event[1]
            if n_down:
                msg += (f"\n\n({n_down} of {n_customer} customer URLs are also unreachable — "
                        f"downstream of this outage; per-site alerts suppressed.)")
            pending_down = [canary_st]           # advance ONLY the platform's alert-state
        else:
            msg = None                           # platform down, renotify not due → stay quiet
            pending_down = []
    elif site_events or (plat_event and plat_event[0] == "up"):
        blocks = []
        if plat_event and plat_event[0] == "up":
            blocks.append(plat_event[1])
        if site_events:
            if any(k == "full" for k, _ in site_events):
                head = "🚨 Hosto uptime — site down"
            elif any(k == "partial" for k, _ in site_events):
                head = "🟠 Hosto uptime — partial (apex down, www still serving)"
            else:
                head = "🟢 Hosto uptime — recovered"
            blocks.append(head + "\n\n" + "\n".join(line for _, line in site_events))
        msg = "\n\n".join(blocks)
    else:
        msg = None

    if msg:
        print(msg)                               # echo to the Action log for visibility
        if send_telegram(msg):
            for st in pending_down:              # advance alert-state only on a successful send
                st["alerted"] = 1
                st["last_alert"] = t
            print("sent alert")
        else:
            # Loud: leave down targets un-alerted so we retry next run, and fail the run.
            print("telegram send FAILED — alert-state not advanced; run marked failed", file=sys.stderr)
            exit_code = 1
    else:
        print("all healthy — no events")

    after = json.dumps(state, sort_keys=True)
    if after != before:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, STATE_PATH)
        print("state changed")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
