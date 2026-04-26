# ---------------------------------
# External functions impotation
# ---------------------------------
from __future__ import annotations

from decimal import Decimal
import code
from email import message
from enum import member
from enum import member
import os
import logging
from datetime import datetime, timedelta
from functools import wraps
import select
from weakref import ref

import psycopg
from psycopg import rows
from psycopg.rows import tuple_row

from flask import Flask, flash, request, redirect, url_for, render_template_string, session, abort
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
# valeur par défaut pour les membres créés via create_member_minimal() (ex: import depuis Excel)
DEFAULT_PASSWORD_HASH = os.getenv("DEFAULT_PASSWORD_HASH", "123456789")  # à utiliser si tu ne veux pas créer de comptes login automatiques pour les membres créés via create_member_minimal() (ex: import depuis Excel)
# pour create_member_minimal(), si tu ne veux pas créer de comptes login automatiques, tu peux laisser DEFAULT_PASSWORD_HASH vide et la fonction mettra une chaîne fixe "NO_LOGIN_CREATED" (ou tu peux aussi définir DEFAULT_PASSWORD_HASH à une chaîne spécifique de ton choix).
MEMBER_TYPES = ("membre", "independant", "mentor", "admin")
STATUTES = ("probatoire","actif", "inactif", "suspendu", "radié")
DECES_STATUTES = ("déclaré", "validé", "comptabilisé", "non-éligible")

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
#def get_conn():
#    if not DATABASE_URL:
#        raise RuntimeError("DATABASE_URL manquant (Render > KM-Project > Environment).")
#    # tuple_row => on garde des tuples (r[0], r[1]...) cohérents avec ton HTML
#    return psycopg.connect(DATABASE_URL, row_factory=tuple_row)
#####
import os

def get_conn():
    DATABASE_URL = os.getenv("DATABASE_URL")

    if not DATABASE_URL:
        # fallback local
        DATABASE_URL = "postgresql://postgres:1234@localhost:5432/kmkimya"

    return psycopg.connect(DATABASE_URL)
#####

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # membres (avec balance)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS membres (
                  id             BIGSERIAL PRIMARY KEY,
                  phone          TEXT NOT NULL UNIQUE,
                  membertype     TEXT NOT NULL,
                  mentor         TEXT NOT  NULL DEFAULT 'admin',
                  lastname       TEXT NOT NULL,
                  firstname      TEXT NOT NULL,
                  birthdate      DATE NOT NULL,
                  idtype         TEXT,
                  idpicture_url  TEXT,
                  currentstatute TEXT NOT NULL,
                  balance        DECIMAL(18,2) NOT NULL DEFAULT 0,
                  updatedate     DATE NOT NULL DEFAULT CURRENT_DATE,
                  updateuser     TEXT NOT NULL,
                  password_hash  TEXT NOT NULL,
                  membershipdate DATE NOT NULL DEFAULT DATE('2099-12-31'),
                  adresse        TEXT,
                  beneficiaire   TEXT NOT NULL DEFAULT 'admin',
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
                  regie        TEXT
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
                  reference     TEXT,
                  statut        TEXT DEFAULT 'déclaré' CHECK (statut IN ('déclaré', 'validé', 'comptabilisé', 'non-éligible')),
                  updated_by    TEXT,
                  updatedate    DATE default CURRENT_DATE,
                  prestation    DECIMAL(18,2)       
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_deces_phone ON deces(phone);")

            # fichier signalétique
            cur.execute("""
                CREATE TABLE IF NOT EXISTS id_data (
                  id            BIGSERIAL PRIMARY KEY,
                  keydata       TEXT NOT NULL,
                  decript       TEXT,
                  quantity      DECIMAL(18,2),
                  note          TEXT,
                  created_by    TEXT NOT NULL,
                  created_at    DATE NOT NULL DEFAULT CURRENT_DATE
                );
            """)

            # comptes techniques (pour comptabiliser les cotisations des membres inactifs ou suspendus qui ne sont pas débités dans la table mouvements)       
            cur.execute(""" 
                CREATE TABLE IF NOT EXISTS comptes_techniques (
                  id            BIGSERIAL PRIMARY KEY, 
                  code          TEXT NOT NULL UNIQUE,  
                  description   TEXT,  
                  balance       DECIMAL(18,2) NOT NULL DEFAULT 0,      
                  updatedate    DATE NOT NULL DEFAULT CURRENT_DATE,    
                  updateuser    TEXT NOT NULL  
                );
            """)

#---------------------------------------------------------------------------------
#           PROCESSUS DE PRISE DE BACKUP & RESTAURATION DES DONNEES 
#           (ex: avant une opération de correction en haut-volume ou 
#            une opération de correction exceptionnelle sur les données
#            de base d'un adhérent) 
#            NOTA: OPERATION EN DEUX ETAPES DISTINCTES - (1) DUMP puis (2) RESTAURATION
#           (jamais les deux en meme temps) 
#---------------------------------------------------------------------------------
####        # DUMP - Prise de backup des tables principales de l'application :
#            # Utilisez des commentaires SQL (--) à l'intérieur de la chaîne
#            sql_commands = """
#            -- DUMP - Création desbackups des tables principales (membres et mouvements) avec un suffixe de date pour différenciation
#            cur.execute(""" 
#                DROP TABLE IF EXISTS membres_BACKUP_20260418;
#                CREATE TABLE membres_BACKUP_20260418 AS SELECT * FROM membres;
#                );
#            """)

#            DROP TABLE IF EXISTS mouvements_BACKUP_20260409;
#            CREATE TABLE mouvements_BACKUP_20260409 AS SELECT * FROM mouvements;
#            """
#            cur.execute(sql_commands)

#            sql_commands = """
#            -- RESTAURATION - Vider les tables sources et réinjecter les données des backups
#            TRUNCATE TABLE membres;
#            INSERT INTO membres SELECT * FROM membres_BACKUP_20260409;
#
#            TRUNCATE TABLE mouvements;
#            INSERT INTO mouvements SELECT * FROM mouvements_BACKUP_20260409;
#            """
#            cur.execute(sql_commands)
#####
#            SELECT * INTO membres_BACKUP_20260409
#            FROM membres;    
#            SELECT * INTO mouvements_BACKUP_20260409 
#            FROM mouvements;                        
#            SELECT * INTO id_data_BACKUP_20260409 
#            FROM id_data;
#            SELECT * INTO deces_BACKUP_20260409 
#            FROM deces;                        
#            SELECT * INTO comptes_techniques_BACKUP_20260409    
#            FROM comptes_techniques;
                        
#            # RESTAURATION - Vider la table source et réinjecter les données du backup                 
#            TRUNCATE TABLE membres;
#            INSERT INTO membres 
#            SELECT * FROM membres_BACKUP_20260409;
#            TRUNCATE TABLE mouvements;
#            INSERT INTO mouvements 
#            SELECT * FROM mouvements_BACKUP_20260409;
#            TRUNCATE TABLE deces;
#            INSERT INTO deces 
#            SELECT * FROM deces_BACKUP_20260409;
#            TRUNCATE TABLE comptes_techniques;
#            INSERT INTO comptes_techniques 
#            SELECT * FROM comptes_techniques_BACKUP_20260409;
#            TRUNCATE TABLE id_data;
#            INSERT INTO id_data 
#            SELECT * FROM id_data_BACKUP_20260409;
#                        """)
#-----------------------------------------------------------------------------------
                        
#           # Correction exceptionnelle sur les données de base d'un adhérent.
#            cur.execute("""
#                UPDATE membres
#                SET phone = '817670140',
#                    firstname = 'Jeanine',
#                    lastname = 'Balola'
#                WHERE id = 65;
#            """)

            # Correction en haut-volume (date d'adhésion des membres potentiels est 2099-12-31).
#            cur.execute("""
#                    UPDATE membres
#                       SET membershipdate = DATE '2099-12-31'
#                    WHERE currentstatute = 'inactif';
#            """)

            # Correction en haut-volume (remplissage de 'beneficiaire. et 'mentor' par la valeur 'admin' là où c'est NULL).
#            cur.execute("""
#                    UPDATE membres
#                        SET beneficiaire = 'admin'
#                    WHERE beneficiaire IS NULL OR beneficiaire = '';
#            """)

#            cur.execute("""
#                    UPDATE membres
#                        SET mentor = 'admin'
#                    WHERE mentor IS NULL OR mentor = '';
#            """)

#            cur.execute("""
#                ALTER TABLE membres
#                  ALTER COLUMN membershipdate SET NOT NULL,
#                  ALTER COLUMN membershipdate SET DEFAULT DATE('2099-12-31');
#            """)

#            cur.execute("""
#                ALTER TABLE id_data 
#                    ALTER COLUMN created_at SET DATA TYPE DATE,
#                    ALTER COLUMN created_at SET DEFAULT CURRENT_DATE;
#            """)


#
#            # Correction exceptionnelle su les donnees de base d'un adhérent.
#            cur.execute("""
#                    UPDATE membres
#                    SET currentstatute = CASE
#                        WHEN phone = %s AND balance > %s THEN 'probatoire'
#                        ELSE currentstatute 
#                    END
#                    WHERE phone in (%s);
#                """, (to_phone,C,to_phone))
#            """)
#
#            # Effaçage de toutes les données de la table deces (table des décès declarés, en cours de traitement ou traités)
#            cur.execute("""
#                DELETE FROM deces;
#            """)

#            # Effaçage de toutes les données des tables MOUVEMENTS et comptes_techniques (données comptables)
#            cur.execute("""
#                DELETE FROM mouvements;
#            """)
#            # Effaçage de toutes les données de la table COMPTES_TECHNIQUES (données comptables)
#            cur.execute("""
#                DELETE FROM comptes_techniques;
#            """)
#
#            # Mise en exploitation : demarrage avec statuts 'inactif' pour tous et mentor 'admin' pour , date d'adhésion 31/12/2099 et date de naissance 01/01/1920 pour tous.
#            cur.execute("""
#                UPDATE membres
#                SET currentstatute = 'inactif',
#                    mentor = 'admin',
#                    membershipdate = DATE '2099-12-31',
#                    birthdate = DATE '1920-01-01',
#                    balance = 0,
#                    updatedate = CURRENT_DATE,
#                    updateuser = 'System'
#            """)
#
#            # Fixation de la prestation visée.
#            cur.execute("""
#                UPDATE id_data
#                SET quantity = 65  -- exemple de valeur pour la prestation ciblée
#                WHERE keydata = 'id-data01'
#            """)
#
#            # Fixation de la marge de sécurité.
#            cur.execute("""
#                UPDATE id_data
#                SET quantity = 1.1  -- exemple de valeur pour la marge de sécurité
#                WHERE keydata = 'id-data02'
#            """)
#            
        conn.commit()

#from dateutil.relativedelta import relativedelta
#def diff_month(d1, d2):
#    r = relativedelta(d2, d1)
#    return r.years * 12 + r.months
def diff_month(d1, d2):
    """Calculates the difference in calendar months between two datetime objects."""
    nb_mois = (d1.year - d2.year) * 12 + d1.month - d2.month
    return nb_mois

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

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

def fetch_dashboard_stats():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # P = prestation ciblée
            cur.execute("""
                SELECT COALESCE(quantity, 0)
                FROM id_data
                WHERE keydata = 'id-data01'  -- clé fixée pour la prestation ciblée
                ORDER BY id DESC
                LIMIT 1
            """)
            P = cur.fetchone()
            P = P[0] if P else Decimal("0")
            
            # S = Marge de securité fixée pour couvrir les frais et imprevues
            cur.execute("""
                SELECT COALESCE(quantity, 0)
                FROM id_data
                WHERE keydata = 'id-data02'  -- clé fixée pour la marge de sécurité
                ORDER BY id DESC
                LIMIT 1
            """)
            S = cur.fetchone()
            S = S[0] if S else Decimal("0")

            # N = actifs
            cur.execute("SELECT COUNT(*) FROM membres WHERE currentstatute = 'actif' or currentstatute = 'probatoire'")
            N = cur.fetchone()[0] or 0

            # B = brut (non radié et non suspendu)
            cur.execute("""
                SELECT COUNT(*)
                FROM membres
                WHERE currentstatute NOT IN ('radié', 'suspendu')
            """)
            B = cur.fetchone()[0] or 0

    # C = 1.2 * P / N (si N=0 => 0)
    try:
        if N > 0:
            C = (Decimal(1+S) * Decimal(P)) / Decimal(N)        # S = marge de sécurité (en %) modifiable pour couvrir les frais et les imprévus.  
            C = C.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            C = Decimal("0.00")
    except (InvalidOperation, ZeroDivisionError):
        C = Decimal("0.00")

    # Optionnel: format affichage (USD)
    def fmt_money(x: Decimal) -> str:
        # 1 234.56
        return f"{x:,.2f}".replace(",", " ")

    return {
        "P": fmt_money(Decimal(P)),
        "N": N,
        "B": B,
        "C": fmt_money(C),
    }


def fetch_mentor_profile(mentor_phone: str):
    mentor_phone = (mentor_phone or "").strip()
    if not mentor_phone:
        return None
    return fetch_member_by_phone(mentor_phone)

from decimal import Decimal
from datetime import datetime


def create_prestation_mouvements(deceased_phone, prestation):

    prestation = Decimal(str(prestation))
    today = datetime.utcnow().date()

    deceased = fetch_member_by_phone(deceased_phone)

    if not deceased:
        raise ValueError("Membre décédé introuvable")

    deceased_firstname = deceased[5]
    deceased_lastname = deceased[4]

    with get_conn() as conn:
        with conn.cursor() as cur:

            # nombre de cotisants actifs
            cur.execute("""
            SELECT COUNT(*)
            FROM membres
            WHERE currentstatute IN ('actif','probatoire')
            AND phone <> 'admin'
            """)
            N = cur.fetchone()[0]

            if N == 0:
                raise ValueError("Aucun membre cotisant.")
#
            # P = prestation ciblée
            cur.execute("""
                SELECT COALESCE(quantity, 0)
                FROM id_data
                WHERE keydata = 'id-data01'  -- clé fixée pour la prestation ciblée
                ORDER BY id DESC
                LIMIT 1
            """)
            P = cur.fetchone()
            P = P[0] if P else Decimal("0")
            
            # S = Marge de securité fixée pour couvrir les frais et imprevues
            cur.execute("""
                SELECT COALESCE(quantity, 0)
                FROM id_data
                WHERE keydata = 'id-data02'  -- clé fixée pour la marge de sécurité
                ORDER BY id DESC
                LIMIT 1
            """)
            S = cur.fetchone()
            S = S[0] if S else Decimal("0")

#
            C = (Decimal(1+S) * prestation) / Decimal(N)
            C = C.quantize(Decimal("0.01"))

            reference = f"PREST-{datetime.utcnow().timestamp()}"

            # 1️⃣ CREDIT prestation
            cur.execute("""
            INSERT INTO mouvements
            (phone,firstname,lastname,mvt_date,amount,debitcredit,reference,libelle)
            VALUES (%s,%s,%s,%s,%s,'C',%s,%s)
            """,
            (
                deceased_phone,
                deceased_firstname,
                deceased_lastname,
                today,
                prestation,
                reference+"-C",
                f"Prestation décès pour {deceased_phone}"
            ))

            # balance du décédé
            cur.execute("""
            UPDATE membres
            SET balance = balance + %s,
                updatedate=CURRENT_DATE,
                updateuser='system'
            WHERE phone=%s
            """,(prestation,deceased_phone))

            # 2️⃣ DEBITS cotisations
            cur.execute("""
            SELECT phone,firstname,lastname,currentstatute
            FROM membres
            WHERE phone <> 'admin'
            """)
            members = cur.fetchall()

            for m in members:

                phone = m[0]
                firstname = m[1]
                lastname = m[2]
                statut = m[3]

                if statut in ('actif','probatoire'):

                    cur.execute("""
                    INSERT INTO mouvements
                    (phone,firstname,lastname,mvt_date,amount,debitcredit,reference,libelle)
                    VALUES (%s,%s,%s,%s,%s,'D',%s,%s)
                    """,
                    (
                        phone,
                        firstname,
                        lastname,
                        today,
                        C,
                        reference+"-"+phone,
                        f"Contribution décès de : {deceased_firstname} {deceased_lastname} // {deceased_phone}"
                    ))

                    cur.execute("""
                    UPDATE membres
                    SET balance = balance - %s,
                        updatedate=CURRENT_DATE,
                        updateuser='system'
                    WHERE phone=%s
                    """,(C,phone))

                elif statut in ('inactif','suspendu'):

                    account = "CT_DUES_INACTIFS" if statut=="inactif" else "CT_DUES_SUSPENDUS"

                    cur.execute("""
                    UPDATE comptes_techniques
                    SET balance = balance + %s,
                        updatedate=CURRENT_DATE,
                        updateuser='system'
                    WHERE code=%s
                    """,(C,account))

        conn.commit()


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
def member_exists(cur, phone: str) -> bool:
    cur.execute("SELECT 1 FROM membres WHERE phone = %s", (phone,))
    return cur.fetchone() is not None

def create_member_minimal(cur, phone: str, firstname: str, lastname: str):
    """
    Crée un membre minimal si absent.
    Important: ta table membres a des champs NOT NULL: mentor, birthdate, idtype, password_hash...
    On met des valeurs par défaut cohérentes.
    """
    # Birthdate: valeur technique si inconnue (à ajuster si tu veux)
    #default_birthdate = datetime.today().date()
    default_birthdate = datetime.strptime("1900-01-01", "%Y-%m-%d").date()
    default_membershipdate = datetime.strptime("2099-12-31", "%Y-%m-%d").date()  # date d'adhésion lointaine pour forcer la mise à jour lors de la première cotisation

    # Password_hash : si tu ne veux pas créer de compte login automatique,
    # tu peux mettre un hash “impossible” et forcer un reset plus tard.
    # Ici: on autorise DEFAULT_PASSWORD_HASH vide => on met une chaîne fixe non vide.
    pwd_hash = DEFAULT_PASSWORD_HASH.strip() or "NO_LOGIN_CREATED"
    DEFAULT_MEMBER_TYPE = "independant"
    DEFAULT_MENTOR="admin"
    DEFAULT_IDTYPE = "CE"
    DEFAULT_STATUTE = "inactif"
    DEFAULT_UPDATEUSER = "System"

    cur.execute("""
        INSERT INTO membres
        (phone, membertype, mentor, lastname, firstname, birthdate, idtype, idpicture_url,
         currentstatute, updatedate, updateuser, password_hash, membershipdate)
        VALUES
        (%s, %s, %s, %s, %s, %s, %s, NULL, %s, CURRENT_DATE, %s, %s, %s)
        ON CONFLICT (phone) DO NOTHING;
    """, (
        phone,
        DEFAULT_MEMBER_TYPE,
        DEFAULT_MENTOR,
        lastname,
        firstname,
        default_birthdate,
        DEFAULT_IDTYPE,
        DEFAULT_STATUTE,
        DEFAULT_UPDATEUSER,
        pwd_hash,
        default_membershipdate
    ))
    #log.info("Nouveau membre créé automatiquement: %s (%s %s)", phone, firstname, lastname)


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


def insert_member(phone, membertype, mentor, lastname, firstname, birthdate_date, 
                  currentstatute,updatedate, updateuser, beneficiaire, adresse, password_plain, membershipdate):
    pwd_hash = generate_password_hash(password_plain)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO membres
                (phone, membertype, mentor, lastname, firstname, birthdate,
                 currentstatute, updatedate, updateuser,beneficiaire, adresse, password_hash, membershipdate)
                VALUES
                (%s,%s,%s,%s,%s,%s,%s,CURRENT_DATE,%s,%s,%s,%s,%s)
            """, (phone, membertype, mentor, lastname, firstname, birthdate_date,
                  'inactif',updateuser, beneficiaire, adresse, pwd_hash, membershipdate))
        conn.commit()

def update_member(
    member_id, phone, membertype, mentor, lastname, firstname,
    birthdate_date, membershipdate, balance,
    currentstatute, updateuser, new_password_plain: str | None
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if new_password_plain:
                pwd_hash = generate_password_hash(new_password_plain)
                cur.execute("""
                    UPDATE membres
                    SET phone=%s, membertype=%s, mentor=%s, lastname=%s, firstname=%s,
                        birthdate=%s, membershipdate=%s, balance=%s, currentstatute=%s,
                        updatedate=CURRENT_DATE, updateuser=%s, password_hash=%s
                    WHERE id=%s
                """, (
                    phone, membertype, mentor, lastname, firstname,
                    birthdate_date, membershipdate, balance, currentstatute,
                    updateuser, pwd_hash, member_id
                ))
            else:
                cur.execute("""
                    UPDATE membres
                    SET phone=%s, membertype=%s, mentor=%s, lastname=%s, firstname=%s,
                        birthdate=%s, membershipdate=%s, balance=%s, currentstatute=%s,
                        updatedate=CURRENT_DATE, updateuser=%s
                    WHERE id=%s
                """, (
                    phone, membertype, mentor, lastname, firstname,
                    birthdate_date, membershipdate, balance, currentstatute,
                    updateuser, member_id
                ))
        conn.commit()


def delete_member(member_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM membres WHERE id = %s", (member_id,))
        conn.commit()

def fetch_deces_by_phone(phone: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, phone, date_deces, declared_by, reference, created_at,statut,updated_by, updatedate, prestation
                FROM deces
                WHERE phone=%s
            """, (phone,))
            return cur.fetchone()


def fetch_member_by_phone(phone: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, phone, membertype, mentor, lastname, firstname, birthdate,idtype, idpicture_url, currentstatute, balance, updatedate, updateuser, adresse, beneficiaire, membershipdate
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
                SELECT id, phone, lastname, mvt_date, amount, debitcredit, reference, libelle, updatedate, updated_by,regie
                FROM mouvements
                WHERE phone=%s
                ORDER BY mvt_date DESC, id DESC
            """, (phone,))
            return cur.fetchall()

def list_all_mouvements():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, phone, lastname, mvt_date, amount, debitcredit, reference, libelle, updatedate, updated_by,regie
                FROM mouvements
                ORDER BY mvt_date DESC, id DESC
            """)
            return cur.fetchall()

def list_deces_pendants():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, phone, date_deces, declared_by, reference, created_at,statut
                FROM deces
                WHERE statut in ('déclaré', 'validé')
                ORDER BY date_deces DESC, id DESC
            """)
            return cur.fetchall()

def list_deces_traites():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT membres.phone,firstname,lastname,date_deces,statut,prestation
                FROM membres left join deces on membres.phone = deces.phone
                WHERE statut in ('comptabilisé', 'non-éligible', 'validé')
                ORDER BY date_deces DESC
            """)
            return cur.fetchall()

def update_mouvement(id: int, phone: str, mvt_date, amount, debitcredit,  libelle, regie):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE mouvements
                SET phone=%s, mvt_date=%s, amount=%s, debitcredit=%s, libelle=%s, updatedate=%s, updated_by=%s, regie=%s
                WHERE id=%s
            """, (phone, mvt_date, amount, debitcredit,  libelle, date.today(), session.get("user"), regie, id))
        conn.commit()

def update_deces(id: int, statut: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE deces
                SET statut=%s, updatedate=CURRENT_DATE, updated_by=%s
                WHERE id=%s
            """, (statut, session.get("user"), id))
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
                INSERT INTO deces (phone, date_deces, declared_by, reference,statut)
                VALUES (%s,%s,%s,%s,'déclaré')
            """, (phone, date_deces, declared_by, reference))
        conn.commit()

def create_cotisation(code: str, description: str, cotisation: float, ref_base: str, today: datetime):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO comptes_techniques (code, description, amount, reference, mvt_date)
                VALUES (%s,%s,%s,%s,%s)
            """, (code, description, cotisation, ref_base, today))
        conn.commit()

def create_donation(code: str, description: str, donation: float, ref_base: str, today: datetime):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO comptes_techniques (code, description, amount, reference, mvt_date)
                VALUES (%s,%s,%s,%s,%s)
            """, (code, description, donation, ref_base, today))
        conn.commit()

def create_transfert(from_phone: str, to_phone: str, amount: float, ref_base: str,today):
    me = fetch_member_by_phone(from_phone)
    to_member = fetch_member_by_phone(to_phone)

    to_balance = to_member[10]
    from_balance = me[10]   
    to_membershipdate = to_member[15]
    from_membershipdate = me[15]

    log.info("from_phone=%s,from_balance=%s, >>> to_phone=%s, to_balance=%s, from_membershipdate=%s, to_membershipdate=%s", from_phone, from_balance, to_phone, to_balance, from_membershipdate, to_membershipdate)

    today = datetime.utcnow().date()
    C= fetch_dashboard_stats()["C"]
    #C=fetch_dashboard_stats().get("C", "0.00").replace(" ", "")
    lib=f"Transfert de {amount} de {from_phone} vers {to_phone}"

    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1) insert 'mouvement' DEBIT (from)
            cur.execute("""
                INSERT INTO mouvements (phone, firstname,lastname, mvt_date, amount, debitcredit, reference,libelle)
                VALUES (%s,%s,%s,%s,%s,'D',%s,%s)
            """, (from_phone, me[5], me[4], today, amount, ref_base + "-D", lib))

            # 2) insert 'mouvement' CREDIT (to)
            cur.execute("""
                INSERT INTO mouvements (phone, firstname,lastname, mvt_date, amount, debitcredit, reference,libelle)
                VALUES (%s,%s,%s,%s,%s,'C',%s, %s)
            """, (to_phone, to_member[5], to_member[4], today, amount, ref_base + "-C", lib))

            # 3) update 'membres'
            #   a)update balance du membre qui donne (from) et du membre qui reçoit (to) 
            cur.execute("UPDATE membres SET balance = balance - %s, updatedate=CURRENT_DATE, updateuser=%s WHERE phone=%s",
                        (amount, from_phone, from_phone))
            cur.execute("UPDATE membres SET balance = balance + %s, updatedate=CURRENT_DATE, updateuser=%s WHERE phone=%s",
                        (amount, from_phone, to_phone))

            #   b) Update de 'membershipdate' et 'currentstatute' en fonction de la date d'adhésion et du solde du membre qui reçoit (to) et du membre qui donne (from) :           
            #       (*) cas de date d'adhésion 2099-12-31 (membre potentiel) : '
            if me[15] == datetime.strptime("31/12/2099", "%d/%m/%Y").date() or to_member[15] == datetime.strptime("31/12/2099", "%d/%m/%Y").date():
                cur.execute("""
                    UPDATE membres
                    SET membershipdate = CURRENT_DATE,
                        currentstatute = 'probatoire'
                    WHERE phone in (%s, %s) and balance >= %s;
                """, (from_phone, to_phone, C))

                cur.execute("""
                    UPDATE membres
                    SET    currentstatute = 'inactif'
                    WHERE phone in (%s, %s) and balance < %s;
                """, (from_phone, to_phone, C))
            else:
            #       (*) cas de dates ordinaires (càd differentes de 2099-12-31): '
                from_month = diff_month(me[15],today)
                to_month = diff_month(to_member[15],today)

                log.info("from_phone=%s, to_phone=%s, >>> from_month=%s, to_month=%s", from_phone, to_phone, from_month, to_month)
                limit_date = datetime.strptime("31/12/2099", "%d/%m/%Y").date()

                from_balance = me[10]
                to_balance = to_member[10]
                log.info("from_phone=%s,from_balance=%s, >>> to_phone=%s, to_balance=%s", from_phone, from_balance, to_phone, to_balance)
    #####
                #pour celui qui reçoit : 
                if  to_month < 3 :
                    cur.execute("""
                        UPDATE membres
                        SET currentstatute = CASE
                            WHEN phone = %s AND balance >= %s THEN 'probatoire'
                            ELSE 'inactif'
                        END
                        WHERE phone in (%s);
                    """, (to_phone,C,to_phone))

                if  to_month >= 3:
                    cur.execute("""
                        UPDATE membres
                        SET currentstatute = CASE
                            WHEN phone = %s AND balance >= %s THEN 'actif'
                            ELSE 'inactif'
                        END
                        WHERE phone IN (%s);
                    """, (to_phone,C,to_phone))

                #pour celui qui donne: 
                if  from_month < 3 :
                    cur.execute("""
                        UPDATE membres
                        SET currentstatute = CASE
                            WHEN phone = %s AND balance >= %s THEN 'probatoire'
                            ELSE 'inactif'
                        END
                        WHERE phone in (%s);
                    """, (from_phone,C,from_phone))

                if  from_month >= 3:
                    cur.execute("""
                        UPDATE membres
                        SET currentstatute = CASE
                            WHEN phone = %s AND balance >= %s THEN 'actif'
                            ELSE 'inactif'
                        END
                        WHERE phone IN (%s);
                    """, (from_phone,C,from_phone))
        conn.commit()

def fetch_member_by_phone_like(q_phone: str):
    q = (q_phone or "").strip()
    if not q:
        return []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_membres + " WHERE phone ILIKE %s ORDER BY id DESC", (f"%{q}%",))
            return cur.fetchall()

def update_member_mentor(phone: str, mentor: str, updateuser: str, lastname: str, firstname: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE membres
                SET mentor=%s,
                    updatedate=CURRENT_DATE,
                    updateuser=%s,
                    lastname=%s,
                    firstname=%s
                WHERE phone=%s
            """, (mentor, updateuser, lastname, firstname, phone))
        conn.commit()

def update_member_beneficiaire(phone: str, beneficiaire: str, updateuser: str, lastname: str, firstname: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE membres
                SET beneficiaire=%s,
                    updatedate=CURRENT_DATE,
                    updateuser=%s,
                    lastname=%s,
                    firstname=%s
                WHERE phone=%s
            """, (beneficiaire, updateuser, lastname, firstname, phone))
        conn.commit()

def update_member_adresse(phone: str, adresse: str, updateuser: str, lastname: str, firstname: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE membres
                SET adresse=%s,
                    updatedate=CURRENT_DATE,
                    updateuser=%s,
                    lastname=%s,
                    firstname=%s
                WHERE phone=%s
            """, (adresse, updateuser, lastname, firstname, phone))
        conn.commit()

def update_id_data(keydata: str, quantity: Decimal, decript: str, note: str):
    # Récupérer l'utilisateur actuel ou définir une valeur par défaut
    current_user = session.get("user", "système")
    
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE id_data
                    SET quantity=%s, 
                        decript=%s, 
                        note=%s, 
                        created_at=CURRENT_DATE, 
                        created_by=%s
                    WHERE keydata=%s
                """, (quantity, decript, note, current_user, keydata))
            conn.commit()
    except Exception as e:
        print(f"Erreur SQL update_id_data: {e}")
        raise e

#def update_id_data(keydata: str, quantity: Decimal,decript: str, note: str):
#    with get_conn() as conn:
#        with conn.cursor() as cur:
#            cur.execute("""
#                UPDATE id_data
#                SET quantity=%s, decript=%s, note=%s, created_at=CURRENT_DATE, created_by=%s
#                WHERE keydata=%s
#            """, (quantity, decript, note, session.get("user"), keydata))
#        conn.commit()

def list_id_data():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT keydata,decript,quantity,note,created_by,created_at,id   
                FROM id_data
                ORDER BY keydata DESC
            """)
            return cur.fetchall()

def delete_id_data(id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM id_data WHERE id=%s", (id,))
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
    password = form.get("password") or ""  # valeur par défaut si pas fourni (ex: update sans changer le mdp)

    if not phone or not membertype or not mentor or not lastname or not firstname or not birthdate_str or not membershipdate_str or not balance_str or not currentstatute:
        raise ValueError("Veuillez remplir tous les champs obligatoires.")

    if membertype not in MEMBER_TYPES:
        raise ValueError("membertype invalide.")
    if currentstatute not in STATUTES:
        raise ValueError("currentstatute invalide.")

    birthdate_date = datetime.strptime(birthdate_str, "%d/%m/%Y").date()
    membershipdate_date = datetime.strptime(membershipdate_str, "%d/%m/%Y").date()
    balance_decimal = float(balance_str) if balance_str else 0.0

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

def validate_mentor_phone_or_raise(mentor_phone: str, *, current_user_phone: str) -> str:
    mentor_phone = (mentor_phone or "").strip()

    if not mentor_phone:
        raise ValueError("Veuillez saisir le phone du mentor.")

    # Optionnel mais recommandé : empêcher auto-mentor
    if mentor_phone == current_user_phone:
        raise ValueError("Mentor invalide : vous ne pouvez pas être votre propre mentor.")

    mentor_row = fetch_member_by_phone(mentor_phone)
    if not mentor_row:
        raise ValueError("Mentor introuvable : ce phone n'existe pas dans la table 'membres'.")

    # Index d’après votre modèle : membertype = row[2]
    if mentor_row[2] not in ("mentor", "admin"):
        raise ValueError("Mentor invalide : ce membre existe mais n'est pas de type 'mentor' ou 'admin'.")

    # (Optionnel) on pourrait aussi vérifier statut mentor_row[9] == 'Suspendu'
    if mentor_row[9]  in ("radié", "suspendu"):
        raise ValueError("Mentor invalide : ce membre existe mais est  'Suspendu' ou 'Radié'.")
    return mentor_phone

def validate_beneficiaire_phone_or_raise(beneficiaire_phone: str, *, current_user_phone: str) -> str:
    beneficiaire_phone = (beneficiaire_phone or "").strip()

    if not beneficiaire_phone:
        raise ValueError("Veuillez saisir le phone du bénéficiaire.")

    # Optionnel mais recommandé : empêcher auto-beneficiaire
    if beneficiaire_phone == current_user_phone:
        raise ValueError("Bénéficiaire invalide : vous ne pouvez pas être votre propre bénéficiaire.")

    beneficiaire_row = fetch_member_by_phone(beneficiaire_phone)
    if not beneficiaire_row:
        raise ValueError("Bénéficiaire introuvable : ce phone n'existe pas dans la table 'membres'.")

    # Index d’après votre modèle : membertype = row[2]
    #if beneficiaire_row[2] not in ("bénéficiaire", "admin"):
    #    raise ValueError("Bénéficiaire invalide : ce membre existe mais n'est pas de type 'mentor' ou 'admin'.")

    # (Optionnel) on pourrait aussi vérifier statut beneficiaire_row[9] == 'Suspendu'
    if beneficiaire_row[9]  in ("radié", "suspendu"):
        raise ValueError("Bénéficiaire invalide : cet adhérent existe mais est  'Suspendu' ou 'Radié'.")
    return beneficiaire_phone

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
    #log.info("Login attempt; data in : row=%s", row)
    if not row:
        return False

    pwd_hash, statut = row
    #log.info("Login attempt: phone=%s statut=%s", phone, statut)

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
        
def statutes_update():
    updateuser = session.get("user") or ADMIN_PHONE
    C = Decimal(str(fetch_dashboard_stats()["C"]).replace(" ", ""))
    limit_date = datetime.strptime("31/12/2099", "%d/%m/%Y").date()

    #log.info("Début de l'actualisation des statuts. Seuil Cotisation=%s, date limite=%s", C, limit_date)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE membres
                SET membershipdate = CURRENT_DATE,
                    currentstatute = 'probatoire'
                WHERE balance >= %s AND (membershipdate = %s OR membershipdate IS NULL)
            """, (C, limit_date))

            log.info("Mise à jour des membres avec date d'adhesion autre que la date limite et solde suffisant.")

            cur.execute("""
                UPDATE membres
                SET 
                  currentstatute = CASE 
                    WHEN (
                        EXTRACT(YEAR FROM age(CURRENT_DATE, membershipdate)) * 12 +
                        EXTRACT(MONTH FROM age(CURRENT_DATE, membershipdate))
                    ) < 3 AND balance >= %s THEN 'probatoire'
                    
                    WHEN (
                        EXTRACT(YEAR FROM age(CURRENT_DATE, membershipdate)) * 12 +
                        EXTRACT(MONTH FROM age(CURRENT_DATE, membershipdate))
                    ) >= 3 AND balance >= %s THEN 'actif'
                    
                    ELSE 'inactif'
                  END
                WHERE membershipdate <> %s
            """, (C,C, limit_date))
            rows_updated = cur.rowcount

        conn.commit()
        log.info("Actualisation des statuts terminée. %s statut(s) mis à jour.", rows_updated)
        flash(f"{rows_updated} statut(s) mis à jour avec succès", "success")

#
# ----------------------------
# Lancement de l'application
# ----------------------------
LOGIN_PAGE = """
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <title>Connexion KM-Kimya</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">

  <style>
    body {
        margin: 0;
        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        background: linear-gradient(rgba(0,0,0,0.5), rgba(0,0,0,0.5)),
                    url("{{ url_for('static', filename='logokmkimya1.jpg') }}");
        background-size: cover;
        background-position: center;
        height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
    }

    .container {
        display: flex;
        max-width: 900px;
        width: 95%;
        border-radius: 16px;
        overflow: hidden;
        box-shadow: 0 10px 40px rgba(0,0,0,0.4);
    }

    /* LEFT PANEL IMAGE */
    .left {
        flex: 1;
        background: url("{{ url_for('static', filename='kmkimya_poster.jpg') }}") center/cover no-repeat;
        display: none;
    }

    /* RIGHT PANEL LOGIN */
    .right {
        flex: 1;
        background: rgba(255,255,255,0.95);
        padding: 40px;
        backdrop-filter: blur(10px);
    }

    h2 {
        margin-top: 0;
        text-align: center;
        color: #1E3A8A;
    }

    label {
        display:block;
        margin-top: 15px;
        font-weight: 600;
        color: #333;
    }

    input {
        width: 100%;
        padding: 12px;
        margin-top: 5px;
        border-radius: 8px;
        border: 1px solid #ccc;
        font-size: 14px;
        transition: 0.3s;
    }

    input:focus {
        border-color: #1E3A8A;
        outline: none;
        box-shadow: 0 0 5px rgba(30,58,138,0.3);
    }

    .btn {
        width: 100%;
        margin-top: 20px;
        padding: 12px;
        border-radius: 10px;
        border: none;
        background: linear-gradient(135deg, #1E3A8A, #2563EB);
        color: white;
        font-size: 16px;
        cursor: pointer;
        transition: 0.3s;
    }

    .btn:hover {
        transform: translateY(-2px);
        box-shadow: 0 5px 15px rgba(0,0,0,0.3);
    }

    .msg {
        margin-top: 15px;
        padding: 10px;
        border-radius: 8px;
        background: #ffe9ea;
        color: #7a0010;
        text-align: center;
    }

    .small {
        margin-top: 15px;
        font-size: 0.9em;
        color: #555;
        text-align: center;
    }

    /* Responsive */
    @media(min-width: 768px) {
        .left {
            display: block;
        }
    }
  </style>
</head>

<body>

<div class="container">

    <!-- IMAGE PANEL -->
    <div class="left"></div>

    <div class="right">
        <h2>Connexion KM-Kimya</h2>

        <form method="post" action="{{ url_for('login') }}">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">

            <label>Identifiant</label>
            <input name="phone" placeholder=" numéro de téléphone sans préfixe" required>

            <label>Mot de passe</label>
            <input name="password" type="password" required>

            <button class="btn" type="submit">Se connecter</button>
        </form>

        {% if message %}
            <div class="msg">{{ message }}</div>
        {% endif %}

        <div class="small">
            Accès refusé aux suspendus et aux radiés
        </div>

        <div class="navigation-buttons" style="margin-top: 20px; display: flex; gap: 10px;justify-content: center; align-items: center;">
            <!-- Bouton Inscription -->
            <a href="{{ url_for('add_member') }}">
                <button type="button">Inscription libre</button>
            </a>

            <!-- Bouton Infos Association -->
            <a href="{{ url_for('infos_association') }}">
                <button type="button">Notre association</button>
            </a>
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
  
from flask import render_template

# 1. Route pour le formulaire d'inscription
@app.route('/add_member')
def inscription():
    # Ici, tu renverras vers ton formulaire d'ajout à la table 'membres'
    return render_template_string(ADD_MEMBER_PAGE)

# 2. Route pour la page d'informations
@app.route('/infos_association')
def infos_association():
    return render_template_string(INFOS_ASSOCIATION_PAGE)


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

  /* ✅ Cadran statistiques */
  .top{display:flex;align-items:flex-start;gap:14px;}
  .hdr{flex:1}

  .statsbox{
    min-width:260px;
    border:1px solid #e7e7e7;
    border-radius:14px;
    padding:10px 12px;
    background:#fafafa;
    font-size:12px;
    line-height:1.25;
    white-space:nowrap;
  }
  .stats-title{font-weight:700;margin-bottom:6px;font-size:12px;}
  .stats-row{display:flex;justify-content:space-between;gap:12px;padding:2px 0;}
  .stats-row span{color:#555;}
  .stats-row b{color:#111;}
   /* ✅ FIN du Cadran statistiques */
 
</style>
</head>
<body>
  <div class="top">
    <div class="brand">KM</div>

    <div class="hdr">
      <h2 style="margin:0;">Kimya</h2>
      <div class="muted">membre connecté : <b>{{ connected_label }}</b></div>
      <div><small>Rôle: <b>{{ connected_role }}</b></small></div>
    </div>

    <!-- ✅ Cadran statistiques (coin supérieur droit) -->
    <div class="statsbox">
      <div class="stats-title">Indicateurs clés : </div>
      <div class="stats-row"><span>Prestation disponible . . . . . . . . . . . . . .</span><b>{{ P }}</b></div>
      <div class="stats-row"><span>Adhérents actifs. . . . . . . . . . . . . . . . .</span><b>{{ N }}</b></div>
      <div class="stats-row"><span>Adhérents (brut). . . . . . . . . . . . . . . . .</span><b>{{ B }}</b></div>
      <div class="stats-row"><span>Contribution individuelle attendue. . . .</span><b>{{ C }}</b></div>
    </div>
    <!-- ✅ FIN Cadran statistiques (coin supérieur droit) -->

    <div class="actions">
      <a class="btn" href="{{ url_for('logout') }}">Logout</a>
    </div>
  </div>

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
        <p class="t">Transfert de crédits</p>
        <p class="d">Transfert de crédits, cotisations et dons </p>
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

    <div class="card">
      <div class="icon">🎓</div>
      <div>
        <p class="t">Historique des décès</p>
        <p class="d">Liste historique des décès d'adhérents.</p>
        <a class="link" href="{{ url_for('deces_history') }}">Ouvrir</a>
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
      <div class="icon">👥</div>
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
    </div>

    <div class="card">
      <div class="icon">➕</div>
      <div>
        <p class="t">Deuils pendants</p>
        <p class="d">Suivi des déclarations de décès d'adhérents.</p>
        <a class="link" href="{{ url_for('deuils_pendants') }}">Ouvrir</a>
      </div>
    </div>
      
    {% endif %}

    </div>
  </div>
</body></html>
"""

# Endpoint#0 HOME PAGE ( menu card + cadran statistiques)
@app.get("/")
@login_required
def home():
    rows = fetch_all_membres()

    phone = session.get("user")
    member = fetch_member_by_phone(phone) if phone else None

    connected_role = ""
    if member:
        connected_phone = member[1]
        connected_firstname = member[5]
        connected_lastname = member[4]
        connected_role = member[2]
        connected_label = f"{connected_phone} — {connected_firstname} {connected_lastname}"
    else:
        connected_label = phone or ""

    stats = fetch_dashboard_stats()  # ✅ P, N, B, C

    return render_template_string(
        DASHBOARD_PAGE,
        rows=rows,
        connected_label=connected_label,
        connected_role=connected_role,
        # ✅ nouvelles variables template
        P=stats["P"],
        N=stats["N"],
        B=stats["B"],
        C=stats["C"],
        # vos autres variables si nécessaires
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
ACCOUNT_PAGE = """"
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mon compte</title>

<style>
 body{font-family:Arial;margin:20px}
 .wrap{max-width:900px;margin:0 auto}
 .card{border:1px solid #e7e7e7;border-radius:16px;padding:16px}

 label{display:block;margin:10px 0 4px;font-weight:700}
 input{width:80%;padding:10px;border:1px solid #ddd;border-radius:10px}
 input[readonly]{background:#f6f6f6}

 .row{display:flex;gap:10px;margin-top:12px}

 .btn{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#111;color:#fff;cursor:pointer}
 .btn2{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#fff;color:#111;cursor:pointer}

 .msg{margin-top:12px;padding:10px;border-radius:12px}
 .ok{background:#eaffea;border:1px solid #b8ffb8}
 .err{background:#ffe9ea;border:1px solid #ffb3b8}

 .mentor-box{
  margin-top:8px;
  padding:10px 12px;
  border:1px solid #d9e7ff;
  background:#f7fbff;
  border-radius:10px;
  font-size:13px;
 }

 .mentor-warn{
  border-color:#f0d9a7;
  background:#fffaf0;
 }

 .flex-row{
   display:flex;
   gap:20px;
 }

.inline-3{
  display:grid;
  grid-template-columns:1fr 1fr 1fr;
  gap:16px;
}

.inline-2{
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:16px;
}

@media (max-width:700px){
  .inline-3, .inline-2{
    grid-template-columns:1fr;
  }
}
</style>
</head>

<body>

<div class="wrap">
  <div style="display:flex;justify-content:space-between;">
      <h2>Mon compte</h2>
      <a href="{{ url_for('home') }}">← Retour</a>
  </div>
</div>

<div class="wrap">
<div class="card">

<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">

<!-- SECTION INFO -->
<div style="color:blue;" class="inline-3">

<div>
<label>Identifiant</label>
<input value="{{ m[1] }}" readonly>
</div>

<div>
<label>Statut</label>
<input value="{{ m[9] }}" readonly>
</div>

<div>
<label>Solde</label>
<input value="{{ m[10] }}" readonly>
</div>

</div>

<!-- SECTION NOM -->
<div style="color:black;" class="inline-2">
<div>
<label>Nom</label>
<input name="lastname" value="{{ m[4] }}">
</div>

<div>
<label>Prénom</label>
<input name="firstname" value="{{ m[5] }}">
</div>
</div>

<!-- ADRESSE -->
<div>
<label>Adresse</label>
<input name="adresse" value="{{ m[13] }}">
</div>

<!-- MENTOR + BENEFICIAIRE -->
<div class="flex-row">

<div>
<label>Mentor</label>
<input name="mentor" value="{{ m[3] }}">

{% if mentor_info %}
  <div class="mentor-box">
    <div><b>Mentor :</b> {{ mentor_info[1] }}</div>
    <div><b>Nom :</b> {{ mentor_info[5] }} {{ mentor_info[4] }}</div>
    <div><b>Type & Statut :</b> {{ mentor_info[2] }} {{ mentor_info[9] }}</div>
  </div>
{% elif m[3] %}
  <div class="mentor-box mentor-warn">
    <div><b>Mentor :</b> {{ m[3] }}</div>
    <div>Profil mentor non trouvé.</div>
  </div>
{% endif %}
</div>

<div>
<label>Bénéficiaire</label>
<input name="beneficiaire" value="{{ m[14] }}">

{% if beneficiaire_info %}
  <div class="mentor-box">
    <div><b>Bénéficiaire :</b> {{ beneficiaire_info[1] }}</div>
    <div><b>Nom :</b> {{ beneficiaire_info[5] }} {{ beneficiaire_info[4] }}</div>
    <div><b>Type & Statut :</b> {{ beneficiaire_info[2] }} {{ beneficiaire_info[9] }}</div>
  </div>
{% elif m[14] %}
  <div class="beneficiaire-box beneficiaire-warn">
    <div><b>Bénéficiaire :</b> {{ m[14] }}</div>
    <div>Profil bénéficiaire non trouvé.</div>
  </div>
{% endif %}
</div>

</div>

<!-- PASSWORD -->
<div>
<label>Nouveau mot de passe</label>
<input name="new_password" type="password">
</div>

<!-- ACTIONS -->
<div class="row">
<button class="btn" type="submit">Enregistrer</button>
<a class="btn2" href="{{ url_for('home') }}">Annuler</a>
</div>

{% if message %}
<div class="msg {{ 'err' if is_error else 'ok' }}">{{ message }}</div>
{% endif %}

</form>

</div>
</div>

</body>
</html>
"""

# 
# Endpoint#01 Mon Compte (menu card)
# 
@app.route("/account", methods=["GET","POST"])
@login_required
def account():
    phone = session["user"]
    m = fetch_member_by_phone(phone)
    mentor_info = fetch_member_by_phone(m[3]) if m and m[3] else None
    beneficiaire_info = fetch_member_by_phone(m[14]) if m and m[14] else None

    if request.method == "POST":
        try:
            mentor_new = (request.form.get("mentor") or "").strip()
            pwd = (request.form.get("new_password") or "").strip()
            ln = request.form.get("lastname")
            fn = request.form.get("firstname")

            mentor_info = fetch_member_by_phone(m[3]) if m and m[3] else None
            beneficiaire_info = fetch_member_by_phone(m[14]) if m and m[14] else None

            beneficiaire_new = (request.form.get("beneficiaire") or "").strip()
            adresse_new = (request.form.get("adresse") or "").strip()
            
            changed = []

            # 0) changement nom/prénom, bénéficiaire ou adresse (si modifié)
            if ln != m[4] or fn != m[5]:
                nom_prenom=1 
            else:
                nom_prenom=0

            if adresse_new != (m[13] or ""):
                update_member_adresse(phone, adresse_new, updateuser=phone, lastname=ln, firstname=fn)
                changed.append("Adresse")

            if beneficiaire_new != (m[14] or ""):
                beneficiaire_ok = validate_beneficiaire_phone_or_raise(beneficiaire_new, current_user_phone=phone)
                update_member_beneficiaire(phone, beneficiaire_ok, updateuser=phone, lastname=ln, firstname=fn)
                changed.append("Bénéficiaire")

            # 1) mentor (si modifié)
            if (mentor_new and mentor_new != (m[3] or "")) or nom_prenom:
                mentor_ok = validate_mentor_phone_or_raise(mentor_new, current_user_phone=phone)
                update_member_mentor(phone, mentor_ok, updateuser=phone, lastname=ln, firstname=fn)
                if mentor_new and mentor_new != (m[3] or ""):
                    changed.append("Mentor")
                if nom_prenom==1:
                    changed.append("Nom et/ou Prénom")

            # 2) mot de passe (si fourni)
            if pwd:
                update_member_password(phone, pwd, updateuser=phone)
                changed.append("Mot de passe")

            # refresh
            m = fetch_member_by_phone(phone)
            mentor_info = fetch_member_by_phone(m[3]) if m and m[3] else None
            beneficiaire_info = fetch_member_by_phone(m[14]) if m and m[14] else None

            if changed:
                return render_template_string(
                    ACCOUNT_PAGE, m=m, mentor_info=mentor_info, beneficiaire_info=beneficiaire_info,
                    message="Changement(s) enregistré(s) : " + ", ".join(changed) + ".",
                    is_error=False
                )
            return render_template_string(ACCOUNT_PAGE, m=m, mentor_info=mentor_info, beneficiaire_info=beneficiaire_info, message="Aucun changement.", is_error=False)

        except Exception as e:
            # log.exception("Erreur update compte")  # si tu veux tracer
            m = fetch_member_by_phone(phone)
            mentor_info = fetch_member_by_phone(m[3]) if m and m[3] else None
            beneficiaire_info = fetch_member_by_phone(m[14]) if m and m[14] else None
            return render_template_string(ACCOUNT_PAGE, m=m, mentor_info=mentor_info, beneficiaire_info=beneficiaire_info, message=f"Erreur: {e}", is_error=True)

    return render_template_string(ACCOUNT_PAGE, m=m, mentor_info=mentor_info,beneficiaire_info=beneficiaire_info, message="", is_error=False)


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
      <th>Date</th><th>Montant</th><th>D/C</th><th>Libellé</th>
    </tr></thead>
    <tbody>
    {% for r in rows %}
      <tr>
        <td>{{ r[3].strftime('%d/%m/%Y') }}</td>
        <td>{{ r[4] }}</td>
        <td>{{ r[5] }}</td>
        <td>{{ r[7] }}</td>
      </tr>
    {% endfor %}
    {% if not rows %}<tr><td colspan="4">Aucun mouvement.</td></tr>{% endif %}
    </tbody>
  </table>
</div></body></html>
"""
# Endpoint#02 Mes mouvements (menu card)
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
 input{width:80%;padding:10px;border:1px solid #ddd;border-radius:10px}
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
# Endpoint#03 Déclaration décès (menu card)
import uuid

@app.route("/deces", methods=["GET","POST"])
@login_required
def deces():
    message, is_error = "", False
    phone_in = (request.form.get("phone") or "").strip() if request.method == "POST" else ""
    date_in  = (request.form.get("date_deces") or "").strip() if request.method == "POST" else ""
    found_name = ""

    dec = fetch_deces_by_phone(phone_in) if phone_in else None
    if dec:
        return render_template_string(DECES_PAGE, phone_in="", date_in="",found_name="", 
                                      message="Ce décès avais déjà été déclaré. Merci de contacter l'administrateur.", is_error=True)

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
 input,select{width:80%;padding:10px;border:1px solid #ddd;border-radius:10px}
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
  <label>Membertype (demandé)</label>
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
# Endpoint#04 Mentor application (menu card)
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
# Endpoint#05 Mon groupe (menu card)
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
 input{width:80%;padding:10px;border:1px solid #ddd;border-radius:10px}
 .row{display:flex;gap:10px;margin-top:12px}
 .btn{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#111;color:#fff;cursor:pointer}
 .btn2{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#fff;color:#111;cursor:pointer}
 .msg{margin-top:12px;padding:10px;border-radius:12px}
 .ok{background:#eaffea;border:1px solid #b8ffb8}
 .err{background:#ffe9ea;border:1px solid #ffb3b8}
</style></head><body><div class="wrap">
<h2>Créer un membre</h2>

<!-- <p><a href="{{ url_for('home') }}">← Retour</a></p> -->

<div class="card">
<form method="post">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  
  <label>Identifiant du nouveau membre (nº de téléphone sans prefixe)</label>
  <input name="phone" 
       placeholder="Exemple: 998889560" 
       required 
       pattern="^[^0\\+].*" 
       title="Le numéro ne doit pas commencer par 0 ou +">
  
  <label>Nom</label><input name="lastname" required>
  <label>Prénom</label><input name="firstname" required>
  <label>Date naissance (JJ/MM/AAAA)</label><input name="birthdate" required>
  <label>Identifiant du bénéficiaire</label><input name="beneficiaire" placeholder="Exemple: 998889560" size="10" required>
  <label>Adresse de domicile</label><input name="adresse" required>

  <label>Mot de passe</label><input name="password" type="password" required>

  <div class="row">
    <button class="btn" type="submit">Créer</button>
    <a class="btn2" href="{{ url_for('home') }}">Annuler</a>
  </div>
  {% if message %}<div class="msg {{ 'err' if is_error else 'ok' }}">{{ message }}</div>{% endif %}
</form>

</div>

<div class="footer">
    <a href="{{ url_for('login') }}" class="btn-back">← Retour à la connexion</a>
</div>

</div></body></html>
"""
# Endpoint#06 Créer un membre (menu card)
from flask import request, session, render_template_string
from datetime import datetime
import psycopg # ou psycopg2 selon ta config

@app.route("/add_member", methods=["GET", "POST"])
#@mentor_required
def add_member():
    if request.method == "POST":
        try:
            # 1. Récupération des données du formulaire
            phone = (request.form.get("phone") or "").strip()
            lastname = (request.form.get("lastname") or "").strip()
            firstname = (request.form.get("firstname") or "").strip()
            birthdate_str = (request.form.get("birthdate") or "").strip()
            beneficiaire = (request.form.get("beneficiaire") or "").strip()
            adressse = (request.form.get("adresse") or "").strip()
            password = (request.form.get("password") or "").strip()

            # 2. Validation du numéro de téléphone (Sécurité Python)
            if phone.startswith("0") or phone.startswith("+"):
                return render_template_string(ADD_MEMBER_PAGE, 
                    message="Erreur : Le numéro ne doit pas commencer par 0 ou +243.", 
                    is_error=True)

            # 3. Conversion de la date
            birthdate = datetime.strptime(birthdate_str, "%d/%m/%Y").date()

            # 4. Préparation des variables automatiques
            if not session.get("user"):
                mentor = 'admin'
                updateuser = 'admin'
            else:
                mentor = session.get("user")
                updateuser = session.get("user")

            membertype = "independant"
            statut = "inactif"
            membershipdate = datetime.strptime("31/12/2099", "%d/%m/%Y").date()

            # 5. Appel de ta fonction d'insertion (assure-toi qu'elle utilise %s)
            insert_member(phone, membertype, mentor, lastname, firstname, birthdate, 
                          None, statut, updateuser,beneficiaire, adressse, password, membershipdate)
            
            return render_template_string(ADD_MEMBER_PAGE, 
                message=f"Succès : {firstname} {lastname} a été créé.", 
                is_error=False)

        except Exception as e:
            return render_template_string(ADD_MEMBER_PAGE, 
                message=f"Erreur d'enregistrement : {e}", 
                is_error=True)

    # Affichage normal de la page (GET)
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

#---------------------------------------------------------------
# Endpoint#07 Importer cotisations (menu card)
#---
import re
from datetime import date

FR_MONTHS = {
    "janv": 1, "jan": 1, "janvier": 1,
    "fevr": 2, "févr": 2, "fev": 2, "fév": 2,"feb": 2,
    "mars": 3, "mar": 3, "març": 3,
    "avr": 4, "avril": 4, "apr": 4,
    "mai": 5, "may": 5, 
    "juin": 6, "jun": 6,
    "juil": 7, "juillet": 7,"jul": 7, "juil": 7, "july"
    "aout": 8, "août": 8, "aug": 8,
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
    
    # CONTRIBUTION ATTENDUE ACTUELLE: à partir de la table id_data et du champ "C" (contribution minimale) pour appliquer la règle d'inactivité
    stats=fetch_dashboard_stats()
    contribution_minimum = stats["C"]

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
        created = 0

        with get_conn() as conn:
            with conn.cursor() as cur:
                for row in reader:
                    # Ignorer les lignes totalement vides
                    if not any(row.values()):
                        continue
                    #log.info("contenu de 'row' dans le reader=%s", row)                   
                    try:
                        phone = (row.get("phone") or "").strip()
                        firstname = (row.get("firstname") or "").strip()
                        lastname = (row.get("lastname") or "").strip()
                        debitcredit = (row.get("debitcredit") or "").strip().upper()  # 'D' / 'C'
                        reference = (row.get("reference") or "").strip()
                        regie = (row.get("regie") or "").strip()

                        #amount = float((row.get("amount") or "0").strip())
                        amount_raw = (row.get("amount") or "0").strip().replace(",", ".")
                        amount = float(amount_raw)

                        # TODO: parse date selon votre format (mvt_date)
                        #mvt_date = row.get("mvt_date")  # à parser si nécessaire
                        mvt_date = parse_date_fr(row.get("date") or "")
                        #mouvem_date = datetime.strptime(mvt_date, "%d/%m/%Y").date()
                        #mvt_date=mouvem_date
                        #libelle = (row.get("reference") or "").strip()
                        libelle="Transfert Mobile Money du %s - %s" % (mvt_date, reference)
                        updatedate=date.today()

                        #log.info("contenu de 'amount' formaté=%s", amount)  
                        #log.info("contenu de 'mvt_date' formaté=%s", mvt_date)  
                        #log.info("contenu de 'updatedate' formaté=%s", updatedate)  

                        if not phone or debitcredit not in ("D", "C"):
                            skipped += 1
                            log.warning("Ligne ignorée (phone ou debitcredit invalide): %s", phone)
                            continue

                        # 0) Vérifier que le membre existe
                        cur.execute("SELECT phone FROM membres WHERE phone = %s", (phone,))
                        if not cur.fetchone(): 
                            create_member_minimal(cur, phone, firstname, lastname)
                            created += 1
                            #log.info("Membre créé automatiquement pour phone=%s", phone)
                           
                        # 1) insert mouvement
                        # Vérifier que la référence n'existe pas déjà (pour éviter les doublons en cas de réimport du même fichier)
                        cur.execute("SELECT 1 FROM mouvements WHERE reference = %s", (reference,))
                        if cur.fetchone():
                            log.warning("Référence déjà existante : %s", reference)
                            skipped += 1
                            continue

                        #insertion :
                        cur.execute("""
                          INSERT INTO mouvements (phone, firstname, lastname, mvt_date, amount, debitcredit,reference,updatedate,libelle,updated_by,regie)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """, (phone, firstname, lastname, mvt_date, amount, debitcredit, reference,date.today(),libelle,session.get("user"), regie))
                        #log.info("Mouvement inséré pour phone=%s, amount=%s, debitcredit=%s, reference=%s", phone, amount, debitcredit, reference)
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
                          WHERE phone = %s AND balance <  %s AND currentstatute in ('actif','probatoire')   
                        """, (session.get("user"), phone, contribution_minimum))
                        if cur.rowcount:
                            flagged_inactif += 1

                        # 4) règle demandée: si phone du mouvement = phone d'un membre ET balance >= C (contribution minimale) ET membershipdate = "31/12/2099" alors membershipdate = date du jour (=> activation du membre)
                        limit_date = datetime.strptime("31/12/2099", "%d/%m/%Y").date()
                        cur.execute("""
                            UPDATE membres
                            SET membershipdate = CURRENT_DATE,
                                currentstatute = 'probatoire',
                                updatedate = CURRENT_DATE,
                                updateuser = %s 
                            WHERE phone = %s AND balance >= %s AND membershipdate = %s 
                        """, (session.get("user"), phone, contribution_minimum, limit_date))
#####
                        # 5) règle demandée: si phone du mouvement = phone d'un membre ET balance >= C (contribution minimale) ET membershipdate <> "31/12/2099" alors currentstatute ='probatoire' ou 'actif' (=> activation du membre)
                        limit_date = datetime.strptime("31/12/2099", "%d/%m/%Y").date()
                        cur.execute("""
                            UPDATE membres
                            SET currentstatute = CASE 
                                WHEN phone = %s AND balance >= %s AND membershipdate <> %s THEN 'probatoire'
                                ELSE currentstatute
                            END
                            WHERE phone = %s;
                        """, (phone, contribution_minimum, limit_date, phone))

                        # 6) règle demandée: si phone du mouvement = phone d'un membre ET balance >= C (contribution minimale) ET la durée entre aujourdhui et membershipdate superieure ou egale à 3 mois (=> currentstatute='actif')
                        limit_date = datetime.strptime("31/12/2099", "%d/%m/%Y").date()
                        cur.execute("""
                            UPDATE membres
                            SET currentstatute = CASE 
                                WHEN phone = %s AND balance >= %s AND (
                                EXTRACT(YEAR FROM age(CURRENT_DATE, membershipdate)) * 12 +
                                EXTRACT(MONTH FROM age(CURRENT_DATE, membershipdate))
                            ) >= 3 THEN 'actif'
                                ELSE currentstatute
                            END
                            WHERE phone = %s;
                        """, (phone, contribution_minimum, phone))

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
            f"- Nouveaux membres detectés: {created}\n"
            f"- Total lignes traitées: {inserted + skipped}\n"
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
 body{font-family:Arial;margin:20px} .wrap{max-width:1400px;margin:0 auto}
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
<thead><tr><th>ID</th><th>Phone</th><th>Nom</th><th>Date</th><th>Montant</th><th>D/C</th><th>Libellé</th><th>Regie</th><th>Action</th></tr></thead>
<tbody>
{% for r in rows %}
<tr>
<form method="post" action="{{ url_for('check_mouvements_update', mvt_id=r[0]) }}">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <td>{{ r[0] }}</td>
  <td><input name="phone" value="{{ r[1] }}" size="12"></td>
  <td>{{ r[2] }}</td>
  <td><input name="mvt_date" value="{{ r[3].strftime('%d/%m/%Y') }}" size="8"></td>
  <td><input name="amount" value="{{ r[4] }}" size="8"></td>
  <td>
    <select name="debitcredit">
      <option value="D" {{ 'selected' if r[5]=='D' else '' }}>D</option>
      <option value="C" {{ 'selected' if r[5]=='C' else '' }}>C</option>
    </select>
  </td>
  
  <td><input name="libelle" value="{{ r[7] }}" size="20"></td>
  <td><input name="regie" value="{{ r[10] }}" size="8"></td>
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
# Endpoint#08 Check mouvements (menu card)
@app.get("/checkmouvements")
@admin_required
def check_mouvements():
    rows = list_all_mouvements()
    return render_template_string(CHECK_MVT_PAGE, rows=rows)

@app.post("/checkmouvements/update/<int:mvt_id>")
@admin_required
def check_mouvements_update(mvt_id: int):
    phone = (request.form.get("phone") or "").strip()
    d = datetime.strptime((request.form.get("mvt_date") or "").strip(), "%d/%m/%Y").date()
    amount = float((request.form.get("amount") or "0").strip())
    dc = (request.form.get("debitcredit") or "D").strip()
    #ref = (request.form.get("reference") or "").strip()
    libelle = (request.form.get("libelle") or ref).strip()  # ou tu peux ajouter un champ libellé dans le form si tu veux
    regie = (request.form.get("regie") or "").strip()
    update_mouvement(mvt_id,phone,d, amount, dc, libelle, regie)
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
    input, select { padding: 10px; width: 80%; box-sizing: border-box; border:1px solid #ccc; border-radius: 8px; }
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
    <div style="display:flex; justify-content:space-between; align-items:center;">
        <h2 style="margin:0;">KM-Kimya. . . . . . . . . . .Suivi des membres et des comptes de gestion</h2>
        <a href="{{ url_for('home') }}">← Retour</a>
    </div>
  </div>  

<div class="card" style="margin-top:0px; padding:12px;">
    <!-- GRILLE PRINCIPALE : On passe à 2 colonnes puis 3 colonnes-->
    <!-- <div class="grid" style="display: grid; grid-template-columns: 1fr 1fr; align-items: end; gap: 20px;"> -->
    <!-- Remplacer la ligne <div class="grid"> par celle-ci -->
    <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px;">
 
        <!-- CADRAN 1 contient BLOC 1 -->
        <div style="border: 1px solid #ddd; padding: 15px; border-radius: 8px;">
            <form method="get" action="{{ url_for('search_member') }}">
                
                <!-- DIV DE REGROUPEMENT EN LIGNE -->
                <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 15px;">
                    <label style="white-space: nowrap;">Chercher par identifiant : </label>
                    <input name="q_phone" placeholder="Ex.998886955" value="{{ q_phone or '' }}" style="flex-grow: 1;">
                </div>

                <div style="display: flex; gap: 10px;">
                    <button class="btn" type="submit">Vérifier</button>
                    <a class="btn secondary" href="{{ url_for('datageneralfollowup') }}">Réinitialiser</a>
                </div>
            </form>
        </div>

        <!-- CADRAN 2 : Contient le BLOC 2 , 3 et 4 -->
        <div style="border: 1px solid #ddd; padding: 15px; border-radius: 8px; display: flex; flex-direction: column; gap: 10px;">
            
            <!-- BLOC 2 -->
            <form action="{{ url_for('parametrage') }}" method="POST">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <button type="submit" class="btn btn-primary" style="width: 100%;" onclick="return confirm('...')">
                    🔄 Actualiser les paramètres
                </button>
            </form>

            <!-- BLOC 3 -->
            <form action="{{ url_for('launch_statutes_update') }}" method="POST">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <button type="submit" class="btn btn-primary" style="width: 100%;" onclick="return confirm('...')">
                    🔄 Actualiser les Statuts
                </button>
            </form>

            <!-- BLOC 4 -->
            <form action="{{ url_for('gestion_comptes') }}" method=["GET","POST"]>
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <button type="submit" class="btn btn-primary" style="width: 100%;" onclick="return confirm('...')">
                    🔄 Gestion des comptes
                </button>
            </form>

        </div>
    </div>

    <!-- Zone d'alerte (en dessous de la grille) -->
    {% with messages = get_flashed_messages(with_categories=true) %}

        <!-- Utilisation des messages flash de Flask -->    
    {% if messages %}
        {% for category, message in messages %}
        <div class="msg {{ 'err' if category == 'danger' else 'ok' }}" 
            style="padding:10px; margin-top:20px; border-radius:4px; 
                    background: {{ '#f8d7da' if category == 'danger' else '#d4edda' }}; 
                    color: {{ '#721c24' if category == 'danger' else '#155724' }};">
            {{ message }}
        </div>
        {% endfor %}
    {% endif %}

    {% endwith %}
</div>


  {% if edit_row %}
  <div class="card">
    <!-- <h18 style="margin-top:0;">Adhérent d'index n° {{ edit_row[0] }}</h18> -->
    <form method="post" action="{{ url_for('update', member_id=edit_row[0]) }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="grid">

        <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px;">

        <div>
          <label>Identifiant </label>
          <input name="phone" value="{{ edit_row[1] }}" required>
        </div>

        <div>
          <label>Type d'adhérent</label>
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
          <label>Nom de famille</label>
          <input name="lastname" value="{{ edit_row[4] }}" required>
        </div>

        <div>
          <label>Prénom</label>
          <input name="firstname" value="{{ edit_row[5] }}" required>
        </div>

        <div>
          <label>Date naissance</label>
          <input name="birthdate" value="{{ edit_birthdate }}" required>
        </div>

        <div>
          <label>Solde </label>
          <input name="balance" value="{{ edit_balance }}" required>
        </div>

        <div>
          <label>Date adhésion</label>
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
          <label>Nouveau mot de passe (optionnel)</label>
          <input name="password" type="password" placeholder="laisser vide pour ne pas changer">
        </div>
      </div>

      <div class="row">
        <button class="btn" type="submit">Enregistrer</button>
        <a class="btn secondary" href="{{ url_for('datageneralfollowup') }}" style="display:inline-flex;align-items:center;justify-content:center;">Annuler</a>
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

# Endpoint#09 Data general follow-up (menu card)
@app.get("/datageneralfollowup")
@admin_required
def datageneralfollowup():
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
        if new_pwd : 
            #log.info("Le mot de passe a été modifié de ? à %s", new_pwd)  
            update_member(
                member_id=member_id,
                phone=data["phone"],
                membertype=data["membertype"],
                mentor=data["mentor"],
                lastname=data["lastname"],
                firstname=data["firstname"],
                birthdate_date=data["birthdate"],           # <= important (clé = "birthdate")
                membershipdate=data["membershipdate"],      # <= important
                balance=data["balance"],                    # <= important
                currentstatute=data["currentstatute"],
                updateuser=updateuser,
                new_password_plain=new_pwd,
            )
        else:
            #log.info("Le mot de passe n'a pas été modifié.")
            update_member(
                member_id=member_id,
                phone=data["phone"],
                membertype=data["membertype"],
                mentor=data["mentor"],
                lastname=data["lastname"],
                firstname=data["firstname"],
                birthdate_date=data["birthdate"],           # <= important (clé = "birthdate")
                membershipdate=data["membershipdate"],      # <= important
                balance=data["balance"],                    # <= important
                currentstatute=data["currentstatute"],
                updateuser=updateuser,
                new_password_plain=None
            )

        return redirect(url_for("datageneralfollowup"))

    except Exception as e:
        rows = fetch_all_membres()
        row = fetch_one(member_id)
        edit_birthdate = row[6].strftime("%d/%m/%Y") if row and row[6] else ""
        edit_membershipdate = row[14].strftime("%d/%m/%Y") if row and row[14] else ""
        edit_balance = float(row[10]) if row and row[10] is not None else 0.0

        return render_template_string(
            DATAGENERALFOLLOWUP_PAGE,
            rows=rows,
            edit_row=row,
            edit_birthdate=edit_birthdate,
            edit_membershipdate=edit_membershipdate,
            edit_balance=edit_balance,
            message=f"Erreur: {str(e)}",
            is_error=True,
            member_types=MEMBER_TYPES,
            statutes=STATUTES,
        )

#suppression d'adhérent (pas l'admin par défaut)
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


@app.get("/datageneralfollowup/search")
@admin_required
def search_member():
    q_phone = (request.args.get("q_phone") or "").strip()

    # si champ vide -> retour page normale
    if not q_phone:
        rows = fetch_all_membres()
        return render_template_string(
            DATAGENERALFOLLOWUP_PAGE,
            rows=rows,
            edit_row=None,
            edit_birthdate="",
            edit_membershipdate="",
            edit_balance=0.0,
            message="Veuillez saisir un phone.",
            is_error=True,
            member_types=MEMBER_TYPES,
            statutes=STATUTES,
            q_phone=q_phone,
        )

    rows = fetch_member_by_phone_like(q_phone)

    # 0 résultat
    if not rows:
        all_rows = fetch_all_membres()
        return render_template_string(
            DATAGENERALFOLLOWUP_PAGE,
            rows=all_rows,
            edit_row=None,
            edit_birthdate="",
            edit_membershipdate="",
            edit_balance=0.0,
            message=f"Aucun membre trouvé pour: {q_phone}",
            is_error=True,
            member_types=MEMBER_TYPES,
            statutes=STATUTES,
            q_phone=q_phone,
        )

    # 1 seul résultat -> ouvrir directement l'écran Edit (optionnel mais pratique)
    if len(rows) == 1:
        return redirect(url_for("edit", member_id=rows[0][0]))

    # plusieurs résultats -> afficher table filtrée
    return render_template_string(
        DATAGENERALFOLLOWUP_PAGE,
        rows=rows,
        edit_row=None,
        edit_birthdate="",
        edit_membershipdate="",
        edit_balance=0.0,
        message=f"{len(rows)} résultat(s) pour: {q_phone}",
        is_error=False,
        member_types=MEMBER_TYPES,
        statutes=STATUTES,
        q_phone=q_phone,
    )

@app.post("/statutes_update")
@login_required
def launch_statutes_update():
    updateuser = session.get("user") or ADMIN_PHONE
    #C = fetch_dashboard_stats()["C"]
    from decimal import Decimal
    C = Decimal(str(fetch_dashboard_stats()["C"]).replace(" ", ""))
    limit_date = datetime.strptime("31/12/2099", "%d/%m/%Y").date()
    #log.info("Début de l'actualisation des statuts. Seuil Cotisation=%s, date limite=%s", C, limit_date)

    statutes_update()
    #log.info("Actualisation des statuts terminée. %s statut(s) mis à jour.", rows_updated)
    #flash(f"{rows_updated} statut(s) mis à jour avec succès", "success")
    flash("statut(s) mis à jour avec succès", "success")

    return redirect(url_for("datageneralfollowup"))


# --------------------------------------------------------------------------------------
# Endpoint #10 — Transfert de cotisations (débit/crédit + blocage si solde insuffisant)
#---------------------------------------------------------------------------------------
TRANSFER_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transfert de cotisations</title>
<style>
 body{font-family:Arial;margin:20px} .wrap{max-width:800px;margin:0 auto}
 .card{border:1px solid #e7e7e7;border-radius:16px;padding:16px}
 label{display:block;margin:10px 0 4px;font-weight:700}
 input{width:80%;padding:10px;border:1px solid #ddd;border-radius:10px}
 .row{display:flex;gap:10px;margin-top:12px}
 .btn{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#111;color:#fff;cursor:pointer}
 .btn2{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#fff;color:#111;cursor:pointer}
 .btn3{padding:10px 14px;border-radius:12px;border:1px solid #111;background:#000;color:#fff;cursor:pointer}
 .msg{margin-top:12px;padding:10px;border-radius:12px}
 .ok{background:#eaffea;border:1px solid #b8ffb8}
 .err{background:#ffe9ea;border:1px solid #ffb3b8}
</style></head><body><div class="wrap">
<h2>Transfert de crédit, cotisations et dons</h2>
<p><a href="{{ url_for('home') }}">← Retour</a></p>

<br>
<h3>Transfert de crédit à un autre membre : </h3>
<div class="card">
<form method="post" style="display: flex; align-items: center; gap: 10px; flex-wrap: nowrap;">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  
  <label>Bénéficiaire:</label>
  <input name="to_phone" placeholder="nº tél ex. 998243554"  value="{{ to_phone or '' }}" required style="width: 120px;">
  
  <label for="amount">Montant:</label>
  <input id="amount" name="amount" type="number" value="{{ amount or 0 }}" step="0.01" min="0" required style="width: 80px;">

  <button class="btn" name="action" value="check" type="submit">Vérifier</button>
  <button style="background-color: lightgreen; color: black;" class="btn2" name="action" value="confirm" type="submit">Confirmer</button>
</form>

{# Gardez les messages d'erreur en dessous si nécessaire #}
{% if found_name or to_phone or message %}
  <div style="margin-top: 10px;">
    {% if found_name %}<b style="color: green;">{{ found_name }}</b>{% endif %}
    {% if message %}<span class="msg">{{ message }}</span>{% endif %}
  </div>
{% endif %}
</div>

<br>
<hr>
<h3>Paiement de cotisation régulière de membre : </h3>
<div class="card">
<form method="post" style="display: flex; align-items: center; gap: 10px; flex-wrap: nowrap;">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  
  <label for="cotisation">Montant:</label>
  <input id="cotisation" name="cotisation" type="number" value="{{ cotisation or 0 }}" step="0.01" min="0" required style="width: 80px;">
  < button style="background-color: lightgreen; color: black;" class="btn2" name="action" value="confirm" type="submit">Confirmer</button>
</form>
</div>

<br>
<hr>
<h3>Donation à l'Association KM-Kimya : </h3>
<div class="card">
<form method="post" style="display: flex; align-items: center; gap: 10px; flex-wrap: nowrap;">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  
  <label for="donation">Montant:</label>
  <input id="donation" name="donation" type="number" value="{{ donation or 0 }}" step="0.01" min="0" required style="width: 80px;">
  <button style="background-color: lightgreen; color: black;" class="btn3" name="action" value="confirm" type="submit">Confirmer</button>
</form>
</div>
<br>

</div></body></html>
"""
#
# Endpoint#10 Transfert de cotisations (menu card)
# Note: on peut faire un seul endpoint pour les 2 actions "check" et "confirm" (simplification) car la logique de vérification est la même dans les 2 cas, et on affiche les messages d'erreur/confirmation dans la même page. Donc pas besoin de faire 2 endpoints séparés.
@app.route("/transfer", methods=["GET","POST"])
@login_required
def transfer():
    message, is_error = "",""
    
    from_phone = session["user"]
    to_phone = (request.form.get("to_phone") or "").strip()

    amount = float((request.form.get("amount") or "0").strip())
    cotisation = float((request.form.get("cotisation") or "0").strip())
    donation = float((request.form.get("donation") or "0").strip())

    my_balance = 0
    found_name = ""

    if request.method == "POST":
        if amount > 0 and not to_phone:
            return render_template_string(TRANSFER_PAGE, balance=my_balance, message="Veuillez saisir le numéro de téléphone du bénéficiaire.", is_error=True)
        
        m = fetch_member_by_phone(to_phone) if to_phone else None
        if not m:
            return render_template_string(TRANSFER_PAGE, balance=my_balance, message="Bénéficiaire introuvable", is_error=True)
        else:
            found_name = f"{m[5]} {m[4]}"
            #log.info("Membre bénéficiaire trouvé: %s (balance=%s)", found_name, my_balance)


        m = fetch_member_by_phone(from_phone) if from_phone else None
        if m:
            my_balance = m[10] if m else 0

        action = request.form.get("action")

        if action == "confirm":
            if not m:
                return render_template_string(TRANSFER_PAGE, balance=my_balance, message="Données introuvables", is_error=True)

            if amount <= 0:
                return render_template_string(TRANSFER_PAGE, balance=my_balance, message="Montant invalide.", is_error=True)

            if my_balance < amount:
                return render_template_string(TRANSFER_PAGE, balance=my_balance, message=f"Solde insuffisant: transfert bloqué. Solde actuel: {my_balance}", is_error=True)

            try:
                d = datetime.strptime(date.today().strftime("%d/%m/%Y"), "%d/%m/%Y")
                ref = f"DC-{uuid.uuid4().hex[:10]}"

                create_transfert(from_phone, to_phone, amount, ref,d)

                message, is_error = "contribution transférée.", False
                to_phone, amount, found_name = "", 0.0, ""
            except Exception as e:
                message, is_error = f"Erreur: {e}", True
                log.exception("Erreur lors de l'enregistrement du mouvement de transfert: %s", e)

        if cotisation > 0:
            if action == "confirm":
                if not m:
                    return render_template_string(TRANSFER_PAGE, balance=my_balance, message="Données introuvables", is_error=True)

                if cotisation <= 0:
                    return render_template_string(TRANSFER_PAGE, balance=my_balance, message=" Montant de cotisation invalide.", is_error=True)

                if my_balance < cotisation:
                    return render_template_string(TRANSFER_PAGE, balance=my_balance, message=f"Solde insuffisant: transfert bloqué. Solde actuel: {my_balance}", is_error=True)

            try:
                d = datetime.strptime(date.today().strftime("%d/%m/%Y"), "%d/%m/%Y")
                ref = f"COT-{uuid.uuid4().hex[:10]}"
                create_cotisation(from_phone, cotisation, ref, d)
                message, is_error = "Cotisation enregistrée. Merci pour votre soutien !", False
            except Exception as e:
                message, is_error = f"Erreur: {e}", True
                log.exception("Erreur lors de l'enregistrement du mouvement de cotisation: %s", e)

        if donation > 0:
            if action == "confirm":
                if not m:
                    return render_template_string(TRANSFER_PAGE, balance=my_balance, message="Données introuvables", is_error=True)

                if donation <= 0:
                    return render_template_string(TRANSFER_PAGE, balance=my_balance, message=" Montant de donation invalide.", is_error=True)

                if my_balance < donation:
                    return render_template_string(TRANSFER_PAGE, balance=my_balance, message=f"Solde insuffisant: transfert bloqué. Solde actuel: {my_balance}", is_error=True)

            try:
                d = datetime.strptime(date.today().strftime("%d/%m/%Y"), "%d/%m/%Y")
                ref = f"DON-{uuid.uuid4().hex[:10]}"
                create_donation(from_phone, donation, ref, d)
                message, is_error = "Donation enregistrée. Merci pour votre générosité !", False
            except Exception as e:
                message, is_error = f"Erreur: {e}", True
                log.exception("Erreur lors de l'enregistrement du mouvement de donation: %s", e)                

    return render_template_string(TRANSFER_PAGE, found_name=found_name, to_phone=to_phone, amount=amount,message=message, is_error=is_error)


# ------------------------------------------
# Endpoint #11 — Suivi des deuils pendants
#-------------------------------------------
DEUILS_PENDANTS_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Suivi des declarations de décès</title>
<style>
 body{font-family:Arial;margin:20px} .wrap{max-width:1400px;margin:0 auto}
 table{width:100%;border-collapse:collapse}
 th,td{padding:10px;border-bottom:1px solid #eee;text-align:left}
 th{background:#f6f6f6}
 input,select{padding:8px;border:1px solid #ddd;border-radius:10px}
 .btn{padding:7px 10px;border:1px solid #111;border-radius:10px;background:#111;color:#fff;cursor:pointer}
 .btn2{padding:7px 10px;border:1px solid #111;border-radius:10px;background:#fff;color:#111;cursor:pointer}
</style></head><body><div class="wrap">
<h2>Deuils pendants</h2>
<p><a href="{{ url_for('home') }}">← Retour</a></p>
<table>
<thead><tr><th>ID</th><th>Identifiant du défunt</th><th>Date de décès</th><th>déclaré par</th><th>Statut</th><th>Action</th></tr></thead>
<tbody>
{% for r in rows %}
<tr>
  <td>{{ r[0] }}</td>
  <td>{{ r[1] }}</td>
  <td>{{ r[2] }}</td>
  <td>{{ r[3] }}</td>

  <td>
    <form method="post" action="{{ url_for('deuils_pendants_update', id=r[0]) }}" style="display:inline;">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <select name="statut" required>
        <option value="déclaré" {{ 'selected' if r[6]=='déclaré' else '' }}>déclaré</option>
        <option value="validé" {{ 'selected' if r[6]=='validé' else '' }}>validé</option>
        <option value="non-éligible" {{ 'selected' if r[6]=='non-éligible' else '' }}>non-éligible</option>
        <option value="comptabilisé" {{ 'selected' if r[6]=='comptabilisé' else '' }}>comptabilisé</option>
      </select>
      <button class="btn" type="submit">Save</button>
    </form>
  </td>

  <td>
    {% if r[6] == "validé" %}
      <form method="post"
            action="{{ url_for('trigger_prestation', deces_id=r[0]) }}"
            onsubmit="return confirm('Confirmer le déclenchement comptable ?');"
            style="display:inline;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <button class="btn" type="submit">Déclencher la prestation décès</button>
      </form>
    {% endif %}
  </td>
</tr>
{% endfor %}

{% if not rows %}
<tr><td colspan="6">Aucun décès pendant.</td></tr>
{% endif %}
</tbody>
</table>
</div></body></html>
"""
#
# Endpoint#11 — Suivi des deuils pendants
#
@app.route("/deuils_pendants", methods=["GET", "POST"])
@admin_required
def deuils_pendants():
    rows = list_deces_pendants()
    return render_template_string(DEUILS_PENDANTS_PAGE, rows=rows)

@app.post("/deuils_pendants/update/<int:id>")
@admin_required
def deuils_pendants_update(id: int):
    statut = (request.form.get("statut") or "déclaré").strip()
    #ref = (request.form.get("reference") or "").strip()

    #log.info("Tentative de mise à jour du statut du décès, index: %d, statut: %s, erreur: %s", id, statut, "Aucune erreur détectée")  # Log initial avant validation

    if request.method == "POST":
       try :
         if statut not in ("déclaré", "validé", "non-éligible", "comptabilisé"):
            raise ValueError("Statut invalide.")

         #log.info("demarrage de la mise à jour du statut du décès, index: %d, statut: %s, erreur: %s", id, statut, "Aucune erreur détectée")

         update_deces(id, statut)
         message, is_error = "Statut mis à jour OK.", False
       except Exception as e:
         message, is_error = f"Erreur: {e}", True
         log.exception("Erreur lors de la mise à jour du statut du décès: %s", e)
    return redirect(url_for("deuils_pendants"))

@app.post("/deces/prestation/<int:deces_id>")
@admin_required
def trigger_prestation(deces_id):

    with get_conn() as conn:
        with conn.cursor() as cur:
            stats=fetch_dashboard_stats()
            prestation = stats["P"]

            cur.execute("""
                UPDATE deces
                SET prestation=%s
                WHERE id=%s
            """,(prestation, deces_id))
            conn.commit()

            cur.execute("""
                SELECT phone, prestation, statut
                FROM deces
                WHERE id=%s
            """,(deces_id,))
            conn.commit()
            row = cur.fetchone()

            if not row:
                abort(404)

            #log.info("Données du décès pour déclenchement prestation, index: %d, phone: %s, prestation: %s, statut: %s", deces_id, row[0], row[1], row[2])

            phone = row[0]
            prestation = float(row[1])
            statut = row[2]

            if statut == "comptabilisé":
                return redirect(url_for("deuils_pendants"))

            if statut != "validé":
                raise ValueError("Le décès doit être validé avant comptabilisation.")
            
            # L'adhérent est radié (statut "radié") et ne peut plus faire de mouvement, mais on garde son historique et ses données pour l'historique et les stats
            #log.info("L'adhérant avec phone %s va être radié suite à la confirmation de son décès.", phone)
            cur.execute("""
             UPDATE membres
             SET currentstatute=%s
             WHERE phone=%s
             """,('radié', phone))
            conn.commit()
            #log.info("L'adhérant avec phone %s radié suite à la confirmation de son décès.", phone)

    create_prestation_mouvements(phone, prestation)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            UPDATE deces
            SET statut='comptabilisé'
            WHERE id=%s or phone=%s
            """,(deces_id, row[0]))
        conn.commit()

    return redirect(url_for("deuils_pendants"))


# ----------------------------------------------------------------------------------------------------------------------------
#   Endpoint #12 — Historique des décès (lecture seule de tous les décès traités, avec prestation versée et nom à l'affichage)
# ----------------------------------------------------------------------------------------------------------------------------
DECES_HISTORY_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Historique des décès</title>
<style>
 body{font-family:Arial;margin:20px} .wrap{max-width:1100px;margin:0 auto}
 .pill{display:inline-block;padding:6px 10px;border:1px solid #ddd;border-radius:999px;background:#fafafa;margin-bottom:10px}
 table{width:100%;border-collapse:collapse}
 th,td{padding:10px;border-bottom:1px solid #eee;text-align:left}
 th{background:#f6f6f6}
</style></head><body><div class="wrap">
  <h2>Historique des décès</h2>
  <p><a href="{{ url_for('home') }}">← Retour</a></p>
  
  <table>
    <thead><tr>
      <th>Date de décès</th><th>phone</th><th>Prénom</th><th>Nom de famille</th><th>Dernier statut</th><th>prestation</th>
    </tr></thead>
    <tbody>
    {% for r in rows %}
      <tr>
        <td>{{ r[3].strftime('%d/%m/%Y') }}</td>  
        <td>{{ r[0] }}</td>
        <td>{{ r[1] }}</td>
        <td>{{ r[2] }}</td>
        <td>{{ r[4] }}</td>
        <td>{{ r[5] }}</td>
      </tr>
    {% endfor %}
    {% if not rows %}<tr><td colspan="4">Aucun décès traité.</td></tr>{% endif %}
    </tbody>
  </table>
</div></body></html>
"""
# Endpoint#12 Historique des décès (menu card)
@app.get("/deces_history")
@login_required
def deces_history():
    rows = list_deces_traites()
    return render_template_string(DECES_HISTORY_PAGE, rows=rows)
#
# ---------------------------------------------------------------------------   
# Endpoint #13 — Texte presentation Association et Methodologie de travail
# ---------------------------------------------------------------------------
INFOS_ASSOCIATION_PAGE = """
<!doctype html>
<html lang="fr">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>À propos - Notre Association</title>
    <style>
        body { font-family: Arial, sans-serif; line-height: 1.6; margin: 0; padding: 20px; background: #f9f9f9; color: #333; }
        .container { max-width: 800px; margin: 0 auto; background: #fff; padding: 30px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        h1 { color: #111; border-bottom: 2px solid #eee; padding-bottom: 10px; }
        h2 { color: #444; margin-top: 25px; }
        p { margin-bottom: 15px; text-align: justify; }
        .footer { margin-top: 30px; border-top: 1px solid #eee; padding-top: 20px; }
        .btn-back { display: inline-block; padding: 10px 20px; background: #111; color: #fff; text-decoration: none; border-radius: 8px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Notre Association</h1>
        
        <h2>Notre Mission</h2>
        <p>
            Bienvenue au sein de notre communauté. Notre association a pour mission principale de rassembler les forces vives afin de promouvoir le développement et l'entraide entre tous les membres. Fondée sur des valeurs de solidarité, nous travaillons quotidiennement à la création d'un réseau solide où chaque adhérent trouve sa place et contribue à l'essor collectif.
        </p>
        <p>
        KM-KIMYA est une association solidaire engagée dans la réduction du choc économique lié aux funérailles par la mutualisation de petites contributions financières et le versement rapide d'une aide significative à la famille éprouvée. KM-Kimya symbolise le départ dans la paix et la sereinité tel que revé dans la tradition bantoue du bassin du Congo KM = Kuenda Mbote - Kuya Mimpe - Kokende Malamu - Kwenda Muzuri.
        </p>

        <h2>Méthodologie de travail</h2>
        <p>
            Nous utilisons des outils numériques modernes pour faciliter la communication et la gestion des données. Les mises à jour régulières de nos fichiers des membres nous permettent de maintenir une base de données active et dynamique, garantissant ainsi que : personne n'est laissé pour compte dans nos initiatives sociales.
        </p>
        <p>Pour adhérer il suffit d'envoyer votre contribution à la première prestation, équivalente à 5.50 $ par mobile-money dans un des numéros ci-dessous :</p>

        <ul>
            <li><strong>+243 824807663</strong> pour Mpesa</li>
            <li><strong>+243 999944459</strong> pour Airtel-money</li>
            <li><strong>+243 891273191</strong> pour Orange-money</li>
            <li><strong>+243 903077077</strong> pour Africell-money</li>
        </ul>

        <p>
            Après le paiement, la capture d'écran du reçu de paiement émis par le service de mobile-money avec votre nom complet constitue la preuve de paiement et/ou de votre d'adhésion le cas échéant. Cette démarche simple et accessible permet à chacun de rejoindre notre communauté et de bénéficier du soutien mutuel que nous offrons.
        </p>

        <p>
            Nous recommandons vivement aux membres residants en dehors de la RDC d'utiliser les services offerts par les réseaux de transfert d'argent internationaux comme <strong>REMITLY</strong> accessible officiellement sous l'URL <strong>www.remitly.com</strong> (pas ailleurs). Pour le cas de 'remitly' l'utilisation des destinations mobile-money :<strong> +243824807663 pour Mpesa et +243891273191 pour Orange-money</strong> sont efficaces ; le transfert est quasi instantané. Dans ce cas, n'oubliez pas d'accompagner votre transfert par un message téléphonique SMS, au numéro de destination, libellé comme suit : "<i>Pour KM-Kimya à partir de '<strong>NOM DU PAYS D'Où VOUS ENVOYEZ</strong>' en faveur de '<strong>IDENTIFIANT KM-KIMYA DU BENEFICAIRE</strong>'  Montant: '<strong>LE MONTANT ENVOYÉ</strong>' </i>" pour nous permettre de vous identifier correctement dans notre base de données.
        </p>


        <p>
            Notre approche repose sur une organisation rigoureuse divisée en plusieurs piliers : 
            l'identification, l'accompagnement et le suivi. Chaque nouveau membre est intégré 
            via un système de mentorat (mentor) qui assure une transmission fluide des valeurs 
            et des procédures de l'association.
        </p>
        <p>
           Cette association ne crée pas une nouvelle pratique. Elle structure et sécurise une valeur culturelle existante : la solidarité.
           L’objectif est de transformer une réaction émotionnelle ponctuelle en un mécanisme organisé, équitable et durable, au service de la dignité des familles et de la cohésion communautaire.
        </p>

        <div class="footer">
            <a href="{{ url_for('login') }}" class="btn-back">← Retour à la connexion</a>
        </div>
    </div>
</body>
</html>
"""

#
# -------------------------------------------------------   
# Endpoint #14 — Paramétrage des indicateurs de travail
# -------------------------------------------------------
#
PARAMETRAGE_PAGE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mise à jour des indicateurs de travail</title>
<style>
 body{font-family:Arial;margin:20px} .wrap{max-width:1100px;margin:0 auto}
 table{width:100%;border-collapse:collapse;margin-top:20px}
 th,td{padding:10px;border-bottom:1px solid #eee;text-align:left}
 th{background:#f6f6f6}
 input[type="number"], input[type="text"]{width:100%; padding:5px; border:1px solid #ccc; border-radius:4px;}
 .btn{background:#28a745; color:white; padding:8px 15px; border:none; border-radius:4px; cursor:pointer}
 .btn2{background:#dc3545; color:white; padding:8px 15px; border:none; border-radius:4px; cursor:pointer}
</style></head><body><div class="wrap">
  <h2>Indicateurs de travail</h2>
  <p><a href="{{ url_for('home') }}">← Retour</a></p>
  
  {% if rows %}

    <table>
      <thead>
        <tr>
          <th>Donnée clef</th><th>Description</th><th>Quantité</th><th>Note</th><th>Modifié par</th><th>Date modif.</th><th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr>
        <form method="POST" action="{{ url_for('update_parameters', rows=rows) }}">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                
          <td><input type="text" name="keydata" value="{{ r[0] }}" size="5" readonly></td>
          <td><input type="text" name="decript" value="{{ r[1] }}" size="10"></td>
          <td>
            <input type="number" name="quantity" 
                   value="{{ "%.2f"|format(r[2]|float) if r[2] else '0.00' }}" 
                   step="0.01">
          </td>
          <td><input type="text" name="note" value="{{ r[3] }}" size="10"></td>
          <td><input type="text" value="{{ r[4] }}" size="5" readonly></td>
          <td><input type="text" value="{{ r[5] }}" size="5" readonly></td>
          <td>
            <button class="btn" type="submit">Save</button>
          </td>
        </form>

          <td>
            <form method="post" action="{{ url_for('id_data_delete', id_data_id=r[0]) }}" style="margin-top:10px">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <button class="btn2" type="submit" onclick="return confirm('Supprimer?')">Delete</button>
            </form>
          </td>
        </tr>
        {% endfor %}

      </tbody>
    </table>

  {% else %}
    <p>Aucun indicateur enregistré.</p>
  {% endif %}

</div></body></html>
"""

#-----------------------------------------------
# Endpoint#14 — Paramétrage des indicateurs de travail
# Note: on peut faire un seul endpoint pour les 2 actions "check" et "confirm" (simplification) car la logique de vérification est la même dans les 2 cas, 
# et on affiche les messages d'erreur/confirmation dans la même page. Donc pas besoin de faire 2 endpoints séparés.
from flask import request, redirect, url_for, flash
from decimal import Decimal, ROUND_HALF_UP

@app.route("/parametrage", methods=["GET", "POST"])
def parametrage():
    if request.method == "GET":
       rows = list_id_data()
       id_data_id = request.args.get("id_data_id")  # Récupérer l'ID de la donnée à mettre à jour depuis les paramètres de l'URL 
       if not rows:
          flash("Aucun indicateur trouvé pour mise à jour.", "danger")
          return redirect(url_for('parametrage'))         
    rows = list_id_data()
    return render_template_string(PARAMETRAGE_PAGE,message=message, rows=rows)

@app.route("/update_parameters", methods=["GET", "POST"])
def update_parameters():
    if request.method == "POST":
        key = request.form.get(f"keydata")
        value_raw = request.form.get(f"quantity")
        if value_raw is None:
           log.warning("Aucune valeur de quantité fournie pour la clé %s , voici les data : %s.", key, rows)
           flash("Valeur invalide pour la quantité.", "danger")
           return redirect(url_for('parametrage'))
        
        new_value = Decimal(value_raw).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        decr = request.form.get(f"decript")
        note = request.form.get(f"note")
        try:
            update_id_data(key, new_value, decript=decr, note=note)            
            flash("Mise à jour réussie !", "success")
        except Exception as e:
            flash(f"Erreur lors de l'enregistrement : {e}", "danger")    
        return redirect(url_for('parametrage'))
    
    # Si c'est un GET, on affiche simplement la page
    rows = list_id_data()
    return render_template_string(PARAMETRAGE_PAGE,message=message, rows=rows)

@app.route("/id_data_delete", methods=["GET", "POST"])
def id_data_delete():
    if request.method == "POST":
        data_id = request.form.get("id_data_id")
        try:
            delete_id_data(data_id)
        except Exception as e:
            flash(f"Erreur lors de la suppression : {e}", "danger")
    return redirect(url_for("parametrage"))

#-------------------------------------------------
# Endpoint#15 — Comptes techniques (comptabilité)
#-------------------------------------------------
COMPTES_PAGE = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gestion des Comptes Techniques</title>
<style>
 body{font-family:'Segoe UI',Arial; margin:20px; background:#f4f7f6}
 .wrap{max-width:1200px; margin:0 auto; background:white; padding:20px; border-radius:8px; shadow:0 2px 5px rgba(0,0,0,0.1)}
 table{width:100%; border-collapse:collapse; margin-top:20px}
 th,td{padding:12px; border-bottom:1px solid #eee; text-align:left}
 th{background:#2c3e50; color:white}
 input{padding:6px; border:1px solid #ccc; border-radius:4px}
 .btn-save{background:#27ae60; color:white; border:none; padding:8px 12px; border-radius:4px; cursor:pointer}
 .btn-save:hover{background:#219150}
 .badge{background:#ebedef; padding:4px 8px; border-radius:4px; font-size:0.9em}
</style></head><body><div class="wrap">
  <h2>⚙️ Comptes Techniques</h2>
  <p><a href="{{ url_for('home') }}">← Retour au menu</a></p>
  <!-- <p><a href="{{ url_for('debug_view') }}">🔍 Debug View</a></p> -->

  <table>
    <thead>
      <tr>
        <th>Code</th><th>Description</th><th>Solde (Balance)</th><th>Dernière Modif</th><th>Utilisateur</th><th>Action</th>
      </tr>
    </thead>
    <tbody>
      {% for c in comptes %}
      <tr>
        <form method="POST" action="{{ url_for('update_compte') }}">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
          <td><input type="text" name="code" value="{{ c[1] }}" readonly class="badge"></td>
          <td><input type="text" name="description" value="{{ c[2] }}" size="30"></td>
          <td><input type="number" name="balance" value="{{ c[3] }}" step="0.01" style="width:100px"></td>
          <td>{{ c[4] }}</td>
          <td><small>{{ c[5] }}</small></td>
          <td><button type="submit" class="btn-save">Mettre à jour</button></td>
        </form>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  
</div></body></html>
"""

#Endpoint 15 — Comptes techniques (comptabilité) (menu card)
@app.route("/gestion_comptes", methods=["GET"])
def gestion_comptes():
    # Récupération de tous les comptes techniques
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT code, description, balance, updatedate, updateuser FROM comptes_techniques ORDER BY code ASC")
            comptes = cur.fetchall()
    return render_template_string(COMPTES_PAGE, comptes=comptes)

@app.route("/update_compte", methods=["POST"])
def update_compte():
    code = request.form.get("code")
    description = request.form.get("description")
    balance = request.form.get("balance")
    user = session.get("user", "admin") # Utilisateur en session

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE comptes_techniques 
                    SET description = %s, 
                        balance = %s, 
                        updatedate = CURRENT_DATE, 
                        updateuser = %s
                    WHERE code = %s
                """, (description, balance, user, code))
            conn.commit()
        flash(f"Compte {code} mis à jour avec succès", "success")
    except Exception as e:
        flash(f"Erreur : {e}", "danger")
    
    return redirect(url_for('gestion_comptes'))

@app.route("/debug_view")
def debug_view():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM comptes_techniques")
            rows = cur.fetchall()
            # On convertit tout en texte pour l'affichage web
            return "<br>".join([str(r) for r in rows]) 

#
#        
# --------------------------------------------------------------------------------------
if __name__ == "__main__":
    # Local uniquement. En prod Render, gunicorn gère le port.
    app.run(host="0.0.0.0", port=5000, debug=True)