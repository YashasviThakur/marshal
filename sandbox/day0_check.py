"""Day-0 check: can a RUNNING Flagsmith experiment's variant weights be changed, and does serving follow?

Marshal rests on this. The docs say a running experiment's flag configuration is locked; the API's rollout action
says it sets "the variant weights they are split across". This script settles it on self-hosted 2.295.2:
start a 50/50 experiment, evaluate fresh identities, PATCH the rollout to 15/85 while it runs, evaluate again,
and check that identities already on the treatment stayed there. Uses a throwaway project and deletes it.
"""
import json
import os
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

env = dict(l.split("=", 1) for l in Path(__file__).with_name(".env").read_text().splitlines() if "=" in l)
BASE = env["FLAGSMITH_API_URL"].rstrip("/") + "/api/v1"
ADMIN = {"Authorization": "Api-Key " + env["FLAGSMITH_API_KEY_localhost_8100"]}


def call(method, path, body=None, headers=ADMIN):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:600].decode(errors="replace")


def ok(result, what):
    status, body = result
    if status >= 300:
        raise SystemExit(f"{what}: HTTP {status} {body}")
    return body


def split(env_key, feature, ids):
    """Serve the flag to each identity exactly as an SDK would (transient: nothing is stored)."""
    out = {}
    for i in ids:
        flags = ok(call("POST", "/identities/", {"identifier": i, "traits": [], "transient": True},
                        {"X-Environment-Key": env_key}), "evaluate")["flags"]
        out[i] = next(f["feature_state_value"] for f in flags if f["feature"]["name"] == feature)
    return out


def main():
    org = int(env["FLAGSMITH_ORGANISATION"])
    project = ok(call("POST", "/projects/", {"name": "day0-check", "organisation": org}), "project")
    try:
        e = ok(call("POST", "/environments/", {"name": "production", "project": project["id"]}), "environment")
        key = e["api_key"]
        feat = ok(call("POST", f"/projects/{project['id']}/features/", {
            "name": "checkout_button", "type": "MULTIVARIATE", "initial_value": "control",
            "multivariate_options": [{"type": "unicode", "string_value": "treatment", "key": "treatment",
                                      "default_percentage_allocation": 0}]}), "feature")
        option = feat["multivariate_options"][0]["id"]
        wh = call("POST", f"/environments/{key}/warehouse-connections/",
                  {"warehouse_type": "postgres", "name": "Flagsmith database", "config": {"source": "flagsmith_database"}})
        print("warehouse:", wh[0], wh[1] if wh[0] >= 300 else {k: wh[1][k] for k in ("id", "warehouse_type", "status")})
        metric = ok(call("POST", f"/environments/{key}/experiment-metrics/", {
            "name": "checkout", "aggregation": "occurrence", "direction": "up",
            "definition": {"version": 1, "event": "checkout"}}), "metric")
        rollout = lambda pct: {"enabled": True, "rollout_percentage": 100,
                               "feature_state_value": {"type": "string", "value": "control"},
                               "multivariate_feature_state_values": [
                                   {"multivariate_feature_option": option, "percentage_allocation": pct}]}
        exp = ok(call("POST", f"/environments/{key}/experiments/", {
            "feature": feat["id"], "name": "button copy", "hypothesis": "treatment converts better",
            "metrics": [{"metric": str(metric["id"]), "expected_direction": "increase"}],
            "experiment_rollout": rollout(50)}), "experiment")
        print("experiment:", exp["id"], exp.get("status"))
        print("start:", ok(call("POST", f"/environments/{key}/experiments/{exp['id']}/start/"), "start").get("status"))

        first = [f"a-{i}" for i in range(600)]
        before = split(key, "checkout_button", first)
        print("running 50/50:", dict(Counter(before.values())))

        status, body = call("PATCH", f"/environments/{key}/experiments/{exp['id']}/rollout/", rollout(85))
        print("PATCH rollout to 15/85 while running ->", status, "" if status < 300 else body)
        after_new = split(key, "checkout_button", [f"b-{i}" for i in range(600)])
        after_old = split(key, "checkout_button", first)
        print("fresh identities after PATCH:", dict(Counter(after_new.values())))
        moved = Counter((before[i], after_old[i]) for i in first)
        print("same identities, before -> after:", {f"{a}->{b}": n for (a, b), n in moved.items()})
        print("status now:", ok(call("GET", f"/environments/{key}/experiments/{exp['id']}/"), "get").get("status"))
    finally:
        call("DELETE", f"/projects/{project['id']}/")
        print("throwaway project deleted")


if __name__ == "__main__":
    main()
