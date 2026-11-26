"""Thin client for the Flagsmith Admin API (master API key) and the SDK endpoints Marshal needs."""
import json
import urllib.error
import urllib.request


class FlagsmithError(RuntimeError):
    pass


class Admin:
    def __init__(self, api_url: str, api_key: str):
        self.base = api_url.rstrip("/") + "/api/v1"
        self.headers = {"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"}

    def call(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers or self.headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise FlagsmithError(f"{method} {path} -> {e.code}: {e.read()[:400].decode(errors='replace')}") from None

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body=None):
        return self.call("POST", path, body)

    def patch(self, path, body):
        return self.call("PATCH", path, body)

    # experiments
    def experiment(self, env_key, exp_id):
        return self.get(f"/environments/{env_key}/experiments/{exp_id}/")

    def results(self, env_key, exp_id):
        return self.get(f"/environments/{env_key}/experiments/{exp_id}/results/")

    def refresh_results(self, env_key, exp_id):
        return self.post(f"/environments/{env_key}/experiments/{exp_id}/results/refresh/")

    def metric(self, env_key, metric_id):
        return self.get(f"/environments/{env_key}/experiment-metrics/{metric_id}/")
