import sqlite3
from datetime import datetime
from pathlib import Path
import psycopg

# --- SQLITE (source) ---
SQLITE_PATH = Path("contributions.db")  # ton fichier SQLite créé par Tkinter/Flask SQLite

# --- POSTGRES (destination) ---
PGHOST = "localhost"
PGPORT = "5432"          # PostgreSQL (pas 5000)
PGDATABASE = "KM_db"
PGUSER = "KM_user"
PGPASSWORD = "1959"

# Si tu veux forcer la remise à zéro de la table Postgres avant migration
RESET_POSTGRES_TABLE = False


def parse_birthdate(value) -> str:
    """
    Retourne une date au format YYYY-MM-DD pour PostgreSQL.
    - Si value est déjà 'YYYY-MM-DD' -> OK
    - Si value est 'JJ/MM/AAAA' -> convertit
    - Sinon -> ValueError
    """
    if value is None:
        raise ValueError("Birthdate is NULL")

    s = str(value).strip()

    # cas déjà ISO
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").date()
        return dt.isoformat()
    except ValueError:
        pass

    # cas JJ/MM/AAAA
    dt = datetime.strptime(s, "%d/%m/%Y").date()
    return dt.isoformat()


def parse_amount(value) -> float:
    if value is None:
        raise ValueError("Amount is NULL")
    s = str(value).strip().replace(",", ".")
    return float(s)


def main():
    if not SQLITE_PATH.exists():
        raise FileNotFoundError(f"SQLite introuvable: {SQLITE_PATH.resolve()}")

    # 1) Lire SQLite
    sconn = sqlite3.connect(SQLITE_PATH)
    sconn.row_factory = sqlite3.Row
    scur = sconn.cursor()

    # Essaye d'abord une structure "ancienne" (CSV->SQLite custom), puis celle qu'on a créée
    # Notre app Tkinter SQLite a créé table members(id, lastname, firstname, birthdate, amount, created_at)
    scur.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='members'
    """)
    if not scur.fetchone():
        sconn.close()
        raise RuntimeError("Table SQLite 'members' introuvable. Vérifie que contributions.db est le bon fichier.")

    scur.execute("SELECT id, lastname, firstname, birthdate, amount FROM members ORDER BY id ASC")
    rows = scur.fetchall()
    sconn.close()

    if not rows:
        print("SQLite: aucune ligne à migrer.")
        return

    # 2) Connecter PostgreSQL
    pconn = psycopg.connect(
        host=PGHOST, port=PGPORT, dbname=PGDATABASE, user=PGUSER, password=PGPASSWORD
    )

    with pconn:
        with pconn.cursor() as pcur:
            # 2a) Créer la table si besoin
            pcur.execute("""
                CREATE TABLE IF NOT EXISTS members (
                  id         BIGSERIAL PRIMARY KEY,
                  lastname   TEXT NOT NULL,
                  firstname  TEXT NOT NULL,
                  birthdate  DATE NOT NULL,
                  amount     NUMERIC(12,2) NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)

            if RESET_POSTGRES_TABLE:
                pcur.execute("TRUNCATE TABLE members RESTART IDENTITY;")

            # 2b) Éviter les doublons : on migre seulement les IDs pas encore présents
            pcur.execute("SELECT COALESCE(MAX(id), 0) FROM members;")
            max_id = int(pcur.fetchone()[0] or 0)

            to_insert = []
            skipped = 0

            for r in rows:
                sid = int(r["id"])
                if sid <= max_id:
                    skipped += 1
                    continue

                lastname = (r["lastname"] or "").strip()
                firstname = (r["firstname"] or "").strip()
                birthdate_iso = parse_birthdate(r["birthdate"])
                amount = parse_amount(r["amount"])

                if not lastname or not firstname:
                    raise ValueError(f"Ligne SQLite id={sid}: lastname/firstname vide(s).")

                to_insert.append((sid, lastname, firstname, birthdate_iso, amount))

            if not to_insert:
                print(f"PostgreSQL: rien à insérer (max id déjà = {max_id}). Lignes sautées: {skipped}.")
                return

            # 2c) Insert en conservant les IDs de SQLite (utile pour cohérence Edit/Update)
            pcur.executemany("""
                INSERT INTO members (id, lastname, firstname, birthdate, amount)
                VALUES (%s, %s, %s, %s::date, %s)
            """, to_insert)

            # 2d) Recaler la séquence (important après insertion manuelle d'ID)
            pcur.execute("""
                SELECT setval(pg_get_serial_sequence('members', 'id'),
                              (SELECT MAX(id) FROM members));
            """)

    pconn.close()

    print(f"Migration OK. Insérées: {len(to_insert)} | Sautées (déjà présentes): {skipped} | MaxID Postgres avant: {max_id}")


if __name__ == "__main__":
    main()
