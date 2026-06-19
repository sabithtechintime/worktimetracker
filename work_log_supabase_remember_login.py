import os
import hmac
import time
import hashlib
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st
from streamlit_cookies_controller import CookieController
from supabase import create_client


TARGET_HOURS = 8
HALF_DAY_MINIMUM_HOURS = 6
APP_TIMEZONE = ZoneInfo("Asia/Kolkata")

# Daily device login
# A user who logs in from one browser/device should not need to log in again
# until the end of the current day in APP_TIMEZONE.
APP_SESSION_COOKIE_NAME = "office_swipe_daily_session"
COOKIE_CONTROLLER_KEY = "office_swipe_cookie_controller"
# Keep False for local/non-HTTPS development. Use True on Streamlit Cloud or HTTPS deployments.
COOKIE_SECURE = True
COOKIE_SAME_SITE = "lax"


# -----------------------------
# Time helpers
# -----------------------------

def now():
    return datetime.now(APP_TIMEZONE)


def today_key():
    return now().date().isoformat()


def current_month_key():
    return now().date().strftime("%Y-%m")


def target_duration():
    return timedelta(hours=TARGET_HOURS)


def half_day_minimum_duration():
    return timedelta(hours=HALF_DAY_MINIMUM_HOURS)


def end_of_today():
    """Return midnight after today in the application timezone."""
    current = now()
    tomorrow = current.date() + timedelta(days=1)

    return datetime(
        year=tomorrow.year,
        month=tomorrow.month,
        day=tomorrow.day,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=APP_TIMEZONE,
    )


def seconds_until_end_of_today():
    seconds = int((end_of_today() - now()).total_seconds())

    # Avoid creating an immediately expired cookie if login happens exactly
    # around midnight. One minute is enough to let the next run cleanly expire.
    return max(seconds, 60)


def parse_datetime(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=APP_TIMEZONE)

    return parsed.astimezone(APP_TIMEZONE)


def format_time(value):
    return value.astimezone(APP_TIMEZONE).strftime("%I:%M:%S %p")


def format_duration(duration):
    total_seconds = int(duration.total_seconds())

    if total_seconds < 0:
        total_seconds = 0

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_signed_duration(duration):
    if duration.total_seconds() < 0:
        return f"-{format_duration(-duration)}"

    return f"+{format_duration(duration)}"


def parse_duration_input(value):
    """
    Supports:
      blank -> use all available extra
      01:30 -> 1 hour 30 minutes
      1:30  -> 1 hour 30 minutes
      2     -> 2 hours
      1.5   -> 1 hour 30 minutes
    """
    value = value.strip()

    if not value:
        return None

    if ":" in value:
        parts = value.split(":")

        if len(parts) != 2:
            raise ValueError("Use HH:MM format.")

        hours = int(parts[0])
        minutes = int(parts[1])

        if hours < 0 or minutes < 0 or minutes >= 60:
            raise ValueError("Invalid duration.")

        return timedelta(hours=hours, minutes=minutes)

    hours = float(value)

    if hours < 0:
        raise ValueError("Duration cannot be negative.")

    return timedelta(hours=hours)


def is_valid_month(value):
    try:
        datetime.strptime(value, "%Y-%m")
        return True
    except ValueError:
        return False


# -----------------------------
# Supabase connection
# -----------------------------

def get_secret_value(key):
    try:
        value = st.secrets.get(key)
    except Exception:
        value = None

    return value or os.environ.get(key)


@st.cache_resource
def get_supabase_client():
    url = get_secret_value("SUPABASE_URL")
    key = get_secret_value("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not key:
        st.error(
            "Supabase credentials are missing. "
            "Add SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets."
        )
        st.stop()

    return create_client(url, key)


def db():
    return get_supabase_client()


def execute_db(query, error_message="Database request failed."):
    """
    Safely executes Supabase requests.

    This prevents temporary Supabase/network errors from showing raw tracebacks
    in the app. The UI will show a clean database error instead.
    """
    try:
        return query.execute()
    except Exception as exc:
        st.session_state["db_error"] = (
            f"{error_message} Please check your internet connection and try again."
        )
        st.session_state["db_error_detail"] = str(exc)
        return None


def show_db_error():
    error = st.session_state.get("db_error")

    if not error:
        return

    st.error(error)

    with st.expander("Technical details"):
        st.code(st.session_state.get("db_error_detail", "No details available."))

    if st.button("Clear database error", width="content", key="clear_db_error"):
        st.session_state.pop("db_error", None)
        st.session_state.pop("db_error_detail", None)
        st.rerun()


# -----------------------------
# Password hashing
# -----------------------------

def make_passcode_hash(passcode, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)

    passcode_hash = hashlib.pbkdf2_hmac(
        "sha256",
        passcode.encode("utf-8"),
        salt.encode("utf-8"),
        100_000
    ).hex()

    return salt, passcode_hash


def verify_passcode(passcode, salt, expected_hash):
    _, actual_hash = make_passcode_hash(passcode, salt)
    return hmac.compare_digest(actual_hash, expected_hash)


# -----------------------------
# Flash/message helpers
# -----------------------------

def set_flash(message, level="success", ttl_seconds=8):
    st.session_state["flash_message"] = message
    st.session_state["flash_level"] = level
    st.session_state["flash_until"] = time.time() + ttl_seconds


def show_flash():
    message = st.session_state.get("flash_message")
    level = st.session_state.get("flash_level", "success")
    flash_until = st.session_state.get("flash_until", 0)

    if not message:
        return

    if time.time() > flash_until:
        st.session_state.pop("flash_message", None)
        st.session_state.pop("flash_level", None)
        st.session_state.pop("flash_until", None)
        return

    if level == "success":
        st.success(message)
    elif level == "warning":
        st.warning(message)
    elif level == "error":
        st.error(message)
    else:
        st.info(message)


# -----------------------------
# User auth
# -----------------------------


# -----------------------------
# Daily device login sessions
# -----------------------------

def get_cookie_controller():
    """
    Create the cookie controller once per Streamlit run.

    Important:
    - Create this only once per run.
    - Restore first tries st.context.cookies and then uses controller.get() as a
      fallback for hosted environments where request cookies are filtered.
    - Do not call controller.refresh() in the same run as initialization.
    """
    return CookieController(key=COOKIE_CONTROLLER_KEY)


def get_request_cookie(name):
    """
    Read cookies from Streamlit's native request context.

    This is fast when available. Some hosted Streamlit environments may filter
    request cookies before they reach st.context, so restore also has a
    CookieController fallback below.
    """
    try:
        return st.context.cookies.get(name)
    except Exception:
        return None


def get_component_cookie(controller, name):
    """Read cookie from the frontend component cache as a fallback."""
    if controller is None:
        return None

    try:
        return controller.get(name)
    except Exception:
        return None


def hash_session_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clear_expired_app_user_sessions():
    execute_db(
        db()
        .table("app_user_sessions")
        .delete()
        .lt("expires_at", now().isoformat()),
        error_message="Could not clear expired login sessions."
    )


def create_daily_device_session(user_id, controller):
    """
    Create a one-day remembered login for this browser/device.

    Browser cookie stores the raw random token.
    Supabase stores only sha256(token), never the raw token.
    """
    token = secrets.token_urlsafe(32)
    token_hash = hash_session_token(token)
    expires_at = end_of_today()
    max_age = seconds_until_end_of_today()

    created = execute_db(
        db()
        .table("app_user_sessions")
        .insert({
            "user_id": user_id,
            "token_hash": token_hash,
            "expires_at": expires_at.isoformat(),
        }),
        error_message="Could not create daily login session."
    )

    if created is None:
        return False

    st.session_state["daily_session_token"] = token

    controller.set(
        APP_SESSION_COOKIE_NAME,
        token,
        max_age=max_age,
        same_site=COOKIE_SAME_SITE,
        secure=COOKIE_SECURE,
    )

    return True


def delete_daily_device_session(controller=None):
    """Delete the current browser/device daily session, if one exists."""
    token = (
        st.session_state.get("daily_session_token")
        or get_request_cookie(APP_SESSION_COOKIE_NAME)
        or get_component_cookie(controller, APP_SESSION_COOKIE_NAME)
    )

    if token:
        execute_db(
            db()
            .table("app_user_sessions")
            .delete()
            .eq("token_hash", hash_session_token(token)),
            error_message="Could not delete daily login session."
        )

    st.session_state.pop("daily_session_token", None)

    if controller is not None:
        try:
            controller.remove(
                APP_SESSION_COOKIE_NAME,
                same_site=COOKIE_SAME_SITE,
                secure=COOKIE_SECURE,
            )
        except KeyError:
            # streamlit-cookies-controller may raise KeyError if its internal
            # cookie cache did not contain the cookie, even though the browser
            # removal command was already sent.
            pass


def extract_embedded_user(session_row):
    embedded_user = session_row.get("app_users")

    if isinstance(embedded_user, list):
        if not embedded_user:
            return None
        return embedded_user[0]

    return embedded_user


def restore_login_from_daily_session(controller=None):
    """
    Restore st.session_state["user"] from the browser cookie and Supabase.

    This should run once at the start of main(), before checking current_user().
    It solves the mobile/minimized-browser issue where Streamlit session_state
    is lost but the browser cookie still exists.
    """
    if current_user():
        return

    token = (
        get_request_cookie(APP_SESSION_COOKIE_NAME)
        or get_component_cookie(controller, APP_SESSION_COOKIE_NAME)
    )

    if not token:
        return

    token_hash = hash_session_token(token)

    result = execute_db(
        db()
        .table("app_user_sessions")
        .select(
            "id, user_id, token_hash, expires_at, "
            "app_users(id, username, display_name)"
        )
        .eq("token_hash", token_hash)
        .limit(1),
        error_message="Could not restore daily login session."
    )

    if result is None or not result.data:
        return

    session_row = result.data[0]
    expires_at = parse_datetime(session_row["expires_at"])

    if expires_at <= now():
        execute_db(
            db()
            .table("app_user_sessions")
            .delete()
            .eq("id", session_row["id"]),
            error_message="Could not remove expired daily login session."
        )
        return

    user = extract_embedded_user(session_row)

    if not user:
        return

    st.session_state["user"] = {
        "id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
    }
    st.session_state["daily_session_token"] = token

def create_user(username, display_name, passcode):
    username = username.strip().lower()
    display_name = display_name.strip()

    if not username:
        return False, "Username is required."

    if not display_name:
        return False, "Display name is required."

    if len(passcode) < 4:
        return False, "Passcode must be at least 4 characters."

    existing = execute_db(
        db()
        .table("app_users")
        .select("id")
        .eq("username", username),
        error_message="Could not check username."
    )

    if existing is None:
        return False, "Could not check username due to a database error."

    if existing.data:
        return False, "Username already exists."

    salt, passcode_hash = make_passcode_hash(passcode)

    created = execute_db(
        db()
        .table("app_users")
        .insert({
            "username": username,
            "display_name": display_name,
            "passcode_salt": salt,
            "passcode_hash": passcode_hash,
        }),
        error_message="Could not create account."
    )

    if created is None:
        return False, "Account creation failed due to a database error."

    return True, "Account created successfully. Please login."


def login_user(username, passcode, remember_for_today=True, controller=None):
    username = username.strip().lower()

    result = execute_db(
        db()
        .table("app_users")
        .select("id, username, display_name, passcode_salt, passcode_hash")
        .eq("username", username)
        .limit(1),
        error_message="Could not login."
    )

    if result is None:
        return False, "Login failed due to a database error."

    if not result.data:
        return False, "Invalid username or passcode."

    user = result.data[0]

    if not verify_passcode(
        passcode,
        user["passcode_salt"],
        user["passcode_hash"]
    ):
        return False, "Invalid username or passcode."

    st.session_state["user"] = {
        "id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
    }

    if remember_for_today and controller is not None:
        clear_expired_app_user_sessions()
        create_daily_device_session(user["id"], controller)

    return True, "Logged in successfully."

def logout_user(controller=None):
    if controller is not None:
        delete_daily_device_session(controller)

    st.session_state.pop("user", None)
    st.session_state.pop("adjusted_result", None)
    st.session_state.pop("adjusted_result_error", None)
    st.session_state.pop("monthly_report_result", None)
    st.session_state.pop("monthly_report_error", None)
    st.session_state.pop("db_error", None)
    st.session_state.pop("db_error_detail", None)
    st.rerun()


def current_user():
    return st.session_state.get("user")


# -----------------------------
# Work session queries
# -----------------------------

def get_sessions_for_date(user_id, work_date):
    result = execute_db(
        db()
        .table("work_sessions")
        .select("id, user_id, work_date, check_in, check_out")
        .eq("user_id", user_id)
        .eq("work_date", work_date)
        .order("check_in"),
        error_message="Could not fetch today's sessions."
    )

    if result is None:
        return []

    return result.data or []


def get_open_session(user_id):
    result = execute_db(
        db()
        .table("work_sessions")
        .select("id, user_id, work_date, check_in, check_out")
        .eq("user_id", user_id)
        .is_("check_out", "null")
        .order("check_in", desc=True)
        .limit(1),
        error_message="Could not fetch active session."
    )

    if result is None:
        return None

    if result.data:
        return result.data[0]

    return None


def get_month_sessions(user_id, month_value):
    start_date = f"{month_value}-01"

    year, month = map(int, month_value.split("-"))

    if month == 12:
        next_month = f"{year + 1}-01-01"
    else:
        next_month = f"{year}-{month + 1:02d}-01"

    result = execute_db(
        db()
        .table("work_sessions")
        .select("id, user_id, work_date, check_in, check_out")
        .eq("user_id", user_id)
        .gte("work_date", start_date)
        .lt("work_date", next_month)
        .order("work_date")
        .order("check_in"),
        error_message="Could not fetch monthly sessions."
    )

    if result is None:
        return []

    return result.data or []


# -----------------------------
# Work logic
# -----------------------------

def get_today_status(user_id):
    open_session = get_open_session(user_id)
    sessions = get_sessions_for_date(user_id, today_key())

    if open_session:
        current_status = "checked_in"
        active_check_in = open_session["check_in"]
    else:
        current_status = "checked_out"
        active_check_in = None

    return {
        "sessions": sessions,
        "current_status": current_status,
        "active_check_in": active_check_in,
        "open_session": open_session,
    }


def calculate_sessions_worked(sessions, include_active=False):
    total = timedelta()

    for session in sessions:
        check_in = parse_datetime(session["check_in"])
        check_out_value = session.get("check_out")

        if check_out_value:
            check_out = parse_datetime(check_out_value)
            total += check_out - check_in
        elif include_active:
            total += now() - check_in

    return total


def calculate_today_worked(user_id):
    today_status = get_today_status(user_id)
    sessions = today_status["sessions"]

    return calculate_sessions_worked(
        sessions,
        include_active=True
    )


def check_in(user_id):
    open_session = get_open_session(user_id)

    if open_session:
        active_time = parse_datetime(open_session["check_in"])
        return False, f"Already checked in at {format_time(active_time)}"

    current_time = now()

    result = execute_db(
        db()
        .table("work_sessions")
        .insert({
            "user_id": user_id,
            "work_date": today_key(),
            "check_in": current_time.isoformat(),
            "check_out": None,
        }),
        error_message="Could not check in."
    )

    if result is None:
        return False, "Check in failed due to a database error."

    return True, f"Checked in successfully at {format_time(current_time)}"


def check_out(user_id):
    open_session = get_open_session(user_id)

    if not open_session:
        return False, "Already checked out."

    current_time = now()
    check_in_time = parse_datetime(open_session["check_in"])

    result = execute_db(
        db()
        .table("work_sessions")
        .update({
            "check_out": current_time.isoformat()
        })
        .eq("id", open_session["id"]),
        error_message="Could not check out."
    )

    if result is None:
        return False, "Check out failed due to a database error."

    session_duration = current_time - check_in_time

    return True, f"Checked out successfully. Session: {format_duration(session_duration)}"


def calculate_month_summary(user_id, month_value=None, include_today=True):
    if month_value is None:
        month_value = current_month_key()

    sessions = get_month_sessions(user_id, month_value)

    grouped = {}

    for session in sessions:
        work_date = session["work_date"]

        if not include_today and work_date == today_key():
            continue

        grouped.setdefault(work_date, []).append(session)

    logged_days = []
    total_worked = timedelta()

    for work_date, day_sessions in grouped.items():
        worked = calculate_sessions_worked(
            day_sessions,
            include_active=(include_today and work_date == today_key())
        )

        logged_days.append({
            "date": work_date,
            "worked": worked,
            "balance": worked - target_duration(),
        })

        total_worked += worked

    logged_days.sort(key=lambda item: item["date"])

    day_count = len(logged_days)
    monthly_target = target_duration() * day_count
    monthly_balance = total_worked - monthly_target
    average_per_day = total_worked / day_count if day_count else timedelta()

    return {
        "month": month_value,
        "day_count": day_count,
        "total_worked": total_worked,
        "monthly_target": monthly_target,
        "monthly_balance": monthly_balance,
        "average_per_day": average_per_day,
        "days": logged_days,
    }


def calculate_previous_month_balance(user_id, month_value=None):
    if month_value is None:
        month_value = current_month_key()

    summary = calculate_month_summary(
        user_id=user_id,
        month_value=month_value,
        include_today=False
    )

    return summary["monthly_balance"]


def build_dashboard_snapshot(user_id, today_status=None):
    """
    Supabase is queried only during normal app rerun.
    The live dashboard fragment uses this snapshot and does not hit Supabase every second.
    """
    if today_status is None:
        today_status = get_today_status(user_id)

    completed_worked = calculate_sessions_worked(
        today_status["sessions"],
        include_active=False
    )

    previous_month_balance = calculate_previous_month_balance(user_id)

    return {
        "current_status": today_status["current_status"],
        "active_check_in": today_status["active_check_in"],
        "completed_worked_seconds": int(completed_worked.total_seconds()),
        "previous_month_balance_seconds": int(previous_month_balance.total_seconds()),
    }


def get_live_total_worked_from_snapshot(snapshot):
    completed_worked = timedelta(
        seconds=snapshot.get("completed_worked_seconds", 0)
    )

    if (
        snapshot.get("current_status") == "checked_in"
        and snapshot.get("active_check_in")
    ):
        active_check_in = parse_datetime(snapshot["active_check_in"])
        return completed_worked + (now() - active_check_in)

    return completed_worked


def calculate_adjusted_logout(user_id, month_value, requested_extra=None):
    today_status = get_today_status(user_id)
    current_time = now()

    today_worked = calculate_today_worked(user_id)
    normal_remaining = target_duration() - today_worked

    if normal_remaining < timedelta():
        normal_remaining = timedelta()

    previous_month_balance = calculate_previous_month_balance(
        user_id=user_id,
        month_value=month_value
    )

    available_extra = max(previous_month_balance, timedelta())

    if requested_extra is None:
        extra_to_use = available_extra
    else:
        extra_to_use = min(requested_extra, available_extra)

    extra_to_use = min(extra_to_use, normal_remaining)
    adjusted_remaining = normal_remaining - extra_to_use

    six_hour_remaining = half_day_minimum_duration() - today_worked

    if six_hour_remaining <= timedelta():
        six_hour_status = "Safe ✓"
    else:
        six_hour_status = f"Risk, need {format_duration(six_hour_remaining)} more"

    if today_status["current_status"] == "checked_in":
        normal_logout_text = format_time(current_time + normal_remaining)
        adjusted_logout_text = format_time(current_time + adjusted_remaining)
    else:
        normal_logout_text = f"If check in now: {format_time(current_time + normal_remaining)}"
        adjusted_logout_text = f"If check in now: {format_time(current_time + adjusted_remaining)}"

    return {
        "month": month_value,
        "today_worked": format_duration(today_worked),
        "normal_remaining": format_duration(normal_remaining),
        "monthly_balance": format_signed_duration(previous_month_balance),
        "available_extra": format_duration(available_extra),
        "extra_used_today": format_duration(extra_to_use),
        "adjusted_remaining": format_duration(adjusted_remaining),
        "six_hour_status": six_hour_status,
        "normal_logout": normal_logout_text,
        "adjusted_logout": adjusted_logout_text,
    }


# -----------------------------
# UI helpers
# -----------------------------

def inject_css():
    st.markdown(
        """
        <style>
            :root {
                --app-bg: #EAF6FF;
                --card-bg: #FFFFFF;
                --primary: #0D6EFD;
                --primary-dark: #0B5ED7;
                --text: #1F2937;
                --muted: #6B7280;
                --border: #CDE7F7;
                --soft-blue: #DFF3FF;
                --soft-green: #EAF8F0;
                --soft-red: #FFF0F2;
                --soft-yellow: #FFF8E1;
                --success: #198754;
                --success-dark: #157347;
                --danger: #DC3545;
                --danger-dark: #BB2D3B;
                --gray: #6C757D;
                --gray-dark: #5C636A;
                --warning-text: #B77900;
            }

            .stApp {
                background: linear-gradient(135deg, #EAF6FF 0%, #F8FBFF 100%);
            }

            .main .block-container {
                padding-top: 1.5rem;
                padding-bottom: 2rem;
                max-width: 1180px;
            }

            .top-header {
                background: linear-gradient(135deg, #0D6EFD 0%, #0B5ED7 100%);
                color: #FFFFFF;
                padding: 24px 26px;
                border-radius: 22px;
                box-shadow: 0 10px 25px rgba(13, 110, 253, 0.20);
                margin-bottom: 18px;
            }

            .top-header-title {
                font-size: 30px;
                font-weight: 800;
                margin-bottom: 4px;
                color: #FFFFFF;
            }

            .top-header-subtitle {
                color: #DDEBFF;
                font-size: 15px;
            }

            .status-badge {
                display: inline-block;
                padding: 8px 14px;
                border-radius: 999px;
                font-weight: 800;
                font-size: 13px;
                margin-top: 12px;
            }

            .badge-in {
                background: #198754;
                color: #FFFFFF;
            }

            .badge-out {
                background: #FFFFFF;
                color: #0D6EFD;
            }

            .card {
                background: #FFFFFF;
                border: 1px solid #CDE7F7;
                border-radius: 22px;
                padding: 20px;
                box-shadow: 0 8px 20px rgba(31, 41, 55, 0.06);
                margin-bottom: 18px;
            }

            .card-title {
                font-size: 19px;
                font-weight: 850;
                color: #1F2937;
                margin-bottom: 4px;
            }

            .card-subtitle {
                font-size: 14px;
                color: #6B7280;
                margin-bottom: 12px;
            }

            .metric-card {
                background: #FFFFFF;
                border: 1px solid #CDE7F7;
                border-radius: 20px;
                padding: 18px 18px;
                box-shadow: 0 8px 20px rgba(31, 41, 55, 0.06);
                min-height: 118px;
                margin-bottom: 12px;
            }

            .metric-title {
                color: #6B7280;
                font-size: 13px;
                font-weight: 800;
                margin-bottom: 8px;
            }

            .metric-value {
                font-size: 23px;
                font-weight: 850;
                line-height: 1.2;
                word-break: break-word;
            }

            .result-card {
                background: #F8FBFF;
                border: 1px solid #CDE7F7;
                border-radius: 18px;
                padding: 16px;
                margin-top: 14px;
            }

            .result-title {
                font-weight: 850;
                color: #198754;
                margin-bottom: 10px;
                font-size: 16px;
            }

            .result-row {
                display: flex;
                justify-content: space-between;
                border-bottom: 1px solid #E5EEF7;
                padding: 7px 0;
                gap: 20px;
            }

            .result-row:last-child {
                border-bottom: 0;
            }

            .result-label {
                color: #6B7280;
                font-weight: 700;
            }

            .result-value {
                color: #1F2937;
                font-weight: 850;
                text-align: right;
            }

            .small-muted {
                color: #6B7280;
                font-size: 13px;
            }

            .stTextInput label,
            .stPasswordInput label,
            .stNumberInput label,
            .stDateInput label,
            .stSelectbox label,
            .stCheckbox label,
            .stTextArea label,
            .stRadio label {
                color: #1F2937 !important;
                font-weight: 700 !important;
            }

            .stTextInput input,
            .stPasswordInput input,
            .stNumberInput input,
            .stDateInput input,
            .stTextArea textarea {
                color: #1F2937 !important;
                background-color: #F8FBFF !important;
                border: 1px solid #CDE7F7 !important;
                border-radius: 14px !important;
            }

            .stTextInput input::placeholder,
            .stPasswordInput input::placeholder {
                color: #6B7280 !important;
                opacity: 1 !important;
            }

            .stCheckbox p,
            .stCheckbox span {
                color: #1F2937 !important;
            }

            .stTabs [data-baseweb="tab"] p {
                color: #1F2937 !important;
                font-weight: 800 !important;
            }

            [data-testid="stMarkdownContainer"] h1,
            [data-testid="stMarkdownContainer"] h2,
            [data-testid="stMarkdownContainer"] h3,
            [data-testid="stMarkdownContainer"] h4,
            [data-testid="stMarkdownContainer"] h5,
            [data-testid="stMarkdownContainer"] h6 {
                color: #1F2937 !important;
                font-weight: 850 !important;
            }

            div.stButton > button,
            div.stFormSubmitButton > button {
                background: #0D6EFD !important;
                color: #FFFFFF !important;
                border: 1px solid #0D6EFD !important;
                border-radius: 999px !important;
                padding: 0.65rem 1.1rem !important;
                font-weight: 800 !important;
                box-shadow: 0 6px 14px rgba(13, 110, 253, 0.18);
                transition: all 0.12s ease-in-out;
            }

            div.stButton > button:hover,
            div.stFormSubmitButton > button:hover {
                background: #0B5ED7 !important;
                border-color: #0B5ED7 !important;
                color: #FFFFFF !important;
                transform: translateY(-1px);
                box-shadow: 0 8px 18px rgba(13, 110, 253, 0.24);
            }

            div.stButton > button:active,
            div.stFormSubmitButton > button:active {
                transform: translateY(0px);
                box-shadow: 0 4px 10px rgba(13, 110, 253, 0.16);
            }

            div.stButton > button:focus,
            div.stFormSubmitButton > button:focus {
                outline: none !important;
                box-shadow: 0 0 0 0.2rem rgba(13, 110, 253, 0.25) !important;
            }

            .st-key-quick_actions div[data-testid="stHorizontalBlock"] > div:nth-child(1) button {
                background: #198754 !important;
                border-color: #198754 !important;
                color: #FFFFFF !important;
            }

            .st-key-quick_actions div[data-testid="stHorizontalBlock"] > div:nth-child(1) button:hover {
                background: #157347 !important;
                border-color: #157347 !important;
                color: #FFFFFF !important;
            }

            .st-key-quick_actions div[data-testid="stHorizontalBlock"] > div:nth-child(2) button {
                background: #DC3545 !important;
                border-color: #DC3545 !important;
                color: #FFFFFF !important;
            }

            .st-key-quick_actions div[data-testid="stHorizontalBlock"] > div:nth-child(2) button:hover {
                background: #BB2D3B !important;
                border-color: #BB2D3B !important;
                color: #FFFFFF !important;
            }

            .st-key-quick_actions div[data-testid="stHorizontalBlock"] > div:nth-child(3) button {
                background: #0D6EFD !important;
                border-color: #0D6EFD !important;
                color: #FFFFFF !important;
            }

            .st-key-quick_actions div[data-testid="stHorizontalBlock"] > div:nth-child(3) button:hover {
                background: #0B5ED7 !important;
                border-color: #0B5ED7 !important;
                color: #FFFFFF !important;
            }

            .st-key-quick_actions div[data-testid="stHorizontalBlock"] > div:nth-child(4) button {
                background: #6C757D !important;
                border-color: #6C757D !important;
                color: #FFFFFF !important;
            }

            .st-key-quick_actions div[data-testid="stHorizontalBlock"] > div:nth-child(4) button:hover {
                background: #5C636A !important;
                border-color: #5C636A !important;
                color: #FFFFFF !important;
            }

            [data-testid="stDataFrame"] {
                border-radius: 14px;
                overflow: hidden;
            }

            [data-testid="stDataFrame"] * {
                color: #1F2937;
            }

            [data-testid="stAlert"] * {
                color: inherit;
            }

            @media (max-width: 768px) {
                .top-header {
                    padding: 20px;
                    border-radius: 18px;
                }

                .top-header-title {
                    font-size: 24px;
                }

                .top-header-subtitle {
                    font-size: 14px;
                }

                .metric-card {
                    min-height: 105px;
                    padding: 15px;
                }

                .metric-value {
                    font-size: 20px;
                }

                .result-row {
                    display: block;
                }

                .result-value {
                    text-align: left;
                    margin-top: 2px;
                }
            }
        </style>
        """,
        unsafe_allow_html=True
    )


def metric_card(title, value, color="#1F2937", bg="#FFFFFF"):
    st.markdown(
        f"""
        <div class="metric-card" style="background:{bg};">
            <div class="metric-title">{title}</div>
            <div class="metric-value" style="color:{color};">{value}</div>
        </div>
        """,
        unsafe_allow_html=True
    )


# -----------------------------
# Auth UI
# -----------------------------

# def render_auth_debug(controller):
#     with st.expander("Login debug", expanded=True):
#         request_cookie = get_request_cookie(APP_SESSION_COOKIE_NAME)
#         component_cookie = get_component_cookie(controller, APP_SESSION_COOKIE_NAME)

#         st.write("Session user exists:", bool(current_user()))
#         st.write("Request cookie exists:", bool(request_cookie))
#         st.write("Component cookie exists:", bool(component_cookie))

#         token = request_cookie or component_cookie

#         if not token:
#             st.error("No browser cookie found.")
#             return

#         token_hash = hash_session_token(token)

#         result = execute_db(
#             db()
#             .table("app_user_sessions")
#             .select("id, user_id, expires_at, created_at")
#             .eq("token_hash", token_hash)
#             .limit(1),
#             error_message="Could not check login session."
#         )

#         if result is None:
#             st.error("Could not query app_user_sessions.")
#             return

#         if not result.data:
#             st.error("Cookie exists, but Supabase has no matching token_hash.")
#             return

#         row = result.data[0]
#         expires_at = parse_datetime(row["expires_at"])

#         st.success("Cookie exists and Supabase session matches.")
#         st.write("User ID:", row["user_id"])
#         st.write("Created at:", row["created_at"])
#         st.write("Expires at:", expires_at)
#         st.write("Expired:", expires_at <= now())

def render_auth_page(controller):
    st.markdown(
        """
        <div class="top-header">
            <div class="top-header-title">Office Swipe Machine</div>
            <div class="top-header-subtitle">
                Login or create an account to track work sessions.
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    show_flash()
    show_db_error()

    # render_auth_debug(controller)
    
    tab_login, tab_signup = st.tabs(["Login", "Create account"])

    with tab_login:
        username = st.text_input("Username", key="login_username")
        passcode = st.text_input("Passcode", type="password", key="login_passcode")
        remember_for_today = st.checkbox(
            "Keep me logged in for today on this device",
            value=True,
            key="remember_for_today"
        )

        if st.button("Login", width="stretch"):
            success, message = login_user(
                username=username,
                passcode=passcode,
                remember_for_today=remember_for_today,
                controller=controller,
            )

            if success:
                set_flash(message, "success")
            
                st.success("Logged in successfully.")
                st.info(
                    "Your login is being saved on this device. "
                    "Tap Continue to open the dashboard."
                )
            
                if st.button("Continue to dashboard", width="stretch", key="continue_after_login"):
                    st.rerun()
            
                st.stop()
            else:
                set_flash(message, "error")
                st.rerun()

    with tab_signup:
        display_name = st.text_input("Display name", key="signup_display_name")
        username = st.text_input("Username", key="signup_username")
        passcode = st.text_input("Passcode", type="password", key="signup_passcode")

        if st.button("Create account", width="stretch"):
            success, message = create_user(username, display_name, passcode)

            if success:
                set_flash(message, "success")
                st.rerun()
            else:
                set_flash(message, "error")
                st.rerun()


# -----------------------------
# Main UI sections
# -----------------------------

def render_header(user, today_status):
    is_checked_in = today_status["current_status"] == "checked_in"

    if is_checked_in:
        badge_class = "badge-in"
        badge_text = "CHECKED IN"
    else:
        badge_class = "badge-out"
        badge_text = "CHECKED OUT"

    st.markdown(
        f"""
        <div class="top-header">
            <div class="top-header-title">Office Swipe Machine</div>
            <div class="top-header-subtitle">
                Welcome, {user["display_name"]}. Track daily hours, monthly balance, 6-hour safety, and smart logout time.
            </div>
            <div class="status-badge {badge_class}">{badge_text}</div>
        </div>
        """,
        unsafe_allow_html=True
    )


@st.fragment(run_every="1s")
def render_today_dashboard(snapshot):
    current_time = now()

    total_worked = get_live_total_worked_from_snapshot(snapshot)

    target = target_duration()
    remaining = target - total_worked

    if remaining < timedelta():
        remaining = timedelta()

    previous_month_balance = timedelta(
        seconds=snapshot.get("previous_month_balance_seconds", 0)
    )

    available_extra = max(previous_month_balance, timedelta())
    usable_extra = min(available_extra, remaining)
    adjusted_remaining = remaining - usable_extra

    six_hour_remaining = half_day_minimum_duration() - total_worked

    if six_hour_remaining <= timedelta():
        six_hour_status = "Safe ✓"
        six_hour_color = "#198754"
        six_hour_bg = "#EAF8F0"
    else:
        six_hour_status = f"Risk: Need {format_duration(six_hour_remaining)}"
        six_hour_color = "#DC3545"
        six_hour_bg = "#FFF0F2"

    if snapshot.get("current_status") == "checked_in":
        active_check_in = parse_datetime(snapshot["active_check_in"])
        active_checkin_text = format_time(active_check_in)
        normal_logout = format_time(current_time + remaining)
        adjusted_logout = format_time(current_time + adjusted_remaining)
    else:
        active_checkin_text = "-"
        normal_logout = f"If in now: {format_time(current_time + remaining)}"
        adjusted_logout = f"If in now: {format_time(current_time + adjusted_remaining)}"

    if total_worked >= target:
        extra = total_worked - target
        required_more = f"Completed +{format_duration(extra)}"
        normal_logout = "Target completed"
        adjusted_logout = "Target completed"
    else:
        required_more = format_duration(remaining)

    row1 = st.columns(4)
    with row1[0]:
        metric_card("Current Time", format_time(current_time), "#0D6EFD", "#DFF3FF")
    with row1[1]:
        metric_card("Total Worked", format_duration(total_worked), "#198754", "#EAF8F0")
    with row1[2]:
        metric_card("Required More", required_more, "#DC3545", "#FFF0F2")
    with row1[3]:
        metric_card("6 Hr Rule", six_hour_status, six_hour_color, six_hour_bg)

    row2 = st.columns(4)
    with row2[0]:
        metric_card("Monthly Balance", format_signed_duration(previous_month_balance), "#B77900", "#FFF8E1")
    with row2[1]:
        metric_card("Active Check-in", active_checkin_text, "#0D6EFD", "#FFFFFF")
    with row2[2]:
        metric_card("Normal Logout", normal_logout, "#6C757D", "#FFFFFF")
    with row2[3]:
        metric_card("Adjusted Logout", adjusted_logout, "#198754", "#FFFFFF")


def render_quick_actions(user_id, controller):
    with st.container(key="quick_actions"):
        st.markdown(
            """
            <div class="card-title">Quick Actions</div>
            <div class="card-subtitle">Use these buttons like your office swipe machine.</div>
            """,
            unsafe_allow_html=True
        )

        col1, col2, col3, col4 = st.columns(4)

        with col1:
            if st.button("✓ Check In", width="stretch"):
                success, message = check_in(user_id)
                set_flash(message, "success" if success else "warning")
                st.rerun()

        with col2:
            if st.button("⏱ Check Out", width="stretch"):
                success, message = check_out(user_id)
                set_flash(message, "success" if success else "warning")
                st.rerun()

        with col3:
            if st.button("↻ Refresh", width="stretch"):
                st.rerun()

        with col4:
            if st.button("Logout", width="stretch"):
                logout_user(controller)


def render_sessions(user_id):
    today_status = get_today_status(user_id)

    st.markdown(
        """
        <div class="card-title">Completed Sessions Today</div>
        <div class="card-subtitle">All completed check-in and check-out sessions for today.</div>
        """,
        unsafe_allow_html=True
    )

    rows = []

    for session in today_status["sessions"]:
        if not session.get("check_out"):
            continue

        check_in_time = parse_datetime(session["check_in"])
        check_out_time = parse_datetime(session["check_out"])
        duration = check_out_time - check_in_time

        rows.append({
            "Check In": format_time(check_in_time),
            "Check Out": format_time(check_out_time),
            "Duration": format_duration(duration),
        })

    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
    else:
        st.info("No completed sessions yet.")


def render_adjusted_logout(user_id):
    st.markdown(
        """
        <div class="card-title">Adjusted Logout Calculator</div>
        <div class="card-subtitle">
            Use monthly extra time to calculate whether you can leave earlier today.
        </div>
        """,
        unsafe_allow_html=True
    )

    with st.form("adjusted_logout_form"):
        col1, col2 = st.columns(2)

        with col1:
            month_value = st.text_input(
                "Month YYYY-MM",
                value=current_month_key()
            )

        with col2:
            extra_input = st.text_input(
                "Extra to use today",
                value="",
                placeholder="Example: 01:30, 1.5, 2"
            )

        submitted = st.form_submit_button(
            "Calculate Adjusted Logout",
            width="stretch"
        )

    if submitted:
        month_value = month_value.strip()

        if not is_valid_month(month_value):
            st.session_state["adjusted_result_error"] = (
                "Invalid month format. Use YYYY-MM, example: 2026-06."
            )
            st.session_state.pop("adjusted_result", None)
        else:
            try:
                requested_extra = parse_duration_input(extra_input)
                result = calculate_adjusted_logout(
                    user_id=user_id,
                    month_value=month_value,
                    requested_extra=requested_extra
                )
                st.session_state["adjusted_result"] = result
                st.session_state.pop("adjusted_result_error", None)
            except ValueError as exc:
                st.session_state["adjusted_result_error"] = str(exc)
                st.session_state.pop("adjusted_result", None)

    error = st.session_state.get("adjusted_result_error")
    result = st.session_state.get("adjusted_result")

    if error:
        st.error(error)

    if result:
        rows = [
            ("Month balance used", result["month"]),
            ("Today worked", result["today_worked"]),
            ("Normal remaining", result["normal_remaining"]),
            ("Monthly balance", result["monthly_balance"]),
            ("Available extra", result["available_extra"]),
            ("Extra used today", result["extra_used_today"]),
            ("Adjusted remaining", result["adjusted_remaining"]),
            ("6 hour rule", result["six_hour_status"]),
            ("Normal logout", result["normal_logout"]),
            ("Adjusted logout", result["adjusted_logout"]),
        ]

        html_rows = ""

        for label, value in rows:
            html_rows += f"""
            <div class="result-row">
                <div class="result-label">{label}</div>
                <div class="result-value">{value}</div>
            </div>
            """

        st.markdown(
            f"""
            <div class="result-card">
                <div class="result-title">Adjusted logout calculated</div>
                {html_rows}
            </div>
            """,
            unsafe_allow_html=True
        )
    else:
        st.markdown(
            """
            <div class="result-card">
                <div class="result-title" style="color:#6B7280;">Result will appear here</div>
                <div class="small-muted">Leave extra blank to use all available monthly extra.</div>
            </div>
            """,
            unsafe_allow_html=True
        )


def render_monthly_report(user_id):
    st.markdown(
        """
        <div class="card-title">Monthly Average Report</div>
        <div class="card-subtitle">
            Review monthly average, extra balance, shortage, and 6-hour risk days.
        </div>
        """,
        unsafe_allow_html=True
    )

    monthly_error = st.session_state.get("monthly_report_error")

    if monthly_error:
        st.error(monthly_error)

    col1, col2, col3 = st.columns([1.2, 1, 1])

    with col1:
        month_value = st.text_input(
            "Report month",
            value=current_month_key(),
            key="monthly_report_month"
        )

    with col2:
        include_today = st.checkbox(
            "Include today",
            value=True,
            key="monthly_include_today"
        )

    with col3:
        show_report = st.button("Show Report", width="stretch")

    if not show_report and "monthly_report_result" not in st.session_state:
        show_report = True

    if show_report:
        month_value = month_value.strip()

        if not is_valid_month(month_value):
            st.session_state["monthly_report_error"] = (
                "Invalid month format. Use YYYY-MM, example: 2026-06."
            )
            st.session_state.pop("monthly_report_result", None)
            return

        st.session_state.pop("monthly_report_error", None)

        summary = calculate_month_summary(
            user_id=user_id,
            month_value=month_value,
            include_today=include_today
        )

        st.session_state["monthly_report_result"] = summary

    summary = st.session_state.get("monthly_report_result")

    if not summary:
        return

    six_hour_ok_count = 0
    six_hour_risk_count = 0

    for day in summary["days"]:
        if day["worked"] >= half_day_minimum_duration():
            six_hour_ok_count += 1
        else:
            six_hour_risk_count += 1

    row = st.columns(4)

    with row[0]:
        metric_card("Logged Days", str(summary["day_count"]), "#0D6EFD", "#DFF3FF")
    with row[1]:
        metric_card("Average / Day", format_duration(summary["average_per_day"]), "#198754", "#EAF8F0")
    with row[2]:
        metric_card("Monthly Balance", format_signed_duration(summary["monthly_balance"]), "#B77900", "#FFF8E1")
    with row[3]:
        metric_card("6h Risk Days", str(six_hour_risk_count), "#DC3545", "#FFF0F2")

    summary_rows = [
        {"Metric": "Month", "Value": str(summary["month"])},
        {"Metric": "Logged days", "Value": str(summary["day_count"])},
        {"Metric": "Total worked", "Value": format_duration(summary["total_worked"])},
        {"Metric": "Monthly target", "Value": format_duration(summary["monthly_target"])},
        {"Metric": "Average per day", "Value": format_duration(summary["average_per_day"])},
        {"Metric": "Monthly balance", "Value": format_signed_duration(summary["monthly_balance"])},
        {"Metric": "6h safe days", "Value": str(six_hour_ok_count)},
        {"Metric": "6h risk days", "Value": str(six_hour_risk_count)},
    ]

    if summary["monthly_balance"] >= timedelta():
        summary_rows.append({
            "Metric": "Extra available",
            "Value": format_duration(summary["monthly_balance"])
        })
    else:
        summary_rows.append({
            "Metric": "Shortage",
            "Value": format_duration(-summary["monthly_balance"])
        })

    st.markdown("#### Monthly Summary")
    st.dataframe(summary_rows, width="stretch", hide_index=True)

    breakdown_rows = []

    for day in summary["days"]:
        if day["worked"] >= half_day_minimum_duration():
            six_hour_status = "OK"
            six_hour_shortage = "-"
        else:
            shortage = half_day_minimum_duration() - day["worked"]
            six_hour_status = "RISK"
            six_hour_shortage = format_duration(shortage)

        breakdown_rows.append({
            "Date": str(day["date"]),
            "Worked": format_duration(day["worked"]),
            "Balance": format_signed_duration(day["balance"]),
            "6h Status": six_hour_status,
            "6h Shortage": six_hour_shortage,
        })

    st.markdown("#### Daily Breakdown")

    if breakdown_rows:
        st.dataframe(breakdown_rows, width="stretch", hide_index=True)
    else:
        st.info("No logged work found for this month.")


# -----------------------------
# Main app
# -----------------------------

def main():
    st.set_page_config(
        page_title="Office Swipe Machine",
        page_icon="⏱",
        layout="wide"
    )

    controller = get_cookie_controller()
    restore_login_from_daily_session(controller)

    inject_css()

    user = current_user()

    if not user:
        render_auth_page(controller)
        return

    user_id = user["id"]
    today_status = get_today_status(user_id)
    dashboard_snapshot = build_dashboard_snapshot(user_id, today_status)

    render_header(user, today_status)
    show_flash()
    show_db_error()

    render_today_dashboard(dashboard_snapshot)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    render_quick_actions(user_id, controller)
    st.markdown('</div>', unsafe_allow_html=True)

    left, right = st.columns(2)

    with left:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        render_sessions(user_id)
        st.markdown('</div>', unsafe_allow_html=True)

    with right:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        render_adjusted_logout(user_id)
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    render_monthly_report(user_id)
    st.markdown('</div>', unsafe_allow_html=True)


if __name__ == "__main__":
    main()
