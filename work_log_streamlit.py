import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from streamlit_autorefresh import st_autorefresh

import streamlit as st


LOG_FILE = "daily_work_log.json"
TARGET_HOURS = 8
HALF_DAY_MINIMUM_HOURS = 6
APP_TIMEZONE = ZoneInfo("Asia/Kolkata")


# -----------------------------
# Core logic
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


def load_data():
    if not os.path.exists(LOG_FILE):
        return {}

    try:
        with open(LOG_FILE, "r") as file:
            return json.load(file)
    except json.JSONDecodeError:
        return {}


def save_data(data):
    with open(LOG_FILE, "w") as file:
        json.dump(data, file, indent=4)


def get_day_log(data, day_key):
    if day_key not in data:
        data[day_key] = {
            "sessions": [],
            "current_status": "checked_out",
            "active_check_in": None
        }

    return data[day_key]


def get_today_log(data):
    return get_day_log(data, today_key())


def parse_datetime(value):
    parsed = datetime.fromisoformat(value)

    # Old saved records may not have timezone info.
    # Treat old naive records as Asia/Kolkata time.
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


def has_logged_work(day_log):
    return bool(day_log["sessions"]) or day_log["current_status"] == "checked_in"


def calculate_day_worked(day_log, day_value=None, include_active=False):
    total = timedelta()

    for session in day_log["sessions"]:
        check_in = parse_datetime(session["check_in"])
        check_out = parse_datetime(session["check_out"])
        total += check_out - check_in

    if (
        include_active
        and day_log["current_status"] == "checked_in"
        and day_log["active_check_in"]
        and day_value == today_key()
    ):
        active_check_in = parse_datetime(day_log["active_check_in"])
        total += now() - active_check_in

    return total


def calculate_today_worked(today_log):
    return calculate_day_worked(
        today_log,
        day_value=today_key(),
        include_active=True
    )


def calculate_month_summary(data, month_value=None, include_today=True):
    if month_value is None:
        month_value = current_month_key()

    total_worked = timedelta()
    logged_days = []

    for day_value, day_log in data.items():
        if not day_value.startswith(month_value):
            continue

        if not include_today and day_value == today_key():
            continue

        if not has_logged_work(day_log):
            continue

        include_active = include_today and day_value == today_key()

        worked = calculate_day_worked(
            day_log,
            day_value=day_value,
            include_active=include_active
        )

        logged_days.append({
            "date": day_value,
            "worked": worked,
            "balance": worked - target_duration()
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
        "days": logged_days
    }


def calculate_previous_month_balance(data, month_value=None):
    """
    Used for today's adjusted logout.

    Today is excluded because today's active session should not be counted
    as already available extra time.
    """
    if month_value is None:
        month_value = current_month_key()

    summary = calculate_month_summary(
        data=data,
        month_value=month_value,
        include_today=False
    )

    return summary["monthly_balance"]


def check_in(data):
    today_log = get_today_log(data)

    if today_log["current_status"] == "checked_in":
        active_time = parse_datetime(today_log["active_check_in"])
        return False, f"Already checked in at {format_time(active_time)}"

    current_time = now()

    today_log["current_status"] = "checked_in"
    today_log["active_check_in"] = current_time.isoformat()

    save_data(data)

    return True, f"Checked in successfully at {format_time(current_time)}"


def check_out(data):
    today_log = get_today_log(data)

    if today_log["current_status"] == "checked_out":
        return False, "Already checked out."

    current_time = now()
    check_in_time = parse_datetime(today_log["active_check_in"])

    today_log["sessions"].append({
        "check_in": check_in_time.isoformat(),
        "check_out": current_time.isoformat()
    })

    today_log["current_status"] = "checked_out"
    today_log["active_check_in"] = None

    save_data(data)

    session_duration = current_time - check_in_time

    return True, f"Checked out successfully. Session: {format_duration(session_duration)}"


def calculate_adjusted_logout(data, month_value, requested_extra=None):
    today_log = get_today_log(data)
    current_time = now()

    today_worked = calculate_today_worked(today_log)
    normal_remaining = target_duration() - today_worked

    if normal_remaining < timedelta():
        normal_remaining = timedelta()

    previous_month_balance = calculate_previous_month_balance(
        data,
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

    if today_log["current_status"] == "checked_in":
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
# Streamlit UI helpers
# -----------------------------

def inject_css():
    st.markdown(
        """
        <style>
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
                color: white;
                padding: 24px 26px;
                border-radius: 22px;
                box-shadow: 0 10px 25px rgba(13, 110, 253, 0.20);
                margin-bottom: 18px;
            }

            .top-header-title {
                font-size: 30px;
                font-weight: 800;
                margin-bottom: 4px;
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
                color: white;
            }

            .badge-out {
                background: white;
                color: #0D6EFD;
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
                color: #1F2937;
                font-size: 23px;
                font-weight: 850;
                line-height: 1.2;
                word-break: break-word;
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

            div.stButton > button,
            div.stFormSubmitButton > button {
                border-radius: 999px !important;
                border: 0 !important;
                padding: 0.65rem 1.1rem !important;
                font-weight: 800 !important;
                box-shadow: 0 6px 16px rgba(13, 110, 253, 0.16);
            }

            div.stButton > button:hover,
            div.stFormSubmitButton > button:hover {
                transform: translateY(-1px);
                transition: all 0.12s ease-in-out;
            }

            input {
                border-radius: 14px !important;
            }

            .small-muted {
                color: #6B7280;
                font-size: 13px;
            }

            @media (max-width: 768px) {
                .top-header-title {
                    font-size: 24px;
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


def set_flash(message, level="success"):
    st.session_state["flash_message"] = message
    st.session_state["flash_level"] = level


def show_flash():
    message = st.session_state.pop("flash_message", None)
    level = st.session_state.pop("flash_level", "success")

    if not message:
        return

    if level == "success":
        st.success(message)
    elif level == "warning":
        st.warning(message)
    elif level == "error":
        st.error(message)
    else:
        st.info(message)


def get_app_passcode():
    env_passcode = os.environ.get("WORKLOG_PASSCODE", "")

    try:
        secrets_passcode = st.secrets.get("WORKLOG_PASSCODE", "")
    except Exception:
        secrets_passcode = ""

    return env_passcode or secrets_passcode


def require_passcode():
    """
    Optional protection.

    Local:
      export WORKLOG_PASSCODE="your-secret"

    Streamlit Cloud:
      Add WORKLOG_PASSCODE in App Secrets.

    If WORKLOG_PASSCODE is not set, the app opens without login.
    """
    app_passcode = get_app_passcode()

    if not app_passcode:
        return True

    if st.session_state.get("authenticated"):
        return True

    st.markdown(
        """
        <div class="top-header">
            <div class="top-header-title">Office Swipe Machine</div>
            <div class="top-header-subtitle">Enter passcode to continue</div>
        </div>
        """,
        unsafe_allow_html=True
    )

    entered = st.text_input("Passcode", type="password")

    if st.button("Unlock"):
        if entered == app_passcode:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Invalid passcode.")

    return False


def render_header(today_log):
    is_checked_in = today_log["current_status"] == "checked_in"

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
                Track daily hours, monthly balance, 6-hour safety, and smart logout time
            </div>
            <div class="status-badge {badge_class}">{badge_text}</div>
        </div>
        """,
        unsafe_allow_html=True
    )


def render_today_dashboard(data):
    today_log = get_today_log(data)

    current_time = now()
    total_worked = calculate_today_worked(today_log)

    target = target_duration()
    remaining = target - total_worked

    if remaining < timedelta():
        remaining = timedelta()

    previous_month_balance = calculate_previous_month_balance(data)
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

    if today_log["current_status"] == "checked_in":
        active_check_in = parse_datetime(today_log["active_check_in"])
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
        metric_card("Monthly Balance", format_signed_duration(previous_month_balance), "#C58A00", "#FFF8E1")
    with row2[1]:
        metric_card("Active Check-in", active_checkin_text, "#0DCAF0", "#FFFFFF")
    with row2[2]:
        metric_card("Normal Logout", normal_logout, "#6C757D", "#FFFFFF")
    with row2[3]:
        metric_card("Adjusted Logout", adjusted_logout, "#198754", "#FFFFFF")


def render_quick_actions(data):
    st.markdown(
        """
        <div class="card-title">Quick Actions</div>
        <div class="card-subtitle">Use these buttons like your office swipe machine.</div>
        """,
        unsafe_allow_html=True
    )

    col1, col2, col3 = st.columns(3)

    with col1:
        if st.button("✓ Check In", use_container_width=True):
            success, message = check_in(data)
            set_flash(message, "success" if success else "warning")
            st.rerun()

    with col2:
        if st.button("⏱ Check Out", use_container_width=True):
            success, message = check_out(data)
            set_flash(message, "success" if success else "warning")
            st.rerun()

    with col3:
        if st.button("↻ Refresh", use_container_width=True):
            st.rerun()


def render_sessions(data):
    today_log = get_today_log(data)

    st.markdown(
        """
        <div class="card-title">Completed Sessions Today</div>
        <div class="card-subtitle">All completed check-in and check-out sessions for today.</div>
        """,
        unsafe_allow_html=True
    )

    rows = []

    for session in today_log["sessions"]:
        check_in_time = parse_datetime(session["check_in"])
        check_out_time = parse_datetime(session["check_out"])
        duration = check_out_time - check_in_time

        rows.append({
            "Check In": format_time(check_in_time),
            "Check Out": format_time(check_out_time),
            "Duration": format_duration(duration),
        })

    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("No completed sessions yet.")


def render_adjusted_logout(data):
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

        submitted = st.form_submit_button("Calculate Adjusted Logout")

    if submitted:
        month_value = month_value.strip()

        if not is_valid_month(month_value):
            st.session_state["adjusted_result_error"] = "Invalid month format. Use YYYY-MM, example: 2026-06."
            st.session_state.pop("adjusted_result", None)
        else:
            try:
                requested_extra = parse_duration_input(extra_input)
                result = calculate_adjusted_logout(
                    data=data,
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


def render_monthly_report(data):
    st.markdown(
        """
        <div class="card-title">Monthly Average Report</div>
        <div class="card-subtitle">
            Review monthly average, extra balance, shortage, and 6-hour risk days.
        </div>
        """,
        unsafe_allow_html=True
    )

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
        show_report = st.button("Show Report", use_container_width=True)

    if not show_report and "monthly_report_result" not in st.session_state:
        show_report = True

    if show_report:
        month_value = month_value.strip()

        if not is_valid_month(month_value):
            st.error("Invalid month format. Use YYYY-MM, example: 2026-06.")
            return

        summary = calculate_month_summary(
            data=data,
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
        metric_card("Logged Days", summary["day_count"], "#0D6EFD", "#DFF3FF")
    with row[1]:
        metric_card("Average / Day", format_duration(summary["average_per_day"]), "#198754", "#EAF8F0")
    with row[2]:
        metric_card("Monthly Balance", format_signed_duration(summary["monthly_balance"]), "#C58A00", "#FFF8E1")
    with row[3]:
        metric_card("6h Risk Days", six_hour_risk_count, "#DC3545", "#FFF0F2")

    summary_rows = [
        {"Metric": "Month", "Value": summary["month"]},
        {"Metric": "Logged days", "Value": summary["day_count"]},
        {"Metric": "Total worked", "Value": format_duration(summary["total_worked"])},
        {"Metric": "Monthly target", "Value": format_duration(summary["monthly_target"])},
        {"Metric": "Average per day", "Value": format_duration(summary["average_per_day"])},
        {"Metric": "Monthly balance", "Value": format_signed_duration(summary["monthly_balance"])},
        {"Metric": "6h safe days", "Value": six_hour_ok_count},
        {"Metric": "6h risk days", "Value": six_hour_risk_count},
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
    st.dataframe(summary_rows, use_container_width=True, hide_index=True)

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
            "Date": day["date"],
            "Worked": format_duration(day["worked"]),
            "Balance": format_signed_duration(day["balance"]),
            "6h Status": six_hour_status,
            "6h Shortage": six_hour_shortage,
        })

    st.markdown("#### Daily Breakdown")

    if breakdown_rows:
        st.dataframe(breakdown_rows, use_container_width=True, hide_index=True)
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

    inject_css()
    st_autorefresh(interval=1000, key="worklog_autorefresh")

    if not require_passcode():
        return

    data = load_data()
    today_log = get_today_log(data)

    render_header(today_log)
    show_flash()

    render_today_dashboard(data)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    render_quick_actions(data)
    st.markdown('</div>', unsafe_allow_html=True)

    left, right = st.columns(2)

    with left:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        render_sessions(data)
        st.markdown('</div>', unsafe_allow_html=True)

    with right:
        st.markdown('<div class="card">', unsafe_allow_html=True)
        render_adjusted_logout(data)
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    render_monthly_report(data)
    st.markdown('</div>', unsafe_allow_html=True)


if __name__ == "__main__":
    main()
