from flask import Flask, request, redirect, url_for, render_template_string, session
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime
import psycopg

#
import os
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

REDIS_URL = os.getenv("REDIS_URL")  # ex: rediss://:password@host:port

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri=REDIS_URL or "memory://",
)

#

from flask_wtf.csrf import CSRFProtect

#import os
#import psycopg

DATABASE_URL = os.getenv("DATABASE_URL")  # fourni par Render
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")

app = Flask(__name__)

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

limiter = Limiter(get_remote_address, app=app, default_limits=[])

csrf = CSRFProtect(app)


#
#import os

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,          # OK car Render est en HTTPS
    PERMANENT_SESSION_LIFETIME=1800,     # 30 minutes
)

#
#app.config.update(SESSION_COOKIE_SECURE=True)  #en production HTTPS (Render est HTTPS)
#
app.secret_key = SECRET_KEY


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL manquant. Ajoute-le dans Render (Environment) ou en local.")
    return psycopg.connect(DATABASE_URL)


#app = Flask(__name__)
#app.secret_key = "CHANGE_ME_TO_A_RANDOM_SECRET_123"  # important pour les sessions
#
# ----------------------------
# PostgreSQL config (KM)
# ----------------------------
#PGHOST = "127.0.0.1"
#PGPORT = "5432"          # PostgreSQL port (PAS 5000)
#PGDATABASE = "KM_db"
#PGUSER = "KM_user"
#PGPASSWORD = "1959"
#
#def get_conn():
#    return psycopg.connect(
#        host=PGHOST,
#        port=PGPORT,
#        dbname=PGDATABASE,
#        user=PGUSER,
#        password=PGPASSWORD,
#    )
#########
def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # table members
            cur.execute("""
                CREATE TABLE IF NOT EXISTS members (
                  id         BIGSERIAL PRIMARY KEY,
                  lastname   TEXT NOT NULL,
                  firstname  TEXT NOT NULL,
                  birthdate  DATE NOT NULL,
                  amount     NUMERIC(12,2) NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)

            # table users
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_users (
                  username TEXT PRIMARY KEY,
                  password_hash TEXT NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)

            # admin par défaut si absent
            cur.execute("SELECT 1 FROM app_users WHERE username = %s", ("admin",))
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO app_users (username, password_hash) VALUES (%s, %s)",
                    ("admin", generate_password_hash("1959"))
                )

        conn.commit()

#def init_db():
#    """Crée la table si elle n'existe pas (id BIGSERIAL)."""
#    with get_conn() as conn:
#        with conn.cursor() as cur:
#            cur.execute("""
#                CREATE TABLE IF NOT EXISTS members (
#                  id         BIGSERIAL PRIMARY KEY,
#                  lastname   TEXT NOT NULL,
#                  firstname  TEXT NOT NULL,
#                  birthdate  DATE NOT NULL,
#                  amount     NUMERIC(12,2) NOT NULL,
#                  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
#                );
#            """)
#        conn.commit()

#        cur.execute("""
#            CREATE TABLE IF NOT EXISTS app_users (
#              username TEXT PRIMARY KEY,
#              password_hash TEXT NOT NULL,
#              created_at TIMESTAMPTZ NOT NULL DEFAULT now()
#            );
#        """)

#        # Crée un utilisateur admin s'il n'existe pas
#        cur.execute("SELECT 1 FROM app_users WHERE username = %s", ("admin",))
#        if cur.fetchone() is None:
#            cur.execute(
#                "INSERT INTO app_users (username, password_hash) VALUES (%s, %s)",
#                ("admin", generate_password_hash("1959"))
#            )

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def verify_user(username: str, password: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM app_users WHERE username = %s", (username,))
            row = cur.fetchone()
            if not row:
                return False
            return check_password_hash(row[0], password)
#



def fetch_all_members():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, lastname, firstname, birthdate, amount
                FROM members
                ORDER BY id DESC
            """)
            return cur.fetchall()


def fetch_one(member_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, lastname, firstname, birthdate, amount
                FROM members
                WHERE id = %s
            """, (member_id,))
            return cur.fetchone()


def insert_member(lastname, firstname, birthdate_date, amount_float):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO members (lastname, firstname, birthdate, amount)
                VALUES (%s, %s, %s, %s)
            """, (lastname, firstname, birthdate_date, amount_float))
        conn.commit()


def update_member(member_id, lastname, firstname, birthdate_date, amount_float):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE members
                SET lastname = %s, firstname = %s, birthdate = %s, amount = %s
                WHERE id = %s
            """, (lastname, firstname, birthdate_date, amount_float, member_id))
        conn.commit()


def delete_member(member_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE id = %s", (member_id,))
        conn.commit()


# ----------------------------
# Validation (JJ/MM/AAAA + montant)
# ----------------------------
def validate_inputs(lastname, firstname, birthdate_str, amount_str):
    lastname = (lastname or "").strip()
    firstname = (firstname or "").strip()
    birthdate_str = (birthdate_str or "").strip()
    amount_str = (amount_str or "").strip()

    if not lastname or not firstname or not birthdate_str or not amount_str:
        raise ValueError("Veuillez remplir tous les champs.")

    # Montant : accepte virgule ou point
    amount = float(amount_str.replace(",", "."))

    # Date attendue : JJ/MM/AAAA -> date Python
    birthdate_date = datetime.strptime(birthdate_str, "%d/%m/%Y").date()

    return lastname, firstname, birthdate_date, amount


# ----------------------------
# HTML (template inline)
# ----------------------------
PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Contributions (Flask + PostgreSQL)</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 30px; }
    .wrap { max-width: 980px; margin: 0 auto; }
    h1 { margin-bottom: 6px; }
    .muted { color:#555; margin-top:0; }
    .card { border:1px solid #ddd; border-radius: 10px; padding: 16px; margin: 18px 0; }
    label { display:block; margin: 8px 0 4px; font-weight:600; }
    input { padding: 10px; width: 100%; box-sizing: border-box; border:1px solid #ccc; border-radius: 8px; }
    .grid { display:grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .btn { padding: 10px 14px; border-radius: 10px; border: 1px solid #111; background:#111; color:#fff; cursor:pointer; }
    .btn.secondary { background:#fff; color:#111; }
    .row { display:flex; gap: 10px; margin-top: 12px; }
    .msg { padding: 10px 12px; border-radius: 10px; margin: 12px 0; }
    .error { background:#ffe9ea; border:1px solid #ffb3b8; color:#7a0010; }
    .ok { background:#eaffea; border:1px solid #b8ffb8; color:#0a5a0a; }
    table { width:100%; border-collapse: collapse; margin-top: 10px; }
    th, td { padding: 10px; border-bottom: 1px solid #eee; text-align:left; }
    th { background:#f6f6f6; }
    .small { font-size: 0.92em; color:#444; }
    a { color:#0b57d0; text-decoration:none; }
    a:hover { text-decoration:underline; }
    @media (max-width: 720px) { .grid { grid-template-columns: 1fr; } }
  </style>
</head>


###
<p class="muted">
  Flask + PostgreSQL (DB: <b>KM_db</b>) —
  Logged in as <b>{{ session.get('user') }}</b> —
  <a href="{{ url_for('logout') }}">Logout</a>
</p>
###


<body>
<div class="wrap">
  <h1>List of contributions</h1>
  <p class="muted">Flask + PostgreSQL (DB: <b>KM_db</b>)</p>

  {% if message %}
    <div class="msg {{ 'error' if is_error else 'ok' }}">{{ message }}</div>
  {% endif %}

  <div class="card">
    <h2 style="margin-top:0;">Entry of new members</h2>
    <form method="post" action="{{ url_for('add') }}">
      #
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      #
      <div class="grid">
        <div>
          <label>Last name</label>
          <input name="lastname" placeholder="Ex: Sanou" required>
        </div>
        <div>
          <label>First name</label>
          <input name="firstname" placeholder="Ex: Clarisse" required>
        </div>
        <div>
          <label>Birth date (JJ/MM/AAAA)</label>
          <input name="birthdate" placeholder="Ex: 05/01/2026" required>
        </div>
        <div>
          <label>Contribution ($)</label>
          <input name="amount" placeholder="Ex: 12.50 ou 12,50" required>
        </div>
      </div>
      <div class="row">
        <button class="btn" type="submit">Add new member</button>
        <button class="btn secondary" type="reset">Cancel these data</button>
      </div>
      <p class="small" style="margin-bottom:0;">
        Validation: date <b>JJ/MM/AAAA</b>, montant numérique (virgule ou point accepté).
      </p>
    </form>
  </div>

  {% if edit_row %}
  <div class="card">
    <h2 style="margin-top:0;">Update selected member (ID {{ edit_row[0] }})</h2>
    <form method="post" action="{{ url_for('update', member_id=edit_row[0]) }}">
      #
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      #
      <div class="grid">
        <div>
          <label>Last name</label>
          <input name="lastname" value="{{ edit_row[1] }}" required>
        </div>
        <div>
          <label>First name</label>
          <input name="firstname" value="{{ edit_row[2] }}" required>
        </div>
        <div>
          <label>Birth date (JJ/MM/AAAA)</label>
          <input name="birthdate" value="{{ edit_birthdate }}" required>
        </div>
        <div>
          <label>Contribution ($)</label>
          <input name="amount" value="{{ '%.2f'|format(edit_row[4]) }}" required>
        </div>
      </div>
      <div class="row">
        <button class="btn" type="submit">Save</button>
        <a class="btn secondary" href="{{ url_for('home') }}" style="display:inline-flex; align-items:center; justify-content:center;">Cancel</a>
      </div>
    </form>
  </div>
  {% endif %}

  <div class="card">
    <h2 style="margin-top:0;">Bulletin board</h2>
    <table>
      <thead>
        <tr>
          <th style="width:70px;">ID</th>
          <th>Lastname</th>
          <th>Firstname</th>
          <th>Birthdate</th>
          <th style="width:120px;">Amount</th>
          <th style="width:180px;">Action</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td>{{ r[0] }}</td>
          <td>{{ r[1] }}</td>
          <td>{{ r[2] }}</td>
          <td>{{ r[3].strftime('%d/%m/%Y') }}</td>
          <td>{{ '%.2f'|format(r[4]) }}</td>
          <td>
            <a href="{{ url_for('edit', member_id=r[0]) }}">Edit</a>
            <form method="post"
                  action="{{ url_for('delete', member_id=r[0]) }}"
                  style="display:inline;"
                  onsubmit="return confirm('Supprimer ce membre (ID {{ r[0] }}) ?');">
              #    
              <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
              #
              <button type="submit" class="btn secondary" style="padding:6px 10px; margin-left:8px;">
                Delete
              </button>
            </form>
          </td>
        </tr>
        {% endfor %}
        {% if not rows %}
        <tr><td colspan="6" class="small">Aucune donnée pour le moment.</td></tr>
        {% endif %}
      </tbody>
    </table>
  </div>

</div>
</body>
</html>
"""
##

LOGIN_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Login</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 30px; }
    .wrap { max-width: 420px; margin: 0 auto; }
    .card { border:1px solid #ddd; border-radius: 10px; padding: 16px; margin-top: 40px; }
    label { display:block; margin: 8px 0 4px; font-weight:600; }
    input { padding: 10px; width: 100%; box-sizing: border-box; border:1px solid #ccc; border-radius: 8px; }
    .btn { margin-top: 12px; padding: 10px 14px; border-radius: 10px; border: 1px solid #111; background:#111; color:#fff; cursor:pointer; width:100%; }
    .msg { padding: 10px 12px; border-radius: 10px; margin-top: 12px; }
    .error { background:#ffe9ea; border:1px solid #ffb3b8; color:#7a0010; }
  </style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h2 style="margin-top:0;">Login</h2>
    <form method="post" action="{{ url_for('login') }}">
      #
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      #
      <label>Username</label>
      <input name="username" value="admin" required>
      <label>Password</label>
      <input name="password" type="password" required>
      <button class="btn" type="submit">Sign in</button>
    </form>

    {% if message %}
      <div class="msg error">{{ message }}</div>
    {% endif %}
  </div>
</div>
</body>
</html>
"""


@app.get("/login")
def login():
    return render_template_string(LOGIN_PAGE, message="")


@app.post("/login")
#
@limiter.limit("5 per minute")
#
def login_post():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    if verify_user(username, password):
        session["user"] = username
#
        session.permanent = True
#
        return redirect(url_for("home"))

    return render_template_string(LOGIN_PAGE, message="Identifiants invalides.")


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

##

##################
from flask_wtf.csrf import generate_csrf

@app.context_processor
def inject_csrf_token():
    return dict(csrf_token=generate_csrf)
##################


# ----------------------------
# Routes
# ----------------------------
@app.get("/")
##
@login_required
##
def home():
    rows = fetch_all_members()
    return render_template_string(
        PAGE,
        rows=rows,
        edit_row=None,
        edit_birthdate="",
        message="",
        is_error=False,
    )


@app.post("/add")
##
@login_required
##
def add():
    try:
        ln, fn, bd_date, amt = validate_inputs(
            request.form.get("lastname"),
            request.form.get("firstname"),
            request.form.get("birthdate"),
            request.form.get("amount"),
        )
        insert_member(ln, fn, bd_date, amt)
    except Exception:
        rows = fetch_all_members()
        return render_template_string(
            PAGE,
            rows=rows,
            edit_row=None,
            edit_birthdate="",
            message="Erreur: vérifier Date (JJ/MM/AAAA) et Montant (numérique).",
            is_error=True,
        )
    return redirect(url_for("home"))


@app.get("/edit/<int:member_id>")
##
@login_required
##
def edit(member_id: int):
    row = fetch_one(member_id)
    rows = fetch_all_members()
    if not row:
        return render_template_string(
            PAGE,
            rows=rows,
            edit_row=None,
            edit_birthdate="",
            message=f"Member ID {member_id} introuvable.",
            is_error=True,
        )

    edit_birthdate = row[3].strftime("%d/%m/%Y")
    return render_template_string(
        PAGE,
        rows=rows,
        edit_row=row,
        edit_birthdate=edit_birthdate,
        message="",
        is_error=False,
    )


@app.post("/update/<int:member_id>")
##
@login_required
##
def update(member_id: int):
    try:
        ln, fn, bd_date, amt = validate_inputs(
            request.form.get("lastname"),
            request.form.get("firstname"),
            request.form.get("birthdate"),
            request.form.get("amount"),
        )
        update_member(member_id, ln, fn, bd_date, amt)
    except Exception:
        rows = fetch_all_members()
        row = fetch_one(member_id)
        edit_birthdate = row[3].strftime("%d/%m/%Y") if row else ""
        return render_template_string(
            PAGE,
            rows=rows,
            edit_row=row,
            edit_birthdate=edit_birthdate,
            message="Erreur: vérifier Date (JJ/MM/AAAA) et Montant (numérique).",
            is_error=True,
        )
    return redirect(url_for("home"))


@app.post("/delete/<int:member_id>")
##
@login_required
##
def delete(member_id: int):
    delete_member(member_id)
    return redirect(url_for("home"))


###
@app.after_request
def add_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Content-Security-Policy"] = "default-src 'self' 'unsafe-inline';"
    return resp
###

# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
