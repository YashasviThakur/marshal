"""marshal serve: a read-only dashboard over the same measure -> decide numbers `run` uses. Standard library only."""
import json
import random
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .cli import configured_split, settings, ts
from .decide import confidence_sequence, guardrail_breaches, win_probabilities
from .flagsmith import Admin
from .measure import FLAGSMITH_DATABASE_TABLE, Metric, read_stats
from .stats import CONTROL


def snapshot(args, trait):
    import psycopg

    s, key = settings(args.env)
    api = Admin(s["FLAGSMITH_API_URL"], key)
    env_key, exp_id = s["EXPERIMENT_ENV_KEY"], s["EXPERIMENT_ID"]
    exp = api.experiment(env_key, exp_id)
    metrics = []
    for em in exp["metrics"]:
        m = api.metric(env_key, em["metric"])
        metrics.append(Metric(m["id"], m["definition"]["event"], m["aggregation"], m["direction"] == "down"))
    start, table = ts(exp["started_at"]), s.get("MARSHAL_EVENTS_TABLE", FLAGSMITH_DATABASE_TABLE)
    now = datetime.now(start.tzinfo)
    steer = metrics[0]
    with psycopg.connect(s["MARSHAL_WAREHOUSE_DSN"]) as conn:
        _, stats = read_stats(conn, env_key, exp["feature"]["name"], start, now, metrics, table, "first")
        seg_stats = {}
        if trait:
            seg_stats = read_stats(conn, env_key, exp["feature"]["name"], start, now, [steer], table, "first",
                                   trait)[1][steer.id]
    arms, served = stats[steer.id], configured_split(exp)
    wins = win_probabilities(arms, occurrence=steer.aggregation == "occurrence", lower_is_better=steer.lower_is_better,
                             draws=5000, rng=random.Random(1)) if all(a.n > 1 for a in arms.values()) else {}

    def row(variant, a, control, alpha=0.05):
        cs = confidence_sequence(control, a, alpha) if variant != CONTROL and control else None
        return {"variant": variant, "n": a.n, "mean": a.mean, "lift": cs and cs.lift, "low": cs and cs.low,
                "high": cs and cs.high, "verdict": cs.state if cs else None}

    log = Path(".marshal/decisions.jsonl")
    history = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return {"experiment": exp["name"], "status": exp["status"], "metric": steer.event, "served": served,
            "arms": [{**row(v, a, arms.get(CONTROL)), "chance_best": wins.get(v)}
                     for v, a in sorted(arms.items(), key=lambda x: (x[0] != CONTROL, x[0]))],
            "guardrails": [{"metric": g.event, "breaches": guardrail_breaches([g], stats)} for g in metrics[1:]],
            "trait": trait,
            "segments": [{"segment": seg or "(none)", **row(v, a, g.get(CONTROL), 0.05 / len(seg_stats))}
                         for seg, g in sorted(seg_stats.items()) for v, a in sorted(g.items()) if v != CONTROL],
            "history": [{"at": r["at"], "served": r["current"], "decision": r["decision"]} for r in history[-60:]]}


def serve(args):
    page = Path(__file__).with_name("dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/api/state":
                try:
                    trait = parse_qs(url.query).get("trait", [args.trait])[0]
                    body, code = json.dumps(snapshot(args, trait), default=str).encode(), 200
                except Exception as e:  # show the problem on the page instead of a blank screen
                    body, code = json.dumps({"error": str(e)}).encode(), 500
                kind = "application/json"
            else:
                body, code, kind = page, 200, "text/html; charset=utf-8"
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    print(f"Marshal dashboard on http://localhost:{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
