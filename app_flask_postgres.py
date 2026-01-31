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


def init_db():
    """Crée la table membres + admin par défaut si absent."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS membres (
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
                  balance        DECIMAL(14,2) NOT NULL DEFAULT 0,      
                  membershipdate DATE NOT NULL DEFAULT CURRENT_DATE,                        
                  CONSTRAINT membres_membertype_chk
                    CHECK (membertype IN ('membre','independant','mentor','admin')),
                  CONSTRAINT membres_currentstatute_chk
                    CHECK (currentstatute IN ('probatoire','actif','inactif','suspendu','radié'))
                );
            """)  
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS membres_phone_uq ON membres(phone);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_membres_phone ON membres(phone);")

            # Admin par défaut si absent
            cur.execute("SELECT 1 FROM membres WHERE phone = %s", (ADMIN_PHONE,))
            if cur.fetchone() is None:
                log.info("Admin absent -> création du compte admin par défaut")
                cur.execute("""
                    INSERT INTO membres
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
###
            cur.execute("""
                CREATE TABLE IF NOT EXISTS deces (
                  id             BIGSERIAL PRIMARY KEY,
                  defunt         TEXT NOT NULL,
                  annonceur      TEXT NOT NULL,
                  date_deces     TEXT NOT NULL,
		              date_annonce	 DATE NOT NULL DEFAULT CURRENT_DATE,
                  lastname       TEXT NOT NULL,
                  firstname      TEXT NOT NULL,
                  birthdate      DATE NOT NULL,
                  document_url   TEXT,
		              commentaire    TEXT NOT NULL, 
		              confirmation   TEXT NOT NULL,
                  montant		     DECIMAL(14,2) NOT NULL DEFAULT 0,
                  membershipdate DATE NOT NULL DEFAULT CURRENT_DATE,                        
                  updatedate     DATE NOT NULL DEFAULT CURRENT_DATE,
                  updateuser     TEXT NOT NULL,
                  CONSTRAINT deces_confirmation_check
                    CHECK (confirmation IN ('attente','refusé','confirmé'))			
                );        
            """)  
### 
        conn.commit()

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


# ✅ IMPORTANT : exécuté aussi sous gunicorn (Render)
try:
    init_db()
except Exception:
    log.exception("init_db() a échoué au démarrage")
    # on laisse continuer pour que les logs apparaissent, mais l'app sera probablement inutilisable


#> Jan 30 2026
#from functools import wraps
#from flask import session, redirect, url_for, request
#def login_required(view):
#    @wraps(view)
#    def wrapped_view(*args, **kwargs):
#        if "user" not in session:
#            # mémorise la page demandée (optionnel mais propre)
#            return redirect(url_for("login", next=request.path))
#        return view(*args, **kwargs)
#    return wrapped_view
#< Jan 30 2026

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

SELECT_membres = """
    SELECT id, phone, membertype, mentor, lastname, firstname, birthdate,
           idtype, idpicture_url, currentstatute, updatedate, updateuser
    FROM membres
"""


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

#####> 30 Jan 2026
# Ajoute ce helper (Python) et passe menu_zone1/2/3 au template PAGE

def get_menu_for_user():
    role = get_current_user_type()  # "membre"/"independant"/"mentor"/"admin"
    zone1 = [
        ("Profil", "profil"),
        ("Mouvements", "mouvements"),
        ("Décès", "deces"),
        ("Mentor application", "mentorapplication"),
    ]
    zone2 = [
        ("Groupe", "groupe"),
        ("Ajouter membre", "addmember"),
    ] if role in ("mentor", "admin") else []
    zone3 = [
        ("Import", "import_mouvements"),
        ("Check mouvements", "checkmouvements"),
        ("Data general follow-up", "datageneralfollowup"),
    ] if role == "admin" else []
    return zone1, zone2, zone3

# Dans home():
# menu_zone1, menu_zone2, menu_zone3 = get_menu_for_user()
# ... render_template_string(PAGE, ..., menu_zone1=menu_zone1, menu_zone2=menu_zone2, menu_zone3=menu_zone3)


# ----------------------------
# RBAC (Access control)
# ----------------------------
def get_current_user_type() -> str | None:
    """Retourne le membertype du user connecté (ou None)."""
    phone = session.get("user")
    if not phone:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT membertype FROM membres WHERE phone = %s", (phone,))
            row = cur.fetchone()
            return row[0] if row else None


def role_required(*allowed_roles):
    """Decorator d'accès selon membertype."""
    def deco(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("user"):
                return redirect(url_for("login"))
            role = get_current_user_type()
            if role not in allowed_roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return deco


# Helpers de zones
member_access = role_required("membre", "independant", "mentor", "admin")
mentor_access = role_required("mentor", "admin")
admin_access  = role_required("admin")


# ----------------------------
# Endpoints (9)
# ----------------------------

# Zone 1 (tous)
@app.get("/profil")
@login_required
@member_access
def profil():
    return "TODO: profil"

@app.get("/mouvements")
@login_required
@member_access
def mouvements():
    return "TODO: mouvements"

@app.get("/deces")
@login_required
@member_access
def deces():
    return "TODO: deces"

@app.get("/mentorapplication")
@login_required
@member_access
def mentorapplication():
    return "TODO: mentorapplication"


# Zone 2 (mentor + admin)
@app.get("/groupe")
@login_required
@mentor_access
def groupe():
    return "TODO: groupe"

@app.route("/addmember", methods=["GET", "POST"])
@login_required
@mentor_access
def addmember():
    return "TODO: addmember"


# Zone 3 (admin uniquement)
@app.route("/import", methods=["GET", "POST"])
@login_required
@admin_access
def import_mouvements():
    return "TODO: import"

@app.get("/checkmouvements")
@login_required
@admin_access
def checkmouvements():
    return "TODO: checkmouvements"

@app.get("/datageneralfollowup")
@login_required
@admin_access
def datageneralfollowup():
    return "TODO: datageneralfollowup"

#####< 30 Jan 2026

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
  <title>membres (Flask + PostgreSQL)</title>
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
  <style>
  :root{
    --bg:#ffffff; --text:#111; --muted:#666; --card:#fff;
    --border:#e8e8e8; --shadow:0 6px 20px rgba(0,0,0,.06);
    --brand:#111; --brand2:#0b57d0;
  }
  body{ margin:0; font-family:Arial, sans-serif; background:#fafafa; color:var(--text); }
  .wrap{ max-width:1150px; margin:0 auto; padding:18px; }

  /* Topbar */
  .topbar{
    position:sticky; top:0; z-index:50;
    background:rgba(250,250,250,.9); backdrop-filter: blur(10px);
    border-bottom:1px solid var(--border);
  }
  .topbar-inner{
    max-width:1150px; margin:0 auto; padding:12px 18px;
    display:flex; align-items:center; gap:12px; justify-content:space-between;
  }
  .brand{ display:flex; align-items:center; gap:10px; }
  .logo{
    width:36px; height:36px; border-radius:10px;
    background:var(--brand); color:#fff;
    display:flex; align-items:center; justify-content:center;
    font-weight:700;
  }
  .brand h1{ margin:0; font-size:16px; letter-spacing:.2px; }
  .userbox{ text-align:right; }
  .userline{ font-size:13px; color:var(--muted); }
  .userline b{ color:var(--text); }
  .role-pill{
    display:inline-flex; align-items:center; gap:6px;
    font-size:12px; padding:4px 10px; border-radius:999px;
    border:1px solid var(--border); background:#fff;
  }
  .logout{
    display:inline-flex; align-items:center; justify-content:center;
    padding:8px 12px; border-radius:10px;
    border:1px solid var(--border);
    background:#fff; color:var(--text); text-decoration:none;
    font-size:13px;
  }

  /* Menu grid */
  .menu{
    margin-top:16px;
    display:grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap:12px;
  }
  .menu-card{
    background:var(--card);
    border:1px solid var(--border);
    border-radius:14px;
    padding:14px;
    box-shadow: var(--shadow);
    text-decoration:none;
    color:var(--text);
    transition: transform .08s ease, border-color .08s ease;
    display:flex; gap:12px; align-items:flex-start;
  }
  .menu-card:hover{ transform: translateY(-1px); border-color:#d9d9d9; }
  .icon{
    width:38px; height:38px; border-radius:12px;
    border:1px solid var(--border);
    display:flex; align-items:center; justify-content:center;
    font-weight:700; color:var(--brand);
    background:#fff;
    flex:0 0 auto;
  }
  .menu-title{ margin:0; font-size:14px; font-weight:700; }
  .menu-desc{ margin:4px 0 0; font-size:12px; color:var(--muted); line-height:1.35; }

  /* Responsive */
  @media (max-width: 980px){ .menu{ grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  @media (max-width: 640px){
    .menu{ grid-template-columns: 1fr; }
    .topbar-inner{ flex-direction:column; align-items:flex-start; gap:8px; }
    .userbox{ text-align:left; width:100%; display:flex; align-items:center; justify-content:space-between; gap:10px; }
  }
</style>

<div class="topbar">
  <div class="topbar-inner">
    <div class="brand">
      <div class="logo">KM</div>
      <div>
        <h1>KM-Kimya</h1>
        <div class="userline">
          Utilisateur connecté <b>{{ session.get('user') }}</b>
          {% if user_fullname %} — <b>{{ user_fullname }}</b>{% endif %}
        </div>
    <div class="userbox">
      <span class="role-pill">Rôle: <b>{{ user_membertype }}</b></span>
      <a class="logout" href="{{ url_for('logout') }}">Logout</a>
    </div>
    
        <ajout du Jan 30 2026 --- !-- 2) Colle ce bloc dans PAGE (juste après le titre H1 par ex.) -->

        <style>
          .nav { display:flex; flex-wrap:wrap; gap:10px; margin:14px 0 18px; }
          .nav .zone { border:1px solid #ddd; border-radius:12px; padding:10px; min-width:240px; flex:1; }
          .nav h3 { margin:0 0 8px; font-size:1.02em; }
          .nav a { display:block; padding:10px 12px; border-radius:10px; border:1px solid #eee; margin:6px 0; }
          .nav a:hover { background:#f6f6f6; text-decoration:none; }
          .nav small { color:#666; }
        </style>

        <div class="nav">
          <div class="zone">
            <h3>Zone 1 <small>(membres)</small></h3>
            {% for label, endpoint in menu_zone1 %}
              <a href="{{ url_for(endpoint) }}">{{ label }}</a>
            {% endfor %}
          </div>

          {% if menu_zone2 %}
          <div class="zone">
            <h3>Zone 2 <small>(mentor)</small></h3>
            {% for label, endpoint in menu_zone2 %}
              <a href="{{ url_for(endpoint) }}">{{ label }}</a>
            {% endfor %}
          </div>
          {% endif %}

          {% if menu_zone3 %}
          <div class="zone">
            <h3>Zone 3 <small>(admin)</small></h3>
            {% for label, endpoint in menu_zone3 %}
              <a href="{{ url_for(endpoint) }}">{{ label }}</a>
            {% endfor %}
          </div>
          {% endif %}
        </div>
        < fin de l'ajout du Jan 30 2026 -->

      </div>
    </div>

  </div>
</div>

<!-- MENU -->
<div class="menu">
  <!-- Zone 1: Tous -->
  <a class="menu-card" href="{{ url_for('home') }}">
    <div class="icon">📄</div>
    <div>
      <p class="menu-title">Mon compte</p>
      <p class="menu-desc">Profil, informations et statut.</p>
    </div>
  </a>

  <a class="menu-card" href="{{ url_for('home') }}#mouvements">
    <div class="icon">💳</div>
    <div>
      <p class="menu-title">Mes mouvements</p>
      <p class="menu-desc">Historique des cotisations et solde.</p>
    </div>
  </a>

  <!-- Zone 2: mentor + admin -->
  {% if user_membertype in ('mentor','admin') %}
  <a class="menu-card" href="{{ url_for('home') }}#groupe">
    <div class="icon">👥</div>
    <div>
      <p class="menu-title">Mon groupe</p>
      <p class="menu-desc">Membres rattachés + soldes.</p>
    </div>
  </a>

  <a class="menu-card" href="{{ url_for('home') }}#addmember">
    <div class="icon">➕</div>
    <div>
      <p class="menu-title">Créer un membre</p>
      <p class="menu-desc">Enregistrer un nouveau membre.</p>
    </div>
  </a>

  <a class="menu-card" href="{{ url_for('home') }}#deces">
    <div class="icon">🕊️</div>
    <div>
      <p class="menu-title">Déclarer un décès</p>
      <p class="menu-desc">Enregistrer un cas de décès.</p>
    </div>
  </a>
  {% endif %}

  <!-- Zone 3: admin uniquement -->
  {% if user_membertype == 'admin' %}
  <a class="menu-card" href="{{ url_for('home') }}#import">
    <div class="icon">⬇️</div>
    <div>
      <p class="menu-title">Importer cotisations</p>
      <p class="menu-desc">Lancer import_mouvements.py (test).</p>
    </div>
  </a>

  <a class="menu-card" href="{{ url_for('home') }}#admin">
    <div class="icon">🛠️</div>
    <div>
      <p class="menu-title">Administration</p>
      <p class="menu-desc">Suivi global & contrôle.</p>
    </div>
  </a>
  {% endif %}
</div>

  <h1>KM Membres</h1>

  <p class="muted">
    Utilisateur connecté <b>{{ logged_user_label }}</b> —
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
            <option value="membre">membre</option>
            <option value="independant">independant</option>
            <option value="mentor">mentor</option>
            <option value="admin">admin</option>
          </select>
        </div>

        <div>
          <label>Mentor</label>
          <input name="mentor" placeholder="rempli automatiquement par le nº d'utilisateur connecté" value="{{ session.get('user') }}" readonly>
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
            <option value="probatoire">probatoire</option>
            <option value="inactif">inactif</option>
            <option value="actif">actif</option>
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
        Notes: phone est unique. password sera stocké hashé. updatedate/updateuser/mentor sont auto.
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

    if verify_user(phone, password):
        log.info("Login attempt: LA SESSION DEMARRE OK")
        session["user"] = phone
        session.permanent = True
        return redirect(url_for("home"))

    return render_template_string(LOGIN_PAGE, message="Identifiants invalides ou membre suspendu/radié.")


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def get_user_profile_by_phone(phone: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT firstname, lastname, membertype
                FROM membres
                WHERE phone = %s
            """, (phone,))
            return cur.fetchone()

@app.get("/")
@login_required
def home():
    rows = fetch_all_membres()
    phone = session.get("user")
    prof = get_user_profile_by_phone(phone) if phone else None
    firstname, lastname, membertype = (prof or ("", "", "membre"))
#    
    menu_zone1, menu_zone2, menu_zone3 = get_menu_for_user()
#
    return render_template_string(
        PAGE,
        rows=rows,
        edit_row=None,
        edit_birthdate="",
        message="",
        is_error=False,
        member_types=MEMBER_TYPES,
        statutes=STATUTES,
        user_fullname=f"{firstname} {lastname}".strip(),
        user_membertype=membertype,
        menu_zone1=menu_zone1, 
        menu_zone2=menu_zone2, 
        menu_zone3=menu_zone3
    )

@app.post("/add")
@login_required
def add():
    try:
        data = validate_member_form(request.form, for_update=False)
        updateuser = session.get("user") or ADMIN_PHONE
        mentor = session.get("user") or ADMIN_PHONE

        insert_member(
            phone=data["phone"],
            membertype=data["membertype"],
            mentor=mentor,
            #mentor=data["mentor"],
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
        rows = fetch_all_membres()
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
        rows = fetch_all_membres()
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
    rows = fetch_all_membres()
    phone = session.get("user")
    prof = get_user_profile_by_phone(phone) if phone else None
    firstname, lastname, membertype = (prof or ("", "", "membre"))
    if not row:
        return render_template_string(
            PAGE,
            rows=rows,
            edit_row=None,
            edit_birthdate="",
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
        rows = fetch_all_membres()
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
