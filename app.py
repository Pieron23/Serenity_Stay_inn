from __future__ import annotations

import base64
from calendar import month_name, monthrange
from datetime import date, datetime, timedelta
import hashlib
import hmac
from io import BytesIO
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
from typing import Callable, Dict, Tuple

import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components

try:
    import psycopg
except ImportError:  # pragma: no cover - optional at import time
    psycopg = None


APP_TITLE = "Serenity Stay Inn Dashboard"
APP_DIR = Path(__file__).resolve().parent
LOGIN_BG_FILE = APP_DIR / "assets" / "login_background.jpg"


def _resolve_data_dir() -> Path:
    requested = os.getenv("SERENITY_DATA_DIR", "").strip()
    candidates = []
    if requested:
        candidates.append(Path(requested))
    candidates.extend([APP_DIR / "data", APP_DIR, Path("/tmp/serenity-data")])

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return candidate
        except Exception:
            continue

    return APP_DIR


DATA_DIR = _resolve_data_dir()
EXCEL_FILE = DATA_DIR / "guest_room_data.xlsx"
BUNDLED_EXCEL_FILE = APP_DIR / "guest_room_data.xlsx"
TUNNEL_LOG_FILE = APP_DIR / ".cloudflared_tunnel.log"
TUNNEL_URL_REGEX = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
DAILY_SHEET = "daily_revenue"
EXPENSE_SHEET = "non_fixed_expenses"
SETTINGS_SHEET = "settings"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

DEFAULT_SETTINGS = {
    "Initial_Balance": 369_308.0,
    "House_Rent": 590_000.0,
    "Labor": 290_000.0,
    "Water_Bill": 20_000.0,
    "Electricity": 30_000.0,
}
DEFAULT_SETTINGS["Total_Fixed_Cost"] = (
    DEFAULT_SETTINGS["House_Rent"]
    + DEFAULT_SETTINGS["Labor"]
    + DEFAULT_SETTINGS["Water_Bill"]
    + DEFAULT_SETTINGS["Electricity"]
)

DAILY_COLUMNS = ["Date", "Revenue_Type", "Revenue", "Note", "Month", "Year", "Created_At"]
EXPENSE_COLUMNS = ["Date", "Expense", "Category", "Note", "Month", "Year", "Created_At"]
REVENUE_TYPES = ("Rooms", "Bar", "Wedding")
EDIT_PIN_HASH = (
    "8c144858bb5ea1a931069f55943c55a4"
    "27e2530405bbe720e97aae0fda85ee8c"
)
LOGIN_PIN_HASH = (
    "2a7ee6dda455e1d550ab5f16df55363c"
    "a2d091bf2802c4a70108db362dddabd5"
)


# -----------------------------
# Formatting helpers
# -----------------------------
def format_rwf(value: float) -> str:
    return f"{value:,.0f} RWF"


def safe_float(value: float | int | None) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_money_input(raw_value: str) -> Tuple[bool, float, str]:
    raw_text = str(raw_value).strip()
    if not raw_text:
        return False, 0.0, "Enter a revenue amount."

    normalized = raw_text.replace(",", "").replace(" ", "")
    try:
        amount = float(normalized)
    except ValueError:
        return False, 0.0, "Revenue must be a valid number (example: 25000)."

    if amount < 0:
        return False, 0.0, "Revenue cannot be negative."

    return True, amount, ""


def parse_expense_input(raw_value: str) -> Tuple[bool, float, str]:
    raw_text = str(raw_value).strip()
    if not raw_text:
        return False, 0.0, "Enter an expense amount."

    normalized = raw_text.replace(",", "").replace(" ", "")
    try:
        amount = float(normalized)
    except ValueError:
        return False, 0.0, "Expense must be a valid number (example: 12000)."

    if amount < 0:
        return False, 0.0, "Expense cannot be negative."

    return True, amount, ""


def verify_edit_pin(pin: str) -> bool:
    """Validate edit PIN using a hash so the raw PIN is not stored in source."""
    candidate = pin.strip()
    if not candidate:
        return False
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, EDIT_PIN_HASH)


def verify_login_pin(pin: str) -> bool:
    """Validate login PIN using a hash so the raw PIN is not stored in source."""
    candidate = pin.strip()
    if not candidate:
        return False
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, LOGIN_PIN_HASH)


def normalize_revenue_type(revenue_type: str) -> str:
    candidate = str(revenue_type).strip().lower()
    if candidate == "bar":
        return "Bar"
    if candidate in {"wedding", "weddings"}:
        return "Wedding"
    return "Rooms"


def auto_unlock_login() -> None:
    candidate = str(st.session_state.get("login_pin_input", "")).strip()
    if not candidate:
        st.session_state["login_pin_invalid"] = False
        return
    if verify_login_pin(candidate):
        st.session_state["is_logged_in"] = True
        st.session_state["login_pin_invalid"] = False
        st.session_state["login_pin_input"] = ""
        st.session_state["flash_message"] = {"ok": True, "message": "Access granted."}
    else:
        st.session_state["login_pin_invalid"] = True


def auto_unlock_sensitive_numbers() -> None:
    candidate = str(st.session_state.get("sensitive_pin_input", "")).strip()
    if not candidate:
        st.session_state["sensitive_pin_invalid"] = False
        return
    if verify_edit_pin(candidate):
        st.session_state["view_unlocked"] = True
        st.session_state["sensitive_pin_invalid"] = False
        st.session_state["sensitive_pin_input"] = ""
        st.session_state["flash_message"] = {
            "ok": True,
            "message": "Protected numbers unlocked.",
        }
    else:
        st.session_state["sensitive_pin_invalid"] = True


def auto_unlock_edit_mode() -> None:
    candidate = str(st.session_state.get("edit_pin_input", "")).strip()
    if not candidate:
        st.session_state["edit_pin_invalid"] = False
        return
    if verify_edit_pin(candidate):
        st.session_state["edit_unlocked"] = True
        st.session_state["edit_pin_invalid"] = False
        st.session_state["edit_pin_input"] = ""
        st.session_state["flash_message"] = {"ok": True, "message": "Edit access unlocked."}
    else:
        st.session_state["edit_pin_invalid"] = True


def build_access_links() -> list[str]:
    try:
        port = int(st.get_option("server.port"))
    except Exception:
        port = 8501

    links = [f"http://localhost:{port}"]

    candidate_ips = set()
    try:
        candidate_ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            candidate_ips.add(sock.getsockname()[0])
    except Exception:
        pass

    for ip in sorted(candidate_ips):
        if ip and not ip.startswith("127.") and "." in ip:
            links.append(f"http://{ip}:{port}")

    # Preserve order and remove duplicates.
    return list(dict.fromkeys(links))


def _init_tunnel_state() -> None:
    defaults = {
        "public_tunnel_process": None,
        "public_tunnel_log_handle": None,
        "public_tunnel_log_path": str(TUNNEL_LOG_FILE),
        "public_tunnel_url": "",
        "public_tunnel_error": "",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _read_tunnel_url_from_log() -> str:
    log_path = Path(str(st.session_state.get("public_tunnel_log_path", TUNNEL_LOG_FILE)))
    if not log_path.exists():
        return ""
    text = log_path.read_text(encoding="utf-8", errors="ignore")
    match = TUNNEL_URL_REGEX.search(text)
    return match.group(0) if match else ""


def find_cloudflared_binary() -> str:
    binary = shutil.which("cloudflared")
    if binary:
        return binary

    windows_candidates = [
        Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
        Path(r"C:\Program Files\cloudflared\cloudflared.exe"),
    ]
    for candidate in windows_candidates:
        if candidate.exists():
            return str(candidate)

    return ""


def public_tunnel_running() -> bool:
    process = st.session_state.get("public_tunnel_process")
    return bool(process is not None and process.poll() is None)


def refresh_public_tunnel_state() -> None:
    _init_tunnel_state()
    discovered_url = _read_tunnel_url_from_log()
    if discovered_url:
        st.session_state["public_tunnel_url"] = discovered_url

    process = st.session_state.get("public_tunnel_process")
    if process is None:
        return
    if process.poll() is not None and not st.session_state.get("public_tunnel_url"):
        st.session_state["public_tunnel_error"] = (
            "Public tunnel stopped unexpectedly. Start it again."
        )


def start_public_tunnel() -> Tuple[bool, str]:
    _init_tunnel_state()
    refresh_public_tunnel_state()

    if public_tunnel_running():
        url = str(st.session_state.get("public_tunnel_url", "")).strip()
        if url:
            return True, f"Public link is active: {url}"
        return True, "Public tunnel is starting. Please wait a few seconds."

    cloudflared = find_cloudflared_binary()
    if not cloudflared:
        return (
            False,
            "cloudflared is not installed. Install it, then click Start public link again.",
        )

    try:
        TUNNEL_LOG_FILE.write_text("", encoding="utf-8")
        log_handle = open(TUNNEL_LOG_FILE, "a", encoding="utf-8")
        port = int(st.get_option("server.port") or 8501)
        process = subprocess.Popen(
            [
                cloudflared,
                "tunnel",
                "--url",
                f"http://127.0.0.1:{port}",
                "--no-autoupdate",
            ],
            cwd=str(APP_DIR),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except Exception as exc:
        return False, f"Failed to start public tunnel: {exc}"

    st.session_state["public_tunnel_process"] = process
    st.session_state["public_tunnel_log_handle"] = log_handle
    st.session_state["public_tunnel_url"] = ""
    st.session_state["public_tunnel_error"] = ""
    return True, "Public tunnel started. Your internet link will appear in a few seconds."


def stop_public_tunnel() -> Tuple[bool, str]:
    _init_tunnel_state()
    process = st.session_state.get("public_tunnel_process")
    log_handle = st.session_state.get("public_tunnel_log_handle")

    try:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=4)
    except Exception:
        try:
            if process is not None and process.poll() is None:
                process.kill()
        except Exception:
            pass

    try:
        if log_handle is not None and not log_handle.closed:
            log_handle.close()
    except Exception:
        pass

    st.session_state["public_tunnel_process"] = None
    st.session_state["public_tunnel_log_handle"] = None
    st.session_state["public_tunnel_url"] = ""
    st.session_state["public_tunnel_error"] = ""
    return True, "Public tunnel stopped."


def _require_postgres_driver() -> None:
    if psycopg is None:
        raise RuntimeError(
            "DATABASE_URL is set, but psycopg is not installed. "
            "Run: pip install 'psycopg[binary]>=3.2.0'"
        )


def _pg_connect():
    _require_postgres_driver()
    return psycopg.connect(DATABASE_URL)


def _initialize_postgres() -> None:
    _require_postgres_driver()
    with _pg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_revenue (
                    entry_date DATE NOT NULL,
                    revenue_type TEXT NOT NULL,
                    revenue DOUBLE PRECISION NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    month INTEGER NOT NULL,
                    year INTEGER NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_daily_revenue UNIQUE (entry_date, revenue_type)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS non_fixed_expenses (
                    id BIGSERIAL PRIMARY KEY,
                    entry_date DATE NOT NULL,
                    expense DOUBLE PRECISION NOT NULL DEFAULT 0,
                    category TEXT NOT NULL DEFAULT 'Unexpected',
                    note TEXT NOT NULL DEFAULT '',
                    month INTEGER NOT NULL,
                    year INTEGER NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    setting TEXT PRIMARY KEY,
                    value DOUBLE PRECISION NOT NULL
                )
                """
            )
            for key, value in DEFAULT_SETTINGS.items():
                cur.execute(
                    """
                    INSERT INTO settings (setting, value)
                    VALUES (%s, %s)
                    ON CONFLICT (setting) DO NOTHING
                    """,
                    (key, float(value)),
                )
        conn.commit()


# -----------------------------
# Excel data layer
# -----------------------------
def initialize_excel_file(path: Path = EXCEL_FILE) -> None:
    """Initialize storage backend (Postgres when configured, else local Excel)."""
    if USE_POSTGRES:
        _initialize_postgres()
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return

    # First-time cloud deploy convenience: seed persistent storage from bundled data if available.
    if path != BUNDLED_EXCEL_FILE and BUNDLED_EXCEL_FILE.exists():
        try:
            shutil.copy2(BUNDLED_EXCEL_FILE, path)
            return
        except Exception:
            pass

    daily_df = pd.DataFrame(columns=DAILY_COLUMNS)
    expense_df = pd.DataFrame(columns=EXPENSE_COLUMNS)
    settings_df = pd.DataFrame(
        {
            "Setting": list(DEFAULT_SETTINGS.keys()),
            "Value": list(DEFAULT_SETTINGS.values()),
        }
    )

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        daily_df.to_excel(writer, sheet_name=DAILY_SHEET, index=False)
        expense_df.to_excel(writer, sheet_name=EXPENSE_SHEET, index=False)
        settings_df.to_excel(writer, sheet_name=SETTINGS_SHEET, index=False)


def _normalize_daily_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=DAILY_COLUMNS)

    for col in DAILY_COLUMNS:
        if col not in df.columns:
            if col in {"Note", "Created_At"}:
                df[col] = ""
            elif col == "Revenue_Type":
                df[col] = "Rooms"
            else:
                df[col] = 0

    df = df[DAILY_COLUMNS].copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
    df["Revenue_Type"] = df["Revenue_Type"].fillna("Rooms").astype(str).map(normalize_revenue_type)
    df["Revenue"] = pd.to_numeric(df["Revenue"], errors="coerce").fillna(0.0)
    df["Note"] = df["Note"].fillna("").astype(str)
    df["Month"] = pd.to_numeric(df["Month"], errors="coerce").fillna(0).astype(int)
    df["Year"] = pd.to_numeric(df["Year"], errors="coerce").fillna(0).astype(int)
    df["Created_At"] = df["Created_At"].fillna("").astype(str)

    df = df.dropna(subset=["Date"]).sort_values(["Date", "Revenue_Type"]).reset_index(drop=True)

    if not df.empty:
        needs_month = df["Month"].eq(0)
        needs_year = df["Year"].eq(0)
        df.loc[needs_month, "Month"] = pd.to_datetime(df.loc[needs_month, "Date"]).dt.month
        df.loc[needs_year, "Year"] = pd.to_datetime(df.loc[needs_year, "Date"]).dt.year

    return df


def read_daily_data(path: Path = EXCEL_FILE) -> pd.DataFrame:
    initialize_excel_file(path)
    if USE_POSTGRES:
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        entry_date AS "Date",
                        revenue_type AS "Revenue_Type",
                        revenue AS "Revenue",
                        note AS "Note",
                        month AS "Month",
                        year AS "Year",
                        TO_CHAR(created_at, 'YYYY-MM-DD HH24:MI:SS') AS "Created_At"
                    FROM daily_revenue
                    ORDER BY entry_date, revenue_type
                    """
                )
                rows = cur.fetchall()
                columns = [desc[0] for desc in cur.description] if cur.description else DAILY_COLUMNS
        df = pd.DataFrame(rows, columns=columns)
        return _normalize_daily_dataframe(df)

    try:
        df = pd.read_excel(path, sheet_name=DAILY_SHEET, engine="openpyxl")
    except ValueError:
        df = pd.DataFrame(columns=DAILY_COLUMNS)
    return _normalize_daily_dataframe(df)


def _normalize_expense_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=EXPENSE_COLUMNS)

    for col in EXPENSE_COLUMNS:
        if col not in df.columns:
            if col in {"Category", "Note", "Created_At"}:
                df[col] = ""
            else:
                df[col] = 0

    df = df[EXPENSE_COLUMNS].copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
    df["Expense"] = pd.to_numeric(df["Expense"], errors="coerce").fillna(0.0)
    df["Category"] = df["Category"].fillna("").astype(str)
    df["Note"] = df["Note"].fillna("").astype(str)
    df["Month"] = pd.to_numeric(df["Month"], errors="coerce").fillna(0).astype(int)
    df["Year"] = pd.to_numeric(df["Year"], errors="coerce").fillna(0).astype(int)
    df["Created_At"] = df["Created_At"].fillna("").astype(str)

    df = df.dropna(subset=["Date"]).sort_values(["Date", "Created_At"]).reset_index(drop=True)

    if not df.empty:
        needs_month = df["Month"].eq(0)
        needs_year = df["Year"].eq(0)
        df.loc[needs_month, "Month"] = pd.to_datetime(df.loc[needs_month, "Date"]).dt.month
        df.loc[needs_year, "Year"] = pd.to_datetime(df.loc[needs_year, "Date"]).dt.year

    return df


def read_expense_data(path: Path = EXCEL_FILE) -> pd.DataFrame:
    initialize_excel_file(path)
    if USE_POSTGRES:
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        entry_date AS "Date",
                        expense AS "Expense",
                        category AS "Category",
                        note AS "Note",
                        month AS "Month",
                        year AS "Year",
                        TO_CHAR(created_at, 'YYYY-MM-DD HH24:MI:SS') AS "Created_At"
                    FROM non_fixed_expenses
                    ORDER BY entry_date, created_at
                    """
                )
                rows = cur.fetchall()
                columns = [desc[0] for desc in cur.description] if cur.description else EXPENSE_COLUMNS
        df = pd.DataFrame(rows, columns=columns)
        return _normalize_expense_dataframe(df)

    try:
        df = pd.read_excel(path, sheet_name=EXPENSE_SHEET, engine="openpyxl")
    except ValueError:
        df = pd.DataFrame(columns=EXPENSE_COLUMNS)
    return _normalize_expense_dataframe(df)


def read_settings(path: Path = EXCEL_FILE) -> Dict[str, float]:
    initialize_excel_file(path)
    if USE_POSTGRES:
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT setting AS \"Setting\", value AS \"Value\" FROM settings")
                rows = cur.fetchall()
                columns = [desc[0] for desc in cur.description] if cur.description else ["Setting", "Value"]
        df = pd.DataFrame(rows, columns=columns)
    else:
        try:
            df = pd.read_excel(path, sheet_name=SETTINGS_SHEET, engine="openpyxl")
        except ValueError:
            df = pd.DataFrame(columns=["Setting", "Value"])

    settings = DEFAULT_SETTINGS.copy()

    if not df.empty and {"Setting", "Value"}.issubset(df.columns):
        for _, row in df.iterrows():
            key = str(row["Setting"]).strip()
            if key:
                settings[key] = safe_float(row["Value"])

    total_fixed = (
        settings.get("House_Rent", 0.0)
        + settings.get("Labor", 0.0)
        + settings.get("Water_Bill", 0.0)
        + settings.get("Electricity", 0.0)
    )
    settings["Total_Fixed_Cost"] = total_fixed

    return settings


def write_all_data(
    daily_df: pd.DataFrame,
    settings: Dict[str, float],
    expense_df: pd.DataFrame | None = None,
    path: Path = EXCEL_FILE,
) -> None:
    if USE_POSTGRES:
        daily_df = _normalize_daily_dataframe(daily_df)
        if expense_df is None:
            expense_df = read_expense_data(path)
        expense_df = _normalize_expense_dataframe(expense_df)

        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM daily_revenue")
                for _, row in daily_df.iterrows():
                    cur.execute(
                        """
                        INSERT INTO daily_revenue
                        (entry_date, revenue_type, revenue, note, month, year, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::timestamptz)
                        """,
                        (
                            row["Date"],
                            normalize_revenue_type(row["Revenue_Type"]),
                            safe_float(row["Revenue"]),
                            str(row["Note"]).strip(),
                            int(row["Month"]),
                            int(row["Year"]),
                            str(row["Created_At"]).strip() or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        ),
                    )

                cur.execute("DELETE FROM non_fixed_expenses")
                for _, row in expense_df.iterrows():
                    cur.execute(
                        """
                        INSERT INTO non_fixed_expenses
                        (entry_date, expense, category, note, month, year, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s::timestamptz)
                        """,
                        (
                            row["Date"],
                            safe_float(row["Expense"]),
                            str(row["Category"]).strip() or "Unexpected",
                            str(row["Note"]).strip(),
                            int(row["Month"]),
                            int(row["Year"]),
                            str(row["Created_At"]).strip() or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        ),
                    )

                for key, value in settings.items():
                    cur.execute(
                        """
                        INSERT INTO settings (setting, value)
                        VALUES (%s, %s)
                        ON CONFLICT (setting) DO UPDATE SET value = EXCLUDED.value
                        """,
                        (str(key), safe_float(value)),
                    )
            conn.commit()
        return

    daily_df = _normalize_daily_dataframe(daily_df)
    if expense_df is None:
        expense_df = read_expense_data(path)
    expense_df = _normalize_expense_dataframe(expense_df)

    export_df = daily_df.copy()
    if not export_df.empty:
        export_df["Date"] = pd.to_datetime(export_df["Date"]).dt.strftime("%Y-%m-%d")
    expense_export_df = expense_df.copy()
    if not expense_export_df.empty:
        expense_export_df["Date"] = pd.to_datetime(expense_export_df["Date"]).dt.strftime("%Y-%m-%d")

    settings_df = pd.DataFrame({"Setting": list(settings.keys()), "Value": list(settings.values())})

    temp_path = path.with_name(f"{path.stem}.tmp{path.suffix}")
    with pd.ExcelWriter(temp_path, engine="openpyxl") as writer:
        export_df.to_excel(writer, sheet_name=DAILY_SHEET, index=False)
        expense_export_df.to_excel(writer, sheet_name=EXPENSE_SHEET, index=False)
        settings_df.to_excel(writer, sheet_name=SETTINGS_SHEET, index=False)
    temp_path.replace(path)


def save_settings(settings: Dict[str, float]) -> Tuple[bool, str]:
    cleaned_settings = DEFAULT_SETTINGS.copy()
    for key in ["Initial_Balance", "House_Rent", "Labor", "Water_Bill", "Electricity"]:
        value = safe_float(settings.get(key, cleaned_settings[key]))
        if value < 0:
            return False, "Settings cannot contain negative amounts."
        cleaned_settings[key] = value

    cleaned_settings["Total_Fixed_Cost"] = (
        cleaned_settings["House_Rent"]
        + cleaned_settings["Labor"]
        + cleaned_settings["Water_Bill"]
        + cleaned_settings["Electricity"]
    )
    write_all_data(read_daily_data(), cleaned_settings, expense_df=read_expense_data())
    return True, "Admin settings saved."


def save_entry(
    entry_date: date,
    revenue: float,
    note: str,
    revenue_type: str,
    settings: Dict[str, float],
) -> Tuple[bool, str]:
    if USE_POSTGRES:
        initialize_excel_file()
        normalized_type = normalize_revenue_type(revenue_type)
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM daily_revenue
                    WHERE entry_date = %s AND revenue_type = %s
                    LIMIT 1
                    """,
                    (entry_date, normalized_type),
                )
                if cur.fetchone():
                    return False, f"{normalized_type} revenue for this date already exists. Use Update Entry instead."

                cur.execute(
                    """
                    INSERT INTO daily_revenue
                    (entry_date, revenue_type, revenue, note, month, year, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    """,
                    (
                        entry_date,
                        normalized_type,
                        safe_float(revenue),
                        note.strip(),
                        entry_date.month,
                        entry_date.year,
                    ),
                )
            conn.commit()
        return True, f"{normalized_type} revenue entry saved."

    df = read_daily_data()
    normalized_type = normalize_revenue_type(revenue_type)
    mask = (df["Date"] == entry_date) & (df["Revenue_Type"] == normalized_type)

    if mask.any():
        return False, f"{normalized_type} revenue for this date already exists. Use Update Entry instead."

    new_row = pd.DataFrame(
        [
            {
                "Date": entry_date,
                "Revenue_Type": normalized_type,
                "Revenue": safe_float(revenue),
                "Note": note.strip(),
                "Month": entry_date.month,
                "Year": entry_date.year,
                "Created_At": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        ]
    )

    if df.empty:
        df = new_row.copy()
    else:
        df = pd.concat([df, new_row], ignore_index=True)
    df = _normalize_daily_dataframe(df)
    write_all_data(df, settings)

    return True, f"{normalized_type} revenue entry saved."


def update_entry(
    entry_date: date,
    revenue: float,
    note: str,
    revenue_type: str,
    settings: Dict[str, float],
) -> Tuple[bool, str]:
    if USE_POSTGRES:
        initialize_excel_file()
        normalized_type = normalize_revenue_type(revenue_type)
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE daily_revenue
                    SET revenue = %s,
                        note = %s,
                        month = %s,
                        year = %s
                    WHERE entry_date = %s
                      AND revenue_type = %s
                    RETURNING entry_date
                    """,
                    (
                        safe_float(revenue),
                        note.strip(),
                        entry_date.month,
                        entry_date.year,
                        entry_date,
                        normalized_type,
                    ),
                )
                updated = cur.fetchone()
            conn.commit()
        if not updated:
            return False, f"No {normalized_type} revenue entry found for that date to update."
        return True, f"{normalized_type} revenue entry updated successfully."

    df = read_daily_data()
    normalized_type = normalize_revenue_type(revenue_type)
    mask = (df["Date"] == entry_date) & (df["Revenue_Type"] == normalized_type)

    if not mask.any():
        return False, f"No {normalized_type} revenue entry found for that date to update."

    created_at = df.loc[mask, "Created_At"].iloc[0]
    df.loc[mask, "Revenue"] = safe_float(revenue)
    df.loc[mask, "Note"] = note.strip()
    df.loc[mask, "Month"] = entry_date.month
    df.loc[mask, "Year"] = entry_date.year
    df.loc[mask, "Revenue_Type"] = normalized_type
    df.loc[mask, "Created_At"] = created_at

    write_all_data(df, settings)
    return True, f"{normalized_type} revenue entry updated successfully."


def delete_entry(entry_date: date, revenue_type: str, settings: Dict[str, float]) -> Tuple[bool, str]:
    if USE_POSTGRES:
        initialize_excel_file()
        normalized_type = normalize_revenue_type(revenue_type)
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM daily_revenue
                    WHERE entry_date = %s
                      AND revenue_type = %s
                    RETURNING entry_date
                    """,
                    (entry_date, normalized_type),
                )
                deleted = cur.fetchone()
            conn.commit()
        if not deleted:
            return False, f"No {normalized_type} revenue entry found for that date to delete."
        return True, f"{normalized_type} revenue entry deleted successfully."

    df = read_daily_data()
    normalized_type = normalize_revenue_type(revenue_type)
    before_count = len(df)
    df = df[~((df["Date"] == entry_date) & (df["Revenue_Type"] == normalized_type))].copy()

    if len(df) == before_count:
        return False, f"No {normalized_type} revenue entry found for that date to delete."

    write_all_data(df, settings)
    return True, f"{normalized_type} revenue entry deleted successfully."


def save_expense_entry(
    entry_date: date,
    expense: float,
    category: str,
    note: str,
    settings: Dict[str, float],
) -> Tuple[bool, str]:
    if USE_POSTGRES:
        initialize_excel_file()
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO non_fixed_expenses
                    (entry_date, expense, category, note, month, year, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    """,
                    (
                        entry_date,
                        safe_float(expense),
                        category.strip() or "Unexpected",
                        note.strip(),
                        entry_date.month,
                        entry_date.year,
                    ),
                )
            conn.commit()
        return True, "Non-fixed expense entry saved."

    expense_df = read_expense_data()

    new_row = pd.DataFrame(
        [
            {
                "Date": entry_date,
                "Expense": safe_float(expense),
                "Category": category.strip() or "Unexpected",
                "Note": note.strip(),
                "Month": entry_date.month,
                "Year": entry_date.year,
                "Created_At": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        ]
    )

    if expense_df.empty:
        expense_df = new_row.copy()
    else:
        expense_df = pd.concat([expense_df, new_row], ignore_index=True)

    expense_df = _normalize_expense_dataframe(expense_df)
    write_all_data(read_daily_data(), settings, expense_df=expense_df)
    return True, "Non-fixed expense entry saved."


def update_expense_entry(
    entry_date: date,
    expense: float,
    category: str,
    note: str,
    settings: Dict[str, float],
) -> Tuple[bool, str]:
    if USE_POSTGRES:
        initialize_excel_file()
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH latest AS (
                        SELECT id
                        FROM non_fixed_expenses
                        WHERE entry_date = %s
                        ORDER BY created_at DESC, id DESC
                        LIMIT 1
                    )
                    UPDATE non_fixed_expenses e
                    SET expense = %s,
                        category = %s,
                        note = %s,
                        month = %s,
                        year = %s
                    FROM latest
                    WHERE e.id = latest.id
                    RETURNING e.id
                    """,
                    (
                        entry_date,
                        safe_float(expense),
                        category.strip() or "Unexpected",
                        note.strip(),
                        entry_date.month,
                        entry_date.year,
                    ),
                )
                updated = cur.fetchone()
            conn.commit()
        if not updated:
            return False, "No expense entry found for that date to update."
        return True, "Latest expense entry for that date updated successfully."

    expense_df = read_expense_data()
    mask = expense_df["Date"] == entry_date
    if not mask.any():
        return False, "No expense entry found for that date to update."

    # For dates with multiple entries, update only the latest record.
    target_idx = expense_df.loc[mask].sort_values("Created_At").index[-1]
    created_at = expense_df.loc[target_idx, "Created_At"]
    expense_df.loc[target_idx, "Expense"] = safe_float(expense)
    expense_df.loc[target_idx, "Category"] = category.strip() or "Unexpected"
    expense_df.loc[target_idx, "Note"] = note.strip()
    expense_df.loc[target_idx, "Month"] = entry_date.month
    expense_df.loc[target_idx, "Year"] = entry_date.year
    expense_df.loc[target_idx, "Created_At"] = created_at

    write_all_data(read_daily_data(), settings, expense_df=expense_df)
    return True, "Latest expense entry for that date updated successfully."


def delete_expense_entry(entry_date: date, settings: Dict[str, float]) -> Tuple[bool, str]:
    if USE_POSTGRES:
        initialize_excel_file()
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH latest AS (
                        SELECT id
                        FROM non_fixed_expenses
                        WHERE entry_date = %s
                        ORDER BY created_at DESC, id DESC
                        LIMIT 1
                    )
                    DELETE FROM non_fixed_expenses e
                    USING latest
                    WHERE e.id = latest.id
                    RETURNING e.id
                    """,
                    (entry_date,),
                )
                deleted = cur.fetchone()
            conn.commit()
        if not deleted:
            return False, "No expense entry found for that date to delete."
        return True, "Latest expense entry for that date deleted successfully."

    expense_df = read_expense_data()
    mask = expense_df["Date"] == entry_date
    if not mask.any():
        return False, "No expense entry found for that date to delete."

    # For dates with multiple entries, delete only the latest record.
    target_idx = expense_df.loc[mask].sort_values("Created_At").index[-1]
    expense_df = expense_df.drop(index=target_idx).reset_index(drop=True)

    write_all_data(read_daily_data(), settings, expense_df=expense_df)
    return True, "Latest expense entry for that date deleted successfully."


def expense_records_for_date(entry_date: date) -> pd.DataFrame:
    if USE_POSTGRES:
        initialize_excel_file()
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id AS "Record_ID",
                        entry_date AS "Date",
                        expense AS "Expense",
                        category AS "Category",
                        note AS "Note",
                        month AS "Month",
                        year AS "Year",
                        TO_CHAR(created_at, 'YYYY-MM-DD HH24:MI:SS') AS "Created_At"
                    FROM non_fixed_expenses
                    WHERE entry_date = %s
                    ORDER BY created_at, id
                    """,
                    (entry_date,),
                )
                rows = cur.fetchall()
                columns = [desc[0] for desc in cur.description] if cur.description else ["Record_ID", *EXPENSE_COLUMNS]
        return pd.DataFrame(rows, columns=columns)

    expense_df = read_expense_data()
    if expense_df.empty:
        return pd.DataFrame(columns=["Record_ID", *EXPENSE_COLUMNS])
    records = expense_df[expense_df["Date"] == entry_date].copy()
    records.insert(0, "Record_ID", records.index)
    return records.reset_index(drop=True)


def update_expense_record(
    record_id: int,
    entry_date: date,
    expense: float,
    category: str,
    note: str,
    settings: Dict[str, float],
) -> Tuple[bool, str]:
    if USE_POSTGRES:
        initialize_excel_file()
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE non_fixed_expenses
                    SET entry_date = %s,
                        expense = %s,
                        category = %s,
                        note = %s,
                        month = %s,
                        year = %s
                    WHERE id = %s
                    RETURNING id
                    """,
                    (
                        entry_date,
                        safe_float(expense),
                        category.strip() or "Unexpected",
                        note.strip(),
                        entry_date.month,
                        entry_date.year,
                        int(record_id),
                    ),
                )
                updated = cur.fetchone()
            conn.commit()
        if not updated:
            return False, "Expense entry was not found."
        return True, "Expense entry updated."

    expense_df = read_expense_data()
    target_idx = int(record_id)
    if target_idx not in expense_df.index:
        return False, "Expense entry was not found."

    created_at = expense_df.loc[target_idx, "Created_At"]
    expense_df.loc[target_idx, "Date"] = entry_date
    expense_df.loc[target_idx, "Expense"] = safe_float(expense)
    expense_df.loc[target_idx, "Category"] = category.strip() or "Unexpected"
    expense_df.loc[target_idx, "Note"] = note.strip()
    expense_df.loc[target_idx, "Month"] = entry_date.month
    expense_df.loc[target_idx, "Year"] = entry_date.year
    expense_df.loc[target_idx, "Created_At"] = created_at
    write_all_data(read_daily_data(), settings, expense_df=expense_df)
    return True, "Expense entry updated."


def delete_expense_record(record_id: int, settings: Dict[str, float]) -> Tuple[bool, str]:
    if USE_POSTGRES:
        initialize_excel_file()
        with _pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM non_fixed_expenses WHERE id = %s RETURNING id",
                    (int(record_id),),
                )
                deleted = cur.fetchone()
            conn.commit()
        if not deleted:
            return False, "Expense entry was not found."
        return True, "Expense entry deleted."

    expense_df = read_expense_data()
    target_idx = int(record_id)
    if target_idx not in expense_df.index:
        return False, "Expense entry was not found."
    expense_df = expense_df.drop(index=target_idx).reset_index(drop=True)
    write_all_data(read_daily_data(), settings, expense_df=expense_df)
    return True, "Expense entry deleted."


# -----------------------------
# Business calculations
# -----------------------------
def month_revenue(df: pd.DataFrame, year: int, month: int) -> float:
    if df.empty:
        return 0.0
    mask = (df["Year"] == year) & (df["Month"] == month)
    return safe_float(df.loc[mask, "Revenue"].sum())


def month_expense(df: pd.DataFrame, year: int, month: int) -> float:
    if df.empty:
        return 0.0
    mask = (df["Year"] == year) & (df["Month"] == month)
    return safe_float(df.loc[mask, "Expense"].sum())


def revenue_entry_exists(df: pd.DataFrame, entry_date: date, revenue_type: str) -> bool:
    if df.empty:
        return False
    normalized_type = normalize_revenue_type(revenue_type)
    return bool(((df["Date"] == entry_date) & (df["Revenue_Type"] == normalized_type)).any())


def period_from_filters(
    df: pd.DataFrame,
    selected_year: str,
    selected_month: str,
    use_custom_range: bool,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    filtered = df.copy()

    if selected_year != "All":
        filtered = filtered[filtered["Year"] == int(selected_year)]

    if selected_month != "All":
        month_number = list(month_name).index(selected_month)
        filtered = filtered[filtered["Month"] == month_number]

    if use_custom_range:
        start = pd.to_datetime(start_date).date()
        end = pd.to_datetime(end_date).date()
        filtered = filtered[(filtered["Date"] >= start) & (filtered["Date"] <= end)]

    return filtered.sort_values("Date").reset_index(drop=True)


def build_monthly_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["Period", "Revenue", "Year", "Month"])

    monthly = (
        df.groupby(["Year", "Month"], as_index=False)["Revenue"]
        .sum()
        .sort_values(["Year", "Month"])
        .reset_index(drop=True)
    )
    monthly["Period"] = monthly.apply(
        lambda r: f"{month_name[int(r['Month'])][:3]} {int(r['Year'])}", axis=1
    )
    return monthly


def compute_kpis(
    all_df: pd.DataFrame,
    filtered_df: pd.DataFrame,
    all_expense_df: pd.DataFrame,
    filtered_expense_df: pd.DataFrame,
    settings: Dict[str, float],
    selected_year: str,
    selected_month: str,
) -> Dict[str, float | str | bool | date | None]:
    today = date.today()
    initial_balance = settings["Initial_Balance"]
    fixed_cost = settings["Total_Fixed_Cost"]

    total_revenue_all = safe_float(all_df["Revenue"].sum()) if not all_df.empty else 0.0
    total_revenue_filtered = safe_float(filtered_df["Revenue"].sum()) if not filtered_df.empty else 0.0
    total_expense_all = safe_float(all_expense_df["Expense"].sum()) if not all_expense_df.empty else 0.0
    total_expense_filtered = (
        safe_float(filtered_expense_df["Expense"].sum()) if not filtered_expense_df.empty else 0.0
    )

    is_filtered_view = selected_year != "All" or selected_month != "All"

    if is_filtered_view:
        active_balance = initial_balance + total_revenue_filtered - total_expense_filtered
    else:
        active_balance = initial_balance + total_revenue_all - total_expense_all

    month_for_projection = (
        int(list(month_name).index(selected_month)) if selected_month != "All" else today.month
    )
    year_for_projection = int(selected_year) if selected_year != "All" else today.year

    today_revenue = 0.0
    today_expense = 0.0
    if not all_df.empty:
        today_revenue = safe_float(all_df.loc[all_df["Date"] == today, "Revenue"].sum())
    if not all_expense_df.empty:
        today_expense = safe_float(all_expense_df.loc[all_expense_df["Date"] == today, "Expense"].sum())

    monthly_rev = month_revenue(all_df, year_for_projection, month_for_projection)
    monthly_expense = month_expense(all_expense_df, year_for_projection, month_for_projection)
    month_df = all_df[(all_df["Year"] == year_for_projection) & (all_df["Month"] == month_for_projection)]
    month_expense_df = all_expense_df[
        (all_expense_df["Year"] == year_for_projection) & (all_expense_df["Month"] == month_for_projection)
    ]
    recorded_days = int(month_df["Date"].nunique()) if not month_df.empty else 0
    recorded_expense_days = int(month_expense_df["Date"].nunique()) if not month_expense_df.empty else 0
    avg_daily = monthly_rev / recorded_days if recorded_days else 0.0
    avg_daily_expense = monthly_expense / recorded_expense_days if recorded_expense_days else 0.0
    days_in_month = monthrange(year_for_projection, month_for_projection)[1]

    est_month_end_revenue = avg_daily * days_in_month
    est_month_end_expense = avg_daily_expense * days_in_month
    projected_remaining_revenue = max(days_in_month - recorded_days, 0) * avg_daily
    projected_remaining_expense = max(days_in_month - recorded_expense_days, 0) * avg_daily_expense
    est_month_end_balance = active_balance + projected_remaining_revenue - projected_remaining_expense
    projected_net_revenue = est_month_end_revenue - est_month_end_expense

    est_profit_loss = projected_net_revenue - fixed_cost
    balance_minus_cost = active_balance - fixed_cost

    remaining_break_even = max(fixed_cost - max(projected_net_revenue, active_balance), 0.0)

    best_day = None
    worst_day = None
    if not filtered_df.empty:
        per_day = (
            filtered_df.groupby("Date", as_index=False)["Revenue"]
            .sum()
            .sort_values("Date")
            .reset_index(drop=True)
        )
        best_idx = per_day["Revenue"].idxmax()
        worst_idx = per_day["Revenue"].idxmin()
        best_day = per_day.loc[best_idx]
        worst_day = per_day.loc[worst_idx]

    # Month-over-month trend for insight text.
    this_month = today.month
    this_year = today.year
    prev_month_date = date(this_year, this_month, 1) - timedelta(days=1)
    prev_month_revenue = month_revenue(all_df, prev_month_date.year, prev_month_date.month)
    current_month_revenue = month_revenue(all_df, this_year, this_month)

    improving_vs_last_month = current_month_revenue > prev_month_revenue

    return {
        "initial_balance": initial_balance,
        "today_revenue": today_revenue,
        "today_expense": today_expense,
        "monthly_revenue": monthly_rev,
        "monthly_expense": monthly_expense,
        "current_available_balance": active_balance,
        "avg_daily_revenue": avg_daily,
        "avg_daily_expense": avg_daily_expense,
        "est_month_end_revenue": est_month_end_revenue,
        "est_month_end_expense": est_month_end_expense,
        "projected_net_revenue": projected_net_revenue,
        "est_month_end_balance": est_month_end_balance,
        "fixed_cost": fixed_cost,
        "est_profit_loss": est_profit_loss,
        "balance_minus_cost": balance_minus_cost,
        "remaining_break_even": remaining_break_even,
        "revenue_progress": (monthly_rev / fixed_cost) if fixed_cost else 0.0,
        "net_progress": (projected_net_revenue / fixed_cost) if fixed_cost else 0.0,
        "balance_progress": (active_balance / fixed_cost) if fixed_cost else 0.0,
        "best_day": best_day,
        "worst_day": worst_day,
        "is_revenue_break_even": projected_net_revenue >= fixed_cost,
        "is_balance_break_even": active_balance >= fixed_cost,
        "improving_vs_last_month": improving_vs_last_month,
        "projection_year": year_for_projection,
        "projection_month": month_for_projection,
    }


def build_zone_status(kpis: Dict[str, float | str | bool | date | None]) -> Dict[str, float | bool | str]:
    fixed_cost = safe_float(kpis["fixed_cost"])
    current_available_balance = safe_float(kpis["current_available_balance"])
    projected_net_revenue = safe_float(kpis["projected_net_revenue"])

    current_zone_green = current_available_balance >= fixed_cost
    projected_zone_green = projected_net_revenue >= fixed_cost

    return {
        "current_zone_green": current_zone_green,
        "projected_zone_green": projected_zone_green,
        "current_gap": current_available_balance - fixed_cost,
        "projected_gap": projected_net_revenue - fixed_cost,
    }


# -----------------------------
# UI components
# -----------------------------
def inject_styles() -> None:
    st.markdown(
        """
        <style>
        :root {
            --bg: #f6f8fb;
            --card: #ffffff;
            --ink: #0f172a;
            --muted: #475569;
            --good: #0f766e;
            --warn: #ea580c;
            --bad: #be123c;
            --accent: #2563eb;
            --accent-soft: #06b6d4;
            --blue-1: #1d4ed8;
            --blue-2: #0284c7;
            --orange-1: #f59e0b;
            --orange-2: #ea580c;
            --orange-3: #c2410c;
            --login-bg-image: linear-gradient(145deg, #c5d8ea 0%, #e2edf8 60%, #f6f1eb 100%);
        }

        .stApp {
            background: var(--bg);
            color: var(--ink);
        }

        h1, h2, h3 {
            color: #0b1324;
            font-family: "Segoe UI Semibold", "Trebuchet MS", sans-serif;
        }

        .kpi-card {
            background: #ffffff;
            border: 1px solid #dbe4f0;
            border-radius: 8px;
            padding: 12px 14px;
            min-height: 142px;
            height: 142px;
            display: grid;
            grid-template-rows: 42px 1fr;
            align-items: center;
            justify-items: center;
            text-align: center;
            box-shadow: 0 8px 18px rgba(15, 23, 42, 0.05);
            transition: transform 0.16s ease, box-shadow 0.16s ease;
        }

        .kpi-card:hover {
            transform: translateY(-2px);
            box-shadow:
                0 12px 24px rgba(15, 23, 42, 0.08);
        }

        .kpi-title {
            margin: 0;
            color: #334155;
            font-size: 0.92rem;
            font-weight: 700;
            line-height: 1.2;
            letter-spacing: 0.3px;
            text-align: center;
            width: 100%;
            max-height: 2.4em;
            overflow: hidden;
        }

        .kpi-value {
            margin-top: 0;
            font-size: 1.42rem;
            font-weight: 700;
            color: var(--ink);
            line-height: 1.18;
            text-align: center;
            width: 100%;
            white-space: normal;
            overflow-wrap: break-word;
            word-break: normal;
            text-wrap: balance;
        }

        .kpi-value-long {
            font-size: 1.15rem;
            line-height: 1.22;
        }

        .kpi-value-slot {
            width: 100%;
            height: 100%;
            display: flex;
            justify-content: center;
            align-items: center;
            overflow: hidden;
        }

        .kpi-good { color: var(--good); }
        .kpi-warn { color: var(--warn); }
        .kpi-bad { color: var(--bad); }

        .insight-box {
            background: #ffffff;
            border: 1px solid #bfdbfe;
            border-left: 5px solid var(--accent-soft);
            border-radius: 8px;
            padding: 10px 12px;
            margin-bottom: 8px;
            box-shadow: 0 6px 16px rgba(15, 23, 42, 0.06);
        }

        .zone-card {
            border-radius: 8px;
            padding: 12px 14px;
            margin-bottom: 10px;
            border: 1px solid #dbeafe;
            box-shadow: 0 6px 16px rgba(15, 23, 42, 0.06);
        }

        .zone-green {
            background: #ecfdf5;
            border-left: 6px solid #059669;
        }

        .zone-red {
            background: #fef2f2;
            border-left: 6px solid #dc2626;
        }

        .zone-title {
            font-weight: 700;
            margin-bottom: 4px;
        }

        .status-card {
            border-radius: 8px;
            padding: 12px 14px;
            margin-bottom: 10px;
            border: 1px solid #dbeafe;
            box-shadow: 0 6px 16px rgba(15, 23, 42, 0.05);
            font-weight: 600;
            min-height: 64px;
            display: flex;
            align-items: center;
        }

        .status-good {
            background: #ecfdf5;
            border-left: 6px solid #059669;
            color: #065f46;
        }

        .status-warn {
            background: #fff7ed;
            border-left: 6px solid #ea580c;
            color: #9a3412;
        }

        .status-bad {
            background: #fef2f2;
            border-left: 6px solid #dc2626;
            color: #991b1b;
        }

        .perf-card {
            background: #ffffff;
            border: 1px solid #dbe4f0;
            border-radius: 8px;
            padding: 12px 14px;
            min-height: 138px;
            height: 138px;
            display: grid;
            grid-template-rows: auto 1fr auto;
            align-items: center;
            justify-items: center;
            text-align: center;
            box-shadow: 0 8px 18px rgba(15, 23, 42, 0.05);
        }

        .perf-title {
            color: #334155;
            font-size: 0.92rem;
            font-weight: 700;
            margin-bottom: 8px;
            text-align: center;
            width: 100%;
        }

        .perf-value {
            color: #0f172a;
            font-size: 1.55rem;
            font-weight: 800;
            line-height: 1.15;
            margin: 0;
            text-align: center;
            width: 100%;
            white-space: normal;
            overflow-wrap: break-word;
            text-wrap: balance;
        }

        .perf-value-long {
            font-size: 1.22rem;
            line-height: 1.2;
        }

        .perf-delta-slot {
            width: 100%;
            min-height: 36px;
            display: flex;
            justify-content: center;
            align-items: center;
        }

        .perf-delta {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 999px;
            font-size: 0.95rem;
            font-weight: 700;
            white-space: normal;
            overflow-wrap: break-word;
        }

        .perf-delta-empty {
            visibility: hidden;
        }

        .perf-delta-positive {
            background: #dcfce7;
            color: #15803d;
        }

        .perf-delta-negative {
            background: #fee2e2;
            color: #b91c1c;
        }

        .perf-delta-neutral {
            background: #e2e8f0;
            color: #334155;
        }

        .progress-row {
            margin-bottom: 14px;
        }

        .progress-label {
            color: #0f172a;
            font-size: 1rem;
            font-weight: 700;
            margin-bottom: 6px;
        }

        .progress-track {
            width: 100%;
            height: 16px;
            background: linear-gradient(90deg, #dbeafe, #c7ddfa);
            border: 1px solid #a7c8f9;
            border-radius: 999px;
            overflow: hidden;
        }

        .progress-fill {
            height: 100%;
            border-radius: 999px;
        }

        .progress-fill-revenue {
            background: linear-gradient(90deg, #2563eb, #38bdf8);
        }

        .progress-fill-balance {
            background: linear-gradient(90deg, #0ea5e9, #14b8a6);
        }

        .progress-fill-net {
            background: linear-gradient(90deg, #14b8a6, #22c55e);
        }

        /* Remove default outer BaseWeb frame to avoid double/black rectangle. */
        div[data-baseweb="input"],
        div[data-baseweb="base-input"],
        div[data-baseweb="input"]::before,
        div[data-baseweb="base-input"]::before,
        div[data-baseweb="input"]::after,
        div[data-baseweb="base-input"]::after {
            border: 0 !important;
            outline: 0 !important;
            box-shadow: none !important;
            background: transparent !important;
        }

        div[data-baseweb="input"] > div,
        div[data-baseweb="base-input"] > div {
            background: #ffffff !important;
            border: 2px solid #bfdbfe !important;
            border-radius: 14px !important;
            box-shadow: none !important;
            transition: all 0.16s ease;
        }

        div[data-baseweb="input"] > div:hover,
        div[data-baseweb="base-input"] > div:hover {
            border-color: #93c5fd !important;
        }

        div[data-baseweb="input"] > div:focus-within,
        div[data-baseweb="base-input"] > div:focus-within {
            border-color: #2563eb !important;
            box-shadow: none !important;
        }

        div[data-baseweb="input"] input,
        div[data-baseweb="base-input"] input {
            color: #0b1324 !important;
            font-weight: 700 !important;
        }

        div[data-baseweb="input"] input::placeholder,
        div[data-baseweb="base-input"] input::placeholder {
            color: #64748b !important;
            opacity: 1 !important;
        }

        /* Hard reset for Streamlit/BaseWeb inner wrappers to avoid dark suffix backgrounds. */
        div[data-testid="stTextInput"] div[data-baseweb="input"],
        div[data-testid="stTextInput"] div[data-baseweb="base-input"],
        div[data-testid="stNumberInput"] div[data-baseweb="input"],
        div[data-testid="stNumberInput"] div[data-baseweb="base-input"] {
            background: transparent !important;
        }

        div[data-testid="stTextInput"] div[data-baseweb="input"] > div,
        div[data-testid="stTextInput"] div[data-baseweb="base-input"] > div,
        div[data-testid="stNumberInput"] div[data-baseweb="input"] > div,
        div[data-testid="stNumberInput"] div[data-baseweb="base-input"] > div {
            background: #ffffff !important;
        }

        div[data-testid="stTextInput"] div[data-baseweb="input"] > div > div,
        div[data-testid="stTextInput"] div[data-baseweb="base-input"] > div > div,
        div[data-testid="stNumberInput"] div[data-baseweb="input"] > div > div,
        div[data-testid="stNumberInput"] div[data-baseweb="base-input"] > div > div {
            background: transparent !important;
        }

        div[data-testid="stTextInput"] div[data-baseweb="input"] > div > div:last-child,
        div[data-testid="stTextInput"] div[data-baseweb="base-input"] > div > div:last-child,
        div[data-testid="stNumberInput"] div[data-baseweb="input"] > div > div:last-child,
        div[data-testid="stNumberInput"] div[data-baseweb="base-input"] > div > div:last-child {
            background: #ffffff !important;
            border-radius: 0 12px 12px 0 !important;
        }

        div[data-testid="stTextInput"] div[data-baseweb="input"] button,
        div[data-testid="stTextInput"] div[data-baseweb="base-input"] button,
        div[data-testid="stNumberInput"] div[data-baseweb="input"] button,
        div[data-testid="stNumberInput"] div[data-baseweb="base-input"] button {
            background: transparent !important;
            box-shadow: none !important;
            border: 0 !important;
            color: #64748b !important;
        }

        div[data-testid="stTextInput"] label p,
        div[data-testid="stTextInput"] label span,
        div[data-testid="stNumberInput"] label p,
        div[data-testid="stNumberInput"] label span {
            color: #0b1324 !important;
            font-weight: 700 !important;
            opacity: 1 !important;
        }

        .stForm {
            background: #ffffff;
            border: 1px solid #dbe4f0;
            border-radius: 8px;
            padding: 14px;
            box-shadow: none;
            min-height: 310px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
        }

        .stForm div[data-baseweb="input"] > div,
        .stForm div[data-baseweb="base-input"] > div {
            background: #ffffff !important;
            border: 1px solid #93c5fd !important;
            border-radius: 8px !important;
            box-shadow: none !important;
        }

        .stForm div[data-baseweb="input"] > div:focus-within,
        .stForm div[data-baseweb="base-input"] > div:focus-within {
            border-color: #1d4ed8 !important;
            box-shadow: none !important;
        }

        .stForm div[data-baseweb="input"] input,
        .stForm div[data-baseweb="base-input"] input {
            color: #0f172a !important;
            font-weight: 700 !important;
        }

        .stForm div[data-baseweb="input"] input::placeholder,
        .stForm div[data-baseweb="base-input"] input::placeholder {
            color: #64748b !important;
        }

        .stForm div[data-baseweb="input"] button {
            color: #1d4ed8 !important;
            background: transparent !important;
        }

        .stForm label {
            color: #1e3a8a !important;
            font-weight: 700 !important;
        }

        [data-testid="stFormSubmitButton"] button {
            border-radius: 12px;
            font-weight: 800;
            border: 1px solid #60a5fa !important;
            min-height: 46px;
            height: 46px;
            width: 100%;
            white-space: nowrap;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            transition: all 0.18s ease;
            color: #0b2f57 !important;
            background: linear-gradient(135deg, #bae6fd, #7dd3fc) !important;
            box-shadow: none;
        }

        [data-testid="stFormSubmitButton"] button p,
        [data-testid="stButton"] button p,
        [data-testid="stPopover"] button p {
            color: inherit !important;
            font-weight: 800 !important;
        }

        [data-testid="stFormSubmitButton"] button:hover {
            transform: translateY(-1px);
            background: linear-gradient(135deg, #93c5fd, #60a5fa) !important;
            box-shadow: none;
        }

        [data-testid="stFormSubmitButton"] button:active {
            transform: translateY(0px);
            background: linear-gradient(135deg, #7dd3fc, #3b82f6) !important;
        }

        [data-testid="stFormSubmitButton"] button:focus-visible {
            outline: 2px solid #93c5fd;
            outline-offset: 2px;
        }

        [data-testid="stFormSubmitButton"] button:disabled {
            background: #dbeafe !important;
            border: 1px solid #bfdbfe !important;
            color: #64748b !important;
            box-shadow: none !important;
            opacity: 1 !important;
            cursor: not-allowed;
        }

        [data-testid="stButton"] button,
        [data-testid="stPopover"] button {
            border-radius: 8px !important;
            font-weight: 800 !important;
            border: 1px solid #93c5fd !important;
            color: #0b2f57 !important;
            background: linear-gradient(135deg, #e0f2fe, #dbeafe) !important;
            min-height: 44px;
        }

        .admin-pill,
        .admin-mini {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 36px;
            padding: 0 12px;
            border-radius: 999px;
            border: 1px solid #99f6e4;
            background: #ecfdf5;
            color: #0f766e;
            font-size: 0.84rem;
            font-weight: 800;
        }

        .admin-mini {
            width: 100%;
            min-height: 44px;
            border-radius: 8px;
        }

        .admin-mini.muted {
            border-color: #dbe4f0;
            background: #f8fafc;
            color: #64748b;
        }

        .sidebar-pill {
            width: 100%;
            margin: 4px 0 8px 0;
        }

        .admin-total {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            margin: 8px 0 12px 0;
            padding: 12px 14px;
            border: 1px solid #dbe4f0;
            border-radius: 8px;
            background: #f8fafc;
        }

        .admin-total span {
            color: #475569;
            font-weight: 700;
        }

        .admin-total strong {
            color: #0f172a;
            font-size: 1.15rem;
        }

        .review-summary {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 10px;
            margin: 8px 0 14px 0;
        }

        .review-summary > div {
            background: #ffffff;
            border: 1px solid #dbe4f0;
            border-radius: 8px;
            padding: 12px 14px;
            box-shadow: 0 8px 18px rgba(15, 23, 42, 0.04);
        }

        .review-summary span {
            display: block;
            color: #64748b;
            font-size: 0.86rem;
            font-weight: 700;
            margin-bottom: 4px;
        }

        .review-summary strong {
            display: block;
            color: #0f172a;
            font-size: 1.1rem;
            line-height: 1.2;
            overflow-wrap: break-word;
        }

        [data-testid="stButton"] button:hover,
        [data-testid="stPopover"] button:hover {
            color: #0b2f57 !important;
            background: linear-gradient(135deg, #dbeafe, #bfdbfe) !important;
            border-color: #60a5fa !important;
        }

        .login-shell {
            max-width: 720px;
            margin: 1.6rem auto 0.4rem auto;
        }

        .login-shell label {
            color: #0f172a !important;
            font-weight: 700 !important;
            font-size: 0.9rem !important;
            letter-spacing: 0.02em;
        }

        .login-shell div[data-testid="stTextInput"] label {
            display: flex !important;
            justify-content: center !important;
        }

        div[data-testid="stTextInput"] input[aria-label="Enter dashboard PIN"] {
            color: #0f172a !important;
            -webkit-text-fill-color: #0f172a !important;
            font-weight: 800 !important;
            background: transparent !important;
            letter-spacing: 0.38em;
            font-size: 1.3rem !important;
            text-align: center;
            padding-left: 0.42em !important;
        }

        div[data-testid="stTextInput"] input[aria-label="Enter dashboard PIN"]::placeholder {
            color: #94a3b8 !important;
            opacity: 1 !important;
        }

        .login-shell div[data-baseweb="input"] > div,
        .login-shell div[data-baseweb="base-input"] > div {
            background: rgba(255, 255, 255, 0.98) !important;
            border: 2px solid #bfdbfe !important;
            border-radius: 14px !important;
            min-height: 54px !important;
        }

        .login-shell div[data-baseweb="input"] > div:focus-within,
        .login-shell div[data-baseweb="base-input"] > div:focus-within {
            border-color: #93c5fd !important;
            box-shadow: none !important;
        }

        .login-shell input[type="password"] {
            color: #0f172a !important;
            -webkit-text-fill-color: #0f172a !important;
            font-weight: 700 !important;
            letter-spacing: 0.28em;
        }

        .login-shell div[data-baseweb="input"] button,
        .login-shell div[data-baseweb="base-input"] button {
            background: transparent !important;
            color: #64748b !important;
            border: 0 !important;
        }

        .login-shell div[data-baseweb="base-input"] > div > div,
        .login-shell div[data-baseweb="input"] > div > div {
            background: transparent !important;
        }

        .pin-caption {
            color: #334155 !important;
            font-weight: 600;
            text-align: center;
            margin-top: 6px;
        }

        .login-hero {
            position: relative;
            overflow: hidden;
            border-radius: 20px;
            padding: 30px 26px 28px 26px;
            background:
                linear-gradient(120deg, rgba(15, 23, 42, 0.54), rgba(15, 23, 42, 0.34)),
                var(--login-bg-image);
            background-size: cover;
            background-position: center;
            border: 1px solid rgba(191, 219, 254, 0.58);
            box-shadow: 0 24px 44px rgba(15, 23, 42, 0.24);
        }

        .login-title {
            margin: 0;
            color: #f8fafc;
            font-size: 2.25rem;
            line-height: 1.15;
            font-weight: 800;
            text-shadow: 0 3px 10px rgba(15, 23, 42, 0.32);
        }

        .login-subtitle {
            margin-top: 10px;
            color: #e2e8f0;
            font-size: 1.08rem;
            font-weight: 600;
            text-shadow: 0 2px 8px rgba(15, 23, 42, 0.28);
        }

        .login-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            margin-top: 14px;
            padding: 8px 12px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.16);
            color: #f8fafc;
            font-size: 0.9rem;
            font-weight: 700;
            border: 1px solid rgba(191, 219, 254, 0.65);
            backdrop-filter: blur(1px);
        }

        .login-pulse-dot {
            width: 9px;
            height: 9px;
            border-radius: 999px;
            background: #fbbf24;
            box-shadow: 0 0 0 rgba(251, 191, 36, 0.52);
            animation: pulseDot 1.7s infinite;
        }

        @keyframes pulseDot {
            0% {
                box-shadow: 0 0 0 0 rgba(251, 191, 36, 0.55);
            }
            80% {
                box-shadow: 0 0 0 14px rgba(251, 191, 36, 0.0);
            }
            100% {
                box-shadow: 0 0 0 0 rgba(251, 191, 36, 0.0);
            }
        }

        .small-muted {
            color: var(--muted);
            font-size: 0.86rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def inject_modern_styles() -> None:
    st.markdown(
        """
        <style>
        :root {
            --shell-bg: #f3f7fc;
            --card-bg: #ffffff;
            --card-border: #d8e4f3;
            --title-ink: #0f172a;
            --body-ink: #334155;
            --muted-ink: #64748b;
            --accent-blue: #1d4ed8;
            --accent-green: #0f766e;
            --accent-orange: #c2410c;
            --soft-blue: #eff6ff;
            --soft-green: #ecfdf5;
            --soft-orange: #fff7ed;
        }

        .stApp {
            background: radial-gradient(circle at 8% 0%, #eef5ff 0%, var(--shell-bg) 36%, #f7fafc 100%);
        }

        .dashboard-header {
            border: 1px solid #c7d9ef;
            border-radius: 16px;
            padding: 18px 20px;
            margin-bottom: 12px;
            background: linear-gradient(124deg, #0f4cc8 0%, #1d67dc 52%, #0f766e 100%);
            box-shadow: 0 14px 28px rgba(30, 64, 175, 0.18);
            color: #f8fafc;
        }

        .dashboard-header .title {
            margin: 0;
            font-size: 1.85rem;
            font-weight: 800;
            letter-spacing: 0.01em;
        }

        .dashboard-header .subtitle {
            margin-top: 8px;
            font-size: 0.98rem;
            color: #e2e8f0;
            font-weight: 600;
        }

        .quick-chip-row {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 14px;
        }

        .quick-chip {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            border-radius: 999px;
            padding: 6px 12px;
            font-size: 0.83rem;
            font-weight: 700;
            border: 1px solid rgba(255, 255, 255, 0.35);
            background: rgba(15, 23, 42, 0.18);
            color: #f8fafc;
        }

        .kpi-modern {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 14px 14px 12px 14px;
            min-height: 124px;
            box-shadow: 0 10px 20px rgba(15, 23, 42, 0.06);
            display: grid;
            grid-template-rows: auto 1fr;
            row-gap: 8px;
        }

        .kpi-modern-head {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
        }

        .kpi-modern-title {
            color: var(--body-ink);
            font-size: 0.84rem;
            font-weight: 700;
            letter-spacing: 0.01em;
            line-height: 1.25;
        }

        .kpi-modern-icon {
            min-width: 34px;
            height: 34px;
            border-radius: 10px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-size: 0.74rem;
            font-weight: 800;
            border: 1px solid #bfdbfe;
            color: #1d4ed8;
            background: #eff6ff;
        }

        .kpi-modern-value {
            align-self: end;
            font-size: 1.35rem;
            font-weight: 800;
            color: var(--title-ink);
            line-height: 1.18;
            overflow-wrap: anywhere;
        }

        .kpi-modern-value.good { color: var(--accent-green); }
        .kpi-modern-value.warn { color: var(--accent-orange); }
        .kpi-modern-value.bad { color: #be123c; }

        .insight-card {
            border: 1px solid #dbe7f5;
            border-left: 5px solid #1d4ed8;
            border-radius: 12px;
            padding: 10px 12px;
            background: #ffffff;
            color: #1e293b;
            font-weight: 600;
            margin-bottom: 8px;
            box-shadow: 0 8px 18px rgba(15, 23, 42, 0.04);
        }

        .insight-card.good {
            border-left-color: #0f766e;
            background: var(--soft-green);
        }

        .insight-card.warn {
            border-left-color: #c2410c;
            background: var(--soft-orange);
        }

        .insight-card.info {
            border-left-color: #1d4ed8;
            background: var(--soft-blue);
        }

        .section-head {
            margin: 4px 0 4px 0;
            color: var(--title-ink);
            font-size: 1.15rem;
            font-weight: 800;
            letter-spacing: 0.01em;
        }

        .section-note {
            color: var(--muted-ink);
            margin-bottom: 10px;
            font-size: 0.9rem;
            font-weight: 600;
        }

        .chart-head {
            margin-bottom: 6px;
            color: var(--title-ink);
            font-size: 1.02rem;
            font-weight: 800;
        }

        .entry-head {
            color: var(--title-ink);
            font-size: 1.08rem;
            font-weight: 800;
            margin-bottom: 4px;
        }

        .entry-note {
            color: var(--muted-ink);
            font-size: 0.88rem;
            font-weight: 600;
            margin-bottom: 8px;
        }

        [data-testid="stFormSubmitButton"] button {
            min-height: 52px;
            font-size: 0.97rem;
            letter-spacing: 0.01em;
        }

        .report-summary td:first-child {
            font-weight: 700;
            color: var(--body-ink);
        }

        .report-summary td:last-child {
            font-weight: 800;
            color: var(--title-ink);
        }

        .stTabs [data-baseweb="tab-list"] {
            gap: 6px;
            margin-bottom: 6px;
        }

        .stTabs [data-baseweb="tab"] {
            border: 1px solid #c8daf0;
            border-radius: 10px;
            background: #f8fbff;
            padding: 8px 12px;
            font-weight: 700;
            color: #1e293b !important;
            transition: background 0.18s ease, color 0.18s ease, border-color 0.18s ease;
        }

        .stTabs [data-baseweb="tab"] * {
            color: #1e293b !important;
            fill: #1e293b !important;
        }

        .stTabs [data-baseweb="tab"]:hover {
            background: #eef5ff !important;
            border-color: #93c5fd !important;
            color: #0f172a !important;
        }

        .stTabs [data-baseweb="tab"]:hover * {
            color: #0f172a !important;
            fill: #0f172a !important;
        }

        .stTabs [data-baseweb="tab"]:focus-visible {
            outline: 2px solid #93c5fd !important;
            outline-offset: 1px !important;
        }

        .stTabs [aria-selected="true"] {
            background: #e8f1ff !important;
            color: #0f172a !important;
            border-color: #93c5fd !important;
        }

        .stTabs [aria-selected="true"] * {
            color: #0f172a !important;
            fill: #0f172a !important;
        }

        [data-testid="stAlert"] {
            border-radius: 10px !important;
            border: 1px solid #dbe7f5 !important;
            background: #f8fbff !important;
        }

        [data-testid="stAlert"] * {
            color: #0f172a !important;
            fill: #0f172a !important;
            opacity: 1 !important;
        }

        [data-testid="stAlert"][kind="warning"] {
            background: #fff7ed !important;
            border-color: #fdba74 !important;
        }

        [data-testid="stAlert"][kind="info"] {
            background: #eff6ff !important;
            border-color: #93c5fd !important;
        }

        [data-testid="stAlert"][kind="success"] {
            background: #ecfdf5 !important;
            border-color: #86efac !important;
        }

        [data-testid="stAlert"][kind="error"] {
            background: #fef2f2 !important;
            border-color: #fca5a5 !important;
        }

        [data-testid="stDownloadButton"] button {
            border-radius: 10px !important;
            font-weight: 800 !important;
            border: 1px solid #93c5fd !important;
            color: #0f172a !important;
            background: #eff6ff !important;
            min-height: 46px !important;
        }

        [data-testid="stDownloadButton"] button:hover {
            color: #0f172a !important;
            background: #dbeafe !important;
            border-color: #60a5fa !important;
        }

        [data-testid="stDownloadButton"] button p,
        [data-testid="stDownloadButton"] button span {
            color: #0f172a !important;
            font-weight: 800 !important;
        }

        div[data-testid="stExpander"] details {
            border: 1px solid #c8daf0 !important;
            border-radius: 12px !important;
            background: #f8fbff !important;
            overflow: hidden !important;
        }

        div[data-testid="stExpander"] details summary {
            background: #eef5ff !important;
            border-bottom: 1px solid #dbe7f5 !important;
            padding-top: 0.3rem !important;
            padding-bottom: 0.3rem !important;
        }

        div[data-testid="stExpander"] details summary:hover {
            background: #e0ecff !important;
        }

        div[data-testid="stExpander"] details summary p,
        div[data-testid="stExpander"] details summary span {
            color: #0f172a !important;
            font-weight: 700 !important;
        }

        div[data-testid="stExpander"] details summary svg {
            fill: #0f172a !important;
        }

        div[data-testid="stRadio"] > label {
            display: none !important;
        }

        div[data-testid="stRadio"] div[role="radiogroup"] {
            display: flex !important;
            width: 100% !important;
            flex-wrap: wrap !important;
            gap: 6px !important;
            padding: 4px !important;
            border: 1px solid #c8daf0 !important;
            border-radius: 10px !important;
            background: #f8fbff !important;
        }

        div[data-testid="stRadio"] div[role="radiogroup"] label {
            flex: 1 1 132px !important;
            min-width: 132px !important;
            min-height: 42px !important;
            box-sizing: border-box !important;
            margin: 0 !important;
            padding: 0 12px !important;
            display: flex !important;
            align-items: center !important;
            justify-content: center !important;
            text-align: center !important;
            border: 1px solid transparent !important;
            border-radius: 8px !important;
            background: #ffffff !important;
            color: #1e293b !important;
            cursor: pointer !important;
            transition: background 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;
        }

        div[data-testid="stRadio"] div[role="radiogroup"] label:hover {
            background: #eef5ff !important;
            border-color: #93c5fd !important;
        }

        div[data-testid="stRadio"] div[role="radiogroup"] label:has(input:checked) {
            background: #e8f1ff !important;
            border-color: #2563eb !important;
            box-shadow: inset 0 -2px 0 #2563eb !important;
        }

        div[data-testid="stRadio"] div[role="radiogroup"] label * {
            color: #0f172a !important;
            font-weight: 800 !important;
            opacity: 1 !important;
            font-size: 0.95rem !important;
            line-height: 1.25 !important;
            overflow-wrap: normal !important;
            word-break: normal !important;
            hyphens: none !important;
        }

        div[data-testid="stRadio"] div[role="radiogroup"] label p,
        div[data-testid="stRadio"] div[role="radiogroup"] label span {
            width: 100% !important;
            max-width: 100% !important;
            text-align: center !important;
            white-space: nowrap !important;
            overflow-wrap: normal !important;
            word-break: keep-all !important;
            hyphens: none !important;
        }

        div[data-testid="stRadio"] div[role="radiogroup"] label > div:first-child {
            display: none !important;
        }

        div[data-testid="stRadio"] div[role="radiogroup"] input[type="radio"] {
            opacity: 0 !important;
            position: absolute !important;
            pointer-events: none !important;
        }

        div[data-testid="stButton"] button,
        div[data-testid="stFormSubmitButton"] button,
        div[data-testid="stDownloadButton"] button {
            color: #0f172a !important;
            background: #eff6ff !important;
            border: 1px solid #93c5fd !important;
        }

        div[data-testid="stButton"] button *,
        div[data-testid="stFormSubmitButton"] button *,
        div[data-testid="stDownloadButton"] button * {
            color: #0f172a !important;
            fill: #0f172a !important;
            opacity: 1 !important;
        }

        div[data-testid="stButton"] button:disabled,
        div[data-testid="stFormSubmitButton"] button:disabled,
        div[data-testid="stDownloadButton"] button:disabled {
            color: #64748b !important;
            background: #e5e7eb !important;
            border-color: #cbd5e1 !important;
            cursor: not-allowed !important;
            opacity: 1 !important;
        }

        div[data-testid="stButton"] button:disabled *,
        div[data-testid="stFormSubmitButton"] button:disabled *,
        div[data-testid="stDownloadButton"] button:disabled * {
            color: #64748b !important;
            fill: #64748b !important;
        }

        @media (max-width: 900px) {
            .dashboard-header .title {
                font-size: 1.45rem;
            }

            .kpi-modern {
                min-height: 110px;
            }

            .kpi-modern-value {
                font-size: 1.16rem;
            }

            div[data-testid="stRadio"] div[role="radiogroup"] label {
                flex-basis: calc(50% - 6px) !important;
                min-width: 148px !important;
            }
        }

        @media (max-width: 520px) {
            div[data-testid="stRadio"] div[role="radiogroup"] label {
                flex-basis: 100% !important;
                min-width: 100% !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def inject_login_background() -> None:
    if not LOGIN_BG_FILE.exists():
        return

    try:
        encoded = base64.b64encode(LOGIN_BG_FILE.read_bytes()).decode("utf-8")
    except Exception:
        return

    st.markdown(
        f"""
        <style>
        :root {{
            --login-bg-image: url("data:image/jpeg;base64,{encoded}");
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def inject_auto_pin_blur_script() -> None:
    components.html(
        """
        <script>
        (function () {
          const doc = window.parent.document;
          if (!doc || doc.__serenityPinAutoBlurInstalled) return;
          doc.__serenityPinAutoBlurInstalled = true;

          const pinLabels = new Set([
            "Enter dashboard PIN",
            "Enter PIN to view protected numbers",
            "Enter admin PIN"
          ]);

          function bindPinInput(el) {
            if (!el || el.dataset.pinAutoblurBound === "1") return;
            const label = el.getAttribute("aria-label") || "";
            if (!pinLabels.has(label)) return;
            el.dataset.pinAutoblurBound = "1";
            el.addEventListener("input", function () {
              const value = (el.value || "").trim();
              if (value.length >= 4) {
                setTimeout(function () {
                  el.blur();
                }, 10);
              }
            });
          }

          function scanAndBind() {
            const inputs = doc.querySelectorAll('input[aria-label]');
            inputs.forEach(bindPinInput);
          }

          const observer = new MutationObserver(scanAndBind);
          observer.observe(doc.body, { childList: true, subtree: true });
          scanAndBind();
        })();
        </script>
        """,
        height=0,
        width=0,
    )


def render_login_home() -> bool:
    if "is_logged_in" not in st.session_state:
        st.session_state["is_logged_in"] = False
    if "login_pin_invalid" not in st.session_state:
        st.session_state["login_pin_invalid"] = False

    if st.session_state["is_logged_in"]:
        return True

    st.markdown(
        """
        <div class="login-shell">
            <div class="login-hero">
                <h1 class="login-title">Serenity Stay Inn Dashboard</h1>
                <div class="login-subtitle">Secure revenue intelligence for Rooms + Bar + Wedding performance.</div>
                <div class="login-badge">
                    <span class="login-pulse-dot"></span>
                    Private local access
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.container():
        left, center, right = st.columns([1.0, 2.8, 1.0])
        with center:
            _, pin_col, _ = st.columns([1.15, 1.25, 1.15])
            with pin_col:
                st.text_input(
                    "Enter dashboard PIN",
                    type="password",
                    key="login_pin_input",
                    on_change=auto_unlock_login,
                    max_chars=4,
                    placeholder="****",
                )
            if st.session_state.get("login_pin_invalid", False):
                st.error("Incorrect PIN. Please try again.")

            st.markdown(
                '<div class="pin-caption">Only authorized users can open business metrics and entry forms.</div>',
                unsafe_allow_html=True,
            )

    return False


def render_kpi_card(title: str, value: str, tone: str = "", icon: str = "KP") -> None:
    value_class = "good" if tone == "good" else "warn" if tone == "warn" else "bad" if tone == "bad" else ""
    st.markdown(
        f"""
        <div class="kpi-modern">
            <div class="kpi-modern-head">
                <div class="kpi-modern-title">{title}</div>
                <div class="kpi-modern-icon">{icon}</div>
            </div>
            <div class="kpi-modern-value {value_class}">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_perf_card(title: str, value: str, delta: float | None = None) -> None:
    display_value = str(value)
    length_class = "perf-value-long" if len(display_value) >= 16 else ""
    delta_html = ""
    if delta is not None:
        if delta > 0:
            delta_class = "perf-delta-positive"
            delta_text = f"+ {format_rwf(delta)}"
        elif delta < 0:
            delta_class = "perf-delta-negative"
            delta_text = f"- {format_rwf(abs(delta))}"
        else:
            delta_class = "perf-delta-neutral"
            delta_text = format_rwf(0.0)
        delta_html = f'<span class="perf-delta {delta_class}">{delta_text}</span>'
    else:
        delta_html = '<span class="perf-delta perf-delta-empty">0 RWF</span>'

    st.markdown(
        f"""
        <div class="perf-card">
            <div class="perf-title" style="text-align:center;width:100%;">{title}</div>
            <div class="perf-value {length_class}">{display_value}</div>
            <div class="perf-delta-slot">{delta_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_progress_row(label: str, ratio: float, fill_class: str) -> None:
    pct = max(0.0, min(100.0, ratio * 100))
    st.markdown(
        f"""
        <div class="progress-row">
            <div class="progress-label">{label}</div>
            <div class="progress-track">
                <div class="progress-fill {fill_class}" style="width: {pct:.2f}%;"></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def protected_currency(value: float, is_unlocked: bool) -> str:
    return format_rwf(value) if is_unlocked else "****"


def protected_percent(value: float, is_unlocked: bool) -> str:
    return f"{value * 100:.1f}%" if is_unlocked else "****"


def render_sensitive_numbers_access() -> bool:
    if "view_unlocked" not in st.session_state:
        st.session_state["view_unlocked"] = False
    if "sensitive_pin_invalid" not in st.session_state:
        st.session_state["sensitive_pin_invalid"] = False

    st.markdown("### Access")
    st.markdown("**Protected numbers**")
    st.caption("Shows balance, fixed-cost, profit/loss, and break-even figures.")

    if st.session_state["view_unlocked"]:
        st.markdown('<div class="admin-pill sidebar-pill">Numbers visible</div>', unsafe_allow_html=True)
        if st.button("Hide numbers", width="stretch", key="hide_sensitive_btn"):
            st.session_state["view_unlocked"] = False
            st.session_state["sensitive_pin_input"] = ""
            st.session_state["sensitive_pin_invalid"] = False
            st.session_state["flash_message"] = {"ok": True, "message": "Protected numbers hidden."}
            st.rerun()
    else:
        with st.popover("See numbers", width="stretch"):
            st.text_input(
                "Enter PIN to view protected numbers",
                type="password",
                key="sensitive_pin_input",
                on_change=auto_unlock_sensitive_numbers,
            )
            if st.session_state.get("sensitive_pin_invalid", False):
                st.caption("Incorrect PIN.")

    return bool(st.session_state["view_unlocked"])


def render_admin_access() -> bool:
    if "edit_unlocked" not in st.session_state:
        st.session_state["edit_unlocked"] = False
    if "edit_pin_invalid" not in st.session_state:
        st.session_state["edit_pin_invalid"] = False

    st.markdown("**Admin editing**")
    st.caption("Controls fixed-cost settings and exact entry corrections.")

    if st.session_state["edit_unlocked"]:
        st.markdown('<div class="admin-pill sidebar-pill">Admin editing active</div>', unsafe_allow_html=True)
        if st.button("Lock admin", width="stretch", key="lock_admin_btn"):
            st.session_state["edit_unlocked"] = False
            st.session_state["edit_pin_input"] = ""
            st.session_state["edit_pin_invalid"] = False
            st.session_state["flash_message"] = {"ok": True, "message": "Admin editing locked."}
            st.rerun()
    else:
        with st.popover("Unlock admin", width="stretch"):
            st.text_input(
                "Enter admin PIN",
                type="password",
                key="edit_pin_input",
                on_change=auto_unlock_edit_mode,
            )
            if st.session_state.get("edit_pin_invalid", False):
                st.caption("Incorrect PIN.")

    return bool(st.session_state["edit_unlocked"])


def render_admin_settings(settings: Dict[str, float], is_unlocked: bool) -> None:
    st.markdown("### Admin Settings")
    if not is_unlocked:
        st.info("Unlock admin editing to adjust fixed monthly costs and protected balance settings.")
        return

    with st.form("admin_settings_form", clear_on_submit=False):
        st.caption("Set a cost to 0 to remove it. Changes update dashboard calculations only after Save settings.")
        top_cols = st.columns(3)
        lower_cols = st.columns(2)
        initial_balance = top_cols[0].number_input(
            "Initial balance",
            min_value=0.0,
            value=safe_float(settings["Initial_Balance"]),
            step=1000.0,
            format="%.0f",
            key="setting_initial_balance",
        )
        house_rent = top_cols[1].number_input(
            "House rent",
            min_value=0.0,
            value=safe_float(settings["House_Rent"]),
            step=1000.0,
            format="%.0f",
            key="setting_house_rent",
        )
        labor = top_cols[2].number_input(
            "Labor",
            min_value=0.0,
            value=safe_float(settings["Labor"]),
            step=1000.0,
            format="%.0f",
            key="setting_labor",
        )
        water_bill = lower_cols[0].number_input(
            "Water bill",
            min_value=0.0,
            value=safe_float(settings["Water_Bill"]),
            step=1000.0,
            format="%.0f",
            key="setting_water_bill",
        )
        electricity = lower_cols[1].number_input(
            "Electricity",
            min_value=0.0,
            value=safe_float(settings["Electricity"]),
            step=1000.0,
            format="%.0f",
            key="setting_electricity",
        )

        total_fixed = house_rent + labor + water_bill + electricity
        st.markdown(
            f"""
            <div class="admin-total">
                <span>Total fixed monthly cost</span>
                <strong>{format_rwf(total_fixed)}</strong>
            </div>
            """,
            unsafe_allow_html=True,
        )
        save_settings_pressed = st.form_submit_button(
            "Save settings",
            type="primary",
            width="stretch",
        )

    if save_settings_pressed:
        ok, msg = save_settings(
            {
                "Initial_Balance": initial_balance,
                "House_Rent": house_rent,
                "Labor": labor,
                "Water_Bill": water_bill,
                "Electricity": electricity,
            }
        )
        st.session_state["flash_message"] = {"ok": ok, "message": msg}
        st.rerun()

    st.markdown("#### Remove Fixed Cost")
    st.caption("Remove sets the selected cost to 0 and recalculates the fixed monthly total.")
    remove_cols = st.columns(4)
    removable_settings = [
        ("House rent", "House_Rent"),
        ("Labor", "Labor"),
        ("Water bill", "Water_Bill"),
        ("Electricity", "Electricity"),
    ]
    for idx, (label, key) in enumerate(removable_settings):
        disabled = safe_float(settings.get(key, 0.0)) == 0.0
        if remove_cols[idx].button(
            f"Remove {label}",
            width="stretch",
            disabled=disabled,
            key=f"remove_setting_{key}",
        ):
            updated_settings = settings.copy()
            updated_settings[key] = 0.0
            ok, msg = save_settings(updated_settings)
            st.session_state["flash_message"] = {
                "ok": ok,
                "message": f"{label} removed from fixed costs." if ok else msg,
            }
            st.rerun()


def render_admin_day_review(
    settings: Dict[str, float],
    all_revenue_df: pd.DataFrame,
    is_unlocked: bool,
) -> None:
    st.markdown("### Admin Entry Review")
    st.caption("Select a date to load every saved Rooms, Bar, Wedding, and expense entry for correction.")
    review_date = st.date_input("Review date", value=date.today(), key="admin_review_date")

    if not is_unlocked:
        st.info("Unlock admin editing to view, update, or delete entries from this date.")
        return

    day_revenue_df = (
        all_revenue_df[all_revenue_df["Date"] == review_date].sort_values("Revenue_Type")
        if not all_revenue_df.empty
        else pd.DataFrame(columns=DAILY_COLUMNS)
    )
    day_expense_df = expense_records_for_date(review_date)

    revenue_total = safe_float(day_revenue_df["Revenue"].sum()) if not day_revenue_df.empty else 0.0
    expense_total = safe_float(day_expense_df["Expense"].sum()) if not day_expense_df.empty else 0.0
    st.markdown(
        f"""
        <div class="review-summary">
            <div><span>Revenue</span><strong>{format_rwf(revenue_total)}</strong></div>
            <div><span>Non-fixed expenses</span><strong>{format_rwf(expense_total)}</strong></div>
            <div><span>Net day movement</span><strong>{format_rwf(revenue_total - expense_total)}</strong></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    review_options = ["Revenue entries", "Expense entries"]
    if "admin_review_mode" not in st.session_state:
        st.session_state["admin_review_mode"] = review_options[0]
    review_mode = st.radio(
        "Admin review mode",
        options=review_options,
        index=review_options.index(st.session_state["admin_review_mode"]),
        key="admin_review_mode",
        horizontal=True,
        label_visibility="collapsed",
    )

    if review_mode == "Revenue entries":
        if day_revenue_df.empty:
            st.info("No revenue entries found for this date.")
        else:
            for _, row in day_revenue_df.iterrows():
                revenue_type = normalize_revenue_type(str(row["Revenue_Type"]))
                form_key = f"admin_revenue_{review_date}_{revenue_type}"
                with st.form(form_key, clear_on_submit=False):
                    st.markdown(f"#### {revenue_type}")
                    cols = st.columns([1, 1, 2])
                    updated_amount = cols[0].number_input(
                        "Revenue",
                        min_value=0.0,
                        value=safe_float(row["Revenue"]),
                        step=1000.0,
                        format="%.0f",
                        key=f"{form_key}_amount",
                    )
                    cols[1].text_input(
                        "Revenue stream",
                        value=revenue_type,
                        disabled=True,
                        key=f"{form_key}_type",
                    )
                    updated_note = cols[2].text_input(
                        "Note",
                        value=str(row["Note"]),
                        key=f"{form_key}_note",
                    )
                    action_cols = st.columns([1, 1, 4])
                    update_pressed = action_cols[0].form_submit_button("Update", type="primary")
                    delete_pressed = action_cols[1].form_submit_button("Delete")

                if update_pressed:
                    ok, msg = update_entry(review_date, updated_amount, updated_note, revenue_type, settings)
                    st.session_state["flash_message"] = {"ok": ok, "message": msg}
                    st.rerun()
                if delete_pressed:
                    ok, msg = delete_entry(review_date, revenue_type, settings)
                    st.session_state["flash_message"] = {"ok": ok, "message": msg}
                    st.rerun()

    if review_mode == "Expense entries":
        if day_expense_df.empty:
            st.info("No non-fixed expense entries found for this date.")
        else:
            for position, row in day_expense_df.reset_index(drop=True).iterrows():
                record_id = int(row["Record_ID"])
                form_key = f"admin_expense_{review_date}_{record_id}_{position}"
                with st.form(form_key, clear_on_submit=False):
                    st.markdown(f"#### Expense {position + 1}")
                    cols = st.columns([1, 1, 2])
                    updated_expense = cols[0].number_input(
                        "Expense",
                        min_value=0.0,
                        value=safe_float(row["Expense"]),
                        step=1000.0,
                        format="%.0f",
                        key=f"{form_key}_amount",
                    )
                    updated_category = cols[1].text_input(
                        "Category",
                        value=str(row["Category"]) or "Unexpected",
                        key=f"{form_key}_category",
                    )
                    updated_note = cols[2].text_input(
                        "Note",
                        value=str(row["Note"]),
                        key=f"{form_key}_note",
                    )
                    created_at = str(row.get("Created_At", "")).strip()
                    if created_at:
                        st.caption(f"Created: {created_at}")
                    action_cols = st.columns([1, 1, 4])
                    update_pressed = action_cols[0].form_submit_button("Update", type="primary")
                    delete_pressed = action_cols[1].form_submit_button("Delete")

                if update_pressed:
                    ok, msg = update_expense_record(
                        record_id,
                        review_date,
                        updated_expense,
                        updated_category,
                        updated_note,
                        settings,
                    )
                    st.session_state["flash_message"] = {"ok": ok, "message": msg}
                    st.rerun()
                if delete_pressed:
                    ok, msg = delete_expense_record(record_id, settings)
                    st.session_state["flash_message"] = {"ok": ok, "message": msg}
                    st.rerun()


def style_plotly_chart(
    fig,
    *,
    is_date_x: bool = False,
    date_values: pd.Series | None = None,
    y_is_currency: bool = False,
) -> None:
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(255,255,255,0.96)",
        margin=dict(l=20, r=20, t=55, b=20),
        font=dict(color="#1f2937", family="Segoe UI, Trebuchet MS, sans-serif"),
        title_font=dict(color="#0f172a"),
        legend_title_font=dict(color="#1f2937"),
        legend_font=dict(color="#1f2937"),
        hoverlabel=dict(font_color="#0f172a", bgcolor="#ffffff"),
    )
    fig.update_xaxes(
        tickfont=dict(color="#334155"),
        title_font=dict(color="#334155"),
        gridcolor="rgba(148,163,184,0.18)",
    )
    fig.update_yaxes(
        tickfont=dict(color="#334155"),
        title_font=dict(color="#334155"),
        gridcolor="rgba(148,163,184,0.18)",
    )

    if y_is_currency:
        fig.update_yaxes(tickformat=",.0f")

    if is_date_x and date_values is not None:
        cleaned_dates = pd.to_datetime(date_values, errors="coerce").dropna().sort_values()
        fig.update_xaxes(tickformat="%b %d, %Y", hoverformat="%Y-%m-%d")

        if not cleaned_dates.empty:
            unique_dates = cleaned_dates.dt.normalize().drop_duplicates()
            if len(unique_dates) == 1:
                single_date = unique_dates.iloc[0]
                fig.update_xaxes(
                    range=[single_date - pd.Timedelta(hours=12), single_date + pd.Timedelta(hours=12)],
                    tickmode="array",
                    tickvals=[single_date],
                    ticktext=[single_date.strftime("%b %d, %Y")],
                )
            elif len(unique_dates) <= 10:
                fig.update_xaxes(dtick="D1")


def render_dashboard(
    kpis: Dict[str, float | str | bool | date | None],
    filtered_revenue_df: pd.DataFrame,
    all_revenue_df: pd.DataFrame,
    filtered_expense_df: pd.DataFrame,
    all_expense_df: pd.DataFrame,
    settings: Dict[str, float],
) -> None:
    view_unlocked = bool(st.session_state.get("view_unlocked", False))
    st.subheader("Financial KPI Overview")
    analysis_month = int(safe_float(kpis["projection_month"]))
    analysis_year = int(safe_float(kpis["projection_year"]))
    st.caption(f"Monthly projection context: {month_name[analysis_month]} {analysis_year}")

    best_day = kpis["best_day"]
    worst_day = kpis["worst_day"]

    best_day_text = "No data"
    if best_day is not None:
        best_day_text = f"{best_day['Date']} ({format_rwf(best_day['Revenue'])})"

    worst_day_text = "No data"
    if worst_day is not None:
        worst_day_text = f"{worst_day['Date']} ({format_rwf(worst_day['Revenue'])})"

    cards = [
        ("Initial Balance", protected_currency(safe_float(kpis["initial_balance"]), view_unlocked), ""),
        ("Revenue Today", format_rwf(safe_float(kpis["today_revenue"])), ""),
        ("Expense Today", format_rwf(safe_float(kpis["today_expense"])), "warn"),
        ("Revenue This Month", format_rwf(safe_float(kpis["monthly_revenue"])), ""),
        ("Non-Fixed Expense This Month", format_rwf(safe_float(kpis["monthly_expense"])), "warn"),
        ("Current Available Balance", protected_currency(safe_float(kpis["current_available_balance"]), view_unlocked), "good"),
        ("Avg Daily Revenue (Month)", format_rwf(safe_float(kpis["avg_daily_revenue"])), ""),
        ("Avg Daily Expense (Month)", format_rwf(safe_float(kpis["avg_daily_expense"])), "warn"),
        ("Estimated Month-End Revenue", protected_currency(safe_float(kpis["est_month_end_revenue"]), view_unlocked), ""),
        ("Estimated Month-End Expense", protected_currency(safe_float(kpis["est_month_end_expense"]), view_unlocked), "warn"),
        ("Projected Net Revenue (After Expense)", protected_currency(safe_float(kpis["projected_net_revenue"]), view_unlocked), ""),
        ("Estimated Month-End Balance", protected_currency(safe_float(kpis["est_month_end_balance"]), view_unlocked), "good"),
        ("Total Fixed Monthly Cost", protected_currency(safe_float(kpis["fixed_cost"]), view_unlocked), "warn"),
        (
            "Estimated Monthly Profit/Loss",
            protected_currency(safe_float(kpis["est_profit_loss"]), view_unlocked),
            "good" if safe_float(kpis["est_profit_loss"]) >= 0 else "bad",
        ),
        (
            "Balance Minus Fixed Cost",
            protected_currency(safe_float(kpis["balance_minus_cost"]), view_unlocked),
            "good" if safe_float(kpis["balance_minus_cost"]) >= 0 else "bad",
        ),
        ("Remaining To Break Even", protected_currency(safe_float(kpis["remaining_break_even"]), view_unlocked), "warn"),
        (
            "Projected Net Coverage",
            protected_percent(safe_float(kpis["net_progress"]), view_unlocked),
            "good" if safe_float(kpis["net_progress"]) >= 1 else "warn",
        ),
        ("Best Revenue Day", best_day_text, "good"),
        ("Worst Revenue Day", worst_day_text, "bad"),
    ]

    columns = st.columns(4)
    for idx, (title, value, tone) in enumerate(cards):
        with columns[idx % 4]:
            render_kpi_card(title, value, tone)

    st.markdown("### Daily, Weekly, Monthly Performance")
    today = date.today()
    yesterday = today - timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())

    today_rev = (
        safe_float(all_revenue_df.loc[all_revenue_df["Date"] == today, "Revenue"].sum())
        if not all_revenue_df.empty
        else 0.0
    )
    yesterday_rev = (
        safe_float(all_revenue_df.loc[all_revenue_df["Date"] == yesterday, "Revenue"].sum())
        if not all_revenue_df.empty
        else 0.0
    )
    this_week_rev = (
        safe_float(all_revenue_df.loc[all_revenue_df["Date"].between(week_start, today), "Revenue"].sum())
        if not all_revenue_df.empty
        else 0.0
    )
    month_rev = safe_float(kpis["monthly_revenue"])

    perf_cols = st.columns(4)
    if view_unlocked:
        with perf_cols[0]:
            render_perf_card("Today", format_rwf(today_rev), delta=today_rev - yesterday_rev)
        with perf_cols[1]:
            render_perf_card("Yesterday", format_rwf(yesterday_rev))
        with perf_cols[2]:
            render_perf_card("This Week", format_rwf(this_week_rev))
        with perf_cols[3]:
            render_perf_card("This Month", format_rwf(month_rev))
    else:
        with perf_cols[0]:
            render_perf_card("Today", "****")
        with perf_cols[1]:
            render_perf_card("Yesterday", "****")
        with perf_cols[2]:
            render_perf_card("This Week", "****")
        with perf_cols[3]:
            render_perf_card("This Month", "****")

    st.markdown("### Progress Toward Fixed Monthly Cost")
    revenue_progress = safe_float(kpis["revenue_progress"])
    balance_progress = safe_float(kpis["balance_progress"])
    net_progress = safe_float(kpis["net_progress"])

    if view_unlocked:
        render_progress_row(
            f"Revenue Progress: {revenue_progress * 100:.1f}% of fixed cost",
            revenue_progress,
            "progress-fill-revenue",
        )
        render_progress_row(
            f"Available Balance Coverage: {balance_progress * 100:.1f}% of fixed cost",
            balance_progress,
            "progress-fill-balance",
        )
        render_progress_row(
            f"Projected Net Coverage (after non-fixed expenses): {net_progress * 100:.1f}%",
            net_progress,
            "progress-fill-net",
        )
    else:
        render_progress_row("Revenue Progress: ****", 0.0, "progress-fill-revenue")
        render_progress_row("Available Balance Coverage: ****", 0.0, "progress-fill-balance")
        render_progress_row("Projected Net Coverage: ****", 0.0, "progress-fill-net")

    status_col_1, status_col_2 = st.columns(2)
    status_1_class = "status-good" if (view_unlocked and kpis["is_revenue_break_even"]) else "status-warn"
    status_1_text = (
        "Projected month-end net revenue (after non-fixed expenses) can cover fixed monthly costs."
        if (view_unlocked and kpis["is_revenue_break_even"])
        else ("Projected month-end net revenue (after non-fixed expenses) is below fixed monthly costs." if view_unlocked else "Protected. Enter PIN to view this status.")
    )
    status_2_class = "status-good" if (view_unlocked and kpis["is_balance_break_even"]) else "status-bad"
    status_2_text = (
        "Current available balance is enough for fixed monthly costs."
        if (view_unlocked and kpis["is_balance_break_even"])
        else ("Current available balance is not enough for fixed monthly costs." if view_unlocked else "Protected. Enter PIN to view this status.")
    )

    status_col_1.markdown(
        f"""
        <div class="status-card {status_1_class}">
            {status_1_text}
        </div>
        """,
        unsafe_allow_html=True,
    )
    status_col_2.markdown(
        f"""
        <div class="status-card {status_2_class}">
            {status_2_text}
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("### Smart Zone Insights")
    if not view_unlocked:
        st.info("Zone insights are protected. Use See numbers to view them.")
    else:
        zone_status = build_zone_status(kpis)
        current_zone_text = "GREEN ZONE" if zone_status["current_zone_green"] else "RED ZONE"
        projected_zone_text = "GREEN ZONE" if zone_status["projected_zone_green"] else "RED ZONE"
        current_zone_class = "zone-green" if zone_status["current_zone_green"] else "zone-red"
        projected_zone_class = "zone-green" if zone_status["projected_zone_green"] else "zone-red"
        current_gap = safe_float(zone_status["current_gap"])
        projected_gap = safe_float(zone_status["projected_gap"])

        zone_col_1, zone_col_2 = st.columns(2)
        with zone_col_1:
            st.markdown(
                f"""
                <div class="zone-card {current_zone_class}">
                    <div class="zone-title">Current Zone: {current_zone_text}</div>
                    <div>
                        Based on current available balance versus fixed monthly cost.
                        {"Coverage achieved." if current_gap >= 0 else f"Need {format_rwf(abs(current_gap))} more for coverage."}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        with zone_col_2:
            st.markdown(
                f"""
                <div class="zone-card {projected_zone_class}">
                    <div class="zone-title">Projected Month-End Zone: {projected_zone_text}</div>
                    <div>
                        Based on estimated month-end net revenue after non-fixed expenses.
                        {"Projected to cover fixed monthly cost." if projected_gap >= 0 else f"Need projected {format_rwf(abs(projected_gap))} more net amount to hit coverage."}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        st.caption("Zone status updates automatically after each revenue or non-fixed expense entry/update/delete.")

    chart_left, chart_right = st.columns(2)

    with chart_left:
        st.markdown("### Daily Revenue Trend")
        if filtered_revenue_df.empty:
            st.info("No revenue records in the selected filter.")
        else:
            trend_df = filtered_revenue_df.copy()
            trend_df["Date"] = pd.to_datetime(trend_df["Date"]).dt.normalize()
            fig_daily = px.line(
                trend_df,
                x="Date",
                y="Revenue",
                markers=True,
                template="plotly_white",
                title="Daily Revenue",
            )
            fig_daily.update_traces(line_color="#1d4ed8", marker_color="#f59e0b")
            style_plotly_chart(fig_daily, is_date_x=True, date_values=trend_df["Date"], y_is_currency=True)
            st.plotly_chart(fig_daily, width="stretch")

    with chart_right:
        st.markdown("### Balance Trend")
        if not view_unlocked:
            st.info("Balance trend is protected. Use See numbers to view it.")
        elif all_revenue_df.empty and all_expense_df.empty:
            st.info("No records to build balance trend.")
        else:
            rev_balance_df = all_revenue_df[["Date", "Revenue"]].copy()
            rev_balance_df["Delta"] = rev_balance_df["Revenue"]

            exp_balance_df = all_expense_df[["Date", "Expense"]].copy()
            exp_balance_df["Delta"] = -exp_balance_df["Expense"]

            balance_df = pd.concat(
                [
                    rev_balance_df[["Date", "Delta"]],
                    exp_balance_df[["Date", "Delta"]],
                ],
                ignore_index=True,
            )
            balance_df = balance_df.sort_values("Date")
            balance_df = balance_df.groupby("Date", as_index=False)["Delta"].sum().sort_values("Date")
            initial = safe_float(kpis["initial_balance"])
            balance_df["Running_Delta"] = balance_df["Delta"].cumsum()
            balance_df["Available_Balance"] = initial + balance_df["Running_Delta"]
            balance_df["Date"] = pd.to_datetime(balance_df["Date"]).dt.normalize()
            fig_balance = px.line(
                balance_df,
                x="Date",
                y="Available_Balance",
                markers=True,
                template="plotly_white",
                title="Available Balance Over Time (Revenue - Expense)",
            )
            fig_balance.update_traces(line_color="#1e40af", marker_color="#fb923c")
            style_plotly_chart(fig_balance, is_date_x=True, date_values=balance_df["Date"], y_is_currency=True)
            st.plotly_chart(fig_balance, width="stretch")

    chart_left, chart_right = st.columns(2)

    with chart_left:
        st.markdown("### Monthly Revenue")
        monthly_df = build_monthly_summary(all_revenue_df)
        if monthly_df.empty:
            st.info("No data available for monthly chart.")
        else:
            fig_monthly = px.bar(
                monthly_df,
                x="Period",
                y="Revenue",
                template="plotly_white",
                title="Revenue by Month",
                color="Revenue",
                color_continuous_scale=["#fff7ed", "#fed7aa", "#fb923c", "#f97316", "#ea580c", "#9a3412"],
            )
            fig_monthly.update_layout(
                coloraxis_showscale=False,
            )
            style_plotly_chart(fig_monthly, y_is_currency=True)
            st.plotly_chart(fig_monthly, width="stretch")

    with chart_right:
        st.markdown("### Fixed Cost Breakdown")
        costs_df = pd.DataFrame(
            {
                "Category": ["House Rent", "Labor", "Water Bill", "Electricity"],
                "Amount": [
                    settings["House_Rent"],
                    settings["Labor"],
                    settings["Water_Bill"],
                    settings["Electricity"],
                ],
            }
        )
        fig_costs = px.pie(
            costs_df,
            names="Category",
            values="Amount",
            hole=0.55,
            template="plotly_white",
            title="Monthly Fixed Cost Composition",
            color_discrete_sequence=["#1d4ed8", "#3b82f6", "#f59e0b", "#ea580c"],
        )
        style_plotly_chart(fig_costs)
        st.plotly_chart(fig_costs, width="stretch")

    chart_left, chart_right = st.columns(2)

    with chart_left:
        st.markdown("### Non-Fixed Expense Trend")
        if filtered_expense_df.empty:
            st.info("No non-fixed expense records in the selected filter.")
        else:
            expense_trend_df = filtered_expense_df.copy()
            expense_trend_df["Date"] = pd.to_datetime(expense_trend_df["Date"]).dt.normalize()
            fig_expense = px.bar(
                expense_trend_df,
                x="Date",
                y="Expense",
                color="Category",
                template="plotly_white",
                title="Daily Unexpected/Non-Fixed Expenses",
                color_discrete_sequence=["#ea580c", "#f59e0b", "#fb923c", "#fdba74"],
            )
            style_plotly_chart(
                fig_expense,
                is_date_x=True,
                date_values=expense_trend_df["Date"],
                y_is_currency=True,
            )
            st.plotly_chart(fig_expense, width="stretch")

    st.markdown("### Break-Even Coverage Snapshot")
    if not view_unlocked:
        st.info("Break-even coverage is protected. Use See numbers to view it.")
    else:
        break_even_df = pd.DataFrame(
            {
                "Metric": ["Projected Net Revenue", "Available Balance", "Fixed Cost Target"],
                "Amount": [
                    safe_float(kpis["projected_net_revenue"]),
                    safe_float(kpis["current_available_balance"]),
                    safe_float(kpis["fixed_cost"]),
                ],
            }
        )
        fig_break_even = px.bar(
            break_even_df,
            x="Amount",
            y="Metric",
            orientation="h",
            template="plotly_white",
            title="Coverage Comparison",
            color="Metric",
            color_discrete_map={
                "Projected Net Revenue": "#f59e0b",
                "Available Balance": "#2563eb",
                "Fixed Cost Target": "#ea580c",
            },
        )
        fig_break_even.update_layout(
            showlegend=False,
        )
        style_plotly_chart(fig_break_even, y_is_currency=True)
        st.plotly_chart(fig_break_even, width="stretch")

    st.markdown("### Monthly Revenue Progress")
    if not view_unlocked:
        st.info("Net month progress is protected. Use See numbers to view it.")
    else:
        projection_year = int(safe_float(kpis["projection_year"]))
        projection_month = int(safe_float(kpis["projection_month"]))
        projected_month_revenue_df = all_revenue_df[
            (all_revenue_df["Year"] == projection_year) & (all_revenue_df["Month"] == projection_month)
        ].copy()
        projected_month_expense_df = all_expense_df[
            (all_expense_df["Year"] == projection_year) & (all_expense_df["Month"] == projection_month)
        ].copy()

        if projected_month_revenue_df.empty and projected_month_expense_df.empty:
            st.info("No entries yet for the selected projection month.")
        else:
            rev_daily = pd.DataFrame(columns=["Date", "Revenue"])
            exp_daily = pd.DataFrame(columns=["Date", "Expense"])

            if not projected_month_revenue_df.empty:
                projected_month_revenue_df["Date"] = pd.to_datetime(projected_month_revenue_df["Date"])
                rev_daily = (
                    projected_month_revenue_df.groupby("Date", as_index=False)["Revenue"]
                    .sum()
                    .sort_values("Date")
                )
            if not projected_month_expense_df.empty:
                projected_month_expense_df["Date"] = pd.to_datetime(projected_month_expense_df["Date"])
                exp_daily = (
                    projected_month_expense_df.groupby("Date", as_index=False)["Expense"]
                    .sum()
                    .sort_values("Date")
                )

            daily_proj = pd.merge(rev_daily, exp_daily, on="Date", how="outer").fillna(0.0).sort_values("Date")
            daily_proj["Date"] = pd.to_datetime(daily_proj["Date"]).dt.normalize()
            daily_proj["Net_Delta"] = daily_proj["Revenue"] - daily_proj["Expense"]
            daily_proj["Cumulative_Net"] = daily_proj["Net_Delta"].cumsum()
            fixed_cost = safe_float(kpis["fixed_cost"])

            fig_progress = px.area(
                daily_proj,
                x="Date",
                y="Cumulative_Net",
                template="plotly_white",
                title="Cumulative Net Progress (Revenue - Non-Fixed Expense) vs Fixed Cost",
            )
            fig_progress.update_traces(line_color="#f59e0b", fillcolor="rgba(245,158,11,0.22)")
            fig_progress.add_hline(
                y=fixed_cost,
                line_width=2,
                line_dash="dash",
                line_color="#1d4ed8",
                annotation_text="Fixed monthly cost",
                annotation_position="top left",
            )
            style_plotly_chart(fig_progress, is_date_x=True, date_values=daily_proj["Date"], y_is_currency=True)
            st.plotly_chart(fig_progress, width="stretch")

    st.markdown("### Revenue Records (Filtered)")
    if filtered_revenue_df.empty:
        st.info("No records found for selected filters.")
    else:
        display_df = filtered_revenue_df.copy()
        display_df["Date"] = pd.to_datetime(display_df["Date"]).dt.strftime("%Y-%m-%d")
        display_df["Revenue"] = display_df["Revenue"].map(format_rwf)
        display_df = display_df[
            ["Date", "Revenue_Type", "Revenue", "Note", "Month", "Year", "Created_At"]
        ].rename(columns={"Revenue_Type": "Revenue Stream"})
        st.dataframe(display_df, width="stretch", hide_index=True)

    st.markdown("### Non-Fixed Expense Records (Filtered)")
    if filtered_expense_df.empty:
        st.info("No non-fixed expense records found for selected filters.")
    else:
        expense_display_df = filtered_expense_df.copy()
        expense_display_df["Date"] = pd.to_datetime(expense_display_df["Date"]).dt.strftime("%Y-%m-%d")
        expense_display_df["Expense"] = expense_display_df["Expense"].map(format_rwf)
        st.dataframe(expense_display_df, width="stretch", hide_index=True)


def render_chart_card(title: str, renderer: Callable[[], None], subtitle: str = "") -> None:
    with st.container(border=True):
        st.markdown(f'<div class="chart-head">{title}</div>', unsafe_allow_html=True)
        if subtitle:
            st.caption(subtitle)
        renderer()


def render_header(kpis: Dict[str, float | str | bool | date | None], view_unlocked: bool) -> None:
    projection_month = int(safe_float(kpis["projection_month"]))
    projection_year = int(safe_float(kpis["projection_year"]))
    period_label = f"{month_name[projection_month]} {projection_year}"

    if not view_unlocked:
        status_text = "Protected view active"
        chip_1 = "Current Balance: ****"
        chip_2 = "Fixed Cost: ****"
        chip_3 = "Break-even Progress: ****"
    else:
        status_text = (
            "On track to cover fixed costs"
            if bool(kpis["is_revenue_break_even"])
            else "Not yet at fixed-cost break-even"
        )
        chip_1 = f"Current Balance: {format_rwf(safe_float(kpis['current_available_balance']))}"
        chip_2 = f"Fixed Cost: {format_rwf(safe_float(kpis['fixed_cost']))}"
        chip_3 = f"Break-even Progress: {safe_float(kpis['net_progress']) * 100:.1f}%"

    st.markdown(
        f"""
        <div class="dashboard-header">
            <div class="title">Serenity Stay Inn</div>
            <div class="subtitle">Modern financial dashboard | Current month: {period_label} | {status_text}</div>
            <div class="quick-chip-row">
                <span class="quick-chip">{chip_1}</span>
                <span class="quick-chip">{chip_2}</span>
                <span class="quick-chip">{chip_3}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_kpi_grid(cards: list[tuple[str, str, str, str]], columns_per_row: int = 4) -> None:
    if not cards:
        return
    for start in range(0, len(cards), columns_per_row):
        row_cards = cards[start : start + columns_per_row]
        cols = st.columns(columns_per_row)
        for idx, card in enumerate(row_cards):
            title, value, tone, icon = card
            with cols[idx]:
                render_kpi_card(title, value, tone=tone, icon=icon)


def _build_smart_insights(
    kpis: Dict[str, float | str | bool | date | None],
    all_revenue_df: pd.DataFrame,
    view_unlocked: bool,
) -> list[tuple[str, str]]:
    insights: list[tuple[str, str]] = []
    today = date.today()
    yesterday = today - timedelta(days=1)

    if view_unlocked:
        if bool(kpis["is_revenue_break_even"]):
            insights.append(("good", "You are on track to cover fixed costs this month."))
        else:
            remaining = safe_float(kpis["remaining_break_even"])
            insights.append(("warn", f"You need {format_rwf(remaining)} more to break even."))
    else:
        insights.append(("info", "Break-even status is protected. Enter PIN from sidebar to view protected numbers."))

    today_revenue = (
        safe_float(all_revenue_df.loc[all_revenue_df["Date"] == today, "Revenue"].sum())
        if not all_revenue_df.empty
        else 0.0
    )
    yesterday_revenue = (
        safe_float(all_revenue_df.loc[all_revenue_df["Date"] == yesterday, "Revenue"].sum())
        if not all_revenue_df.empty
        else 0.0
    )
    if today_revenue > yesterday_revenue:
        diff = today_revenue - yesterday_revenue
        insights.append(("good", f"Today's revenue is higher than yesterday by {format_rwf(diff)}."))
    elif today_revenue < yesterday_revenue:
        diff = yesterday_revenue - today_revenue
        insights.append(("warn", f"Today's revenue is lower than yesterday by {format_rwf(diff)}."))
    else:
        insights.append(("info", "Today's revenue is equal to yesterday."))

    projection_year = int(safe_float(kpis["projection_year"]))
    projection_month = int(safe_float(kpis["projection_month"]))
    this_month_df = all_revenue_df[
        (all_revenue_df["Year"] == projection_year) & (all_revenue_df["Month"] == projection_month)
    ].copy()
    if not this_month_df.empty:
        stream_totals = (
            this_month_df.groupby("Revenue_Type", as_index=False)["Revenue"]
            .sum()
            .sort_values("Revenue", ascending=False)
            .reset_index(drop=True)
        )
        if not stream_totals.empty:
            top_stream = str(stream_totals.loc[0, "Revenue_Type"])
            top_value = safe_float(stream_totals.loc[0, "Revenue"])
            insights.append(("info", f"{top_stream} is the top revenue stream this month ({format_rwf(top_value)})."))
            if len(stream_totals) > 1:
                second_stream = str(stream_totals.loc[1, "Revenue_Type"])
                second_value = safe_float(stream_totals.loc[1, "Revenue"])
                gap = max(top_value - second_value, 0.0)
                insights.append(("info", f"{top_stream} is ahead of {second_stream} by {format_rwf(gap)}."))
    else:
        insights.append(("info", "No revenue entries have been recorded yet this month."))

    return insights


def render_dashboard_tab(
    kpis: Dict[str, float | str | bool | date | None],
    month_revenue_df: pd.DataFrame,
    all_revenue_df: pd.DataFrame,
    month_expense_df: pd.DataFrame,
    all_expense_df: pd.DataFrame,
    settings: Dict[str, float],
    view_unlocked: bool,
) -> None:
    st.markdown('<div class="section-head">Smart Insights</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-note">Simple daily guidance so you can react quickly.</div>',
        unsafe_allow_html=True,
    )
    for tone, message in _build_smart_insights(kpis, all_revenue_df, view_unlocked):
        st.markdown(f'<div class="insight-card {tone}">{message}</div>', unsafe_allow_html=True)

    key_kpis = [
        ("Today's Revenue", format_rwf(safe_float(kpis["today_revenue"])), "", "DAY"),
        ("This Month Revenue", format_rwf(safe_float(kpis["monthly_revenue"])), "", "MON"),
        ("This Month Expenses", format_rwf(safe_float(kpis["monthly_expense"])), "warn", "EXP"),
        (
            "Current Balance",
            protected_currency(safe_float(kpis["current_available_balance"]), view_unlocked),
            "good",
            "BAL",
        ),
        ("Fixed Monthly Cost", protected_currency(safe_float(kpis["fixed_cost"]), view_unlocked), "warn", "FIX"),
        (
            "Estimated Profit/Loss",
            protected_currency(safe_float(kpis["est_profit_loss"]), view_unlocked),
            "good" if safe_float(kpis["est_profit_loss"]) >= 0 else "bad",
            "P/L",
        ),
        (
            "Break-even Progress",
            protected_percent(safe_float(kpis["net_progress"]), view_unlocked),
            "good" if (view_unlocked and safe_float(kpis["net_progress"]) >= 1) else "warn",
            "BE%",
        ),
    ]

    st.markdown('<div class="section-head">Key KPIs</div>', unsafe_allow_html=True)
    _render_kpi_grid(key_kpis, columns_per_row=4)

    best_day = kpis["best_day"]
    worst_day = kpis["worst_day"]
    best_day_label = "No data"
    worst_day_label = "No data"
    if best_day is not None:
        best_day_date = best_day["Date"]
        best_day_text = best_day_date.strftime("%Y-%m-%d") if hasattr(best_day_date, "strftime") else str(best_day_date)
        best_day_label = f"{best_day_text} | {format_rwf(safe_float(best_day['Revenue']))}"
    if worst_day is not None:
        worst_day_date = worst_day["Date"]
        worst_day_text = worst_day_date.strftime("%Y-%m-%d") if hasattr(worst_day_date, "strftime") else str(worst_day_date)
        worst_day_label = f"{worst_day_text} | {format_rwf(safe_float(worst_day['Revenue']))}"

    advanced_kpis = [
        ("Average Daily Revenue", format_rwf(safe_float(kpis["avg_daily_revenue"])), "", "ADR"),
        ("Average Daily Expense", format_rwf(safe_float(kpis["avg_daily_expense"])), "warn", "ADE"),
        (
            "Estimated Month-end Revenue",
            protected_currency(safe_float(kpis["est_month_end_revenue"]), view_unlocked),
            "",
            "EMR",
        ),
        (
            "Estimated Month-end Expense",
            protected_currency(safe_float(kpis["est_month_end_expense"]), view_unlocked),
            "warn",
            "EME",
        ),
        (
            "Projected Net Revenue",
            protected_currency(safe_float(kpis["projected_net_revenue"]), view_unlocked),
            "good" if safe_float(kpis["projected_net_revenue"]) >= 0 else "bad",
            "NET",
        ),
        (
            "Remaining To Break-even",
            protected_currency(safe_float(kpis["remaining_break_even"]), view_unlocked),
            "warn",
            "REM",
        ),
        ("Best Revenue Day", best_day_label, "good", "TOP"),
        ("Lowest Revenue Day", worst_day_label, "warn", "LOW"),
    ]

    with st.expander("Advanced details", expanded=False):
        _render_kpi_grid(advanced_kpis, columns_per_row=4)

    st.markdown('<div class="section-head">Performance Charts</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-note">Clean trend visuals for revenue, expenses, and break-even coverage.</div>',
        unsafe_allow_html=True,
    )

    chart_col_1, chart_col_2 = st.columns(2)

    with chart_col_1:
        def _revenue_trend_chart() -> None:
            if month_revenue_df.empty:
                st.info("No revenue records for this month yet.")
                return
            trend_df = (
                month_revenue_df.groupby("Date", as_index=False)["Revenue"]
                .sum()
                .sort_values("Date")
                .reset_index(drop=True)
            )
            trend_df["Date"] = pd.to_datetime(trend_df["Date"]).dt.normalize()
            fig_trend = px.line(
                trend_df,
                x="Date",
                y="Revenue",
                markers=True,
                template="plotly_white",
                color_discrete_sequence=["#1d4ed8"],
            )
            fig_trend.update_traces(line_width=3, marker_size=8)
            style_plotly_chart(fig_trend, is_date_x=True, date_values=trend_df["Date"], y_is_currency=True)
            st.plotly_chart(fig_trend, width="stretch")

        render_chart_card("Revenue Trend", _revenue_trend_chart, "Daily total revenue for the current month.")

    with chart_col_2:
        def _monthly_bar_chart() -> None:
            monthly_df = build_monthly_summary(all_revenue_df)
            if monthly_df.empty:
                st.info("No monthly revenue data available yet.")
                return
            fig_monthly = px.bar(
                monthly_df,
                x="Period",
                y="Revenue",
                template="plotly_white",
                color="Revenue",
                color_continuous_scale=["#e0ecff", "#60a5fa", "#1d4ed8"],
            )
            fig_monthly.update_layout(coloraxis_showscale=False)
            style_plotly_chart(fig_monthly, y_is_currency=True)
            st.plotly_chart(fig_monthly, width="stretch")

        render_chart_card("Monthly Revenue", _monthly_bar_chart, "Month-by-month revenue performance.")

    chart_col_3, chart_col_4 = st.columns(2)

    with chart_col_3:
        def _revenue_vs_expense_chart() -> None:
            rev_daily = pd.DataFrame(columns=["Date", "Revenue"])
            exp_daily = pd.DataFrame(columns=["Date", "Expense"])
            if not month_revenue_df.empty:
                rev_daily = month_revenue_df.groupby("Date", as_index=False)["Revenue"].sum()
            if not month_expense_df.empty:
                exp_daily = month_expense_df.groupby("Date", as_index=False)["Expense"].sum()
            compare_df = pd.merge(rev_daily, exp_daily, on="Date", how="outer").sort_values("Date")
            if compare_df.empty:
                st.info("No revenue or expense records for this month yet.")
                return
            for amount_col in ["Revenue", "Expense"]:
                compare_df[amount_col] = pd.to_numeric(
                    compare_df[amount_col], errors="coerce"
                ).fillna(0.0)
            compare_df["Date"] = pd.to_datetime(compare_df["Date"]).dt.normalize()
            long_df = compare_df.melt(
                id_vars="Date",
                value_vars=["Revenue", "Expense"],
                var_name="Metric",
                value_name="Amount",
            )
            fig_compare = px.bar(
                long_df,
                x="Date",
                y="Amount",
                color="Metric",
                barmode="group",
                template="plotly_white",
                color_discrete_map={"Revenue": "#1d4ed8", "Expense": "#ea580c"},
            )
            style_plotly_chart(fig_compare, is_date_x=True, date_values=compare_df["Date"], y_is_currency=True)
            st.plotly_chart(fig_compare, width="stretch")

        render_chart_card(
            "Revenue vs Expense",
            _revenue_vs_expense_chart,
            "Daily revenue compared with non-fixed expenses.",
        )

    with chart_col_4:
        def _break_even_progress_chart() -> None:
            if not view_unlocked:
                st.info("Break-even progress is protected. Unlock protected numbers from the sidebar.")
                return
            progress_df = pd.DataFrame(
                {
                    "Metric": ["Current Balance", "Projected Net Revenue", "Fixed Cost Target"],
                    "Amount": [
                        safe_float(kpis["current_available_balance"]),
                        safe_float(kpis["projected_net_revenue"]),
                        safe_float(kpis["fixed_cost"]),
                    ],
                }
            )
            fig_break_even = px.bar(
                progress_df,
                x="Metric",
                y="Amount",
                template="plotly_white",
                color="Metric",
                color_discrete_map={
                    "Current Balance": "#0f766e",
                    "Projected Net Revenue": "#1d4ed8",
                    "Fixed Cost Target": "#ea580c",
                },
            )
            style_plotly_chart(fig_break_even, y_is_currency=True)
            st.plotly_chart(fig_break_even, width="stretch")

        render_chart_card(
            "Break-even Progress",
            _break_even_progress_chart,
            "Coverage comparison against fixed monthly cost.",
        )

    def _fixed_cost_donut_chart() -> None:
        if not view_unlocked:
            st.info("Fixed cost breakdown is protected. Unlock protected numbers from the sidebar.")
            return
        costs_df = pd.DataFrame(
            {
                "Category": ["House Rent", "Labor", "Water Bill", "Electricity"],
                "Amount": [
                    safe_float(settings["House_Rent"]),
                    safe_float(settings["Labor"]),
                    safe_float(settings["Water_Bill"]),
                    safe_float(settings["Electricity"]),
                ],
            }
        )
        fig_costs = px.pie(
            costs_df,
            names="Category",
            values="Amount",
            hole=0.58,
            template="plotly_white",
            color_discrete_sequence=["#1d4ed8", "#0ea5e9", "#f59e0b", "#ea580c"],
        )
        style_plotly_chart(fig_costs)
        st.plotly_chart(fig_costs, width="stretch")

    render_chart_card(
        "Fixed Cost Breakdown",
        _fixed_cost_donut_chart,
        "Donut chart of monthly fixed-cost composition.",
    )


def render_revenue_tab(all_revenue_df: pd.DataFrame, settings: Dict[str, float]) -> None:
    st.markdown('<div class="section-head">Add Revenue</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-note">Save Rooms, Bar, and Wedding revenue quickly. Each stream can be saved once per date.</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Entry wording guide: use today's date by default, type amount as 25000 or 25,000, and use note only when needed."
    )
    st.caption("If an entry already exists, update or delete it from Admin -> Review/Edit Entries.")

    if st.session_state.pop("clear_rooms_inputs", False):
        st.session_state["rooms_revenue_input"] = ""
        st.session_state["rooms_note"] = ""
    if st.session_state.pop("clear_bar_inputs", False):
        st.session_state["bar_revenue_input"] = ""
        st.session_state["bar_note"] = ""
    if st.session_state.pop("clear_wedding_inputs", False):
        st.session_state["wedding_revenue_input"] = ""
        st.session_state["wedding_note"] = ""

    rooms_col, bar_col, wedding_col = st.columns(3)

    with rooms_col:
        with st.container(border=True):
            st.markdown('<div class="entry-head">Rooms Revenue</div>', unsafe_allow_html=True)
            st.markdown(
                '<div class="entry-note">Enter total rooms revenue for one day. Amount format: 25000 or 25,000.</div>',
                unsafe_allow_html=True,
            )
            with st.form("revenue_form_rooms", clear_on_submit=False):
                rooms_date = st.date_input("Date", value=date.today(), key="rooms_date")
                rooms_revenue_raw = st.text_input(
                    "Rooms amount (RWF)",
                    value="",
                    placeholder="25,000",
                    key="rooms_revenue_input",
                )
                rooms_note = st.text_input("Rooms note (optional)", key="rooms_note")
                room_exists = revenue_entry_exists(all_revenue_df, rooms_date, "Rooms")
                if room_exists:
                    st.warning("Rooms revenue already saved for this date. Go to Admin -> Review/Edit Entries to change it.")
                else:
                    st.info("No Rooms revenue saved for this date. Save is ready.")
                save_rooms_pressed = st.form_submit_button(
                    "Save Rooms Revenue",
                    type="primary",
                    width="stretch",
                    disabled=room_exists,
                )

    with bar_col:
        with st.container(border=True):
            st.markdown('<div class="entry-head">Bar Revenue</div>', unsafe_allow_html=True)
            st.markdown(
                '<div class="entry-note">Enter total bar revenue for one day. Amount format: 25000 or 25,000.</div>',
                unsafe_allow_html=True,
            )
            with st.form("revenue_form_bar", clear_on_submit=False):
                bar_date = st.date_input("Date", value=date.today(), key="bar_date")
                bar_revenue_raw = st.text_input(
                    "Bar amount (RWF)",
                    value="",
                    placeholder="25,000",
                    key="bar_revenue_input",
                )
                bar_note = st.text_input("Bar note (optional)", key="bar_note")
                bar_exists = revenue_entry_exists(all_revenue_df, bar_date, "Bar")
                if bar_exists:
                    st.warning("Bar revenue already saved for this date. Go to Admin -> Review/Edit Entries to change it.")
                else:
                    st.info("No Bar revenue saved for this date. Save is ready.")
                save_bar_pressed = st.form_submit_button(
                    "Save Bar Revenue",
                    type="primary",
                    width="stretch",
                    disabled=bar_exists,
                )

    with wedding_col:
        with st.container(border=True):
            st.markdown('<div class="entry-head">Wedding Revenue</div>', unsafe_allow_html=True)
            st.markdown(
                '<div class="entry-note">Enter total wedding revenue for one day. Amount format: 25000 or 25,000.</div>',
                unsafe_allow_html=True,
            )
            with st.form("revenue_form_wedding", clear_on_submit=False):
                wedding_date = st.date_input("Date", value=date.today(), key="wedding_date")
                wedding_revenue_raw = st.text_input(
                    "Wedding amount (RWF)",
                    value="",
                    placeholder="25,000",
                    key="wedding_revenue_input",
                )
                wedding_note = st.text_input("Wedding note (optional)", key="wedding_note")
                wedding_exists = revenue_entry_exists(all_revenue_df, wedding_date, "Wedding")
                if wedding_exists:
                    st.warning(
                        "Wedding revenue already saved for this date. Go to Admin -> Review/Edit Entries to change it."
                    )
                else:
                    st.info("No Wedding revenue saved for this date. Save is ready.")
                save_wedding_pressed = st.form_submit_button(
                    "Save Wedding Revenue",
                    type="primary",
                    width="stretch",
                    disabled=wedding_exists,
                )

    if save_rooms_pressed:
        is_valid_amount, rooms_revenue, rooms_amount_err = parse_money_input(rooms_revenue_raw)
        if not is_valid_amount:
            st.session_state["flash_message"] = {"ok": False, "message": rooms_amount_err}
            st.rerun()
        ok, msg = save_entry(rooms_date, rooms_revenue, rooms_note, "Rooms", settings)
        if ok:
            st.session_state["clear_rooms_inputs"] = True
            msg = f"Rooms revenue saved for {rooms_date.strftime('%Y-%m-%d')}."
        st.session_state["flash_message"] = {"ok": ok, "message": msg}
        st.rerun()

    if save_bar_pressed:
        is_valid_amount, bar_revenue, bar_amount_err = parse_money_input(bar_revenue_raw)
        if not is_valid_amount:
            st.session_state["flash_message"] = {"ok": False, "message": bar_amount_err}
            st.rerun()
        ok, msg = save_entry(bar_date, bar_revenue, bar_note, "Bar", settings)
        if ok:
            st.session_state["clear_bar_inputs"] = True
            msg = f"Bar revenue saved for {bar_date.strftime('%Y-%m-%d')}."
        st.session_state["flash_message"] = {"ok": ok, "message": msg}
        st.rerun()

    if save_wedding_pressed:
        is_valid_amount, wedding_revenue, wedding_amount_err = parse_money_input(wedding_revenue_raw)
        if not is_valid_amount:
            st.session_state["flash_message"] = {"ok": False, "message": wedding_amount_err}
            st.rerun()
        ok, msg = save_entry(wedding_date, wedding_revenue, wedding_note, "Wedding", settings)
        if ok:
            st.session_state["clear_wedding_inputs"] = True
            msg = f"Wedding revenue saved for {wedding_date.strftime('%Y-%m-%d')}."
        st.session_state["flash_message"] = {"ok": ok, "message": msg}
        st.rerun()


def render_expense_tab(settings: Dict[str, float]) -> None:
    st.markdown('<div class="section-head">Add Expense</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-note">Record non-fixed expenses such as stock, cleaning, and transport.</div>',
        unsafe_allow_html=True,
    )

    expense_categories = ["Bar Stock", "Cleaning", "Maintenance", "Transport", "Other"]

    if st.session_state.pop("clear_expense_inputs", False):
        st.session_state["expense_amount_input"] = ""
        st.session_state["expense_category_input"] = "Other"
        st.session_state["expense_note_input"] = ""

    if "expense_category_input" not in st.session_state or st.session_state["expense_category_input"] not in expense_categories:
        st.session_state["expense_category_input"] = "Other"

    with st.container(border=True):
        with st.form("expense_form", clear_on_submit=False):
            expense_date = st.date_input("Date", value=date.today(), key="expense_date")
            expense_amount_raw = st.text_input(
                "Amount (RWF)",
                value="",
                placeholder="12,000",
                key="expense_amount_input",
            )
            expense_category = st.selectbox(
                "Category",
                options=expense_categories,
                key="expense_category_input",
            )
            expense_note = st.text_input("Note (optional)", key="expense_note_input")
            save_expense_pressed = st.form_submit_button(
                "Save Expense",
                type="primary",
                width="stretch",
            )

    if save_expense_pressed:
        is_valid_expense, expense_amount, expense_amount_err = parse_expense_input(expense_amount_raw)
        if not is_valid_expense:
            st.session_state["flash_message"] = {"ok": False, "message": expense_amount_err}
            st.rerun()
        ok, msg = save_expense_entry(expense_date, expense_amount, expense_category, expense_note, settings)
        if ok:
            st.session_state["clear_expense_inputs"] = True
            msg = f"Expense saved for {expense_date.strftime('%Y-%m-%d')}."
        st.session_state["flash_message"] = {"ok": ok, "message": msg}
        st.rerun()


def render_reports_tab(all_revenue_df: pd.DataFrame, all_expense_df: pd.DataFrame) -> None:
    st.markdown('<div class="section-head">Reports</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-note">Filter by month and year, review records, and download data.</div>',
        unsafe_allow_html=True,
    )

    revenue_years = all_revenue_df["Year"].unique().tolist() if not all_revenue_df.empty else []
    expense_years = all_expense_df["Year"].unique().tolist() if not all_expense_df.empty else []
    years = sorted({*revenue_years, *expense_years})
    year_options = ["All"] + [str(y) for y in years]
    if str(date.today().year) in year_options:
        default_year_index = year_options.index(str(date.today().year))
    else:
        default_year_index = 0

    month_options = ["All"] + [month_name[m] for m in range(1, 13)]
    default_month_index = date.today().month

    filter_col_1, filter_col_2 = st.columns(2)
    selected_year = filter_col_1.selectbox(
        "Year",
        options=year_options,
        index=default_year_index,
        key="reports_year_filter",
    )
    selected_month = filter_col_2.selectbox(
        "Month",
        options=month_options,
        index=default_month_index,
        key="reports_month_filter",
    )

    filtered_revenue_df = period_from_filters(
        all_revenue_df,
        selected_year,
        selected_month,
        False,
        date.today(),
        date.today(),
    )
    filtered_expense_df = period_from_filters(
        all_expense_df,
        selected_year,
        selected_month,
        False,
        date.today(),
        date.today(),
    )

    rooms_revenue = (
        safe_float(filtered_revenue_df.loc[filtered_revenue_df["Revenue_Type"] == "Rooms", "Revenue"].sum())
        if not filtered_revenue_df.empty
        else 0.0
    )
    bar_revenue = (
        safe_float(filtered_revenue_df.loc[filtered_revenue_df["Revenue_Type"] == "Bar", "Revenue"].sum())
        if not filtered_revenue_df.empty
        else 0.0
    )
    wedding_revenue = (
        safe_float(filtered_revenue_df.loc[filtered_revenue_df["Revenue_Type"] == "Wedding", "Revenue"].sum())
        if not filtered_revenue_df.empty
        else 0.0
    )
    total_revenue = safe_float(filtered_revenue_df["Revenue"].sum()) if not filtered_revenue_df.empty else 0.0
    total_expense = safe_float(filtered_expense_df["Expense"].sum()) if not filtered_expense_df.empty else 0.0
    net_total = total_revenue - total_expense

    summary_df = pd.DataFrame(
        {
            "Metric": [
                "Total Revenue",
                "Rooms Revenue",
                "Bar Revenue",
                "Wedding Revenue",
                "Total Expenses",
                "Net Movement",
                "Revenue Records",
                "Expense Records",
            ],
            "Value": [
                format_rwf(total_revenue),
                format_rwf(rooms_revenue),
                format_rwf(bar_revenue),
                format_rwf(wedding_revenue),
                format_rwf(total_expense),
                format_rwf(net_total),
                str(len(filtered_revenue_df)),
                str(len(filtered_expense_df)),
            ],
        }
    )

    st.markdown("#### Summary")
    st.dataframe(summary_df, width="stretch", hide_index=True)

    st.markdown("#### Revenue Records")
    if filtered_revenue_df.empty:
        st.info("No revenue records found for the selected month/year.")
    else:
        revenue_display_df = filtered_revenue_df.copy()
        revenue_display_df["Date"] = pd.to_datetime(revenue_display_df["Date"]).dt.strftime("%Y-%m-%d")
        revenue_display_df["Revenue"] = revenue_display_df["Revenue"].map(format_rwf)
        revenue_display_df = revenue_display_df[
            ["Date", "Revenue_Type", "Revenue", "Note", "Month", "Year", "Created_At"]
        ].rename(columns={"Revenue_Type": "Revenue Stream"})
        st.dataframe(revenue_display_df, width="stretch", hide_index=True)

    st.markdown("#### Expense Records")
    if filtered_expense_df.empty:
        st.info("No expense records found for the selected month/year.")
    else:
        expense_display_df = filtered_expense_df.copy()
        expense_display_df["Date"] = pd.to_datetime(expense_display_df["Date"]).dt.strftime("%Y-%m-%d")
        expense_display_df["Expense"] = expense_display_df["Expense"].map(format_rwf)
        st.dataframe(expense_display_df, width="stretch", hide_index=True)

    download_col_1, download_col_2, download_col_3 = st.columns(3)

    revenue_csv = filtered_revenue_df.to_csv(index=False).encode("utf-8")
    expense_csv = filtered_expense_df.to_csv(index=False).encode("utf-8")
    report_file_key = f"{selected_year}_{selected_month.replace(' ', '_')}".lower()

    download_col_1.download_button(
        "Download Revenue CSV",
        data=revenue_csv,
        file_name=f"serenity_revenue_{report_file_key}.csv",
        mime="text/csv",
        width="stretch",
    )
    download_col_2.download_button(
        "Download Expense CSV",
        data=expense_csv,
        file_name=f"serenity_expense_{report_file_key}.csv",
        mime="text/csv",
        width="stretch",
    )

    export_buffer = BytesIO()
    with pd.ExcelWriter(export_buffer, engine="openpyxl") as writer:
        filtered_revenue_df.to_excel(writer, index=False, sheet_name="Revenue")
        filtered_expense_df.to_excel(writer, index=False, sheet_name="Expenses")
        summary_df.to_excel(writer, index=False, sheet_name="Summary")
    export_buffer.seek(0)

    download_col_3.download_button(
        "Download Excel Report",
        data=export_buffer.getvalue(),
        file_name=f"serenity_report_{report_file_key}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
    )


def render_admin_tab(
    settings: Dict[str, float],
    all_revenue_df: pd.DataFrame,
    view_unlocked: bool,
) -> None:
    st.markdown('<div class="section-head">Admin</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-note">Admin PIN required for settings changes and entry corrections.</div>',
        unsafe_allow_html=True,
    )

    if USE_POSTGRES:
        st.info("Storage backend: PostgreSQL (DATABASE_URL active).")
    else:
        st.info(f"Data file location: {EXCEL_FILE}")

    if view_unlocked:
        st.caption(f"Current fixed monthly cost: {format_rwf(safe_float(settings['Total_Fixed_Cost']))}")
    else:
        st.caption("Current fixed monthly cost: ****")

    is_admin_unlocked = render_admin_access()
    admin_sections = ["Review/Edit Entries", "Fixed Costs & Balance"]
    if "admin_section_mode" not in st.session_state:
        st.session_state["admin_section_mode"] = admin_sections[0]
    admin_mode = st.radio(
        "Admin section",
        options=admin_sections,
        index=admin_sections.index(st.session_state["admin_section_mode"]),
        key="admin_section_mode",
        horizontal=True,
        label_visibility="collapsed",
    )

    if admin_mode == "Review/Edit Entries":
        render_admin_day_review(settings, all_revenue_df, is_admin_unlocked)
    if admin_mode == "Fixed Costs & Balance":
        render_admin_settings(settings, is_admin_unlocked)


# -----------------------------
# Main app
# -----------------------------
def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    inject_styles()
    inject_modern_styles()
    inject_login_background()
    inject_auto_pin_blur_script()

    if not render_login_home():
        return

    initialize_excel_file()
    settings = read_settings()
    all_revenue_df = read_daily_data()
    all_expense_df = read_expense_data()
    if "view_unlocked" not in st.session_state:
        st.session_state["view_unlocked"] = False
    if "edit_unlocked" not in st.session_state:
        st.session_state["edit_unlocked"] = False
    if "edit_pin_invalid" not in st.session_state:
        st.session_state["edit_pin_invalid"] = False
    if "clear_rooms_inputs" not in st.session_state:
        st.session_state["clear_rooms_inputs"] = False
    if "clear_bar_inputs" not in st.session_state:
        st.session_state["clear_bar_inputs"] = False
    if "clear_wedding_inputs" not in st.session_state:
        st.session_state["clear_wedding_inputs"] = False
    if "clear_expense_inputs" not in st.session_state:
        st.session_state["clear_expense_inputs"] = False
    if "expense_category_input" not in st.session_state:
        st.session_state["expense_category_input"] = "Other"
    if "active_app_section" not in st.session_state:
        st.session_state["active_app_section"] = "Dashboard"
    if "admin_section_mode" not in st.session_state:
        st.session_state["admin_section_mode"] = "Review/Edit Entries"
    if "admin_review_mode" not in st.session_state:
        st.session_state["admin_review_mode"] = "Revenue entries"

    flash_message = st.session_state.pop("flash_message", None)
    if flash_message:
        if flash_message["ok"]:
            st.success(flash_message["message"])
        else:
            st.error(flash_message["message"])

    with st.sidebar:
        st.markdown("### Session")
        if st.button("Log out", width="stretch", key="logout_btn"):
            st.session_state["is_logged_in"] = False
            st.session_state["view_unlocked"] = False
            st.session_state["edit_unlocked"] = False
            st.session_state["login_pin_input"] = ""
            st.session_state["sensitive_pin_input"] = ""
            st.session_state["edit_pin_input"] = ""
            st.session_state["rooms_revenue_input"] = ""
            st.session_state["rooms_note"] = ""
            st.session_state["bar_revenue_input"] = ""
            st.session_state["bar_note"] = ""
            st.session_state["wedding_revenue_input"] = ""
            st.session_state["wedding_note"] = ""
            st.session_state["active_app_section"] = "Dashboard"
            st.session_state["admin_section_mode"] = "Review/Edit Entries"
            st.session_state["admin_review_mode"] = "Revenue entries"
            st.session_state["login_pin_invalid"] = False
            st.session_state["sensitive_pin_invalid"] = False
            st.session_state["edit_pin_invalid"] = False
            st.session_state["flash_message"] = {"ok": True, "message": "You are logged out."}
            st.rerun()

        st.markdown("---")
        view_unlocked = render_sensitive_numbers_access()

        st.markdown("---")
        public_app_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
        if public_app_url:
            st.markdown("### App Link")
            st.code(public_app_url)

        st.markdown("---")
        st.markdown("### Data")
        if USE_POSTGRES:
            st.caption("Backend: PostgreSQL")
            st.code("DATABASE_URL is active")
        else:
            st.caption("Backend: Excel")
            st.code(str(EXCEL_FILE))

        st.markdown("---")
        st.markdown("**Fixed Monthly Costs**")
        if view_unlocked:
            st.write(f"House Rent: {format_rwf(settings['House_Rent'])}")
            st.write(f"Labor: {format_rwf(settings['Labor'])}")
            st.write(f"Water Bill: {format_rwf(settings['Water_Bill'])}")
            st.write(f"Electricity: {format_rwf(settings['Electricity'])}")
            st.write(f"Total Fixed Cost: {format_rwf(settings['Total_Fixed_Cost'])}")
        else:
            st.write("House Rent: ****")
            st.write("Labor: ****")
            st.write("Water Bill: ****")
            st.write("Electricity: ****")
            st.write("Total Fixed Cost: ****")

    today = date.today()
    selected_year = str(today.year)
    selected_month = month_name[today.month]
    filtered_revenue_df = period_from_filters(
        all_revenue_df,
        selected_year,
        selected_month,
        False,
        today,
        today,
    )
    filtered_expense_df = period_from_filters(
        all_expense_df,
        selected_year,
        selected_month,
        False,
        today,
        today,
    )

    kpis = compute_kpis(
        all_revenue_df,
        filtered_revenue_df,
        all_expense_df,
        filtered_expense_df,
        settings,
        selected_year,
        selected_month,
    )

    render_header(kpis, view_unlocked)
    app_sections = ["Dashboard", "Add Revenue", "Add Expense", "Reports", "Admin"]
    if "active_app_section" not in st.session_state:
        st.session_state["active_app_section"] = app_sections[0]
    active_section = st.radio(
        "Main navigation",
        options=app_sections,
        index=app_sections.index(st.session_state["active_app_section"]),
        key="active_app_section",
        horizontal=True,
        label_visibility="collapsed",
    )

    if active_section == "Dashboard":
        render_dashboard_tab(
            kpis,
            filtered_revenue_df,
            all_revenue_df,
            filtered_expense_df,
            all_expense_df,
            settings,
            view_unlocked,
        )

    if active_section == "Add Revenue":
        render_revenue_tab(all_revenue_df, settings)

    if active_section == "Add Expense":
        render_expense_tab(settings)

    if active_section == "Reports":
        render_reports_tab(all_revenue_df, all_expense_df)

    if active_section == "Admin":
        render_admin_tab(settings, all_revenue_df, view_unlocked)


if __name__ == "__main__":
    main()
