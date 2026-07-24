#!/usr/bin/env python3
"""Simulation test harness for the Non-Profit AI Toolkit prototype.

Replays scripted user runs against the LIVE app — the same `/api/chat` calls the
browser makes — so each persona exercises the full sequence a real user would walk
through in the browser at the URL in TOOLKIT_BASE_URL (or http://127.0.0.1:8765):

    free-write -> strategic-fit interview -> Entry record -> Red Line Test

Each step is checked against the invariants the app is supposed to hold. Because the
model is live, replies vary run to run, so the checks are keyword-tolerant, not
exact-match.

Usage (start the server first, with OLLAMA_API_KEY set):
    python3 tests/simulate.py                 # all personas
    python3 tests/simulate.py --persona maple # one persona
    python3 tests/simulate.py --verbose       # print fuller model replies
"""
import argparse, json, os, sys, urllib.request

BASE = os.environ.get("TOOLKIT_BASE_URL", "http://127.0.0.1:8765").rstrip("/")

def call(payload):
    req = urllib.request.Request(BASE + "/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=120))


def short(s, n=220):
    return (s or "").strip().replace("\n", " / ")[:n]


# reasoning-trace markers that must never appear in returned content (see
# server.strip_reasoning) — GLM/DeepSeek <think>, Kimi ◁think▷, and variants
REASONING_MARKERS = ("<think", "</think", "◁think▷", "◁/think▷", "<thinking", "</thinking")


def leaks_reasoning(text):
    low = (text or "").lower()
    return any(m.lower() in low for m in REASONING_MARKERS)


# ---- personas: strategic-fit answers plus category-level Red Line Test answers ----
PERSONAS = [
    {
        "id": "maple", "org": "Maple Community Center", "services": "",
        "freewrite": ("After-school programs, a food pantry, and ESL classes. Staff answer the same "
                      "eligibility questions for hours, intake notes are all on paper, and I worry "
                      "about client privacy."),
        "answers": ["Families and front-desk staff are affected by delayed answers.",
                    "A good result would cut lookup time while keeping staff responsible for eligibility answers.",
                    "About six staff use consumer chatbots occasionally; none are technical.",
                    "The program director could own a review, and we would stop if client privacy could not be protected."],
        "redlines": ["Participant names, contact details, household income ranges, and program eligibility categories.",
                     "The intake team collects the information with service consent; external AI use was not covered.",
                     "Staff must make every eligibility decision and families need a route to correct an answer.",
                     "We have not completed an equity or accessibility review.",
                     "The program director can stop the work, but no one has been assigned to audit it."],
    },
    {
        "id": "harbor", "org": "Harbor Legal Aid",
        "services": "- Eviction defense\n- Public-benefits appeals\n- Know-your-rights clinics",
        "freewrite": ("We're a small legal-aid office drowning in intake. Lawyers repeat the same "
                      "know-your-rights explanations every day. Everything we hold is confidential."),
        "answers": ["Tenants and the lawyers who advise them are affected.",
                    "A good result would help staff find approved explanations without treating them as legal advice.",
                    "Two paralegals could maintain a narrow pilot, but staff AI knowledge varies.",
                    "The supervising attorney would own it; confidential case facts must stay out."],
        "redlines": ["Public know-your-rights materials and confidential case information are the two main categories.",
                     "Clients did not consent to external AI processing of their case information.",
                     "Lawyers must retain every legal judgment and clients need a route to reach counsel.",
                     "The office has not tested language access or disability access.",
                     "The supervising attorney can stop the work; a privacy lead still needs to review the boundary."],
    },
]


class Checks:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.fails = []

    def ok(self, cond, label):
        if cond:
            self.passed += 1
            print("   ✓ " + label)
        else:
            self.failed += 1
            self.fails.append(label)
            print("   ✗ " + label)


def run_persona(p, chk, verbose):
    print("\n=== persona: %s (%s) ===" % (p["org"], p["id"]))
    org = {"name": p["org"], "services": p["services"]}
    ctx = "org: %s. %s" % (p["org"], p["freewrite"])
    hist = [{"role": "user", "content": ctx}]
    print("  free-write: " + short(p["freewrite"]))

    # ---- adaptive interview: each answer should shape the next single question ----
    routing = None
    for i in range(5):
        c = (call({"mode": "onboard", "messages": hist, "context": ctx}).get("content") or "")
        chk.ok(bool(c), "interview turn %d returned content" % (i + 1))
        chk.ok(not leaks_reasoning(c), "turn %d has no leaked reasoning tokens" % (i + 1))
        if "→ step 1" in c.lower() or "→step 1" in c.lower():
            routing = c
            break
        print("  Q%d: %s" % (i + 1, short(c, 300 if verbose else 160)))
        chk.ok(c.count("?") <= 1, "turn %d asks one question, not a batch" % (i + 1))
        ans = p["answers"][i] if i < len(p["answers"]) else "not sure — what do you suggest?"
        hist += [{"role": "assistant", "content": c}, {"role": "user", "content": ans}]
        ctx += "\n— " + ans
    chk.ok(routing is not None, "interview reached a routing recommendation")
    chk.ok(bool(routing) and "step 1" in routing.lower(), "routing names the next step")
    if routing:
        print("  ROUTE: " + short(routing, 300 if verbose else 200))

    # ---- Strategic-fit Entry record ----
    hist.append({"role": "user", "content": "Now write my Entry record and next test, exactly as instructed."})
    est = (call({"mode": "estimate", "messages": hist, "context": ctx, "org": org}).get("content") or "")
    print("  RECORD: " + short(est, 600 if verbose else 240))
    chk.ok(not leaks_reasoning(est), "Entry record has no leaked reasoning tokens")
    chk.ok("entry record" in est.lower(), "record has an 'Entry record' section")
    chk.ok("decisions made" in est.lower(), "record separates decisions made")
    chk.ok("next test" in est.lower(), "record names the next test")
    ctx += "\n— Entry record: " + est

    # ---- Step 1: adaptive Red Line Test ----
    p_hist = [{"role": "user", "content": "Begin Step 1 using my Entry record. Ask exactly one Red Line Test question."}]
    redline_record = None
    for i in range(7):
        a = (call({"mode": "redline", "messages": p_hist, "context": ctx, "org": org}).get("content") or "")
        chk.ok(bool(a), "red-line turn %d returned content" % (i + 1))
        chk.ok(not leaks_reasoning(a), "red-line turn %d has no leaked reasoning tokens" % (i + 1))
        if "outcome:" in a.lower():
            redline_record = a
            break
        print("  RED LINE Q%d: %s" % (i + 1, short(a, 300 if verbose else 160)))
        chk.ok(a.count("?") <= 1, "red-line turn %d asks one question, not a batch" % (i + 1))
        ans = p["redlines"][i] if i < len(p["redlines"]) else "The responsible owner has not decided that yet."
        p_hist += [{"role": "assistant", "content": a}, {"role": "user", "content": ans}]
        ctx += "\n— Step 1 response: " + ans
    chk.ok(redline_record is not None, "Red Line Test reached a decision record")
    if redline_record:
        print("  RED LINE RECORD: " + short(redline_record, 600 if verbose else 240))
        low = redline_record.lower()
        chk.ok(all(w in low for w in ["data boundary", "human authority", "unknown"]),
               "Red Line record carries boundaries, authority, and unknowns")
        chk.ok(any(route in low for route in ["outcome: yes", "outcome: maybe", "outcome: no"]),
               "Red Line record names one of the three routes")
        if "fortune" not in p["org"].lower():
            chk.ok("fortune" not in low, "no Fortune leakage in the Red Line record")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", help="run only this persona id")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    try:
        urllib.request.urlopen(BASE + "/", timeout=5)
    except Exception as e:
        sys.exit("server not reachable on :8765 — start it first (%s)" % e)

    chk = Checks()
    for p in PERSONAS:
        if args.persona and p["id"] != args.persona:
            continue
        run_persona(p, chk, args.verbose)

    print("\n──────── %d passed · %d failed ────────" % (chk.passed, chk.failed))
    if chk.fails:
        print("failures:")
        for f in chk.fails:
            print("  - " + f)
        sys.exit(1)
    print("all simulated runs passed ✓")


if __name__ == "__main__":
    main()
