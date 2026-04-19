# -*- coding: utf-8 -*-
# auth.py -- User model + authentication routes
# Includes: login, signup, logout, password reset, guest access

import os
import secrets
from datetime import datetime, timedelta
from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, session)
from flask_login import (LoginManager, UserMixin, login_user,
                         logout_user, login_required, current_user)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db           = SQLAlchemy()
login_manager = LoginManager()


# ─── User model ───────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    id                  = db.Column(db.Integer, primary_key=True)
    email               = db.Column(db.String(120), unique=True, nullable=False)
    password_hash       = db.Column(db.String(256), nullable=True)  # nullable for guest
    plan                = db.Column(db.String(20),  default="free") # free | pro | guest
    scans_today         = db.Column(db.Integer,     default=0)
    last_scan_date      = db.Column(db.Date,        nullable=True)
    is_guest            = db.Column(db.Boolean,     default=False)
    reset_token         = db.Column(db.String(100), nullable=True)
    reset_token_expires = db.Column(db.DateTime,    nullable=True)
    created_at          = db.Column(db.DateTime,    default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def can_scan(self):
        """Guest: 1 scan per session. Free: 1/day. Pro: unlimited."""
        if self.plan == "pro":
            return True
        if self.is_guest:
            return session.get("guest_scanned", False) is False
        today = datetime.utcnow().date()
        if self.last_scan_date != today:
            return True
        return self.scans_today < 1

    def record_scan(self):
        if self.is_guest:
            session["guest_scanned"] = True
            return
        today = datetime.utcnow().date()
        if self.last_scan_date != today:
            self.scans_today    = 0
            self.last_scan_date = today
        self.scans_today += 1
        db.session.commit()

    def generate_reset_token(self):
        """Create a secure token valid for 1 hour."""
        self.reset_token         = secrets.token_urlsafe(32)
        self.reset_token_expires = datetime.utcnow() + timedelta(hours=1)
        db.session.commit()
        return self.reset_token

    def verify_reset_token(self, token):
        if self.reset_token != token:
            return False
        if datetime.utcnow() > self.reset_token_expires:
            return False
        return True

    def clear_reset_token(self):
        self.reset_token         = None
        self.reset_token_expires = None
        db.session.commit()


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ─── Auth blueprint ───────────────────────────────────────────────────────────

auth = Blueprint("auth", __name__)


# ── Signup ──────────────────────────────────────────────────────────────────

@auth.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated and not current_user.is_guest:
        return redirect(url_for("index"))
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not email or not password:
            flash("Email and password are required.", "error")
            return redirect(url_for("auth.signup"))
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return redirect(url_for("auth.signup"))
        if User.query.filter_by(email=email, is_guest=False).first():
            flash("An account with that email already exists.", "error")
            return redirect(url_for("auth.signup"))
        # If converting from guest, reuse the guest record
        if current_user.is_authenticated and current_user.is_guest:
            user = current_user
            user.email    = email
            user.is_guest = False
            user.plan     = "free"
        else:
            user = User(email=email, is_guest=False)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash("Account created! Welcome to StockRecommender.", "success")
        return redirect(url_for("index"))
    return render_template("signup.html")


# ── Login ───────────────────────────────────────────────────────────────────

@auth.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated and not current_user.is_guest:
        return redirect(url_for("index"))
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user     = User.query.filter_by(email=email, is_guest=False).first()
        if not user or not user.check_password(password):
            flash("Invalid email or password.", "error")
            return redirect(url_for("auth.login"))
        login_user(user, remember=True)
        next_page = request.args.get("next")
        return redirect(next_page or url_for("index"))
    return render_template("login.html")


# ── Logout ──────────────────────────────────────────────────────────────────

@auth.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


# ── Guest access ─────────────────────────────────────────────────────────────

@auth.route("/guest")
def guest():
    """Create a temporary guest session — no registration needed."""
    # Generate a unique guest identifier
    guest_token = secrets.token_hex(8)
    guest_email = f"guest_{guest_token}@guest.local"
    user = User(email=guest_email, is_guest=True, plan="guest")
    db.session.add(user)
    db.session.commit()
    login_user(user)
    session["guest_scanned"] = False
    return redirect(url_for("index"))


# ── Forgot password ──────────────────────────────────────────────────────────

@auth.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user  = User.query.filter_by(email=email, is_guest=False).first()
        # Always show success to prevent email enumeration
        if user:
            token = user.generate_reset_token()
            reset_url = url_for("auth.reset_password",
                                token=token, _external=True)
            _send_reset_email(user.email, reset_url)
        flash("If that email exists, a reset link has been sent.", "success")
        return redirect(url_for("auth.login"))
    return render_template("forgot_password.html")


# ── Reset password ───────────────────────────────────────────────────────────

@auth.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    user = User.query.filter_by(reset_token=token, is_guest=False).first()
    if not user or not user.verify_reset_token(token):
        flash("This reset link is invalid or has expired.", "error")
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return redirect(url_for("auth.reset_password", token=token))
        if password != confirm:
            flash("Passwords do not match.", "error")
            return redirect(url_for("auth.reset_password", token=token))
        user.set_password(password)
        user.clear_reset_token()
        flash("Password updated! Please sign in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("reset_password.html", token=token)


# ─── Email helper ─────────────────────────────────────────────────────────────

def _send_reset_email(to_email, reset_url):
    """Send password reset email using app SMTP settings."""
    import ssl, smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASSWORD", "").strip('"').strip("'")
    from_addr = os.getenv("EMAIL_FROM", smtp_user)

    if not smtp_user or not smtp_pass:
        return  # SMTP not configured, skip silently

    subject = "Reset your StockRecommender password"
    html = f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:480px;margin:0 auto;padding:24px;">
      <h2 style="color:#22d3ee;font-size:1.4rem;margin-bottom:8px;">StockRecommender</h2>
      <p style="color:#374151;font-size:0.9rem;line-height:1.6;">
        You requested a password reset. Click the button below to set a new password.
        This link expires in <strong>1 hour</strong>.
      </p>
      <a href="{reset_url}"
         style="display:inline-block;margin:20px 0;padding:12px 28px;
                background:#22d3ee;color:#000;border-radius:8px;
                font-weight:600;text-decoration:none;font-size:0.95rem;">
        Reset password
      </a>
      <p style="color:#9ca3af;font-size:0.78rem;">
        If you didn't request this, you can safely ignore this email.
      </p>
      <p style="color:#d1d5db;font-size:0.75rem;word-break:break-all;">
        Or copy this link: {reset_url}
      </p>
    </div>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_email
    msg.attach(MIMEText(f"Reset your password: {reset_url}", "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        ctx = ssl.create_default_context()
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port,
                                  context=ctx, timeout=15) as s:
                s.login(smtp_user, smtp_pass)
                s.sendmail(from_addr, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
                s.ehlo(); s.starttls(context=ctx); s.ehlo()
                s.login(smtp_user, smtp_pass)
                s.sendmail(from_addr, [to_email], msg.as_string())
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Reset email failed: %s", e)
