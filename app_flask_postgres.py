from __future__ import annotations

import os
import logging
from datetime import datetime, timedelta
from functools import wraps

import psycopg
from psycopg.rows import tuple_row

from flask import Flask, request, redirect, url_for, render_template_string, session, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from flask_wtf.csrf import CSRFProtect, generate_csrf
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# ----------------------------
# Config
# ----------------------------
DATABASE_URL = os.getenv("DATABASE_URL")  # fourni par Render
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
ADMIN_PHONE = os.getenv("ADMIN_PHONE", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "1959")

MEMBER_TYPES = ("admin", "memberR", "memberM", "memberI")
STATUTES = ("actif", "inactif", "suspendu", "radié")

RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")


# ----------------------------
# App
# ----------------------------
app = Flask(__name__)
app.secret_key = SECRET_KEY

# Render/HTTPS headers
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Secure cookies
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,  # Render = HTTPS
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
)

# CSRF
csrf = CSRFProtect(app)


@app.context_processor
def inject_csrf_token():
    return dict(csrf_token=generate_csrf)


# Rate limiting
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri=RATELIMIT_STORAGE_URI,
)


# ----------------------------
# DB helpers
# ----------------------------
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL manquant (Render > KM-Project > Environment).")
    # tuple_row => on garde des tuples (r[0], r[1]...) cohérents avec ton HTML
    return psycopg.connect(DATABASE_URL, row_factory=tuple_row)


def init_db():
    """Crée la table members + admin par défaut si absent."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS members (
                  id             BIGSERIAL PRIMARY KEY,
                  phone          TEXT NOT NULL,
                  membertype     TEXT NOT NULL,
                  mentor         TEXT NOT NULL,
                  lastname       TEXT NOT NULL,
                  firstname      TEXT NOT NULL,
                  birthdate      DATE NOT NULL,
                  idtype         TEXT NOT NULL,
                  idpicture_url  TEXT,
                  currentstatute TEXT NOT NULL,
                  updatedate     DATE NOT NULL DEFAULT CURRENT_DATE,
                  updateuser     TEXT NOT NULL,
                  password_hash  TEXT NOT NULL,
                  CONSTRAINT members_membertype_chk
                    CHECK (membertype IN ('admin','memberR','memberM','memberI')),
                  CONSTRAINT members_currentstatute_chk
                    CHECK (currentstatute IN ('actif','inactif','suspendu','radié'))
                );
            """)

            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS members_phone_uq ON members(phone);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_members_phone ON members(phone);")

            # Admin par défaut si absent
            cur.execute("SELECT 1 FROM members WHERE phone = %s", (ADMIN_PHONE,))
            if cur.fetchone() is None:
                log.info("Admin absent -> création du compte admin par défaut")
                cur.execute("""
                    INSERT INTO members
                    (phone, membertype, mentor, lastname, firstname, birthdate, idtype, idpicture_url,
                     currentstatute, updatedate, updateuser, password_hash)
                    VALUES
                    (%s, 'admin', 'Admin', 'Admin', 'KM', %s, 'N/A', NULL, 'actif', CURRENT_DATE, %s, %s)
                """, (
                    ADMIN_PHONE,
                    datetime.strptime("01/01/2000", "%d/%m/%Y").date(),
                    ADMIN_PHONE,
                    generate_password_hash(ADMIN_PASSWORD),
                ))

        conn.commit()


# ✅ IMPORTANT : exécuté aussi sous gunicorn (Render)
try:
    init_db()
except Exception:
    log.exception("init_db() a échoué au démarrage")
    # on laisse continuer pour que les logs apparaissent, mais l'app sera probablement inutilisable


# ----------------------------
# Queries (ORDER des colonnes = contrat avec le HTML)
# ----------------------------
# Contrat tuple (r[index]) :
# 0 id
# 1 phone
# 2 membertype
# 3 mentor
# 4 lastname
# 5 firstname
# 6 birthdate
# 7 idtype
# 8 idpicture_url
# 9 currentstatute
# 10 updatedate
# 11 updateuser

SELECT_MEMBERS = """
    SELECT id, phone, membertype, mentor, lastname, firstname, birthdate,
           idtype, idpicture_url, currentstatute, updatedate, updateuser
    FROM members
"""


def fetch_all_members():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_MEMBERS + " ORDER BY id DESC")
            return cur.fetchall()


def fetch_one(member_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_MEMBERS + " WHERE id = %s", (member_id,))
            return cur.fetchone()


def fetch_password_hash_and_statute_by_phone(phone: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT password_hash, currentstatute
                FROM members
                WHERE phone = %s
            """, (phone,))
            return cur.fetchone()


def insert_member(phone, membertype, mentor, lastname, firstname, birthdate_date, idtype, idpicture_url,
                  currentstatute, updateuser, password_plain):
    pwd_hash = generate_password_hash(password_plain)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO members
                (phone, membertype, mentor, lastname, firstname, birthdate, idtype, idpicture_url,
                 currentstatute, updatedate, updateuser, password_hash)
                VALUES
                (%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_DATE,%s,%s)
            """, (phone, membertype, mentor, lastname, firstname, birthdate_date, idtype, idpicture_url,
                  currentstatute, updateuser, pwd_hash))
        conn.commit()


def update_member(member_id, phone, membertype, mentor, lastname, firstname, birthdate_date, idtype, idpicture_url,
                  currentstatute, updateuser, new_password_plain: str | None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if new_password_plain:
                pwd_hash = generate_password_hash(new_password_plain)
                cur.execute("""
                    UPDATE members
                    SET phone=%s, membertype=%s, mentor=%s, lastname=%s, firstname=%s, birthdate=%s,
                        idtype=%s, idpicture_url=%s, currentstatute=%s,
                        updatedate=CURRENT_DATE, updateuser=%s, password_hash=%s
                    WHERE id=%s
                """, (phone, membertype, mentor, lastname, firstname, birthdate_date, idtype, idpicture_url,
                      currentstatute, updateuser, pwd_hash, member_id))
            else:
                cur.execute("""
                    UPDATE members
                    SET phone=%s, membertype=%s, mentor=%s, lastname=%s, firstname=%s, birthdate=%s,
                        idtype=%s, idpicture_url=%s, currentstatute=%s,
                        updatedate=CURRENT_DATE, updateuser=%s
                    WHERE id=%s
                """, (phone, membertype, mentor, lastname, firstname, birthdate_date, idtype, idpicture_url,
                      currentstatute, updateuser, member_id))
        conn.commit()


def delete_member(member_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE id = %s", (member_id,))
        conn.commit()


# ----------------------------
# Auth helpers
# ----------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def verify_user(phone: str, password: str) -> bool:
    row = fetch_password_hash_and_statute_by_phone(phone)
    log.info("Login attempt; data in : row=%s", row)
    if not row:
        return False

    pwd_hash, statut = row
    log.info("Login attempt: phone=%s statut=%s pwd_hash=%s password=%s", phone, statut, pwd_hash, password) 

    # bloque login pour suspendu & radié
    if statut in ("radié", "suspendu"):
        return False

    return check_password_hash(pwd_hash, password)


# ----------------------------
# Validation
# ----------------------------
def _strip(x): return (x or "").strip()


def validate_member_form(form, for_update=False):
    phone = _strip(form.get("phone"))
    membertype = _strip(form.get("membertype"))
    mentor = _strip(form.get("mentor"))
    lastname = _strip(form.get("lastname"))
    firstname = _strip(form.get("firstname"))
    birthdate_str = _strip(form.get("birthdate"))
    idtype = _strip(form.get("idtype"))
    idpicture_url = _strip(form.get("idpicture_url")) or None
    currentstatute = _strip(form.get("currentstatute"))
    password = form.get("password") or ""

    if not phone or not membertype or not mentor or not lastname or not firstname or not birthdate_str or not idtype or not currentstatute:
        raise ValueError("Veuillez remplir tous les champs obligatoires.")

    if membertype not in MEMBER_TYPES:
        raise ValueError("membertype invalide.")
    if currentstatute not in STATUTES:
        raise ValueError("currentstatute invalide.")

    birthdate_date = datetime.strptime(birthdate_str, "%d/%m/%Y").date()

    # password obligatoire en création, optionnel en update
    if not for_update and not password:
        raise ValueError("Mot de passe obligatoire pour créer un membre.")

    return {
        "phone": phone,
        "membertype": membertype,
        "mentor": mentor,
        "lastname": lastname,
        "firstname": firstname,
        "birthdate_date": birthdate_date,
        "idtype": idtype,
        "idpicture_url": idpicture_url,
        "currentstatute": currentstatute,
        "password": password,
    }


# ----------------------------
# Templates
# ----------------------------
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
    .small { font-size: 0.92em; color:#444; margin-top: 10px; }
  </style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h2 style="margin-top:0;">Login</h2>
    <form method="post" action="{{ url_for('login') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <label>Phone (username)</label>
      <input name="phone" value="admin" required>
      <label>Password</label>
      <input name="password" type="password" required>
      <button class="btn" type="submit">Sign in</button>
    </form>

    {% if message %}
      <div class="msg error">{{ message }}</div>
    {% endif %}

    <div class="small">
      Accès refusé si statut = 'suspendu' ou 'radié', ou membre inexistant.
    </div>
  </div>
</div>
</body>
</html>
"""

PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Members (Flask + PostgreSQL)</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 30px; }
    .wrap { max-width: 1150px; margin: 0 auto; }
    h1 { margin-bottom: 6px; }
    .muted { color:#555; margin-top:0; }
    .card { border:1px solid #ddd; border-radius: 10px; padding: 16px; margin: 18px 0; }
    label { display:block; margin: 8px 0 4px; font-weight:600; }
    input, select { padding: 10px; width: 100%; box-sizing: border-box; border:1px solid #ccc; border-radius: 8px; }
    .grid { display:grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .btn { padding: 10px 14px; border-radius: 10px; border: 1px solid #111; background:#111; color:#fff; cursor:pointer; }
    .btn.secondary { background:#fff; color:#111; }
    .row { display:flex; gap: 10px; margin-top: 12px; }
    .msg { padding: 10px 12px; border-radius: 10px; margin: 12px 0; }
    .error { background:#ffe9ea; border:1px solid #ffb3b8; color:#7a0010; }
    .ok { background:#eaffea; border:1px solid #b8ffb8; color:#0a5a0a; }
    table { width:100%; border-collapse: collapse; margin-top: 10px; font-size: 0.95em; }
    th, td { padding: 10px; border-bottom: 1px solid #eee; text-align:left; vertical-align: top; }
    th { background:#f6f6f6; }
    .small { font-size: 0.92em; color:#444; }
    a { color:#0b57d0; text-decoration:none; }
    a:hover { text-decoration:underline; }
    @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  </style>
</head>

<body>
<div class="wrap">
  <h1>KM Members</h1>
  <p class="muted">
    Logged in as <b>{{ session.get('user') }}</b> —
    <a href="{{ url_for('logout') }}">Logout</a>
  </p>

  {% if message %}
    <div class="msg {{ 'error' if is_error else 'ok' }}">{{ message }}</div>
  {% endif %}

  <div class="card">
    <h2 style="margin-top:0;">Add new member</h2>
    <form method="post" action="{{ url_for('add') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="grid">
        <div>
          <label>Phone (unique)</label>
          <input name="phone" placeholder="Ex: 0700..." required>
        </div>

        <div>
          <label>Member type</label>
          <select name="membertype" required>
            <option value="memberR">memberR</option>
            <option value="memberM">memberM</option>
            <option value="memberI">memberI</option>
            <option value="admin">admin</option>
          </select>
        </div>

        <div>
          <label>Mentor</label>
          <input name="mentor" placeholder="Ex: Admin / Nom mentor..." required>
        </div>

        <div>
          <label>Last name</label>
          <input name="lastname" required>
        </div>

        <div>
          <label>First name</label>
          <input name="firstname" required>
        </div>

        <div>
          <label>Birth date (JJ/MM/AAAA)</label>
          <input name="birthdate" placeholder="Ex: 25/01/2026" required>
        </div>

        <div>
          <label>IdType (texte libre)</label>
          <input name="idtype" placeholder="Ex: Passeport, Carte nationale..." required>
        </div>

        <div>
          <label>IdPicture URL (optionnel)</label>
          <input name="idpicture_url" placeholder="https://...">
        </div>

        <div>
          <label>Statut</label>
          <select name="currentstatute" required>
            <option value="actif">actif</option>
            <option value="inactif">inactif</option>
            <option value="suspendu">suspendu</option>
            <option value="radié">radié</option>
          </select>
        </div>

        <div>
          <label>Password (obligatoire)</label>
          <input name="password" type="password" required>
        </div>
      </div>

      <div class="row">
        <button class="btn" type="submit">Create member</button>
        <button class="btn secondary" type="reset">Reset</button>
      </div>

      <p class="small" style="margin-bottom:0;">
        Notes: phone est unique. password sera stocké hashé. updatedate/updateuser sont auto.
      </p>
    </form>
  </div>

  {% if edit_row %}
  <div class="card">
    <h2 style="margin-top:0;">Edit member (ID {{ edit_row[0] }})</h2>
    <form method="post" action="{{ url_for('update', member_id=edit_row[0]) }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="grid">
        <div>
          <label>Phone (unique)</label>
          <input name="phone" value="{{ edit_row[1] }}" required>
        </div>

        <div>
          <label>Member type</label>
          <select name="membertype" required>
            {% for t in member_types %}
              <option value="{{ t }}" {{ 'selected' if t==edit_row[2] else '' }}>{{ t }}</option>
            {% endfor %}
          </select>
        </div>

        <div>
          <label>Mentor</label>
          <input name="mentor" value="{{ edit_row[3] }}" required>
        </div>

        <div>
          <label>Last name</label>
          <input name="lastname" value="{{ edit_row[4] }}" required>
        </div>

        <div>
          <label>First name</label>
          <input name="firstname" value="{{ edit_row[5] }}" required>
        </div>

        <div>
          <label>Birth date (JJ/MM/AAAA)</label>
          <input name="birthdate" value="{{ edit_birthdate }}" required>
        </div>

        <div>
          <label>IdType (texte libre)</label>
          <input name="idtype" value="{{ edit_row[7] }}" required>
        </div>

        <div>
          <label>IdPicture URL (optionnel)</label>
          <input name="idpicture_url" value="{{ edit_row[8] or '' }}" placeholder="https://...">
          {% if edit_row[8] %}
            <div class="small" style="margin-top:6px;">
              <a href="{{ edit_row[8] }}" target="_blank" rel="noopener">Open ID picture</a>
            </div>
          {% endif %}
        </div>

        <div>
          <label>Statut</label>
          <select name="currentstatute" required>
            {% for s in statutes %}
              <option value="{{ s }}" {{ 'selected' if s==edit_row[9] else '' }}>{{ s }}</option>
            {% endfor %}
          </select>
        </div>

        <div>
          <label>New password (optionnel)</label>
          <input name="password" type="password" placeholder="laisser vide pour ne pas changer">
        </div>
      </div>

      <div class="row">
        <button class="btn" type="submit">Save</button>
        <a class="btn secondary" href="{{ url_for('home') }}" style="display:inline-flex;align-items:center;justify-content:center;">Cancel</a>
      </div>
    </form>
  </div>
  {% endif %}

  <div class="card">
    <h2 style="margin-top:0;">Members list</h2>
    <table>
      <thead>
        <tr>
          <th style="width:70px;">ID</th>
          <th>Phone</th>
          <th>Type</th>
          <th>Mentor</th>
          <th>Lastname</th>
          <th>Firstname</th>
          <th>Birthdate</th>
          <th>IdType</th>
          <th>IdPicture</th>
          <th>Statut</th>
          <th>Update date</th>
          <th>Update user</th>
          <th style="width:160px;">Action</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td>{{ r[0] }}</td>
          <td>{{ r[1] }}</td>
          <td>{{ r[2] }}</td>
          <td>{{ r[3] }}</td>
          <td>{{ r[4] }}</td>
          <td>{{ r[5] }}</td>
          <td>{{ r[6].strftime('%d/%m/%Y') }}</td>
          <td>{{ r[7] }}</td>
          <td>
            {% if r[8] %}
              <a href="{{ r[8] }}" target="_blank" rel="noopener">link</a>
            {% else %}
              <span class="small">—</span>
            {% endif %}
          </td>
          <td>{{ r[9] }}</td>
          <td>{{ r[10].strftime('%d/%m/%Y') }}</td>
          <td>{{ r[11] }}</td>
          <td>
            <a href="{{ url_for('edit', member_id=r[0]) }}">Edit</a>
            <form method="post"
                  action="{{ url_for('delete', member_id=r[0]) }}"
                  style="display:inline;"
                  onsubmit="return confirm('Supprimer ce membre (ID {{ r[0] }}) ?');">
              <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
              <button type="submit" class="btn secondary" style="padding:6px 10px; margin-left:8px;">
                Delete
              </button>
            </form>
          </td>
        </tr>
        {% endfor %}
        {% if not rows %}
        <tr><td colspan="13" class="small">Aucune donnée pour le moment.</td></tr>
        {% endif %}
      </tbody>
    </table>
  </div>

</div>
</body>
</html>
"""


# ----------------------------
# Routes
# ----------------------------
@app.get("/login")
def login():
    return render_template_string(LOGIN_PAGE, message="")


@app.post("/login")
@limiter.limit("5 per minute")
def login_post():
    phone = (request.form.get("phone") or "").strip()
    password = request.form.get("password") or ""
###
    phone_save = phone
    password_save = password
    log.info("Login attempt; data from HTMLscreen : phone_save=%s password_save=%s", phone_save, password_save)
### 

######### DO NOT
#    if verify_user(phone, password):
#        log.info("Login attempt: LA SESSION DEMARRE OK")
#        session["user"] = phone
#        session.permanent = True
#        return redirect(url_for("home"))
#
#    return render_template_string(LOGIN_PAGE, message="Identifiants invalides ou membre suspendu/radié.")
############

############ DO           
    log.info("Login attempt: LA SESSION DEMARRE OK")
    session["user"] = phone
    session.permanent = True
    redirect(url_for("home"))
############ END DO




@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def home():
    rows = fetch_all_members()
    return render_template_string(
        PAGE,
        rows=rows,
        edit_row=None,
        edit_birthdate="",
        message="",
        is_error=False,
        member_types=MEMBER_TYPES,
        statutes=STATUTES,
    )


@app.post("/add")
@login_required
def add():
    try:
        data = validate_member_form(request.form, for_update=False)
        updateuser = session.get("user") or ADMIN_PHONE

        insert_member(
            phone=data["phone"],
            membertype=data["membertype"],
            mentor=data["mentor"],
            lastname=data["lastname"],
            firstname=data["firstname"],
            birthdate_date=data["birthdate_date"],
            idtype=data["idtype"],
            idpicture_url=data["idpicture_url"],
            currentstatute=data["currentstatute"],
            updateuser=updateuser,
            password_plain=data["password"],
        )
        return redirect(url_for("home"))

    except psycopg.errors.UniqueViolation:
        rows = fetch_all_members()
        return render_template_string(
            PAGE,
            rows=rows,
            edit_row=None,
            edit_birthdate="",
            message="Erreur: ce phone existe déjà (unique).",
            is_error=True,
            member_types=MEMBER_TYPES,
            statutes=STATUTES,
        )
    except Exception as e:
        rows = fetch_all_members()
        return render_template_string(
            PAGE,
            rows=rows,
            edit_row=None,
            edit_birthdate="",
            message=f"Erreur: {str(e)}",
            is_error=True,
            member_types=MEMBER_TYPES,
            statutes=STATUTES,
        )


@app.get("/edit/<int:member_id>")
@login_required
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
            member_types=MEMBER_TYPES,
            statutes=STATUTES,
        )

    edit_birthdate = row[6].strftime("%d/%m/%Y")
    return render_template_string(
        PAGE,
        rows=rows,
        edit_row=row,
        edit_birthdate=edit_birthdate,
        message="",
        is_error=False,
        member_types=MEMBER_TYPES,
        statutes=STATUTES,
    )


@app.post("/update/<int:member_id>")
@login_required
def update(member_id: int):
    try:
        data = validate_member_form(request.form, for_update=True)
        updateuser = session.get("user") or ADMIN_PHONE
        new_pwd = (data["password"] or "").strip() or None

        update_member(
            member_id=member_id,
            phone=data["phone"],
            membertype=data["membertype"],
            mentor=data["mentor"],
            lastname=data["lastname"],
            firstname=data["firstname"],
            birthdate_date=data["birthdate_date"],
            idtype=data["idtype"],
            idpicture_url=data["idpicture_url"],
            currentstatute=data["currentstatute"],
            updateuser=updateuser,
            new_password_plain=new_pwd,
        )
        return redirect(url_for("home"))

    except Exception as e:
        rows = fetch_all_members()
        row = fetch_one(member_id)
        edit_birthdate = row[6].strftime("%d/%m/%Y") if row else ""
        return render_template_string(
            PAGE,
            rows=rows,
            edit_row=row,
            edit_birthdate=edit_birthdate,
            message=f"Erreur: {str(e)}",
            is_error=True,
            member_types=MEMBER_TYPES,
            statutes=STATUTES,
        )


@app.post("/delete/<int:member_id>")
@login_required
def delete(member_id: int):
    # empêcher suppression de l'admin par défaut
    row = fetch_one(member_id)
    if row and row[1] == ADMIN_PHONE:
        abort(403)

    delete_member(member_id)
    return redirect(url_for("home"))


@app.after_request
def add_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Content-Security-Policy"] = "default-src 'self' 'unsafe-inline';"
    return resp


if __name__ == "__main__":
    # Local uniquement. En prod Render, gunicorn gère le port.
    app.run(host="0.0.0.0", port=5000, debug=True)
