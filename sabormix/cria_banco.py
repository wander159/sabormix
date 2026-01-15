import os
import sqlite3
from hashlib import sha256

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "oficina.db")  # mesmo nome que o app.py usa

def hash_password(password: str) -> str:
    return sha256(password.encode()).hexdigest()

def main():
    print(f"Usando banco em: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # TABELA USUÁRIOS
    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            senha TEXT NOT NULL,
            tipo TEXT NOT NULL
        )
    """)

    # Garante pelo menos um usuário admin (senha 1234)
    cur.execute("SELECT COUNT(*) FROM usuarios")
    count_users = cur.fetchone()[0]
    if count_users == 0:
        senha_hashed = hash_password("1234")
        cur.execute(
            "INSERT INTO usuarios (nome, senha, tipo) VALUES (?, ?, ?)",
            ("admin", senha_hashed, "gerente")
        )
        print("Usuário padrão 'admin/1234' criado.")
    else:
        print(f"Já existe(m) {count_users} usuário(s) na tabela.")

    # TABELA ITENS
    cur.execute("""
        CREATE TABLE IF NOT EXISTS itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            preco REAL NOT NULL,
            estoque INTEGER NOT NULL,
            codigo TEXT NOT NULL UNIQUE,
            valor_custo REAL NOT NULL,
            tipo TEXT NOT NULL,
            classe TEXT,
            imagem TEXT
        )
    """)

    # TABELA ORDENS_DE_SERVICO
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ordens_de_servico (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER,
            cliente_nome TEXT,
            quantidade INTEGER,
            total REAL,
            metodo_pagamento TEXT,
            data_hora TEXT,
            status TEXT DEFAULT 'Em Aberto',
            observacao TEXT,
            FOREIGN KEY (item_id) REFERENCES itens(id)
        )
    """)

    conn.commit()
    conn.close()
    print("Tabelas criadas/verificadas com sucesso.")

if __name__ == "__main__":
    main()
