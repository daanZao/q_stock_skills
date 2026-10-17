"""申万股票分类文件解析（issue #73，spec #70）：纯函数，不连库。

旧版 .xls（BIFF，xlrd 懒加载读取，未安装报明确错误），首个 sheet 表头
固定四列：股票代码 / 计入日期 / 行业代码 / 更新日期。文件含全历史变更
（同股票多行不同计入日期）。股票代码与行业代码统一补零至 6 位字符串；
计入日期解析为 date，更新日期解析为 datetime。

另含 sw_industry_meta 种子加载（issue #74）：仓库内置
data/sw_industry_meta.csv（申万一/二/三级行业代码+名称，多版本合并，
来源与口径见 docs/research/2026-10-17-sw-industry-meta-sources.md）。
"""

import csv
from datetime import datetime
from importlib import resources
from pathlib import Path

EXPECTED_HEADER = ["股票代码", "计入日期", "行业代码", "更新日期"]


class ClassificationFileError(ValueError):
    """分类文件缺失或格式不符。"""


def _xlrd():
    try:
        import xlrd
    except ImportError as e:
        raise ClassificationFileError(
            "解析申万 .xls 分类文件需要 xlrd：pip install qstock-mcp[classify]"
        ) from e
    return xlrd


def _norm_code(value, ctype: int, row_no: int, field: str) -> str:
    """代码单元格（文本或数字）归一化为 6 位数字字符串。"""
    if ctype == 2:  # number：Excel 数字单元格丢前导零
        value = int(value)
    code = str(value).strip()
    if not code.isdigit():
        raise ClassificationFileError(f"第 {row_no} 行{field}非法: {value!r}")
    return code.zfill(6)


def _norm_dt(xlrd, book, value, ctype: int, row_no: int, field: str) -> datetime:
    if ctype == 3:  # date
        return xlrd.xldate_as_datetime(value, book.datemode)
    try:
        return datetime.fromisoformat(str(value).strip())
    except ValueError as e:
        raise ClassificationFileError(
            f"第 {row_no} 行{field}日期非法: {value!r}"
        ) from e


def parse_sw_classification(file_path: str) -> list[dict]:
    """解析申万分类 xls → 行 dict 列表。

    行键：stock_code / board_code / effective_date(date) / updated_at(datetime)。
    """
    xlrd = _xlrd()
    if not Path(file_path).is_file():
        raise ClassificationFileError(f"文件不存在: {file_path}")
    try:
        book = xlrd.open_workbook(file_path)
    except Exception as e:  # noqa: BLE001 - XLRDError 等统一收敛为格式错误
        raise ClassificationFileError(f"无法按 .xls 解析: {file_path}（{e}）") from e
    sheet = book.sheet_by_index(0)
    header = [str(v).strip() for v in sheet.row_values(0)] if sheet.nrows else []
    if header[:4] != EXPECTED_HEADER:
        raise ClassificationFileError(
            f"表头不符：期望 {EXPECTED_HEADER}，实际 {header[:4]}"
        )
    rows = []
    for r in range(1, sheet.nrows):
        ctypes = [sheet.cell_type(r, c) for c in range(4)]
        if all(t == 0 for t in ctypes):  # 整行空
            continue
        values = [sheet.cell_value(r, c) for c in range(4)]
        row_no = r + 1
        rows.append(
            {
                "stock_code": _norm_code(values[0], ctypes[0], row_no, "股票代码"),
                "effective_date": _norm_dt(
                    xlrd, book, values[1], ctypes[1], row_no, "计入日期"
                ).date(),
                "board_code": _norm_code(values[2], ctypes[2], row_no, "行业代码"),
                "updated_at": _norm_dt(xlrd, book, values[3], ctypes[3], row_no, "更新日期"),
            }
        )
    return rows


def load_sw_industry_meta_seed() -> list[dict]:
    """加载仓库内置 sw_industry_meta 种子（issue #74）→ 行 dict 列表。

    行键：industry_code / name / level(int)。种子按代码值收录、不区分
    分类版本（新版名称优先）。
    """
    seed = resources.files("qstock_mcp").joinpath("data/sw_industry_meta.csv")
    with seed.open(encoding="utf-8") as f:
        return [
            {
                "industry_code": r["industry_code"],
                "name": r["name"],
                "level": int(r["level"]),
            }
            for r in csv.DictReader(f)
        ]
