"""Barcode-label print queue — the CRM server (a remote VPS) has no
network path to a printer sitting behind a shop/home router, so a
staff member's "🖨 Отправить на печать" tap just enqueues a job here;
a small poller (print_agent.py, run on a Linux box on the same LAN as
the printer) picks it up, prints it, and acks it. See
webapp.routers.print_agent for the agent-facing endpoints and
webapp.routers.inventory for where a job gets created.
"""
from __future__ import annotations

import sqlite3


def create_job(conn: sqlite3.Connection, product_id: int, staff_id: int) -> int:
    return conn.execute(
        "INSERT INTO print_jobs (product_id, staff_id) VALUES (?, ?)",
        (product_id, staff_id),
    ).lastrowid


def list_pending_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM print_jobs WHERE status = 'pending' ORDER BY id"
    ).fetchall()


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM print_jobs WHERE id = ?", (job_id,)).fetchone()


def mark_printed(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE print_jobs SET status = 'printed', printed_at = datetime('now') WHERE id = ?",
        (job_id,),
    )


def mark_failed(conn: sqlite3.Connection, job_id: int, error: str) -> None:
    conn.execute(
        "UPDATE print_jobs SET status = 'failed', error = ? WHERE id = ?",
        (error, job_id),
    )


def job_status(conn: sqlite3.Connection, job_id: int) -> str | None:
    row = conn.execute("SELECT status FROM print_jobs WHERE id = ?", (job_id,)).fetchone()
    return row["status"] if row else None
