"""Endpoints for print_agent.py — a small unattended poller Павел runs
on a Linux box on the same LAN as the Xprinter XP-420B (19.08). Not
staff-authenticated (no Telegram session token involved at all): the
agent is a long-running background process with its own shared secret
(PRINT_AGENT_TOKEN), checked on every call via _require_agent.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from core import barcode_label
from core import inventory as core_inventory
from core import print_queue
from core.storage import get_conn

router = APIRouter(prefix="/print-agent")

_AGENT_TOKEN = os.environ.get("PRINT_AGENT_TOKEN")


def _require_agent(token: str | None) -> None:
    if not _AGENT_TOKEN or not token or token != _AGENT_TOKEN:
        raise HTTPException(status_code=403, detail="Неверный или отсутствующий токен агента печати.")


@router.get("/jobs")
def list_jobs(token: str | None = None):
    _require_agent(token)
    with get_conn() as conn:
        jobs = print_queue.list_pending_jobs(conn)
    return JSONResponse({"jobs": [{"id": j["id"], "product_id": j["product_id"]} for j in jobs]})


@router.get("/jobs/{job_id}/label.png")
def job_label(job_id: int, token: str | None = None):
    _require_agent(token)
    with get_conn() as conn:
        job = print_queue.get_job(conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Задание не найдено.")
        product = core_inventory.get_product(conn, job["product_id"])
    if not product or not product["sku"]:
        raise HTTPException(status_code=404, detail="У товара нет SKU.")
    png = barcode_label.generate_label_png(product["sku"], product["name"], product["price"], compact=True)
    return Response(content=png, media_type="image/png")


@router.post("/jobs/{job_id}/ack")
def ack_job(job_id: int, token: str | None = None, ok: bool = True, error: str = ""):
    _require_agent(token)
    with get_conn() as conn:
        if not print_queue.get_job(conn, job_id):
            raise HTTPException(status_code=404, detail="Задание не найдено.")
        if ok:
            print_queue.mark_printed(conn, job_id)
        else:
            print_queue.mark_failed(conn, job_id, error or "агент сообщил об ошибке печати")
    return JSONResponse({"ok": True})
