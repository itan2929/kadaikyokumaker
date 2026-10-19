import os
from pathlib import Path

import requests
import yaml


APPLICATION_ID = "1344306931598360627"
DISCORD_API_URL = f"https://discord.com/api/v10/applications/{APPLICATION_ID}/commands"
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

if not BOT_TOKEN:
    raise SystemExit("Set DISCORD_BOT_TOKEN in the environment before registering commands.")

commands_path = Path(__file__).with_name("discord_commands.yaml")
with commands_path.open("r", encoding="utf-8-sig") as file:
    commands = yaml.safe_load(file) or []

headers = {
    "Authorization": f"Bot {BOT_TOKEN}",
    "Content-Type": "application/json",
}

for command in commands:
    response = requests.post(
        DISCORD_API_URL,
        json=command,
        headers=headers,
        timeout=(5, 20),
    )
    response.raise_for_status()
    print(f"Registered /{command['name']}")
