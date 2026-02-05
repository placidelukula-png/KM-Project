from __future__ import annotations

import os
import sys
import logging
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Iterable, Dict, Any

import pandas as pd
import psycopg
from psycopg.rows import tuple_row
from dateutil import parser as date_parser


# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("km_import")


# ----------------------------
# Config (ENV)
# ----------------------------
DATABASE_URL = os.getenv("DATABASE_URL")  # Render Internal Database URL
DEFAULT_MEMBER_TYPE = os.getenv("DEFAULT_MEMBER_TYPE", "membre")
DEFAULT_STATUTE = os.getenv("DEFAULT_STATUTE", "actif")
DEFAULT_MENTOR = os.getenv("DEFAULT_MENTOR", "admin")  # ou le mentor “système”
DEFAULT_UPDATEUSER = os.getenv("DEFAULT_UPDATEUSER", "system_import")
DEFAULT_IDTYPE = os.getenv("DEFAULT_IDTYPE", "N/A")
DEFAULT_PASSWORD_HASH = os.getenv("DEFAULT_PASSWORD_HASH", "")  # optionnel

# Colonnes attendues (contrat)
REQUIRED_COLS = [
    "payment_id",
    "phone",
    "firstname",
    "lastname",
    "date",
    "amount",
    "debitcredit",
    "reference",
]


# ----------------------------
# Helpers
# ----------------------------
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL manquant (ENV).")
    return psycopg.connect(DATABASE_URL, row_factory=tuple_row)


def parse_amount(x) -> Decimal:
    """
    Convertit amount en Decimal proprement.
    Gère "50", "50.25", "50,25" etc.
    """
    if x is None or (isinstance(x, float) and pd.isna(x)):
        raise ValueError("amount manquant")
    s = str(x).strip().replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"amount invalide: {x}")


def parse_debitcredit(x) -> str:
    """
    Accepte C/D (ou Credit/Debit) - on normalise vers 'C' ou 'D'.
    """
    if x is None:
        raise ValueError("debitcredit manquant")
    s = str(x).strip().upper()
    if s in ("C", "CREDIT", "CR", "+"):
        return "C"
    if s in ("D", "DEBIT", "DR", "-"):
        return "D"
    raise ValueError(f"debitcredit invalide: {x} (attendu C ou D)")


def parse_date_any(x) -> date:
    """
    Accepte:
    - date python (date/datetime)
    - string "2026-01-15" ou "15/01/2026" etc.
    """
    if x is None or (isinstance(x, float) and pd.isna(x)):
        raise ValueError("date manquante")

    if isinstance(x, date) and not isinstance(x, datetime):
        return x
    if isinstance(x, datetime):
        return x.date()

    s = str(x).strip()
    # dateutil gère beaucoup de formats; dayfirst=True utile si "15/01/2026"
    dt = date_parser.parse(s, dayfirst=True)
    return dt.date()


def norm_text(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ""
    return str(x).strip()


def ensure_schema():
    """
    - Ajoute balance à membres si absent
    - Crée table mouvements si absente
    - Index + contraintes
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 1) balance dans membres
            # (Si ta table s'appelle "membres" — c'est ton cas actuel)
            cur.execute("""
                ALTER TABLE IF EXISTS membres
                ADD COLUMN IF NOT EXISTS balance DECIMAL(14,2) NOT NULL DEFAULT 0;
            """)

            # 2) mouvements
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mouvements (
                    id BIGSERIAL PRIMARY KEY,
                    payment_id TEXT NOT NULL UNIQUE,
                    phone TEXT NOT NULL,
                    firstname TEXT NOT NULL,
                    lastname TEXT NOT NULL,
                    mouvement_date DATE NOT NULL,
                    amount DECIMAL(14,2) NOT NULL,
                    debitcredit CHAR(1) NOT NULL CHECK (debitcredit IN ('D','C')),
                    reference TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mouvements_phone ON mouvements(phone);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mouvements_date ON mouvements(mouvement_date);")

        conn.commit()
    log.info("Schema OK (membres.balance + mouvements prêts)")


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
    default_birthdate = date(2000, 1, 1)

    # Password_hash : si tu ne veux pas créer de compte login automatique,
    # tu peux mettre un hash “impossible” et forcer un reset plus tard.
    # Ici: on autorise DEFAULT_PASSWORD_HASH vide => on met une chaîne fixe non vide.
    pwd_hash = DEFAULT_PASSWORD_HASH.strip() or "NO_LOGIN_CREATED"

    cur.execute("""
        INSERT INTO membres
        (phone, membertype, mentor, lastname, firstname, birthdate, idtype, idpicture_url,
         currentstatute, updatedate, updateuser, password_hash, balance)
        VALUES
        (%s, %s, %s, %s, %s, %s, %s, NULL, %s, CURRENT_DATE, %s, %s, 0)
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
    ))
    log.info("Nouveau membre créé automatiquement: %s (%s %s)", phone, firstname, lastname)


def payment_exists(cur, payment_id: str) -> bool:
    cur.execute("SELECT 1 FROM mouvements WHERE payment_id = %s", (payment_id,))
    return cur.fetchone() is not None


def insert_mouvement(cur, payment_id: str, phone: str, firstname: str, lastname: str,
                    mvt_date: date, amount: Decimal, dc: str, reference: str):
    cur.execute("""
        INSERT INTO mouvements
        (payment_id, phone, firstname, lastname, mouvement_date, amount, debitcredit, reference)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """, (payment_id, phone, firstname, lastname, mvt_date, amount, dc, reference))


def apply_balance(cur, phone: str, amount: Decimal, dc: str):
    """
    C => crédit => +amount
    D => débit  => -amount
    """
    if dc == "C":
        cur.execute("UPDATE membres SET balance = balance + %s WHERE phone = %s", (amount, phone))
    else:
        cur.execute("UPDATE membres SET balance = balance - %s WHERE phone = %s", (amount, phone))


def read_input_file(path: str) -> pd.DataFrame:
    """
    Lit CSV ou Excel. Détecte via extension.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    ext = os.path.splitext(path.lower())[1]
    if ext in (".csv",):
        # encoding: on essaye utf-8-sig (souvent), sinon fallback latin1
        try:
            df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(path, dtype=str, encoding="latin1")
    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str)  # openpyxl requis pour xlsx
    else:
        raise ValueError(f"Extension non supportée: {ext} (attendu .csv/.xlsx/.xls)")

    # Normalise les noms de colonnes
    df.columns = [c.strip().lower() for c in df.columns]
    return df


def validate_columns(df: pd.DataFrame):
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"Colonnes manquantes: {missing}. Colonnes trouvées={list(df.columns)}")


@dataclass
class ImportStats:
    total_rows: int = 0
    inserted_mouvements: int = 0
    skipped_duplicates: int = 0
    created_members: int = 0
    updated_balances: int = 0
    failed_rows: int = 0


def process_file(path: str) -> ImportStats:
    ensure_schema()
    df = read_input_file(path)
    validate_columns(df)

    stats = ImportStats(total_rows=len(df))

    with get_conn() as conn:
        # Transaction globale : si tu préfères “par ligne”, on peut adapter
        with conn.cursor() as cur:
            for i, row in df.iterrows():
                try:
                    payment_id = norm_text(row["payment_id"])
                    phone = norm_text(row["phone"])
                    firstname = norm_text(row["firstname"])
                    lastname = norm_text(row["lastname"])
                    reference = norm_text(row["reference"])

                    if not payment_id or not phone or not firstname or not lastname or not reference:
                        raise ValueError("payment_id/phone/firstname/lastname/reference obligatoires")

                    mvt_date = parse_date_any(row["date"])
                    amount = parse_amount(row["amount"])
                    dc = parse_debitcredit(row["debitcredit"])

                    # 1) Doublons
                    if payment_exists(cur, payment_id):
                        stats.skipped_duplicates += 1
                        continue

                    # 2) Membre absent -> créer
                    if not member_exists(cur, phone):
                        create_member_minimal(cur, phone, firstname, lastname)
                        stats.created_members += 1

                    # 3) Insérer mouvement
                    insert_mouvement(cur, payment_id, phone, firstname, lastname, mvt_date, amount, dc, reference)
                    stats.inserted_mouvements += 1

                    # 4) MAJ balance
                    apply_balance(cur, phone, amount, dc)
                    stats.updated_balances += 1

                except Exception as e:
                    stats.failed_rows += 1
                    # Log détaillé + continue
                    log.exception("Ligne %s invalide: %s | row=%s", i, e, row.to_dict())

        conn.commit()

    return stats


def main():
    if len(sys.argv) < 2:
        print("Usage: python import_mouvements.py <fichier.csv|fichier.xlsx>")
        sys.exit(1)

    path = sys.argv[1]
    log.info("Import démarré: %s", path)

    stats = process_file(path)

    log.info("Import terminé ✅")
    log.info(
        "Stats: total=%s inserted=%s duplicates=%s created_members=%s balances=%s failed=%s",
        stats.total_rows,
        stats.inserted_mouvements,
        stats.skipped_duplicates,
        stats.created_members,
        stats.updated_balances,
        stats.failed_rows,
    )


if __name__ == "__main__":
    main()
