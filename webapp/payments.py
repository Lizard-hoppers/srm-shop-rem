"""Reading the «Оплата» block of a form (templates/_payments.html): one
amount field per account of the точка, `pay_<account_id>`, plus
`rate_<account_id>` next to a foreign-currency one. Whatever was left
empty wasn't paid into. core.cash.resolve_payments validates the result."""
from __future__ import annotations


def payments_from_form(form) -> list[tuple[int, str, str | None]]:
    payments = []
    for name, value in form.items():
        if not name.startswith("pay_") or not isinstance(value, str) or not value.strip():
            continue
        account_part = name[len("pay_"):]
        if not account_part.isdigit():
            continue
        payments.append((int(account_part), value, form.get(f"rate_{account_part}")))
    return payments
