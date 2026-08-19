"""
utils/mailer.py
Production SMTP email sending for OTP, password reset, and notifications.
"""

import os
import smtplib
import ssl
import logging

from email.message import EmailMessage
from email.utils import formataddr
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("mailer")


class Mailer:
    def __init__(self):
        self.host = os.getenv("SMTP_HOST")
        self.port = int(os.getenv("SMTP_PORT", "587"))
        self.username = os.getenv("SMTP_USER")
        self.password = os.getenv("SMTP_PASSWORD")
        self.from_email = os.getenv("SMTP_FROM")
        self.from_name = os.getenv("SMTP_FROM_NAME", "AgriVenture")
        self.use_tls = os.getenv("SMTP_TLS", "True").lower() == "true"

    def send(self, recipient: str, subject: str, html: str, plain: str = "") -> bool:
        try:
            message = EmailMessage()
            message["Subject"] = subject
            message["From"] = formataddr((self.from_name, self.from_email))
            message["To"] = recipient

            if plain:
                message.set_content(plain)
            else:
                message.set_content("Please view this email using an HTML capable email client.")

            message.add_alternative(html, subtype="html")

            context = ssl.create_default_context()

            with smtplib.SMTP(self.host, self.port) as smtp:
                smtp.ehlo()
                if self.use_tls:
                    smtp.starttls(context=context)
                    smtp.ehlo()
                smtp.login(self.username, self.password)
                smtp.send_message(message)

            logger.info(f"Email sent successfully to {recipient}")
            return True

        except Exception as e:
            logger.exception(f"Failed to send email to {recipient}: {e}")
            return False


mailer = Mailer()


def send_otp_email(to_email: str, otp: str, purpose: str = "verification", expires: int = 10) -> bool:
    """Send OTP email for registration, verification, or password reset."""
    if purpose == "password_reset":
        subject = "Reset your AgriVenture password"
        html = f"""
        <html>
        <body style="font-family:Arial;padding:30px;">
            <h2 style="color:#2E7D32;">Password Reset</h2>
            <p>Use this code to reset your password.</p>
            <div style="font-size:40px;font-weight:bold;color:#2E7D32;letter-spacing:8px;margin:25px 0;">
                {otp}
            </div>
            <p>This code expires in <strong>{expires} minutes</strong>.</p>
            <hr>
            <small>If you didn't request this, simply ignore this email.</small>
        </body>
        </html>
        """
        plain = f"AgriVenture Password Reset\nOTP: {otp}\nExpires in {expires} minutes."
    else:
        subject = "AgriVenture Verification Code"
        html = f"""
        <html>
        <body style="font-family:Arial;padding:30px;">
            <h2 style="color:#2E7D32;">AgriVenture</h2>
            <p>Your verification code is:</p>
            <div style="font-size:40px;font-weight:bold;letter-spacing:10px;color:#2E7D32;margin:25px 0;">
                {otp}
            </div>
            <p>This OTP expires in <strong>{expires} minutes</strong>.</p>
            <hr>
            <small>If you didn't request this, simply ignore this email.</small>
        </body>
        </html>
        """
        plain = f"AgriVenture Verification\nOTP: {otp}\nExpires in {expires} minutes."

    return mailer.send(recipient=to_email, subject=subject, html=html, plain=plain)


def send_welcome_email(to_email: str, full_name: str) -> bool:
    """Send welcome email after account verification."""
    html = f"""
    <html>
    <body style="font-family:Arial;padding:30px;">
        <h2 style="color:#2E7D32;">Welcome {full_name}!</h2>
        <p>Thank you for creating your AgriVenture account.</p>
        <p>You may now login using the mobile application.</p>
        <p style="margin-top:30px;color:#666;">
            Get started by registering your farms and connecting IoT sensors.
        </p>
    </body>
    </html>
    """
    return mailer.send(recipient=to_email, subject="Welcome to AgriVenture", html=html)


def send_notification_email(to_email: str, title: str, body: str) -> bool:
    """Send notification emails (recommendations, alerts, etc.)."""
    html = f"""
    <html>
    <body style="font-family:Arial;padding:30px;">
        <h2 style="color:#2E7D32;">{title}</h2>
        <p>{body}</p>
        <p style="margin-top:30px;color:#999;font-size:12px;">
            Check your AgriVenture app for more details.
        </p>
    </body>
    </html>
    """
    return mailer.send(recipient=to_email, subject=f"AgriVenture: {title}", html=html)