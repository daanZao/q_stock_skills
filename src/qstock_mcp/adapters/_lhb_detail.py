def seat_problem(rows: list[dict]) -> str | None:
    seen: set[tuple] = set()
    for row in rows:
        buy = row.get("buy_amount")
        sell = row.get("sell_amount")
        net = row.get("net_buy_amount")
        if buy is not None and sell is not None and net is not None:
            if abs((buy - sell) - net) > 0.02:
                return f"席位净额不一致: {row.get('seat_name') or ''}"
        identity = (
            row.get("seat_type"), row.get("seat_code"), row.get("seat_name"),
            buy, sell, net,
        )
        code_identity = (row.get("seat_type"), row.get("seat_code"))
        if identity in seen or (
            row.get("seat_code") is not None and code_identity in seen
        ):
            return f"席位重复: {row.get('seat_name') or ''}"
        seen.add(identity)
        if row.get("seat_code") is not None:
            seen.add(code_identity)
    return None
