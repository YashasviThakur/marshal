"""Create the demo experiment on the sandbox: project, environment, multivariate flag, the "Flagsmith database"
warehouse, a purchase metric and a running 50/50 experiment. Idempotent: reuses what exists, or with --reset deletes the
demo project first.

Writes the client environment key into simulator/sim.toml and EXPERIMENT_* ids into sandbox/.env.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
from marshal_ab.flagsmith import Admin  # noqa: E402

ENV_FILE = Path(__file__).with_name(".env")
env = dict(l.split("=", 1) for l in ENV_FILE.read_text().splitlines() if "=" in l)
api = Admin(env["FLAGSMITH_API_URL"], env["FLAGSMITH_API_KEY_localhost_8100"])
FEATURE, METRIC_EVENT = "checkout_button", "purchase"


def items(path):
    """List endpoints answer either a plain list or a page with results."""
    r = api.get(path)
    return r["results"] if isinstance(r, dict) else r


def main():
    org = int(env["FLAGSMITH_ORGANISATION"])
    projects = {p["name"]: p for p in items("/projects/")}
    if "--reset" in sys.argv and "marshal-demo" in projects:  # a free organisation holds one project: start clean
        api.call("DELETE", f"/projects/{projects.pop('marshal-demo')['id']}/")
    project = projects.get("marshal-demo") or api.post("/projects/", {"name": "marshal-demo", "organisation": org})
    envs = {e["name"]: e for e in items(f"/environments/?project={project['id']}")}
    environment = envs.get("production") or api.post("/environments/", {"name": "production", "project": project["id"]})
    key = environment["api_key"]

    features = {f["name"]: f for f in items(f"/projects/{project['id']}/features/")}
    feature = features.get(FEATURE) or api.post(f"/projects/{project['id']}/features/", {
        "name": FEATURE, "type": "MULTIVARIATE", "initial_value": "control"})
    # Created on its own: a variant nested in the feature create loses its `key` on 2.295.2, and a variant without
    # a key gets no exposures recorded (the SDK skips flags whose variant is null).
    options = items(f"/projects/{project['id']}/features/{feature['id']}/mv-options/")
    option = (options[0] if options else api.post(f"/projects/{project['id']}/features/{feature['id']}/mv-options/", {
        "type": "unicode", "string_value": "treatment", "key": "treatment", "default_percentage_allocation": 0,
        "feature": feature["id"]}))["id"]

    if not items(f"/environments/{key}/warehouse-connections/"):
        api.post(f"/environments/{key}/warehouse-connections/", {
            "warehouse_type": "postgres", "name": "Flagsmith database", "config": {"source": "flagsmith_database"}})
    metrics = {m["name"]: m for m in items(f"/environments/{key}/experiment-metrics/")}
    metric = metrics.get(METRIC_EVENT) or api.post(f"/environments/{key}/experiment-metrics/", {
        "name": METRIC_EVENT, "aggregation": "occurrence", "direction": "up",
        "definition": {"version": 1, "event": METRIC_EVENT}})

    running = [x for x in items(f"/environments/{key}/experiments/") if x["status"] == "running"]
    exp = running[0] if running else api.post(f"/environments/{key}/experiments/", {
        "feature": feature["id"], "name": "Checkout button copy",
        "hypothesis": "'Get it today' converts better than 'Buy now'",
        "metrics": [{"metric": str(metric["id"]), "expected_direction": "increase"}],
        "experiment_rollout": {"enabled": True, "rollout_percentage": 100,
                               "feature_state_value": {"type": "string", "value": "control"},
                               "multivariate_feature_state_values": [
                                   {"multivariate_feature_option": option, "percentage_allocation": 50}]}})
    if exp["status"] != "running":
        exp = api.post(f"/environments/{key}/experiments/{exp['id']}/start/")

    env.update(EXPERIMENT_ENV_KEY=key, EXPERIMENT_ID=str(exp["id"]), EXPERIMENT_OPTION_ID=str(option))
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    sim = ROOT / "simulator" / "sim.toml"
    text = re.sub(r'environment_key = ".*"', f'environment_key = "{key}"', sim.read_text())
    sim.write_text(text.replace("localhost:8000", "localhost:8100"))
    print(f"ok: experiment {exp['id']} {exp['status']} on {FEATURE} in project marshal-demo; sim.toml updated")


if __name__ == "__main__":
    main()
