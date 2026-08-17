"""Section landing pages for the bottom tab bar — Склад and Ещё each group
several pages that don't fit as their own top-level tab."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from webapp.deps import require_staff
from webapp.templating import render

router = APIRouter()


@router.get("/warehouse")
def warehouse_hub(request: Request, staff=Depends(require_staff)):
    return render(request, "warehouse_hub.html", staff=staff)


@router.get("/more")
def more_hub(request: Request, staff=Depends(require_staff)):
    return render(request, "more_hub.html", staff=staff)
