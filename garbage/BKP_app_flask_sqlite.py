from flask import Flask, request, redirect, url_for, render_template_string
import sqlite3
from pathlib import Path
from datetime import datetime

app = Flask(__name__)

DB_PATH = Path("contributions.db")  # IMPORTANT: même fichier que ton Tkinter

# ----------------------------
# DB helpers
# ----------------------------
def get_conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    # au cas où tu lances Flask avant Tkinter un jour
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lastname   TEXT NOT NULL,
                firstname  TEXT NOT NULL,
                birthdate  TEXT NOT NULL,
                amount     REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()

def fetch_all_members():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, lastname, firstname, birthdate, amount
            FROM members
            ORDER BY id DESC
        """)
        return cur.fetchall()

def insert_member(lastname, firstname, birthdate_str, amount_float):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO members (lastname, firstname, birthdate, amount)
            VALUES (?, ?, ?, ?)
        """, (lastname, firstname, birthdate_str, amount_float))
        conn.commit()

def fetch_one(member_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, lastname, firstname, birthdate, amount
            FROM members
            WHERE id = ?
        """, (member_id,))
        return cur.fetchone()

def update_member(member_id, lastname, firstname, birthdate_str, amount_float):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE members
            SET lastname = ?, firstname = ?, birthdate = ?, amount = ?
            WHERE id = ?
        """, (lastname, firstname, birthdate_str, amount_float, member_id))
        conn.commit()

#
def delete_member(member_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM members WHERE id = %s", (member_id,))
        conn.commit()
#

# ----------------------------
# Validation (comme Tkinter)
# ----------------------------
def validate_inputs(lastname, firstname, birthdate_str, amount_str):
    lastname = (lastname or "").strip()
    firstname = (firstname or "").strip()
    birthdate_str = (birthdate_str or "").strip()
    amount_str = (amount_str or "").strip()

    if not lastname or not firstname or not birthdate_str or not amount_str:
        raise ValueError("Veuillez remplir tous les champs.")

    # autorise virgule ou point
    amount = float(amount_str.replace(",", "."))

    # JJ/MM/AAAA
    datetime.strptime(birthdate_str, "%d/%m/%Y")

    return lastname, firstname, birthdate_str, amount

# ----------------------------
# HTML (template inline)
# ----------------------------
PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Contributions (Flask + SQLite)</title>
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
<body>
<div class="wrap">
  <h1>List of contributions</h1>
  <p class="muted">Flask + SQLite (même base que ton Tkinter : <b>{{ db_name }}</b>)</p>

  {% if message %}
    <div class="msg {{ 'error' if is_error else 'ok' }}">{{ message }}</div>
  {% endif %}

  <div class="card">
    <h2 style="margin-top:0;">Entry of new members</h2>
    <form method="post" action="{{ url_for('add') }}">
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
          <input name="birthdate" value="{{ edit_row[3] }}" required>
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
          <th style="width:120px;">Action</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td>{{ r[0] }}</td>
          <td>{{ r[1] }}</td>
          <td>{{ r[2] }}</td>
          <td>{{ r[3] }}</td>
          <td>{{ '%.2f'|format(r[4]) }}</td>
          <td><a href="{{ url_for('edit', member_id=r[0]) }}">Edit</a></td>
        </tr>
        {% endfor %}
        {% if not rows %}
        <tr><td colspan="6" class="small">Aucune donnée pour le moment.</td></tr>
        {% endif %}
      </tbody>
    </table>
  </div>

#  
<td>
  <a href="{{ url_for('edit', member_id=r[0]) }}">Edit</a>
  <form method="post"
        action="{{ url_for('delete', member_id=r[0]) }}"
        style="display:inline;"
        onsubmit="return confirm('Supprimer ce membre (ID {{ r[0] }}) ?');">
    <button type="submit" class="btn secondary" style="padding:6px 10px; margin-left:8px;">
      Delete
    </button>
  </form>
</td>
#

</div>
</body>
</html>
"""

# ----------------------------
# Routes
# ----------------------------
@app.get("/")
def home():
    rows = fetch_all_members()
    return render_template_string(
        PAGE,
        rows=rows,
        edit_row=None,
        message="",
        is_error=False,
        db_name=DB_PATH.name
    )

@app.post("/add")
def add():
    try:
        ln, fn, bd, amt = validate_inputs(
            request.form.get("lastname"),
            request.form.get("firstname"),
            request.form.get("birthdate"),
            request.form.get("amount"),
        )
        insert_member(ln, fn, bd, amt)
    except ValueError:
        rows = fetch_all_members()
        return render_template_string(
            PAGE,
            rows=rows,
            edit_row=None,
            message="Erreur: vérifier Date (JJ/MM/AAAA) et Montant (numérique).",
            is_error=True,
            db_name=DB_PATH.name
        )
    return redirect(url_for("home"))

@app.get("/edit/<int:member_id>")
def edit(member_id: int):
    row = fetch_one(member_id)
    rows = fetch_all_members()
    if not row:
        return render_template_string(
            PAGE,
            rows=rows,
            edit_row=None,
            message=f"Member ID {member_id} introuvable.",
            is_error=True,
            db_name=DB_PATH.name
        )
    return render_template_string(
        PAGE,
        rows=rows,
        edit_row=row,
        message="",
        is_error=False,
        db_name=DB_PATH.name
    )

@app.post("/update/<int:member_id>")
def update(member_id: int):
    try:
        ln, fn, bd, amt = validate_inputs(
            request.form.get("lastname"),
            request.form.get("firstname"),
            request.form.get("birthdate"),
            request.form.get("amount"),
        )
        update_member(member_id, ln, fn, bd, amt)
    except ValueError:
        rows = fetch_all_members()
        row = fetch_one(member_id)
        return render_template_string(
            PAGE,
            rows=rows,
            edit_row=row,
            message="Erreur: vérifier Date (JJ/MM/AAAA) et Montant (numérique).",
            is_error=True,
            db_name=DB_PATH.name
        )
    return redirect(url_for("home"))

#
@app.post("/delete/<int:member_id>")
def delete(member_id: int):
    delete_member(member_id)
    return redirect(url_for("home"))


# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    init_db()
    # host=0.0.0.0 => accessible sur le réseau local
    app.run(host="0.0.0.0", port=5000, debug=True)
