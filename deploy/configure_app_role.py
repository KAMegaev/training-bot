"""Run on the server after `tools init`; secrets travel only through stdin/files."""
import os
import re
import secrets
import subprocess
from pathlib import Path


def main():
    path = Path(".env.server")
    content = path.read_text()
    values = dict(line.split("=", 1) for line in content.splitlines() if "=" in line and not line.startswith("#"))
    password = values.get("APP_DB_PASSWORD") or secrets.token_urlsafe(36)
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", password):
        raise ValueError("Unsupported application password format")
    sql = f"""
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='training_app') THEN
  CREATE ROLE training_app LOGIN;
 END IF;
END $$;
ALTER ROLE training_app PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT CONNECT ON DATABASE trainer TO training_app;
GRANT USAGE ON SCHEMA public TO training_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO training_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO training_app;
ALTER DEFAULT PRIVILEGES FOR ROLE trainer IN SCHEMA public
 GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO training_app;
ALTER DEFAULT PRIVILEGES FOR ROLE trainer IN SCHEMA public
 GRANT USAGE, SELECT ON SEQUENCES TO training_app;
"""
    # Use a temporary env for Compose parsing; do not print its expanded config.
    environment = os.environ | {"APP_DB_PASSWORD": password}
    result = subprocess.run(["docker", "compose", "--env-file", str(path), "exec", "-T", "db",
                             "psql", "-U", "trainer", "-d", "trainer", "-v", "ON_ERROR_STOP=1"],
                            input=sql, text=True, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, env=environment)
    if result.returncode:
        raise RuntimeError("Database role configuration failed; no credentials were logged")
    if "APP_DB_PASSWORD" not in values:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
        with os.fdopen(descriptor, "w") as output:
            output.write(("" if content.endswith("\n") else "\n") + "APP_DB_PASSWORD=" + password + "\n")
    path.chmod(0o600)
    print("Application database role configured")


if __name__ == "__main__":
    main()
