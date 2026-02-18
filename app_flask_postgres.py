# ---------------------------------
# External functions impotation
# ---------------------------------
from __future__ import annotations

from enum import member
from enum import member
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
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Melissa@1991")

MEMBER_TYPES = ("membre", "independant", "mentor", "admin")
STATUTES = ("probatoire","actif", "inactif", "suspendu", "radié")

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

@app.context_processor
def inject_logged_user_label():
    phone = session.get("user")
    if not phone:
        return dict(logged_user_label="")

    try:
        row = fetch_first_last_by_phone(phone)
        if row:
            firstname, lastname = row
            # ex: "8324940214 — Clarisse Lukula"
            return dict(logged_user_label=f"{phone} — {firstname} {lastname}")
    except Exception:
        log.exception("Impossible de récupérer firstname/lastname pour phone=%s", phone)

    # fallback si pas trouvé
    return dict(logged_user_label=f"{phone}")


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

#>20260203
def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # membres (avec balance)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS membres (
                  id             BIGSERIAL PRIMARY KEY,
                  phone          TEXT NOT NULL UNIQUE,
                  membertype     TEXT NOT NULL,
                  mentor         TEXT NOT NULL,
                  lastname       TEXT NOT NULL,
                  firstname      TEXT NOT NULL,
                  birthdate      DATE NOT NULL,
                  idtype         TEXT NOT NULL,
                  idpicture_url  TEXT,
                  currentstatute TEXT NOT NULL,
                  balance        DECIMAL(18,2) NOT NULL DEFAULT 0,
                  updatedate     DATE NOT NULL DEFAULT CURRENT_DATE,
                  updateuser     TEXT NOT NULL,
                  password_hash  TEXT NOT NULL,
                  membershipdate DATE NOT NULL DEFAULT CURRENT_DATE,
                  CONSTRAINT membres_membertype_chk
                    CHECK (membertype IN ('membre','independant','mentor','admin')),
                  CONSTRAINT membres_currentstatute_chk
                    CHECK (currentstatute IN ('probatoire','actif','inactif','suspendu','radié'))
                );
            """)

            # mouvements
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mouvements (
                  id           BIGSERIAL PRIMARY KEY,
                  phone        TEXT NOT NULL,
                  firstname    TEXT NOT NULL,
                  lastname     TEXT NOT NULL,
                  mvt_date     DATE NOT NULL,
                  amount       DECIMAL(18,2) NOT NULL DEFAULT 0,
                  debitcredit  VARCHAR(1) NOT NULL CHECK (debitcredit IN ('D','C')),
                  reference    TEXT NOT NULL UNIQUE,
                  updatedate   DATE NOT NULL DEFAULT CURRENT_DATE,
                  libelle      TEXT,
                  updated_by   TEXT,
                  );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mouvements_phone ON mouvements(phone);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mouvements_date ON mouvements(mvt_date);")

            # décès
            cur.execute("""
                CREATE TABLE IF NOT EXISTS deces (
                  id            BIGSERIAL PRIMARY KEY,
                  phone         TEXT NOT NULL,
                  date_deces    DATE NOT NULL,
                  declared_by   TEXT NOT NULL,
                  created_at    TIMESTAMP NOT NULL DEFAULT NOW(),
                  reference     TEXT
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_deces_phone ON deces(phone);")

        conn.commit()

#

def fetch_first_last_by_phone(phone: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT firstname, lastname
                FROM membres
                WHERE phone = %s
                LIMIT 1
            """, (phone,))
            return cur.fetchone()   # tuple: (firstname, lastname) ou None

#< 20260203

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
# 10 balance
# 11 updatedate
# 12 updateuser
# 13 password_hash
# 14 membershipdate

SELECT_membres = """
    SELECT id, phone, membertype, mentor, lastname, firstname, birthdate,
           idtype, idpicture_url, currentstatute, balance, updatedate, updateuser, password_hash, membershipdate
    FROM membres
"""
#
def fetch_all_membres():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_membres + " ORDER BY id DESC")
            return cur.fetchall()


def fetch_one(member_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_membres + " WHERE id = %s", (member_id,))
            return cur.fetchone()


def fetch_password_hash_and_statute_by_phone(phone: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT password_hash, currentstatute
                FROM membres
                WHERE phone = %s
            """, (phone,))
            return cur.fetchone()


def insert_member(phone, membertype, mentor, lastname, firstname, birthdate_date, idtype, idpicture_url,
                  currentstatute, updateuser, password_plain):
    pwd_hash = generate_password_hash(password_plain)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO membres
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
                    UPDATE membres
                    SET phone=%s, membertype=%s, mentor=%s, lastname=%s, firstname=%s, birthdate=%s,
                        idtype=%s, idpicture_url=%s, currentstatute=%s,
                        updatedate=CURRENT_DATE, updateuser=%s, password_hash=%s
                    WHERE id=%s
                """, (phone, membertype, mentor, lastname, firstname, birthdate_date, idtype, idpicture_url,
                      currentstatute, updateuser, pwd_hash, member_id))
            else:
                cur.execute("""
                    UPDATE membres
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
            cur.execute("DELETE FROM membres WHERE id = %s", (member_id,))
        conn.commit()
#
def fetch_member_by_phone(phone: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, phone, membertype, mentor, lastname, firstname, birthdate,
                       idtype, idpicture_url, currentstatute, balance, updatedate, updateuser
                FROM membres
                WHERE phone=%s
            """, (phone,))
            return cur.fetchone()

def update_member_password(phone: str, new_password_plain: str, updateuser: str):
    pwd_hash = generate_password_hash(new_password_plain)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE membres
                SET password_hash=%s, updatedate=CURRENT_DATE, updateuser=%s
                WHERE phone=%s
            """, (pwd_hash, updateuser, phone))
        conn.commit()

def list_mouvements_by_phone(phone: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, phone, firstname, mvt_date, amount, debitcredit, reference, libelle, updatedate, updated_by
                FROM mouvements
                WHERE phone=%s
                ORDER BY mvt_date DESC, id DESC
            """, (phone,))
            return cur.fetchall()

def list_all_mouvements():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, phone, firstname, mvt_date, amount, debitcredit, reference, libelle, updatedate, updated_by
                FROM mouvements
                ORDER BY mvt_date DESC, id DESC
            """)
            return cur.fetchall()

def update_mouvement(id: int, mvt_date, amount, debitcredit, reference, libelle):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE mouvements
                SET mvt_date=%s, amount=%s, debitcredit=%s, reference=%s, libelle=%s, updatedate=CURRENT_DATE, updated_by=%s
                WHERE id=%s
            """, (id,mvt_date, amount, debitcredit, reference, libelle,date.today(), session.get("user")))
        conn.commit()

def delete_mouvement(id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM mouvements WHERE id=%s", (id,))
        conn.commit()

def list_groupe_for_mentor(mentor_phone: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT phone, firstname, lastname, membertype, currentstatute, balance
                FROM membres
                WHERE mentor=%s
                ORDER BY lastname, firstname
            """, (mentor_phone,))
            return cur.fetchall()

def create_deces(phone: str, date_deces, declared_by: str, reference: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO deces (phone, date_deces, declared_by, reference)
                VALUES (%s,%s,%s,%s)
            """, (phone, date_deces, declared_by, reference))
        conn.commit()

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
    membershipdate_str = _strip(form.get("membershipdate"))
  
    balance_str = _strip(form.get("balance")) or None
    currentstatute = _strip(form.get("currentstatute"))
    password = form.get("password") or ""

    if not phone or not membertype or not mentor or not lastname or not firstname or not birthdate_str or not membershipdate_str or not balance_str or not currentstatute:
        raise ValueError("Veuillez remplir tous les champs obligatoires.")

    if membertype not in MEMBER_TYPES:
        raise ValueError("membertype invalide.")
    if currentstatute not in STATUTES:
        raise ValueError("currentstatute invalide.")

    birthdate_date = datetime.strptime(birthdate_str, "%d/%m/%Y").date()
    membershipdate_date = datetime.strptime(membershipdate_str, "%d/%m/%Y").date()
    balance_decimal = float(balance_str)

    # password obligatoire en création, optionnel en update
    if not for_update and not password:
        raise ValueError("Mot de passe obligatoire pour créer un membre.")
    
    return {
        "phone": phone,
        "membertype": membertype,
        "mentor": mentor,
        "lastname": lastname,
        "firstname": firstname,
        "birthdate": birthdate_date,
        "membershipdate": membershipdate_date,
        "balance": balance_decimal,
        "currentstatute": currentstatute,
        "password": password,
    }


# ------------------------------------
# Décorateurs d'accès (login + rôles)
# ------------------------------------
def role_required(*roles):
    def deco(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "user" not in session:
                return redirect(url_for("login", next=request.path))
            if session.get("membertype") not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return deco

admin_required = role_required("admin")
mentor_required = role_required("mentor", "admin")   # admin voit tout


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
    """Retourne True si (phone, password) est valide et statut autorisé."""
    row = fetch_password_hash_and_statute_by_phone(phone)
    log.info("Login attempt; data in : row=%s", row)
    if not row:
        return False

    pwd_hash, statut = row
    log.info("Login attempt: phone=%s statut=%s", phone, statut)

    # bloque login pour suspendu & radié
    if statut in ("radié", "suspendu"):
        return False

    return check_password_hash(pwd_hash, password)

def get_user_profile_by_phone(phone: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT firstname, lastname, membertype
                FROM membres
                WHERE phone = %s
            """, (phone,))
            return cur.fetchone()

#
# ----------------------------
# Lancement de l'application
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
    <h2 style="margin-top:0;">Connexion KM-Kimya</h2>
    <form method="post" action="{{ url_for('login') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <label>Identifiant <small>(nº téléphone sans prefixes)</small> :</label>
      <input name="phone" value="admin" required>
      <label>Mot de passe :</label>
      <input name="password" type="password" required>
      <button class="btn" type="submit">Se connecter</button>
    </form>

    {% if message %}
      <div class="msg error">{{ message }}</div>
    {% endif %}

    <div class="small">
      <small>Accès refusé si statut = 'suspendu' ou 'radié', ou membre inexistant.</small>
    </div>
  </div>
</div>
</body>
</html>
"""

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone = request.form.get("phone")
        password = request.form.get("password")

        if verify_user(phone, password):
            member = fetch_member_by_phone(phone)  # ✅ ici phone existe
            session["user"] = phone
            session["membertype"] = member[2]  # index 2 = membertype
            session["firstname"] = member[5]
            session["lastname"] = member[4]
            session.permanent = True
            return redirect(url_for("home"))
        else:
            error = "Identifiants incorrects"
            return render_template_string(LOGIN_PAGE, message=error)

    return render_template_string(LOGIN_PAGE)
  


# --------------------------------------
# ENDPOINT #0 HOME PAGE ( ménu général)
# --------------------------------------
DASHBOARD_PAGE = """
<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KM-Kimya</title>
<style>
  body{font-family:Arial;margin:24px;background:#fff;}
  .top{display:flex;align-items:flex-start;gap:14px;}
  .brand{width:54px;height:54px;border-radius:16px;background:#111;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;}
  .hdr{flex:1}
  .muted{color:#666;margin:2px 0 0}
  .actions{display:flex;gap:10px;align-items:center;}
  .pill{padding:6px 10px;border:1px solid #ddd;border-radius:999px;font-size:13px;background:#fafafa;}
  .btn{padding:7px 12px;border:1px solid #111;border-radius:999px;background:#fff;cursor:pointer;}
  .grid{margin-top:22px;display:grid;grid-template-columns:repeat(3,1fr);gap:14px;}
  .card{border:1px solid #e7e7e7;border-radius:16px;padding:14px;display:flex;gap:12px;align-items:flex-start;}
  .icon{width:42px;height:42px;border-radius:12px;border:1px solid #eee;display:flex;align-items:center;justify-content:center;background:#fafafa;}
  .t{font-weight:700;margin:0}
  .d{color:#666;margin:4px 0 0;font-size:13px}
  .link{color:#0b57d0;text-decoration:none;font-weight:600;}
  .link:hover{text-decoration:underline;}
  @media (max-width: 900px){ .grid{grid-template-columns:1fr;} body{margin:14px;} }
</style>
</head>
<body>
  <span class="top">
    <div class="brand">KM</div>
    <div class="hdr">
      <h2 style="margin:0;">Kimya</h2>
      <div class="muted">membre connecté : <b>{{ connected_label }}</b></div>
      <div><small>Rôle: <b>{{ connected_role }}</b></small></div>
      <p style="text-align:right;"><a class="btn" href="{{ url_for('logout') }}">Logout</a></p>
      <style>max-width:48px;<style/></div>
    </div>
    <style>div{white-space:nowrap;}</style>
  </span>

  <!-- Zone 1: Tous -->

  <div class="grid">
    <div class="card">
      <div class="icon">📄</div>
      <div>
        <p class="t">Mon compte</p>
        <p class="d">Profil, informations et statut.</p>
        <a class="link" href="{{ url_for('account') }}">Ouvrir</a>
      </div>
    </div>

    <div class="card">
      <div class="icon">💳</div>
      <div>
        <p class="t">Mes mouvements</p>
        <p class="d">Historique des cotisations et solde.</p>
        <a class="link" href="{{ url_for('my_mouvements') }}">Ouvrir</a>
      </div>
    </div>

    <div class="card">
      <div class="icon">🕊️</div>
      <div>
        <p class="t">Déclarer un décès</p>
        <p class="d">Enregistrer un cas de décès.</p>
        <a class="link" href="{{ url_for('deces') }}">Ouvrir</a>
      </div>
    </div>

    <div class="card">
      <div class="icon">🔁</div>
      <div>
        <p class="t">Transfert cotisations</p>
        <p class="d">Transférer un montant vers un autre membre.</p>
        <a class="link" href="{{ url_for('transfer') }}">Ouvrir</a>
      </div>
    </div>

    <div class="card">
      <div class="icon">🎓</div>
      <div>
        <p class="t">Mentor application</p>
        <p class="d">Demande de statut Mentor.</p>
        <a class="link" href="{{ url_for('mentor_application') }}">Ouvrir</a>
      </div>
    </div>

    
    <!-- Zone 2: mentor + admin -->
    {% if connected_role in ('mentor','admin') %}

    <div class="card">
      <div class="icon">👥</div>
      <div>
        <p class="t">Mon groupe</p>
        <p class="d">Membres rattachés + soldes.</p>
        <a class="link" href="{{ url_for('groupe') }}">Ouvrir</a>
      </div>
    </div>

    <div class="card">
      <div class="icon">➕</div>
      <div>
        <p class="t">Créer un membre</p>
        <p class="d">Enregistrer un nouveau membre.</p>
        <a class="link" href="{{ url_for('add_member') }}">Ouvrir</a>
      </div>
    </div>

    {% endif %}



    <!-- Zone 3: admin only -->
    {% if connected_role == 'admin' %}

    <div class="card">
      <div class="icon">⬇️</div>
      <div>
        <p class="t">Importer cotisations</p>
        <p class="d">Lancer l'importation des mouvements</p>
        <a class="link" href="{{ url_for('import_mouvements') }}">Ouvrir</a>
      </div>
    </div>

    <div class="card">
      <div class="icon">🧾</div>
      <div>
        <p class="t">Check mouvements</p>
        <p class="d">Voir/modifier toute la table mouvements.</p>
        <a class="link" href="{{ url_for('check_mouvements') }}">Ouvrir</a>
      </div>
    </div>

    <div class="card">
      <div class="icon">🛠️</div>
      <div>
        <p class="t">Administration</p>
        <p class="d">Suivi global & contrôle.</p>
        <a class="link" href="{{ url_for('datageneralfollowup') }}">Ouvrir</a>
      </div>

      {% endif %}

    </div>
  </div>
</body></html>
"""
@app.get("/")
@login_required
def home():
    rows = fetch_all_membres()

    phone = session.get("user")  # ✅ ici on a phone
    member = fetch_member_by_phone(phone) if phone else None

    if member:
        connected_phone = member[1]
        connected_firstname = member[5]
        connected_lastname = member[4]
        connected_role = member[2]
        connected_label = f"{connected_phone} — {connected_firstname} {connected_lastname}"
    else:
        connected_label = phone or ""

    return render_template_string(
        DASHBOARD_PAGE,
        rows=rows,
        connected_label=connected_label,   # ✅ variable pour l'affichage
        connected_role=connected_role if member else "",  
        edit_row=None,
        edit_birthdate="",
        message="",
        is_error=False,
        member_types=MEMBER_TYPES,
        statutes=STATUTES,
    )

@app.route("/logout", methods=["GET"])
def logout():
    session.clear()              # supprime user, membertype, etc.
    return redirect(url_for("login"))



# ---------------------------------------------------------------
#   Endpoint #1 — Mon compte (lecture + mot de passe modifiable)
# ---------------------------------------------------------------
ACCOUNT_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mon compte</title>
<style>
 body{font-family:Arial;margin:20px} .wrap{max-width:900px;margin:0 auto}
 .card{border:1px solid #e7e7e7;border-radius:16px;padding:16px}
 label{display:block;margin:10px 0 4px;font-weight:700}
 input{width:100%;padding:10px;border:1px solid #ddd;border-radius:10px}
 input[readonly]{background:#f6f6f6}
 .row{display:flex;gap:10px;margin-top:12px}
 .btn{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#111;color:#fff;cursor:pointer}
 .btn2{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#fff;color:#111;cursor:pointer}
 .msg{margin-top:12px;padding:10px;border-radius:12px}
 .ok{background:#eaffea;border:1px solid #b8ffb8}
 .err{background:#ffe9ea;border:1px solid #ffb3b8}
</style></head><body>
<div class="wrap">
  <h2>Mon compte</h2>
  <p><a href="{{ url_for('home') }}">← Retour</a></p>
  <div class="card">
    <form method="post">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <label>Phone</label><input value="{{ m[1] }}" readonly>
      <label>Nom</label><input value="{{ m[4] }}" readonly>
      <label>Prénom</label><input value="{{ m[5] }}" readonly>
      <label>Type</label><input value="{{ m[2] }}" readonly>
      <label>Statut</label><input value="{{ m[9] }}" readonly>
      <label>Solde</label><input value="{{ m[10] }}" readonly>

      <label>Nouveau mot de passe</label>
      <input name="new_password" type="password" placeholder="laisser vide pour ne pas changer">
      <div class="row">
        <button class="btn" type="submit">Enregistrer</button>
        <a class="btn2" href="{{ url_for('home') }}">Annuler</a>
      </div>
      {% if message %}
        <div class="msg {{ 'err' if is_error else 'ok' }}">{{ message }}</div>
      {% endif %}
    </form>
  </div>
</div></body></html>
"""
# Endpoint1 Mon Compte (menu card)
@app.route("/account", methods=["GET","POST"])
@login_required
def account():
    phone = session["user"]
    m = fetch_member_by_phone(phone)
    if request.method == "POST":
        pwd = (request.form.get("new_password") or "").strip()
        if pwd:
            update_member_password(phone, pwd, updateuser=phone)
            return render_template_string(ACCOUNT_PAGE, m=m, message="Mot de passe modifié.", is_error=False)
        return render_template_string(ACCOUNT_PAGE, m=m, message="Aucun changement.", is_error=False)
    return render_template_string(ACCOUNT_PAGE, m=m, message="", is_error=False)


# ---------------------------------------------------------------
#   Endpoint #2 — Mes mouvements (lecture seule + balance)
# ---------------------------------------------------------------
MY_MVT_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mes mouvements</title>
<style>
 body{font-family:Arial;margin:20px} .wrap{max-width:1100px;margin:0 auto}
 .pill{display:inline-block;padding:6px 10px;border:1px solid #ddd;border-radius:999px;background:#fafafa;margin-bottom:10px}
 table{width:100%;border-collapse:collapse}
 th,td{padding:10px;border-bottom:1px solid #eee;text-align:left}
 th{background:#f6f6f6}
</style></head><body><div class="wrap">
  <h2>Mes mouvements</h2>
  <p><a href="{{ url_for('home') }}">← Retour</a></p>
  <div class="pill">Solde actuel: <b>{{ balance }}</b></div>
  <table>
    <thead><tr>
      <th>Date</th><th>Montant</th><th>D/C</th><th>Référence</th>
    </tr></thead>
    <tbody>
    {% for r in rows %}
      <tr>
        <td>{{ r[3].strftime('%d/%m/%Y') }}</td>
        <td>{{ r[4] }}</td>
        <td>{{ r[5] }}</td>
        <td>{{ r[6] }}</td>
      </tr>
    {% endfor %}
    {% if not rows %}<tr><td colspan="4">Aucun mouvement.</td></tr>{% endif %}
    </tbody>
  </table>
</div></body></html>
"""
# Endpoint2 Mes mouvements (menu card)
@app.get("/mouvements")
@login_required
def my_mouvements():
    phone = session["user"]
    m = fetch_member_by_phone(phone)
    rows = list_mouvements_by_phone(phone)
    return render_template_string(MY_MVT_PAGE, rows=rows, balance=(m[10] if m else 0))


# ----------------------------------------------------------------------------
# Endpoint #3 — Déclarer un décès (saisie phone + date, affichage nom/prénom)
# ----------------------------------------------------------------------------
DECES_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Déclarer un décès</title>
<style>
 body{font-family:Arial;margin:20px} .wrap{max-width:800px;margin:0 auto}
 .card{border:1px solid #e7e7e7;border-radius:16px;padding:16px}
 label{display:block;margin:10px 0 4px;font-weight:700}
 input{width:100%;padding:10px;border:1px solid #ddd;border-radius:10px}
 .row{display:flex;gap:10px;margin-top:12px}
 .btn{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#111;color:#fff;cursor:pointer}
 .btn2{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#fff;color:#111;cursor:pointer}
 .msg{margin-top:12px;padding:10px;border-radius:12px}
 .ok{background:#eaffea;border:1px solid #b8ffb8}
 .err{background:#ffe9ea;border:1px solid #ffb3b8}
</style></head><body><div class="wrap">
<h2>Déclarer un décès</h2>
<p><a href="{{ url_for('home') }}">← Retour</a></p>
<div class="card">
<form method="post">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <label>Phone du membre</label>
  <input name="phone" value="{{ phone_in or '' }}" required>
  <label>Date de décès (JJ/MM/AAAA)</label>
  <input name="date_deces" value="{{ date_in or '' }}" required>

  {% if found_name %}
    <div class="msg ok">Membre trouvé: <b>{{ found_name }}</b></div>
  {% elif phone_in %}
    <div class="msg err">Phone inconnu (membre non trouvé).</div>
  {% endif %}

  <div class="row">
    <button class="btn" name="action" value="check" type="submit">Vérifier</button>
    <button class="btn2" name="action" value="confirm" type="submit">Confirmer</button>
  </div>

  {% if message %}
    <div class="msg {{ 'err' if is_error else 'ok' }}">{{ message }}</div>
  {% endif %}
</form>
</div>
</div></body></html>
"""
# Endpoint3 Déclaration décès (menu card)
import uuid

@app.route("/deces", methods=["GET","POST"])
@login_required
def deces():
    message, is_error = "", False
    phone_in = (request.form.get("phone") or "").strip() if request.method == "POST" else ""
    date_in  = (request.form.get("date_deces") or "").strip() if request.method == "POST" else ""
    found_name = ""

    if request.method == "POST":
        m = fetch_member_by_phone(phone_in) if phone_in else None
        if m:
            found_name = f"{m[5]} {m[4]}"
        action = request.form.get("action")

        if action == "confirm":
            if not m:
                return render_template_string(DECES_PAGE, phone_in=phone_in, date_in=date_in,
                                              found_name="", message="Phone inconnu.", is_error=True)
            try:
                d = datetime.strptime(date_in, "%d/%m/%Y").date()
                ref = f"DC-{uuid.uuid4().hex[:10]}"
                create_deces(phone_in, d, declared_by=session["user"], reference=ref)
                message, is_error = "Décès enregistré.", False
                phone_in, date_in, found_name = "", "", ""
            except Exception as e:
                message, is_error = f"Erreur: {e}", True

    return render_template_string(DECES_PAGE, phone_in=phone_in, date_in=date_in,
                                  found_name=found_name, message=message, is_error=is_error)

#----------------------------------------------------------------------
# Endpoint #4 — Mentor application (membertype => uniquement 'mentor')
#----------------------------------------------------------------------
MENTOR_APP_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mentor application</title>
<style>
 body{font-family:Arial;margin:20px} .wrap{max-width:800px;margin:0 auto}
 .card{border:1px solid #e7e7e7;border-radius:16px;padding:16px}
 label{display:block;margin:10px 0 4px;font-weight:700}
 input,select{width:100%;padding:10px;border:1px solid #ddd;border-radius:10px}
 input[readonly]{background:#f6f6f6}
 .row{display:flex;gap:10px;margin-top:12px}
 .btn{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#111;color:#fff;cursor:pointer}
 .btn2{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#fff;color:#111;cursor:pointer}
 .msg{margin-top:12px;padding:10px;border-radius:12px}
 .ok{background:#eaffea;border:1px solid #b8ffb8}
 .err{background:#ffe9ea;border:1px solid #ffb3b8}
</style></head><body><div class="wrap">
<h2>Mentor application</h2>
<p><a href="{{ url_for('home') }}">← Retour</a></p>
<div class="card">
<form method="post">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <label>Phone</label><input value="{{ m[1] }}" readonly>
  <label>Nom</label><input value="{{ m[4] }}" readonly>
  <label>Prénom</label><input value="{{ m[5] }}" readonly>
  <label>Membertype (demande)</label>
  <select name="membertype" required>
    <option value="mentor">mentor</option>
  </select>
  <div class="row">
    <button class="btn" type="submit">Envoyer la demande</button>
    <a class="btn2" href="{{ url_for('home') }}">Annuler</a>
  </div>
  {% if message %}<div class="msg {{ 'err' if is_error else 'ok' }}">{{ message }}</div>{% endif %}
</form>
</div>
</div></body></html>
"""
# Endpoint4 Mentor application (menu card)
@app.route("/mentor-application", methods=["GET","POST"])
@login_required
def mentor_application():
    phone = session["user"]
    m = fetch_member_by_phone(phone)
    if request.method == "POST":
        # ici: on applique directement le changement (ou tu peux mettre "probatoire")
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE membres
                    SET membertype='mentor', updatedate=CURRENT_DATE, updateuser=%s
                    WHERE phone=%s
                """, (phone, phone))
            conn.commit()
        session["membertype"] = "mentor"
        return render_template_string(MENTOR_APP_PAGE, m=m, message="Votre compte est maintenant 'mentor'.", is_error=False)
    return render_template_string(MENTOR_APP_PAGE, m=m, message="", is_error=False)

#------------------------------------------------------------------
# Endpoint #5 — Mon groupe (mentor/admin uniquement, lecture seule)
#------------------------------------------------------------------
GROUPE_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mon groupe</title>
<style>
 body{font-family:Arial;margin:20px} .wrap{max-width:1100px;margin:0 auto}
 table{width:100%;border-collapse:collapse}
 th,td{padding:10px;border-bottom:1px solid #eee;text-align:left}
 th{background:#f6f6f6}
</style></head><body><div class="wrap">
<h2>Mon groupe</h2>
<p><a href="{{ url_for('home') }}">← Retour</a></p>
<table>
  <thead><tr><th>Phone</th><th>Prénom</th><th>Nom</th><th>Type</th><th>Statut</th><th>Solde</th></tr></thead>
  <tbody>
  {% for r in rows %}
    <tr><td>{{r[0]}}</td><td>{{r[1]}}</td><td>{{r[2]}}</td><td>{{r[3]}}</td><td>{{r[4]}}</td><td>{{r[5]}}</td></tr>
  {% endfor %}
  {% if not rows %}<tr><td colspan="6">Aucun membre rattaché.</td></tr>{% endif %}
  </tbody>
</table>
</div></body></html>
"""
# Endpoint5 Mon groupe (menu card)
@app.get("/groupe")
@mentor_required
def groupe():
    rows = list_groupe_for_mentor(session["user"])
    return render_template_string(GROUPE_PAGE, rows=rows)


# ------------------------------------------------------------------------------------
# Endpoint #6 — Créer un membre (mentor/statut/membertype/updatedate/updateuser auto)
# ------------------------------------------------------------------------------------
ADD_MEMBER_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Créer un membre</title>
<style>
 body{font-family:Arial;margin:20px} .wrap{max-width:900px;margin:0 auto}
 .card{border:1px solid #e7e7e7;border-radius:16px;padding:16px}
 label{display:block;margin:10px 0 4px;font-weight:700}
 input{width:100%;padding:10px;border:1px solid #ddd;border-radius:10px}
 .row{display:flex;gap:10px;margin-top:12px}
 .btn{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#111;color:#fff;cursor:pointer}
 .btn2{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#fff;color:#111;cursor:pointer}
 .msg{margin-top:12px;padding:10px;border-radius:12px}
 .ok{background:#eaffea;border:1px solid #b8ffb8}
 .err{background:#ffe9ea;border:1px solid #ffb3b8}
</style></head><body><div class="wrap">
<h2>Créer un membre</h2>
<p><a href="{{ url_for('home') }}">← Retour</a></p>
<div class="card">
<form method="post">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <label>Phone</label><input name="phone" required>
  <label>Nom</label><input name="lastname" required>
  <label>Prénom</label><input name="firstname" required>
  <label>Date naissance (JJ/MM/AAAA)</label><input name="birthdate" required>
  <label>IdType</label><input name="idtype" required>
  <label>Mot de passe</label><input name="password" type="password" required>

  <div class="row">
    <button class="btn" type="submit">Créer</button>
    <a class="btn2" href="{{ url_for('home') }}">Annuler</a>
  </div>
  {% if message %}<div class="msg {{ 'err' if is_error else 'ok' }}">{{ message }}</div>{% endif %}
</form>
</div></div></body></html>
"""
# Endpoint6 Créer un membre (menu card)
@app.route("/addmember", methods=["GET","POST"])
@mentor_required
def add_member():
    if request.method == "POST":
        try:
            phone = (request.form.get("phone") or "").strip()
            lastname = (request.form.get("lastname") or "").strip()
            firstname = (request.form.get("firstname") or "").strip()
            birthdate = datetime.strptime((request.form.get("birthdate") or "").strip(), "%d/%m/%Y").date()
            idtype = (request.form.get("idtype") or "").strip()
            password = (request.form.get("password") or "").strip()

            mentor = session["user"]
            membertype = "membre"
            statut = "probatoire"
            updateuser = session["user"]

            insert_member(phone, membertype, mentor, lastname, firstname, birthdate, idtype, None, statut, updateuser, password)
            return render_template_string(ADD_MEMBER_PAGE, message="Membre créé.", is_error=False)
        except psycopg.errors.UniqueViolation:
            return render_template_string(ADD_MEMBER_PAGE, message="Ce phone existe déjà.", is_error=True)
        except Exception as e:
            return render_template_string(ADD_MEMBER_PAGE, message=f"Erreur: {e}", is_error=True)

    return render_template_string(ADD_MEMBER_PAGE, message="", is_error=False)

#----------------------------------------------------------------------------
# Endpoint #7 — Importer cotisations (admin) : exécuter import_mouvements.py
#----------------------------------------------------------------------------
IMPORT_PAGE = """
<!doctype html><html><head><meta charset="utf-8">
<title>Import Mouvements</title>
<style>
 body{font-family:Arial;margin:20px} .wrap{max-width:900px;margin:0 auto}
 .card{border:1px solid #e7e7e7;border-radius:16px;padding:16px}
 .btn{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#111;color:#fff;cursor:pointer}
 .msg{margin-top:12px;padding:10px;border-radius:12px}
 .ok{background:#eaffea;border:1px solid #b8ffb8}
 .err{background:#ffe9ea;border:1px solid #ffb3b8}
</style></head><body><div class="wrap">
<h2>Import mouvements (CSV)</h2>
<p><a href="{{ url_for('home') }}">← Retour</a></p>

<div class="card">
  <form method="post" enctype="multipart/form-data">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <label>Fichier CSV (champ: <b>mobilemoneyfile</b>)</label><br>
    <input type="file" name="mobilemoneyfile" accept=".csv" required><br><br>
    <button class="btn" type="submit">Importer</button>
  </form>

  {% if message %}
    <div class="msg {{ 'err' if is_error else 'ok' }}">{{ message }}</div>
  {% endif %}

  {% if stats %}
    <pre style="white-space:pre-wrap; margin-top:10px;">{{ stats }}</pre>
  {% endif %}
</div>
</div></body></html>
"""
# Endpoint7 Importer cotisations (menu card)
import re
from datetime import date

FR_MONTHS = {
    "janv": 1, "jan": 1,
    "fevr": 2, "févr": 2, "fev": 2, "fév": 2,
    "mars": 3,
    "avr": 4, "avril": 4,
    "mai": 5,
    "juin": 6,
    "juil": 7, "juillet": 7,
    "aout": 8, "août": 8,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12, "déc": 12,
}

def parse_date_fr(s: str) -> date:
    # ex: "2-oct.-25" / "27-janv.-25"
    s = (s or "").strip().lower()
    s = s.replace(".", "")  # "oct." -> "oct", "janv." -> "janv"
    m = re.match(r"^(\d{1,2})-([a-zéûôîàç]+)-(\d{2,4})$", s)
    if not m:
        raise ValueError(f"Date invalide: {s!r}")
    d = int(m.group(1))
    mon_txt = m.group(2)
    y = int(m.group(3))
    if y < 100:
        y += 2000
    if mon_txt not in FR_MONTHS:
        raise ValueError(f"Mois FR inconnu: {mon_txt!r}")
    return date(y, FR_MONTHS[mon_txt], d)



from io import StringIO
import csv

@app.route("/import-mouvements", methods=["GET", "POST"])
@admin_required
def import_mouvements():
    if request.method == "GET":
        return render_template_string(IMPORT_PAGE, message="", is_error=False, stats="")

    # POST
    f = request.files.get("mobilemoneyfile")
    if not f or not f.filename:
        return render_template_string(IMPORT_PAGE, message="Aucun fichier reçu.", is_error=True, stats="")

    try:
        content = f.read().decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(StringIO(content))

        inserted = 0
        updated_balances = 0
        flagged_inactif = 0
        skipped = 0

        with get_conn() as conn:
            with conn.cursor() as cur:
                for row in reader:
                    log.info("contenu de 'row' dans le reader=%s", row)                   
                    try:
                        phone = (row.get("phone") or "").strip()
                        firstname = (row.get("firstname") or "").strip()
                        lastname = (row.get("lastname") or "").strip()
                        debitcredit = (row.get("debitcredit") or "").strip().upper()  # 'D' / 'C'
                        reference = (row.get("reference") or "").strip()

                        #amount = float((row.get("amount") or "0").strip())
                        amount_raw = (row.get("amount") or "0").strip().replace(",", ".")
                        amount = float(amount_raw)

                        # TODO: parse date selon votre format (mvt_date)
                        #mvt_date = row.get("mvt_date")  # à parser si nécessaire
                        mvt_date = parse_date_fr(row.get("date") or "")
                        #mouvem_date = datetime.strptime(mvt_date, "%d/%m/%Y").date()
                        #mvt_date=mouvem_date
                        libelle = (row.get("reference") or "").strip()
                        updatedate=date.today()

                        log.info("contenu de 'amount' formaté=%s", amount)  
                        log.info("contenu de 'mvt_date' formaté=%s", mvt_date)  
                        log.info("contenu de 'updatedate' formaté=%s", updatedate)  

                        if not phone or debitcredit not in ("D", "C"):
                            skipped += 1
                            log.warning("Ligne ignorée (phone ou debitcredit invalide): %s", phone)
                            continue

                        # 1) insert mouvement
                        cur.execute("""
                          INSERT INTO mouvements (phone, firstname, lastname, mvt_date, amount, debitcredit,reference,updatedate,libelle,updated_by)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """, (phone, firstname, lastname, mvt_date, amount, debitcredit, reference,date.today(),libelle,"system"))
                        log.info("Mouvement inséré pour phone=%s, amount=%s, debitcredit=%s, reference=%s", phone, amount, debitcredit, reference)
                        inserted += 1

                        # 2) update balance membre
                        delta = -amount if debitcredit == "D" else amount
                        cur.execute("""
                          UPDATE membres
                          SET balance = balance + %s,
                              updatedate = CURRENT_DATE,
                              updateuser = %s
                          WHERE phone = %s
                        """, (delta, session.get("user"), phone))

                        if cur.rowcount:
                            updated_balances += 1

                        # 3) règle demandée: si balance < 0 alors currentstatute="inactif", s'il etait 'actif' (=> s'il etait 'actif' on le passe à 'inactif', pas pour 'suspendu' et 'radié')
                        cur.execute("""
                          UPDATE membres
                          SET currentstatute = 'inactif',
                              updatedate = CURRENT_DATE,
                              updateuser = %s
                          WHERE phone = %s AND balance <  0 AND currentstatute = 'actif'
                        """, (session.get("user"), phone))
                        if cur.rowcount:
                            flagged_inactif += 1

                    except Exception:
                        skipped += 1
                        log.exception("Ligne ignorée - Erreur traitement ligne: %s", row)

            conn.commit()

        stats = (
            f"Import terminé.\n"
            f"- Mouvements insérés: {inserted}\n"
            f"- Balances mises à jour: {updated_balances}\n"
            f"- Membres passés inactif (balance<0): {flagged_inactif}\n"
            f"- Lignes ignorées: {skipped}\n"
        )

        return render_template_string(IMPORT_PAGE, message="Import OK.", is_error=False, stats=stats)

    except Exception as e:
        log.exception("Erreur import: %s", e)
        log.info("Ligne concernée : %s", row if 'row' in locals() else "Aucune ligne")
        conn.rollback()
        return render_template_string(IMPORT_PAGE, message=f"Erreur import: {e}", is_error=True, stats="")

#------------------------------------------------------------------------------
# Endpoint #8 — Check mouvements (admin) : afficher toute la table 'mouvements'
#------------------------------------------------------------------------------
CHECK_MVT_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Check mouvements</title>
<style>
 body{font-family:Arial;margin:20px} .wrap{max-width:1200px;margin:0 auto}
 table{width:100%;border-collapse:collapse}
 th,td{padding:10px;border-bottom:1px solid #eee;text-align:left}
 th{background:#f6f6f6}
 input,select{padding:8px;border:1px solid #ddd;border-radius:10px}
 .btn{padding:7px 10px;border:1px solid #111;border-radius:10px;background:#111;color:#fff;cursor:pointer}
 .btn2{padding:7px 10px;border:1px solid #111;border-radius:10px;background:#fff;color:#111;cursor:pointer}
</style></head><body><div class="wrap">
<h2>Check mouvements (admin)</h2>
<p><a href="{{ url_for('home') }}">← Retour</a></p>
<table>
<thead><tr><th>ID</th><th>Phone</th><th>Firstname</th><th>Date</th><th>Amount</th><th>D/C</th><th>Reference</th><th>Action</th></tr></thead>
<tbody>
{% for r in rows %}
<tr>
<form method="post" action="{{ url_for('check_mouvements_update', mvt_id=r[0]) }}">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <td>{{ r[0] }}</td>
  <td>{{ r[1] }}</td>
  <td>{{ r[2] }}</td>
  <td><input name="mvt_date" value="{{ r[3].strftime('%d/%m/%Y') }}" size="10"></td>
  <td><input name="amount" value="{{ r[4] }}" size="8"></td>
  <td>
    <select name="debitcredit">
      <option value="D" {{ 'selected' if r[5]=='D' else '' }}>D</option>
      <option value="C" {{ 'selected' if r[5]=='C' else '' }}>C</option>
    </select>
  </td>
  <td><input name="reference" value="{{ r[6] }}" size="16"></td>
  <td>
    <button class="btn" type="submit">Save</button>
</form>
<form method="post" action="{{ url_for('check_mouvements_delete', mvt_id=r[0]) }}" style="display:inline">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <button class="btn2" type="submit" onclick="return confirm('Supprimer?')">Delete</button>
</form>
  </td>
</tr>
{% endfor %}
{% if not rows %}<tr><td colspan="8">Aucun mouvement.</td></tr>{% endif %}
</tbody>
</table>
</div></body></html>
"""

@app.get("/checkmouvements")
@admin_required
def check_mouvements():
    rows = list_all_mouvements()
    return render_template_string(CHECK_MVT_PAGE, rows=rows)

@app.post("/checkmouvements/update/<int:mvt_id>")
@admin_required
def check_mouvements_update(mvt_id: int):
    d = datetime.strptime((request.form.get("mvt_date") or "").strip(), "%d/%m/%Y").date()
    amount = float((request.form.get("amount") or "0").strip())
    dc = (request.form.get("debitcredit") or "D").strip()
    ref = (request.form.get("reference") or "").strip()
    libelle = (request.form.get("libelle") or ref).strip()  # ou tu peux ajouter un champ libellé dans le form si tu veux
    update_mouvement(mvt_id, d, amount, dc, ref,libelle)
    return redirect(url_for("check_mouvements"))

@app.post("/checkmouvements/delete/<int:mvt_id>")
@admin_required
def check_mouvements_delete(mvt_id: int):
    delete_mouvement(mvt_id)
    return redirect(url_for("check_mouvements"))

#----------------------------------------------------------------------------------------------
# Endpoint #9 — Data general follow-up (admin) : CRUD sur membres (sauf updatedate/updateuser)
#----------------------------------------------------------------------------------------------
DATAGENERALFOLLOWUP_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>les membres</title>
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
  <h2>KM-Kimya  Les Membres</h2>
  <p class="muted"><small>Interface d'administration des membres. Mentor et admin peuvent créer des membres, mais seuls les admins peuvent voir cette page.</small></p>
  <p><a href="{{ url_for('home') }}">← Retour</a></p>
  

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
          <label>Solde </label>
          <input name="balance" value="{{ edit_balance }}" required>
        </div>

        <div>
          <label>Date adhésion (JJ/MM/AAAA)</label>
          <input name="membershipdate" value="{{ edit_membershipdate }}" required>
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
        <button class="btn" type="submit">Enregistrer</button>
        <a class="btn secondary" href="{{ url_for('home') }}" style="display:inline-flex;align-items:center;justify-content:center;">Annuler</a>
      </div>
    </form>
  </div>
  {% endif %}

  <div class="card">
    <h2 style="margin-top:0;">Liste des membres</h2>
    <table>
      <thead>
        <tr>
          <th style="width:70px;">ID</th>
          <th>Phone</th>
          <th>Type</th>
          <th>Mentor</th>
          <th>Lastname</th>
          <th>Firstname</th>
          <th>Statut</th>
          <th>Update</th>
          <th>Update by</th>
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
          <td>{{ r[9] }}</td>
          <td>{{ r[11].strftime('%d/%m/%Y') }}</td>
          <td>{{ r[12] }}</td>
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

# Endpoint9 Data general follow-up (menu card)
@app.get("/datageneralfollowup")
@admin_required
def datageneralfollowup():
#   ## Réutilise ton écran existant "Liste des membres + Edit/Delete"
#   ## (le code que tu as déjà, c’est ici que ça vit)
    rows = fetch_all_membres()
    return render_template_string(DATAGENERALFOLLOWUP_PAGE, rows=rows, edit_row=None, edit_birthdate="",edit_membershipdate="", edit_balance=0.0,
                                  message="", is_error=False, member_types=MEMBER_TYPES, statutes=STATUTES)

@app.get("/edit/<int:member_id>")
@login_required
def edit(member_id: int):
    row = fetch_one(member_id)
    rows = fetch_all_membres()
    phone = session.get("user")
    prof = get_user_profile_by_phone(phone) if phone else None
    firstname, lastname, membertype = (prof or ("", "", "membre"))
    if not row:
        return render_template_string(
            DATAGENERALFOLLOWUP_PAGE,
            rows=rows,
            edit_row=None,
            edit_birthdate="",
            edit_membershipdate="",
            edit_balance=0.0,
            #message=f"Member ID {member_id} introuvable.",
            #is_error=True,
            message="",
            is_error=False,
            ##
            member_types=MEMBER_TYPES,
            statutes=STATUTES,
            #
            user_fullname=f"{firstname} {lastname}".strip(),
            user_membertype=membertype,
            #
        )

    edit_birthdate = row[6].strftime("%d/%m/%Y") if row[6] else ""
    edit_membershipdate = row[14].strftime("%d/%m/%Y")
    edit_balance = float(row[10]) if row[10] is not None else 0.0

    
    return render_template_string(
        DATAGENERALFOLLOWUP_PAGE,
        rows=rows,
        edit_row=row,
        edit_birthdate=edit_birthdate,
        edit_membershipdate=edit_membershipdate,
        edit_balance=edit_balance,
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
            birthdate=data["birthdate"],
            membershipdate=data["membershipdate"],
            balance=data["balance"],
            currentstatute=data["currentstatute"],
            updateuser=updateuser,
            new_password_plain=new_pwd,            
        )

        log.info("FORM=%s", request.form.to_dict())

        return redirect(url_for("datageneralfollowup"))

    except Exception as e:
        rows = fetch_all_membres()
        row = fetch_one(member_id)
        edit_birthdate = row[6].strftime("%d/%m/%Y") if row else ""
        return render_template_string(
            DATAGENERALFOLLOWUP_PAGE,
            rows=rows,
            edit_row=row,
            edit_birthdate=edit_birthdate,
            #edit_membershipdate=row[14].strftime("%d/%m/%Y") if row else "",
            edit_balance=float(row[10]) if row and row[10] is not None else 0.0,
            #edit_balance = str(row[10]) if row else "",
            edit_membershipdate = row[14].strftime("%d/%m/%Y") if row and row[14] else "",
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


# --------------------------------------------------------------------------------------
# Endpoint #10 — Transfert de cotisations (débit/crédit + blocage si solde insuffisant)
#---------------------------------------------------------------------------------------
TRANSFER_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transfert cotisations</title>
<style>
 body{font-family:Arial;margin:20px} .wrap{max-width:800px;margin:0 auto}
 .card{border:1px solid #e7e7e7;border-radius:16px;padding:16px}
 label{display:block;margin:10px 0 4px;font-weight:700}
 input{width:100%;padding:10px;border:1px solid #ddd;border-radius:10px}
 .row{display:flex;gap:10px;margin-top:12px}
 .btn{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#111;color:#fff;cursor:pointer}
 .btn2{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#fff;color:#111;cursor:pointer}
 .msg{margin-top:12px;padding:10px;border-radius:12px}
 .ok{background:#eaffea;border:1px solid #b8ffb8}
 .err{background:#ffe9ea;border:1px solid #ffb3b8}
</style></head><body><div class="wrap">
<h2>Transfert de cotisations</h2>
<p><a href="{{ url_for('home') }}">← Retour</a></p>

<div class="card">
  <p>Solde actuel: <b>{{ balance }}</b></p>
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <label>Phone du bénéficiaire</label>
    <input name="to_phone" required>
    <label>Montant à transférer</label>
    <input name="amount" required>
    <div class="row">
      <button class="btn" type="submit">Transférer</button>
      <a class="btn2" href="{{ url_for('home') }}">Annuler</a>
    </div>
    {% if message %}<div class="msg {{ 'err' if is_error else 'ok' }}">{{ message }}</div>{% endif %}
  </form>
</div>
</div></body></html>
"""
#(10) Endpoint10 Transfert de cotisations (menu card)
@app.route("/transfer", methods=["GET","POST"])
@login_required
def transfer():
    from_phone = session["user"]
    me = fetch_member_by_phone(from_phone)
    my_balance = me[10] if me else 0

    if request.method == "POST":
        to_phone = (request.form.get("to_phone") or "").strip()
        amount = float((request.form.get("amount") or "0").strip())

        if amount <= 0:
            return render_template_string(TRANSFER_PAGE, balance=my_balance, message="Montant invalide.", is_error=True)

        to_member = fetch_member_by_phone(to_phone)
        if not to_member:
            return render_template_string(TRANSFER_PAGE, balance=my_balance, message="Bénéficiaire introuvable.", is_error=True)

        if my_balance < amount:
            return render_template_string(TRANSFER_PAGE, balance=my_balance, message="Solde insuffisant: transfert bloqué.", is_error=True)

        # transaction atomique
        try:
            #ref_base = f"TR-{uuid.uuid4().hex[:10]}"
            ref_base = f"TR-{session['user']}"
            today = datetime.utcnow().date()

            with get_conn() as conn:
                with conn.cursor() as cur:
                    # 1) insert mouvement DEBIT (from)
                    cur.execute("""
                        INSERT INTO mouvements (phone, firstname,lastname, mvt_date, amount, debitcredit, reference)
                        VALUES (%s,%s,%s,%s,%s,'D',%s)
                    """, (from_phone, me[5], me[4], today, amount, ref_base + "-D"))

                    # 2) insert mouvement CREDIT (to)
                    cur.execute("""
                        INSERT INTO mouvements (phone, firstname,lastname, mvt_date, amount, debitcredit, reference)
                        VALUES (%s,%s,%s,%s,%s,'C',%s)
                    """, (to_phone, to_member[5], to_member[6], today, amount, ref_base + "-C"))

                    # 3) update balances
                    cur.execute("UPDATE membres SET balance = balance - %s, updatedate=CURRENT_DATE, updateuser=%s WHERE phone=%s",
                                (amount, from_phone, from_phone))
                    cur.execute("UPDATE membres SET balance = balance + %s, updatedate=CURRENT_DATE, updateuser=%s WHERE phone=%s",
                                (amount, from_phone, to_phone))

                conn.commit()

            # refresh
            me2 = fetch_member_by_phone(from_phone)
            return render_template_string(TRANSFER_PAGE, balance=(me2[10] if me2 else 0),
                                          message="Transfert effectué avec succès.", is_error=False)
        except Exception as e:
            log.exception("Erreur transfert")
            return render_template_string(TRANSFER_PAGE, balance=my_balance, message=f"Erreur: {e}", is_error=True)

    return render_template_string(TRANSFER_PAGE, balance=my_balance, message="", is_error=False)


if __name__ == "__main__":
    # Local uniquement. En prod Render, gunicorn gère le port.
    app.run(host="0.0.0.0", port=5000, debug=True)