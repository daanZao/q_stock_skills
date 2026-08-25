"""数据库连接与建表。

连接串只从环境变量 `PG_DSN` 读取，不写死（决策见 docs/adr/0002）。
"""

import os
from importlib import resources

import psycopg2


class MissingDsnError(RuntimeError):
    """PG_DSN 未设置。"""


def get_dsn() -> str:
    dsn = os.environ.get("PG_DSN")
    if not dsn:
        raise MissingDsnError(
            "环境变量 PG_DSN 未设置：请配置 PostgreSQL 连接串，"
            "例如 postgresql://localhost/qstock"
        )
    return dsn


def ensure_schema(conn) -> list[str]:
    """幂等执行全部 DDL（sql/*.sql，按文件名排序），返回执行的文件名列表。"""
    sql_dir = resources.files("qstock_mcp").joinpath("sql")
    applied = []
    with conn.cursor() as cur:
        for entry in sorted(sql_dir.iterdir(), key=lambda e: e.name):
            if entry.name.endswith(".sql"):
                cur.execute(entry.read_text(encoding="utf-8"))
                applied.append(entry.name)
    conn.commit()
    return applied


def list_tables(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
        )
        return [row[0] for row in cur.fetchall()]
