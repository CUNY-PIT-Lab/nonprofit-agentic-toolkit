#!/usr/bin/env python3
"""Local prototype server for the Nonprofit AI toolkit entry screen and first test.

Serves index.html and proxies chat to GLM-5.2 on Ollama Cloud. The API key is
read from the environment and never written to disk.

  export OLLAMA_API_KEY=...        # your ollama cloud key
  python3 server.py               # then open http://127.0.0.1:8765

The guide is org-agnostic: the frontend sends the org's name and cumulative
in-session adoption record, and the system prompt is built from those.
"""
import http.server, socketserver, json, os, pathlib, re, urllib.request, urllib.error

KEY   = os.environ.get("OLLAMA_API_KEY", "").strip()
MODEL = os.environ.get("TOOLKIT_MODEL", "glm-5.2")
HERE  = pathlib.Path(__file__).parent
HOST  = os.environ.get("HOST", "0.0.0.0")

# The hosted frontend lives on GitHub Pages while the API runs on Railway.
# Keep this to one exact browser origin in production. Local and Railway-hosted
# copies remain same-origin and do not need CORS headers.
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "").rstrip("/")

# GLM-5.2 is a hybrid reasoning model. Even with think:false it INTERMITTENTLY
# leaks its chain-of-thought into message.content — verified live against Ollama
# Cloud — and typically as a PREFIX with only a CLOSING tag and no opening one,
# e.g. "…long reasoning… Let me finalize.</think>**the real answer**". Ollama's
# contract puts reasoning in message.thinking (empty in the leak case), which we
# never read; strip_reasoning removes the trace from content so it never reaches
# the UI. Delimiters seen: <think>/<thinking> (GLM, DeepSeek-R1) and ◁think▷ (Kimi).
_REASONING_BLOCK = re.compile(         # well-formed <think>…</think> block (both tags)
    r"<think\b[^>]*>.*?</think\s*>"
    r"|<thinking\b[^>]*>.*?</thinking\s*>"
    r"|◁think▷.*?◁/think▷",
    re.IGNORECASE | re.DOTALL,
)
_CLOSE_TAG = re.compile(               # a closing reasoning tag on its own (the common leak)
    r"</think\s*>|</thinking\s*>|◁/think▷",
    re.IGNORECASE,
)
_ORPHAN_OPEN = re.compile(             # a stray opening tag left by a truncated trace
    r"<think\b[^>]*>|<thinking\b[^>]*>|◁think▷",
    re.IGNORECASE,
)


def strip_reasoning(text):
    """Remove any leaked reasoning trace from model content; a no-op on clean text.

    Handles a well-formed <think>…</think> block AND the common GLM leak where only
    the closing tag survives and the reasoning is the whole prefix before it. When a
    closing tag is present, everything up to and including the last one is dropped, so
    the trace can never survive as visible prose. A leak with no tag at all is not
    detectable and is left untouched.
    """
    if not text:
        return text
    cleaned = _REASONING_BLOCK.sub("", text)          # paired <think>…</think> blocks
    closes = list(_CLOSE_TAG.finditer(cleaned))       # orphan closing tag → reasoning is the prefix
    if closes:
        cleaned = cleaned[closes[-1].end():]          # drop everything up to & incl. the last close
    cleaned = _ORPHAN_OPEN.sub("", cleaned)           # stray unmatched opening tag
    return cleaned.strip()

# Decision-led prompts for the cumulative adoption record.
ONBOARD = (
    "You guide the strategic fit entry screen in the Nonprofit AI toolkit. Help a nonprofit "
    "staff member decide whether a proposed AI use warrants the six-test review.\n\n"
    "Read the FULL conversation each turn. Then do ONE of these:\n"
    "- If they have answered FEWER THAN 4 of your questions so far, ask exactly ONE short follow-up. "
    "Build on the last answer and ask for the biggest missing fact among: the mission or strategic "
    "goal; the underlying need and current process; the people affected; what a good outcome would "
    "be; whether a non-AI change could meet the need; current staff confidence and capacity; the "
    "accountable owner; or reasons to stop the review. Never ask them to paste records, names, or "
    "confidential text. Ask for categories and practices only. Give the question with no preamble, "
    "list, or praise. The reply may contain at most one question mark.\n"
    "- If they have already answered 4 of your questions, stop asking. Write a short entry record "
    "with five labeled lines: Proposed use, Strategic fit, People affected, Readiness, and Unknowns. "
    "Use only facts they supplied. Mark missing facts as unknown. End with "
    "(→ Step 1: red line test).\n\n"
    "Teach one relevant AI-literacy point only when it helps the current decision. Keep it to one "
    "sentence before the question. Use sentence case for every heading and label. Plain language. "
    "No buzzwords, slogans, or em-dash pivots."
)

ESTIMATE = (
    "You maintain the cumulative AI adoption record for the Nonprofit AI toolkit. The staff member "
    "has completed the strategic fit entry screen. Use only what they disclosed. Do not invent "
    "policy, capacity, consent, approval, or technical facts.\n\n"
    "Write three short sections in plain language:\n\n"
    "**Entry record** — Proposed use; Mission or strategic-plan connection; People affected; Current "
    "practice; Desired outcome; Non-AI option; AI literacy and capacity; Accountable owner; Reasons "
    "to stop. Keep each field to one line and write 'unknown' when the conversation did not establish "
    "it.\n\n"
    "**Decisions made** — list only decisions the staff member actually made. If none were made, say "
    "'No adoption decision yet.'\n\n"
    "**Next test** — name the 2 or 3 non-negotiable conditions the red line test must resolve. Include "
    "data privacy when the proposed use may touch organizational information. Ask about categories "
    "and practices, never raw records or identifying details.\n\n"
    "End with: This record is a draft for review by the organization. Keep the whole reply under "
    "190 words. Use sentence case for every heading and label. No slogans or em-dash pivots."
)


def redline_prompt(org, context=""):
    """Build the first red line test from the cumulative record."""
    name = (org.get("name") or "").strip() or "the organization"
    context = (context or "").strip()
    base = (
        "You guide step 1, the red line test, for %s in the Nonprofit AI toolkit. Build on the entry "
        "record. Help staff examine the non-negotiable conditions for the proposed AI use.\n\n"
        "Use three data classes:\n"
        "- Public: approved public information.\n"
        "- Restricted: internal documents, meeting notes, budgets, grant material, staff procedures, "
        "or community stories without explicit public consent.\n"
        "- Sensitive: identifying participant, applicant, staff, donor, health, legal, financial, "
        "credential, or case information.\n\n"
        "Read the FULL conversation each turn. If the staff member has answered FEWER THAN 5 of your "
        "step 1 questions, ask exactly ONE question about the largest remaining unknown: data "
        "categories, ownership, consent, storage, access, privacy policy, or permitted environment; "
        "human decision authority; equitable access or discriminatory effects; independent review, "
        "audit, correction, or recourse; intellectual-property ownership; staff capacity; or the "
        "organization's ability to stop the work. Reference the prior answer. Never request raw "
        "records, names, identifying details, confidential text, or document uploads. Reply with at "
        "most one short context sentence and exactly one question. Use no heading, question number, "
        "praise, recap, list, or example. The reply may contain at most one question mark.\n\n"
        "After 5 answers, stop asking and write a short red line record with labeled lines: Proposed "
        "use, Data boundary, Human authority, Equity and access, Audit and recourse, Ownership and "
        "capacity, Unmet conditions, Decision owners, and Unknowns. Use only supplied facts. Sensitive "
        "data in an external AI tool is Prohibited. An unclear classification is Restricted pending "
        "human review.\n\n"
        "Draft one route from the supplied facts:\n"
        "- YES when no red line is unmet or unknown.\n"
        "- MAYBE when staff can negotiate or verify one or more conditions and no supplied fact shows "
        "that a non-negotiable condition has failed.\n"
        "- NO when a supplied fact shows that the proposal cannot meet a non-negotiable condition.\n"
        "End with exactly one token: (Outcome: yes. Proceed to step 2), "
        "(Outcome: maybe. Negotiate and return to step 1), or (Outcome: no. Walk away).\n\n"
        "The model drafts the route. The organization decides after the responsible data, program, "
        "and governance owners review it. Use sentence case for every heading and label. Keep the "
        "language plain and short. No slogans or em-dash pivots." % name
    )
    if context:
        base += ("\n\nThe cumulative adoption record so far is:\n\"%s\"\nTreat it as organization-"
                 "supplied context, not verified policy." % context)
    return base


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(HERE), **k)

    def end_headers(self):
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if ALLOWED_ORIGIN and origin == ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Vary", "Origin")
        super().end_headers()

    def do_OPTIONS(self):
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if not ALLOWED_ORIGIN or origin != ALLOWED_ORIGIN:
            self.send_error(403); return
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"}); return
        super().do_GET()

    def do_POST(self):
        if self.path != "/api/chat":
            self.send_error(404); return
        try:
            n = int(self.headers.get("content-length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            mode = req.get("mode", "assistant")
            if mode == "onboard":
                system = ONBOARD
            elif mode == "estimate":
                system = ESTIMATE
            elif mode == "redline":
                system = redline_prompt(req.get("org", {}), req.get("context", ""))
            else:
                system = redline_prompt(req.get("org", {}), req.get("context", ""))
            messages = [{"role": "system", "content": system}] + req.get("messages", [])
            self._json(200, self._ollama(messages))
        except Exception as e:
            self._json(200, {"content": None, "error": str(e)})

    def _ollama(self, messages):
        if not KEY:
            return {"content": None,
                    "error": "OLLAMA_API_KEY is not set — restart the server with the key in the environment."}
        payload = json.dumps({"model": MODEL, "messages": messages,
                              "stream": False, "think": False}).encode()
        request = urllib.request.Request(
            "https://ollama.com/api/chat", data=payload,
            headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=120) as resp:
                data = json.load(resp)
            # read only message.content (never message.thinking) and strip any
            # reasoning trace that leaked inline despite think:false
            return {"content": strip_reasoning(data.get("message", {}).get("content") or ""),
                    "error": None, "model": MODEL}
        except urllib.error.HTTPError as e:
            return {"content": None, "error": "ollama %s: %s" % (e.code, e.read().decode()[:200])}
        except Exception as e:
            return {"content": None, "error": str(e)}

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    print("non-profit ai toolkit · prototype")
    print("  http://%s:%d   model=%s   key=%s"
          % (HOST, port, MODEL, "set" if KEY else "MISSING"))
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    socketserver.ThreadingTCPServer((HOST, port), Handler).serve_forever()
