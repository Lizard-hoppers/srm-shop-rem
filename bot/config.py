import os

BOT_TOKEN = os.environ.get("CRM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("CRM_BOT_TOKEN env var is required")
