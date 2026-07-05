"""Outbound email composition and SMTP delivery."""

import smtplib
from email.message import EmailMessage

from .config import VERIFICATION_TTL_MINUTES
from .settings import effective_email_settings

def send_email_message(conn, message):
    settings, _ = effective_email_settings(conn)
    missing = [name for name, value in (("SMTP_HOST", settings["host"]), ("SMTP_PORT", settings["port"]), ("SMTP_FROM", settings["from"])) if not value]
    if missing:
        raise RuntimeError(f"邮箱服务器未配置 {', '.join(missing)}")
    smtp_class = smtplib.SMTP_SSL if settings["use_ssl"] else smtplib.SMTP
    with smtp_class(settings["host"], settings["port"], timeout=20) as smtp:
        if settings["starttls"] and not settings["use_ssl"]:
            smtp.starttls()
        if settings["user"]:
            smtp.login(settings["user"], settings["password"])
        smtp.send_message(message)


def send_verification_email(conn, email, code):
    settings, _ = effective_email_settings(conn)
    message = EmailMessage()
    message["Subject"] = "预约邮箱验证码"
    message["From"] = settings["from"]
    message["To"] = email
    message.set_content(f"您的预约验证码是：{code}\n\n验证码 {VERIFICATION_TTL_MINUTES} 分钟内有效。")
    send_email_message(conn, message)


def send_notification_email(conn, email, event_type, payload):
    task = payload["task"]
    booking = payload["booking"]
    subjects = {"booking_confirmed": "预约成功", "booking_rescheduled": "预约已改期", "booking_cancelled": "预约已取消", "booking_reminder": "预约提醒"}
    settings, _ = effective_email_settings(conn)
    message = EmailMessage()
    message["Subject"] = f"{task['title']}｜{subjects.get(event_type, '预约通知')}"
    message["From"] = settings["from"]
    message["To"] = email
    lines = [
        f"{booking['name']}，您好：", "", f"{subjects.get(event_type, '预约通知')}：{task['title']}",
        f"日期：{booking['date']}", f"时间：{booking['time']} - {booking['endTime']}",
        f"预约编号：{booking.get('bookingRef', '')}",
    ]
    if task["location"]:
        lines.append(f"地点：{task['location']}")
    if payload.get("manageUrl"):
        lines.extend(["", f"管理预约：{payload['manageUrl']}"])
    if payload.get("calendarUrl"):
        lines.append(f"添加到日历：{payload['calendarUrl']}")
    message.set_content("\n".join(lines))
    send_email_message(conn, message)
