_GENERIC_SEAT_NAMES = {"机构专用", "深股通专用", "沪股通专用"}


def seat_problem(rows: list[dict]) -> str | None:
    seen_rows: set[tuple] = set()
    seen_codes: set[tuple] = set()
    seen_names: set[tuple] = set()
    for row in rows:
        buy = row.get("buy_amount")
        sell = row.get("sell_amount")
        net = row.get("net_buy_amount")
        if buy is not None and sell is not None and net is not None:
            if abs((buy - sell) - net) > 0.02:
                return f"席位净额不一致: {row.get('seat_name') or ''}"
        seat_type = row.get("seat_type")
        seat_code = row.get("seat_code")
        seat_name = " ".join((row.get("seat_name") or "").split())
        identity = (seat_type, seat_code, seat_name, buy, sell, net)
        if identity in seen_rows:
            return f"席位重复: {seat_name}"
        if seat_code is not None:
            code_identity = (seat_type, seat_code)
            if code_identity in seen_codes:
                return f"席位重复: {seat_name}"
            seen_codes.add(code_identity)
        elif seat_name and seat_name not in _GENERIC_SEAT_NAMES:
            name_identity = (seat_type, seat_name)
            if name_identity in seen_names:
                return f"席位重复: {seat_name}"
            seen_names.add(name_identity)
        seen_rows.add(identity)
    return None
