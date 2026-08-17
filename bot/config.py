import os

BOT_TOKEN = os.environ.get("CRM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("CRM_BOT_TOKEN env var is required")

MINIAPP_URL = os.environ.get("CRM_MINIAPP_URL")
if not MINIAPP_URL:
    raise RuntimeError("CRM_MINIAPP_URL env var is required (https:// URL of the web panel /miniapp)")
