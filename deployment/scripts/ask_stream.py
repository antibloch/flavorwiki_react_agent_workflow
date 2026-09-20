"""Reference client for api_funda_agent_exp.py -- prints the answer as it is generated.

    python scripts/ask_stream.py "What are the products tested in this survey?"

    # against a public tunnel, i.e. the same path a remote machine takes
    python scripts/ask_stream.py "..." --url https://<name>.ngrok-free.dev
    FLAVORAI_API=https://<name>.ngrok-free.dev \
      HERBALIFE_EXTERNAL_ACCESS_SECRET=<secret> python scripts/ask_stream.py "..."

    # is the tunnel up at all? (no LLM call, no cost)
    python scripts/ask_stream.py --health --url https://<name>.ngrok-free.dev

The only thing a client MUST get right is `reset`: the agent is a ReAct loop, and prose emitted
by a turn that then calls a tool is not the answer. On `reset`, throw away what you have printed
so far for that turn. Everything else is optional decoration -- `done.answer` always carries the
finished text, so a client that does not want live output can ignore every other event.
"""
import argparse
import json
import os
import sys

import requests

DEFAULT_SURVEY = "39af3240-42a8-4e35-8c7d-c61703d5ce3f"

# ngrok's free plan answers browser-looking requests with an HTML interstitial instead of the
# response, which turns every JSON parse into a syntax error on "<!DOCTYPE html>". Harmless
# against a local server, so it is sent unconditionally rather than guessed at per host.
HEADERS = {"ngrok-skip-browser-warning": "true"}


def endpoint(url: str, path: str) -> str:
    """Accept either a base URL or a full endpoint, so --url takes what ngrok prints."""
    url = url.rstrip("/")
    for known in ("/ask", "/health"):
        if url.endswith(known):
            url = url[: -len(known)]
            break
    return url + path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?", default=None)
    ap.add_argument("--url", default=os.getenv("FLAVORAI_API", "http://localhost:8000"),
                    help="base URL or full /ask endpoint; env FLAVORAI_API also works")
    ap.add_argument("--health", action="store_true",
                    help="just check reachability and print the server config, then exit")
    ap.add_argument("--survey-id", default=DEFAULT_SURVEY)
    # Derived from the survey server-side; pass these only to override the lookup.
    ap.add_argument("--client-id", default=None)
    ap.add_argument("--org-id", default=None)
    ap.add_argument("--thread-id", default=None, help="repeat to continue a conversation")
    ap.add_argument("--no-inventory", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="answer only, no progress notes")
    a = ap.parse_args()

    if a.health:
        url = endpoint(a.url, "/health")
        print(f"GET {url}", file=sys.stderr)
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
        except requests.RequestException as exc:
            print(f"UNREACHABLE: {type(exc).__name__}: {exc}", file=sys.stderr)
            sys.exit(1)
        # A tunnel that is up but has nothing behind it answers with ngrok's own error page,
        # so a 200 is not enough -- the body has to be the health JSON.
        try:
            print(json.dumps(r.json(), indent=2))
        except ValueError:
            print(f"HTTP {r.status_code} but the body is not JSON -- most likely the tunnel is "
                  f"up and the server behind it is not:\n{r.text[:400]}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if not a.prompt:
        ap.error("a prompt is required (or use --health)")

    secret = os.getenv("HERBALIFE_EXTERNAL_ACCESS_SECRET", "")
    if len(secret) < 32:
        ap.error(
            "HERBALIFE_EXTERNAL_ACCESS_SECRET is required and must contain at least 32 characters"
        )
    authenticated_headers = {**HEADERS, "Authorization": f"Bearer {secret}"}

    body = {"prompt": a.prompt, "survey_id": a.survey_id, "no_inventory": a.no_inventory}
    for key, val in (("client_id", a.client_id), ("organization_id", a.org_id),
                     ("thread_id", a.thread_id)):
        if val:
            body[key] = val

    url = endpoint(a.url, "/ask")
    if not a.quiet:
        print(f"[· POST {url}]", file=sys.stderr)

    printed = 0  # characters of the current candidate answer already on screen
    with requests.post(url, json=body, headers=authenticated_headers, stream=True, timeout=900) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            ev = json.loads(line)
            kind = ev["type"]

            if kind == "token":
                sys.stdout.write(ev["text"])
                sys.stdout.flush()
                printed += len(ev["text"])

            elif kind == "reset":
                # That turn was a tool call. Erase the commentary it produced, if any.
                if printed and not a.quiet:
                    sys.stdout.write("\r" + " " * printed + "\r")
                    sys.stdout.flush()
                printed = 0
                if not a.quiet:
                    print(f"[· calling {', '.join(ev['tools'])}]", file=sys.stderr)

            elif kind == "tool_result" and not a.quiet:
                print(f"[· {ev['name']} -> {ev['chars']} chars]", file=sys.stderr)

            elif kind == "status" and not a.quiet:
                if ev["stage"] == "scope":
                    # Show what the server derived from survey_id, so it is visible that the
                    # agent still gets a client_id -- you just no longer have to supply it.
                    print(f"[· scope {'resolved' if ev['resolved'] else 'NOT RESOLVED'}"
                          f" · client={ev['client_id'] or 'n/a'}"
                          f" · org={ev['organization_id'] or 'n/a'}]", file=sys.stderr)
                else:
                    print(f"[· {ev['stage']}"
                          + (f" {ev['chars']} chars" if "chars" in ev else "") + "]",
                          file=sys.stderr)

            elif kind == "done":
                print()
                if not a.quiet:
                    # Telemetry existed in an older API build. The current service guarantees
                    # only answer + thread_id, so print telemetry when present without requiring
                    # it and crashing after an otherwise successful request.
                    t = ev.get("tokens")
                    if t and "wall_s" in ev and "turns" in ev:
                        print(f"[{ev['wall_s']}s · {ev['turns']} turns · in {t['input']} "
                              f"(cached {t['cached_input']}) · out {t['output']}]",
                              file=sys.stderr)
                    else:
                        print(f"[done · thread {ev['thread_id']}]", file=sys.stderr)

            elif kind == "error":
                print(f"\nERROR: {ev['error']}", file=sys.stderr)
                sys.exit(1)


if __name__ == "__main__":
    main()
