"""Create the sandbox admin, organisation and admin API key on the local Flagsmith (port 8100).

Idempotent. Writes sandbox/.env (git-ignored): FLAGSMITH_API_URL, FLAGSMITH_API_KEY_localhost_8100 (the name the
Flagsmith CLI looks for), FLAGSMITH_ADMIN_EMAIL, FLAGSMITH_ADMIN_PASSWORD, FLAGSMITH_ORGANISATION. Prints no secrets.
"""
import json
import secrets
import subprocess
import urllib.request
from pathlib import Path

API = "http://localhost:8100/api/v1"
ENV_FILE = Path(__file__).with_name(".env")
KEY_VAR = "FLAGSMITH_API_KEY_localhost_8100"


def call(method, path, body=None, token=None, key=None):
    req = urllib.request.Request(f"{API}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Token {token}")
    if key:
        req.add_header("Authorization", f"Api-Key {key}")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or "null")


def main():
    env = dict(l.split("=", 1) for l in ENV_FILE.read_text().splitlines() if "=" in l) if ENV_FILE.exists() else {}
    env.setdefault("FLAGSMITH_API_URL", "http://localhost:8100")
    env.setdefault("FLAGSMITH_ADMIN_EMAIL", "admin@marshal.test")
    env.setdefault("FLAGSMITH_ADMIN_PASSWORD", secrets.token_urlsafe(18))
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in env.items()))

    # createsuperuser fails harmlessly when the user exists; it needs these fields on 2.295.2.
    subprocess.run(["docker", "compose", "exec", "-T",
                    "-e", f"DJANGO_SUPERUSER_EMAIL={env['FLAGSMITH_ADMIN_EMAIL']}",
                    "-e", f"DJANGO_SUPERUSER_PASSWORD={env['FLAGSMITH_ADMIN_PASSWORD']}",
                    "-e", "DJANGO_SUPERUSER_FIRST_NAME=Sandbox", "-e", "DJANGO_SUPERUSER_LAST_NAME=Admin",
                    "-e", "DJANGO_SUPERUSER_SIGN_UP_TYPE=NO_INVITE",
                    "flagsmith", "python", "manage.py", "createsuperuser", "--noinput"],
                   cwd=Path(__file__).parent, capture_output=True, check=False)
    token = call("POST", "/auth/login/", {"email": env["FLAGSMITH_ADMIN_EMAIL"],
                                          "password": env["FLAGSMITH_ADMIN_PASSWORD"]})["key"]
    # Created with the login token: creating an organisation with an API key fails on 2.295.2.
    orgs = {o["name"]: o for o in call("GET", "/organisations/", token=token)["results"]}
    org = orgs.get("Marshal Sandbox") or call("POST", "/organisations/", {"name": "Marshal Sandbox"}, token=token)
    env["FLAGSMITH_ORGANISATION"] = str(org["id"])
    if KEY_VAR not in env:
        env[KEY_VAR] = call("POST", f"/organisations/{org['id']}/master-api-keys/",
                            {"name": "marshal-sandbox", "is_admin": True}, token=token)["key"]
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    ok = org["id"] in [o["id"] for o in call("GET", "/organisations/", key=env[KEY_VAR])["results"]]
    print(f"ok: organisation {org['name']!r} (id {org['id']}), admin API key works: {ok}; wrote {ENV_FILE.name}")


if __name__ == "__main__":
    main()
