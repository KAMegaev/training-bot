"""Create a local server-only database credential without printing it."""
import os
import secrets
from pathlib import Path

path = Path(".env.server")
if not path.exists():
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write("POSTGRES_PASSWORD=" + secrets.token_urlsafe(36) + "\n")
content = path.read_text()
if not any(line.startswith("APP_DB_PASSWORD=") for line in content.splitlines()):
    with path.open("a") as output:
        output.write(("" if content.endswith("\n") else "\n") + "APP_DB_PASSWORD=" + secrets.token_urlsafe(36) + "\n")
path.chmod(0o600)
bot_path = Path(".env.bot")
if not bot_path.exists():
    descriptor = os.open(bot_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
print("Server env files ready with mode 0600")
