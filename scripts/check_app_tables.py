"""Assert that no table the app renders shows a raw, unformatted number.

    python scripts/check_app_tables.py

Why this exists: the Data Health scorecard rendered its FP column as
"855.000000". The previous version of this check passed, for two reasons worth
recording because both are easy to repeat:

 1. It only inspected plain DataFrames. The scorecard is passed to
    st.dataframe as a pandas Styler, so it was skipped entirely.
 2. It looked at dtypes rather than at output. FP is float only because the
    per-type rows store NULL there - every value it holds is a whole number, so
    a dtype check would have called it "numeric and fine" either way.

So this version renders each table to HTML - Styler or DataFrame - and reads
the cell text the user would actually see. Two defects are flagged:

    excess precision   855.000000, 0.027304   (4+ decimal places, per cell)
    integer column     a column whose every value is a whole number GREATER
                       THAN ONE, shown with a decimal tail - 855.0, 57.0

The second test is per COLUMN, not per cell, and the "greater than one" clause
matters. A single "1.00" among 0.027 and 0.950 is a deliberate 2dp format. A
recall column holding only 0.00 and 1.00 is also deliberate - every value is
whole, but it is a ratio bounded at 1, not a count. A column reading 855.0 /
57.0 is a count being rendered as a float. Requiring a value above 1
separates the two without needing to know the column's meaning.

It drives the real app with a stubbed Streamlit, so it checks every table on
every page rather than a hand-maintained list.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EXCESS_PRECISION = re.compile(r"^-?[\d,]+\.\d{4,}$")
FLOAT_INTEGER = re.compile(r"^-?[\d,]+\.0+$")
ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
CELL = re.compile(r"<t([dh])[^>]*>(.*?)</t\1>", re.DOTALL)
TAG = re.compile(r"<[^>]+>")

TABLES: list = []


def _install_stubs() -> None:
    """A Streamlit stub that records every table and swallows everything else."""
    class Ctx:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class State(dict):
        def __getattr__(self, k):
            try: return self[k]
            except KeyError: raise AttributeError(k)
        def __setattr__(self, k, v): self[k] = v

    class Sidebar:
        title = caption = warning = staticmethod(lambda *a, **k: None)
        def radio(self, label, options, **k): return options[0]
        def selectbox(self, label, options, index=0, **k): return options[index]

    st = types.ModuleType("streamlit")
    for name in ("markdown", "caption", "info", "warning", "error", "title",
                 "set_page_config", "altair_chart", "rerun"):
        setattr(st, name, lambda *a, **k: None)
    st.dataframe = lambda obj, **k: TABLES.append(obj)
    st.columns = lambda spec, **k: [Ctx() for _ in (range(spec) if isinstance(spec, int) else spec)]
    st.button = lambda *a, **k: False
    # Widgets return their default so the app renders its default state.
    st.checkbox = lambda label, value=False, **k: value
    st.toggle = lambda label, value=False, **k: value
    st.slider = lambda label, *a, value=None, **k: value
    st.text_input = lambda label, value="", **k: value
    st.radio = lambda label, options, **k: options[0]
    st.selectbox = lambda label, options, index=0, **k: options[index]
    st.expander = lambda *a, **k: Ctx()
    st.json = lambda *a, **k: None
    st.code = lambda *a, **k: None
    st.session_state = State()
    st.sidebar = Sidebar()

    def cache_data(*a, **k):
        if len(a) == 1 and callable(a[0]) and not k:
            return a[0]
        return lambda fn: fn
    st.cache_data = cache_data
    sys.modules["streamlit"] = st

    import snowflake.connector
    snowflake.connector.paramstyle = "qmark"
    sys.path.insert(0, str(REPO_ROOT))
    from common.snowflake_conn import connect

    conn = connect(schema="MARTS")

    class Result:
        def __init__(self, cur): self._cur = cur
        def to_pandas(self): return self._cur.fetch_pandas_all()

    class Session:
        def sql(self, sql, params=None):
            cur = conn.cursor()
            cur.execute(sql, params) if params else cur.execute(sql)
            return Result(cur)

    ctx_mod = types.ModuleType("snowflake.snowpark.context")
    ctx_mod.get_active_session = lambda: Session()
    sys.modules["snowflake.snowpark"] = types.ModuleType("snowflake.snowpark")
    sys.modules["snowflake.snowpark.context"] = ctx_mod
    sys.path.insert(0, str(REPO_ROOT / "app"))
    return conn


def body_columns(table) -> list[list[str]]:
    """Rendered body cells grouped by column, for a Styler or a DataFrame.

    Parsed from the HTML rather than the dtypes, because the defect is in what
    the user sees. Header rows (all <th>) are skipped so column names are not
    mistaken for values.
    """
    html = table.to_html() if hasattr(table, "to_html") else str(table)
    rows: list[list[str]] = []
    for raw_row in ROW.findall(html):
        cells = CELL.findall(raw_row)
        if not cells or all(kind == "h" for kind, _ in cells):
            continue  # header row
        values = [
            TAG.sub("", body).replace("&nbsp;", " ").strip()
            for kind, body in cells if kind == "d"
        ]
        if values:
            rows.append(values)
    if not rows:
        return []
    width = max(len(r) for r in rows)
    return [[r[i] for r in rows if i < len(r)] for i in range(width)]


def main() -> int:
    conn = _install_stubs()
    import streamlit as st  # the stub
    try:
        import streamlit_app as app
        for page in ("Weekly Review", "Data Health", "AI Summary"):
            st.session_state.clear()
            st.session_state["page"] = page
            st.sidebar.radio = lambda label, options, page=page, **k: page
            app.main()
    finally:
        conn.close()

    print(f"Inspected {len(TABLES)} rendered table(s).")
    failures: list[tuple[int, str, str]] = []
    for i, table in enumerate(TABLES):
        for col_index, column in enumerate(body_columns(table)):
            # Per cell: excess precision is always a defect.
            for text in column:
                if EXCESS_PRECISION.match(text):
                    failures.append((i, "excess precision", text))
            # Per column: all values whole numbers with a decimal tail means an
            # integer column is being rendered as a float.
            numeric = [t for t in column if re.match(r"^-?[\d,]+(\.\d+)?$", t)]
            if numeric and all(FLOAT_INTEGER.match(t) for t in numeric):
                magnitudes = [abs(float(t.replace(",", ""))) for t in numeric]
                # A ratio column of 0.00 / 1.00 is formatted on purpose; a count
                # column carries values above 1.
                if max(magnitudes) > 1:
                    failures.append(
                        (i, "integer column", f"column {col_index}: "
                                              f"{', '.join(numeric[:4])}")
                    )

    if failures:
        print(f"\nFAIL: {len(failures)} unformatted cell(s):")
        seen = set()
        for idx, kind, text in failures:
            key = (idx, kind, text)
            if key in seen:
                continue
            seen.add(key)
            print(f"   table {idx}  [{kind}]  {text}")
        print("\nFormat these columns in app/streamlit_app.py via styled_table().")
        return 1

    print("PASS: every rendered cell is formatted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
