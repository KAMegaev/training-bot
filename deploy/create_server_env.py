"""Create a local server-only database credential without printing it."""
import os
import secrets
from pathlib import Path

path = Path(".env.server")
if path.exists():
    print(".env.server already exists; unchanged")
else:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write("POSTGRES_PASSWORD=" + secrets.token_urlsafe(36) + "\n")
    print("Created .env.server with mode 0600")
