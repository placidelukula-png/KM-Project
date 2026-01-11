import tkinter as tk
from tkinter import ttk, messagebox
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path("contributions.db")


# ------------------------------------------------------
# DB helpers
# ------------------------------------------------------
def get_conn():
    # check_same_thread=False utile si tu fais du threading plus tard;
    # sinon tu peux l’enlever. Ici c’est safe.
    return sqlite3.connect(DB_PATH)


def init_db():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lastname   TEXT NOT NULL,
                firstname  TEXT NOT NULL,
                birthdate  TEXT NOT NULL,  -- stockée en texte JJ/MM/AAAA (comme ton UI)
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
        rows = cur.fetchall()
    return rows


def insert_member(lastname, firstname, birthdate_str, amount_float):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO members (lastname, firstname, birthdate, amount)
            VALUES (?, ?, ?, ?)
        """, (lastname, firstname, birthdate_str, amount_float))
        conn.commit()


def update_member(member_id, lastname, firstname, birthdate_str, amount_float):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE members
            SET lastname = ?, firstname = ?, birthdate = ?, amount = ?
            WHERE id = ?
        """, (lastname, firstname, birthdate_str, amount_float, member_id))
        conn.commit()


# ------------------------------------------------------
# Classe principale
# ------------------------------------------------------
class Application(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("List of contributions (SQLite)")
        self.geometry("650x450")
        self.resizable(False, False)

        init_db()
        self.creer_widgets()
        self.show_table()

    # --------------------------------------------------
    # Construire l'interface
    # --------------------------------------------------
    def creer_widgets(self):
        frame_saisie = tk.LabelFrame(self, text="Entry of new members")
        frame_saisie.pack(fill="x", padx=10, pady=10)

        # Last-name
        tk.Label(frame_saisie, text="Last name :").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.Lastname_var = tk.StringVar()
        tk.Entry(frame_saisie, textvariable=self.Lastname_var).grid(row=0, column=1, padx=5, pady=5)

        # First-name
        tk.Label(frame_saisie, text="First name :").grid(row=0, column=2, padx=5, pady=5, sticky="w")
        self.Firstname_var = tk.StringVar()
        tk.Entry(frame_saisie, textvariable=self.Firstname_var).grid(row=0, column=3, padx=5, pady=5)

        # Birth-date
        tk.Label(frame_saisie, text="Birth date (JJ/MM/AAAA) :").grid(row=1, column=0, padx=5, pady=5, sticky="w")
        self.Birthdate_var = tk.StringVar()
        tk.Entry(frame_saisie, textvariable=self.Birthdate_var).grid(row=1, column=1, padx=5, pady=5)

        # Contribution
        tk.Label(frame_saisie, text="Contribution ($) :").grid(row=1, column=2, padx=5, pady=5, sticky="w")
        self.Amount_var = tk.StringVar()
        tk.Entry(frame_saisie, textvariable=self.Amount_var).grid(row=1, column=3, padx=5, pady=5)

        # Buttons
        tk.Button(frame_saisie, text="Add new member", command=self.add_member).grid(
            row=2, column=1, pady=10, sticky="w"
        )
        tk.Button(frame_saisie, text="Cancel these data", command=self.cancel_newmember).grid(
            row=2, column=3, pady=10, sticky="e"
        )

        # Bulletin board (Treeview)
        self.table = ttk.Treeview(self, columns=("id", "Lastname", "Firstname", "Birthdate", "Amount"), show="headings")
        self.table.heading("id", text="ID")
        self.table.heading("Lastname", text="Last name")
        self.table.heading("Firstname", text="First name")
        self.table.heading("Birthdate", text="Birth date")
        self.table.heading("Amount", text="Amount ($)")

        self.table.column("id", width=50)
        self.table.column("Lastname", width=120)
        self.table.column("Firstname", width=120)
        self.table.column("Birthdate", width=140)
        self.table.column("Amount", width=100)

        self.table.pack(padx=10, pady=10, fill="both", expand=True)

        # Update button
        frame_btn = tk.Frame(self)
        frame_btn.pack(pady=5)

        tk.Button(frame_btn, text="Update data of selected member", command=self.update_selected).pack(
            side="left", padx=10
        )

    # --------------------------------------------------
    # Validations
    # --------------------------------------------------
    def validate_inputs(self, lastname, firstname, birthdate_str, amount_str):
        if not lastname or not firstname or not birthdate_str or not amount_str:
            raise ValueError("Veuillez remplir tous les champs.")

        # Amount : autoriser virgule OU point (plus pratique)
        s = amount_str.strip().replace(",", ".")
        amount = float(s)  # ValueError si invalide

        # Birthdate : JJ/MM/AAAA
        datetime.strptime(birthdate_str.strip(), "%d/%m/%Y")  # ValueError si invalide

        return birthdate_str.strip(), amount

    # --------------------------------------------------
    # Ajouter
    # --------------------------------------------------
    def add_member(self):
        lastname = self.Lastname_var.get().strip()
        firstname = self.Firstname_var.get().strip()
        birthdate_str = self.Birthdate_var.get().strip()
        amount_str = self.Amount_var.get().strip()

        try:
            birthdate_str, amount = self.validate_inputs(lastname, firstname, birthdate_str, amount_str)
        except ValueError:
            messagebox.showerror(
                "Erreur",
                "Veuillez vérifier les formats :\n"
                "- Date : JJ/MM/AAAA\n"
                "- Montant : numérique (ex: 12.50 ou 12,50)\n"
            )
            return

        insert_member(lastname, firstname, birthdate_str, amount)
        messagebox.showinfo("Success", "Contribution registered properly.")
        self.show_table()
        self.cancel_newmember()

    # --------------------------------------------------
    # Annuler la saisie
    # --------------------------------------------------
    def cancel_newmember(self):
        self.Lastname_var.set("")
        self.Firstname_var.set("")
        self.Birthdate_var.set("")
        self.Amount_var.set("")

    # --------------------------------------------------
    # Affichage table
    # --------------------------------------------------
    def show_table(self):
        for ligne in self.table.get_children():
            self.table.delete(ligne)

        for (member_id, lastname, firstname, birthdate, amount) in fetch_all_members():
            self.table.insert("", "end", iid=str(member_id),
                              values=(member_id, lastname, firstname, birthdate, f"{amount:.2f}"))

    # --------------------------------------------------
    # Update
    # --------------------------------------------------
    def update_selected(self):
        selection = self.table.selection()
        if not selection:
            messagebox.showwarning("Error", "Please select a member.")
            return

        member_id = int(selection[0])
        current = self.table.item(selection[0], "values")
        # current = (id, lastname, firstname, birthdate, amount_str)

        fen = tk.Toplevel(self)
        fen.title("Update a member")
        fen.geometry("350x260")
        fen.resizable(False, False)

        tk.Label(fen, text="Last name :").pack(anchor="w", padx=10, pady=(10, 0))
        lastname_var = tk.StringVar(value=current[1])
        tk.Entry(fen, textvariable=lastname_var).pack(fill="x", padx=10)

        tk.Label(fen, text="First name :").pack(anchor="w", padx=10, pady=(10, 0))
        firstname_var = tk.StringVar(value=current[2])
        tk.Entry(fen, textvariable=firstname_var).pack(fill="x", padx=10)

        tk.Label(fen, text="Birth date (JJ/MM/AAAA) :").pack(anchor="w", padx=10, pady=(10, 0))
        birthdate_var = tk.StringVar(value=current[3])
        tk.Entry(fen, textvariable=birthdate_var).pack(fill="x", padx=10)

        tk.Label(fen, text="Contribution ($) :").pack(anchor="w", padx=10, pady=(10, 0))
        amount_var = tk.StringVar(value=current[4])
        tk.Entry(fen, textvariable=amount_var).pack(fill="x", padx=10)

        def save_modif():
            ln = lastname_var.get().strip()
            fn = firstname_var.get().strip()
            bd = birthdate_var.get().strip()
            amt_str = amount_var.get().strip()

            try:
                bd, amt = self.validate_inputs(ln, fn, bd, amt_str)
            except ValueError:
                messagebox.showerror(
                    "Erreur",
                    "Veuillez vérifier les formats :\n"
                    "- Date : JJ/MM/AAAA\n"
                    "- Montant : numérique (ex: 12.50 ou 12,50)\n"
                )
                return

            update_member(member_id, ln, fn, bd, amt)
            self.show_table()
            messagebox.showinfo("Success", "Update made properly.")
            fen.destroy()

        tk.Button(fen, text="Save", command=save_modif).pack(pady=15)


# ------------------------------------------------------
# Lancement
# ------------------------------------------------------
if __name__ == "__main__":
    app = Application()
    app.mainloop()
