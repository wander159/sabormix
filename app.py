import os
import sqlite3
from hashlib import sha256
from datetime import datetime, timezone
from functools import wraps
from io import BytesIO

import pandas as pd
import pytz
from flask import (
    Flask, request, redirect, url_for, session,
    render_template, send_file, flash
)
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

# -------------------------------------------------
# CONFIG FLASK
# -------------------------------------------------
app = Flask(__name__)
app.secret_key = "chave_super_secreta_muda_isto"

DB_PATH = os.path.join(os.path.dirname(__file__), "oficina.db")

# -------------------------------------------------
# BANCO / MODELO
# -------------------------------------------------
def hash_password(password: str) -> str:
    return sha256(password.encode()).hexdigest()


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # usuarios
    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            senha TEXT NOT NULL,
            tipo TEXT NOT NULL
        )
    """)

    # itens
    cur.execute("""
        CREATE TABLE IF NOT EXISTS itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            preco REAL NOT NULL,
            estoque INTEGER NOT NULL,
            codigo TEXT NOT NULL UNIQUE,
            valor_custo REAL NOT NULL,
            tipo TEXT NOT NULL,
            classe TEXT
        )
    """)

    # ordens_de_servico
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ordens_de_servico (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER,
            cliente_nome TEXT,
            quantidade INTEGER,
            total REAL,
            metodo_pagamento TEXT,
            data_hora TEXT,
            status TEXT DEFAULT 'Finalizado',
            observacao TEXT,
            FOREIGN KEY (item_id) REFERENCES itens(id)
        )
    """)

    # usuário padrão admin/1234 se não existir
    cur.execute("SELECT COUNT(*) AS c FROM usuarios")
    if cur.fetchone()["c"] == 0:
        cur.execute(
            "INSERT INTO usuarios (nome, senha, tipo) VALUES (?, ?, ?)",
            ("admin", hash_password("1234"), "gerente")
        )

    conn.commit()
    conn.close()


# -------------------------------------------------
# HELPERS / DECORATORS
# -------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrap


def gerente_required(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        if "user_tipo" not in session or session["user_tipo"] != "gerente":
            flash("Acesso restrito a gerentes.", "danger")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrap


def get_current_user():
    if "user_id" not in session:
        return None
    return {
        "id": session["user_id"],
        "nome": session.get("user_nome"),
        "tipo": session.get("user_tipo")
    }


def utc_now_str():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


# -------------------------------------------------
# CONTEXT PROCESSOR
# -------------------------------------------------
@app.context_processor
def inject_globals():
    return {
        "current_user": get_current_user()
    }


# -------------------------------------------------
# ROTAS
# -------------------------------------------------

@app.route("/")
@login_required
def index():
    # Resumo do dia atual (horário de São Paulo)
    tz_local = pytz.timezone("America/Sao_Paulo")
    hoje_local = datetime.now(tz_local).date()
    inicio_dia = f"{hoje_local} 00:00:00"
    fim_dia = f"{hoje_local} 23:59:59"

    conn = get_conn()
    cur = conn.cursor()

    # Consulta vendas do dia
    cur.execute("""
        SELECT os.*, i.valor_custo
        FROM ordens_de_servico os
        JOIN itens i ON os.item_id = i.id
        WHERE os.data_hora BETWEEN ? AND ?
        ORDER BY os.data_hora DESC
    """, (inicio_dia, fim_dia))

    vendas_dia = cur.fetchall()

    total_vendas_dia = 0.0
    total_custo_dia = 0.0
    qtd_vendas = len(vendas_dia)

    for v in vendas_dia:
        total_vendas_dia += float(v["total"])
        custo_item = int(v["quantidade"]) * float(v["valor_custo"])
        total_custo_dia += custo_item

    lucro_dia = total_vendas_dia - total_custo_dia

    conn.close()

    return render_template("index.html",
                           qtd_vendas=qtd_vendas,
                           total_vendas_dia=total_vendas_dia,
                           total_custo_dia=total_custo_dia,
                           lucro_dia=lucro_dia)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        senha = request.form.get("senha", "").strip()
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, nome, senha, tipo FROM usuarios WHERE LOWER(nome)=?",
            (nome.lower(),)
        )
        row = cur.fetchone()
        conn.close()
        if row and row["senha"] == hash_password(senha):
            session["user_id"] = row["id"]
            session["user_nome"] = row["nome"]
            session["user_tipo"] = row["tipo"]
            return redirect(url_for("index"))
        flash("Usuário ou senha inválidos.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------------------- CARDÁPIO ----------------------

@app.route("/cardapio")
@login_required
def cardapio():
    conn = get_conn()
    cur = conn.cursor()
    # só produtos com estoque > 0
    cur.execute("""
        SELECT nome, preco, estoque, classe
        FROM itens
        WHERE tipo = 'produto' AND estoque > 0
        ORDER BY nome
    """)
    itens = cur.fetchall()
    conn.close()
    return render_template("cardapio.html", itens=itens)


# ---------------------- ESTOQUE ----------------------

@app.route("/estoque", methods=["GET", "POST"])
@login_required
@gerente_required
def estoque():
    tipo = request.args.get("tipo")
    classe = request.args.get("classe")
    edit_id = request.args.get("edit_id")

    conn = get_conn()
    cur = conn.cursor()

    # Busca item para edição
    edit_item = None
    if edit_id:
        cur.execute("SELECT * FROM itens WHERE id=?", (edit_id,))
        edit_item = cur.fetchone()

    # Filtro
    query = "SELECT * FROM itens WHERE 1=1"
    params = []
    if tipo:
        query += " AND tipo = ?"
        params.append(tipo)
    if classe:
        query += " AND classe LIKE ?"
        params.append(f"%{classe}%")

    query += " ORDER BY nome"
    cur.execute(query, params)
    itens = cur.fetchall()
    conn.close()

    return render_template("estoque.html", itens=itens, edit_item=edit_item,
                           tipo=tipo, classe=classe)


@app.route("/estoque/salvar", methods=["POST"])
@login_required
@gerente_required
def estoque_salvar():
    item_id = request.form.get("id")
    nome = request.form.get("nome").strip()
    preco = float(request.form.get("preco"))
    estoque = int(request.form.get("estoque"))
    codigo = request.form.get("codigo").strip()
    valor_custo = float(request.form.get("valor_custo"))
    tipo_item = request.form.get("tipo_item")
    classe = request.form.get("classe", "").strip() or None

    conn = get_conn()
    cur = conn.cursor()
    try:
        if item_id:
            cur.execute("""
                UPDATE itens SET nome=?, preco=?, estoque=?, codigo=?, valor_custo=?, tipo=?, classe=?
                WHERE id=?
            """, (nome, preco, estoque, codigo, valor_custo, tipo_item, classe, item_id))
        else:
            cur.execute("""
                INSERT INTO itens (nome, preco, estoque, codigo, valor_custo, tipo, classe)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (nome, preco, estoque, codigo, valor_custo, tipo_item, classe))
        conn.commit()
        flash("Item salvo com sucesso.", "success")
    except sqlite3.IntegrityError:
        flash("Erro: Código já existe.", "danger")
        conn.rollback()
    except Exception as e:
        flash(f"Erro ao salvar: {e}", "danger")
        conn.rollback()
    conn.close()

    # === CORREÇÃO: Mantém filtros de tipo e classe ===
    tipo = request.args.get("tipo")
    classe = request.args.get("classe")
    return redirect(url_for("estoque", tipo=tipo, classe=classe))


@app.route("/estoque/excluir/<int:item_id>", methods=["POST"])
@login_required
@gerente_required
def estoque_excluir(item_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM itens WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    flash("Item excluído com sucesso.", "success")

    # === CORREÇÃO: Mantém filtros ===
    tipo = request.form.get("tipo") or request.args.get("tipo")
    classe = request.form.get("classe") or request.args.get("classe")
    return redirect(url_for("estoque", tipo=tipo, classe=classe))


# ---------------------- VENDAS ----------------------

@app.route("/venda")
@login_required
def venda():
    cart = session.get("cart", [])
    total = sum(i["preco"] * i["quantidade"] for i in cart)
    cliente = session.get("cliente_atual", "")
    return render_template("venda.html", cart=cart, total=total, cliente=cliente)


@app.route("/venda/adicionar", methods=["POST"])
@login_required
def venda_adicionar():
    cliente = request.form.get("cliente", "").strip()
    item_busca = request.form.get("item_busca", "").strip()
    quantidade = int(request.form.get("quantidade", "1") or 1)

    session["cliente_atual"] = cliente

    if not cliente:
        flash("Preencha o nome do cliente.", "danger")
        return redirect(url_for("venda"))

    if not item_busca:
        flash("Digite parte do nome ou código do item.", "danger")
        return redirect(url_for("venda"))

    conn = get_conn()
    cur = conn.cursor()

    busca_lower = item_busca.lower()

    # BUSCA SUPER INTELIGENTE - PRIORIZA NOME!
    cur.execute("""
        SELECT id, nome, preco, estoque, tipo, classe, codigo
        FROM itens
        WHERE (estoque > 0 OR tipo = 'serviço')
          AND (
            LOWER(nome) LIKE ? OR                     -- Contém a palavra em qualquer lugar
            LOWER(nome) LIKE ? OR                     -- Começa com a palavra
            codigo LIKE ?                             -- Código exato ou parcial
          )
        ORDER BY 
            CASE
                WHEN LOWER(nome) = ? THEN 1            -- Nome exato
                WHEN LOWER(nome) LIKE ? THEN 2         -- Começa com a busca
                WHEN LOWER(nome) LIKE ? THEN 3         -- Contém a busca
                WHEN codigo = ? THEN 4
                ELSE 5
            END,
            nome
        LIMIT 20
    """, (
        f"%{busca_lower}%",       # Contém
        f"{busca_lower}%",        # Começa com
        f"%{item_busca}%",        # Código parcial
        busca_lower,              # Nome exato
        f"{busca_lower}%",        # Começa com (CASE)
        f"%{busca_lower}%",       # Contém (CASE)
        item_busca                # Código exato
    ))

    itens_encontrados = cur.fetchall()
    conn.close()

    if not itens_encontrados:
        flash(f"Nenhum item encontrado com '{item_busca}'.", "danger")
        return redirect(url_for("venda"))

    # Pega o primeiro (o mais relevante)
    item = itens_encontrados[0]

    if len(itens_encontrados) > 1:
        flash(f"Vários itens encontrados. Adicionado: {item['nome']} (mais relevante)", "info")

    # Verifica estoque
    if item["estoque"] < quantidade and item["tipo"] == "produto":
        flash(f"Estoque insuficiente para {item['nome']}. Disponível: {item['estoque']}", "danger")
        return redirect(url_for("venda"))

    # Baixa estoque
    if item["tipo"] == "produto":
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE itens SET estoque = estoque - ? WHERE id = ?", (quantidade, item["id"]))
        conn.commit()
        conn.close()

    # Adiciona ao carrinho
    cart = session.get("cart", [])
    encontrado = False
    for c in cart:
        if c["id"] == item["id"]:
            c["quantidade"] += quantidade
            encontrado = True
            break

    if not encontrado:
        cart.append({
            "id": item["id"],
            "nome": item["nome"],
            "preco": float(item["preco"]),
            "quantidade": quantidade,
            "tipo": item["tipo"]
        })

    session["cart"] = cart
    flash(f"{quantidade} × {item['nome']} adicionado!", "success")
    return redirect(url_for("venda"))


@app.route("/venda/remover/<int:index>", methods=["POST"])
@login_required
def venda_remover(index):
    cart = session.get("cart", [])
    if 0 <= index < len(cart):
        item = cart.pop(index)
        if item["tipo"] == "produto":
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("UPDATE itens SET estoque = estoque + ? WHERE id = ?", (item["quantidade"], item["id"]))
            conn.commit()
            conn.close()
        session["cart"] = cart
        flash("Item removido do carrinho.", "success")
    else:
        flash("Item inválido.", "danger")
    return redirect(url_for("venda"))


@app.route("/venda/limpar", methods=["POST"])
@login_required
def venda_limpar():
    cart = session.get("cart", [])
    conn = get_conn()
    cur = conn.cursor()
    for item in cart:
        if item["tipo"] == "produto":
            cur.execute("UPDATE itens SET estoque = estoque + ? WHERE id = ?", (item["quantidade"], item["id"]))
    conn.commit()
    conn.close()
    session["cart"] = []
    session["cliente_atual"] = ""
    flash("Carrinho limpo e estoque revertido.", "success")
    return redirect(url_for("venda"))


@app.route("/venda/finalizar", methods=["POST"])
@login_required
def venda_finalizar():
    cart = session.get("cart", [])
    if not cart:
        flash("Carrinho vazio.", "danger")
        return redirect(url_for("venda"))

    cliente = request.form.get("cliente", "").strip()
    metodo_pagamento = request.form.get("metodo_pagamento")
    valor_pago = float(request.form.get("valor_pago", "0") or 0)

    total = sum(i["preco"] * i["quantidade"] for i in cart)
    if valor_pago < total:
        flash("Valor pago insuficiente.", "danger")
        return redirect(url_for("venda"))

    conn = get_conn()
    cur = conn.cursor()
    try:
        dh = utc_now_str()
        for item in cart:
            cur.execute("""
                INSERT INTO ordens_de_servico
                (item_id, cliente_nome, quantidade, total, metodo_pagamento, data_hora, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (item["id"], cliente, item["quantidade"], item["preco"] * item["quantidade"],
                  metodo_pagamento, dh, "Finalizado"))
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        conn.close()
        flash(f"Erro ao finalizar venda: {e}", "danger")
        return redirect(url_for("venda"))

    conn.close()
    troco = valor_pago - total
    session["cart"] = []
    session["cliente_atual"] = ""
    flash(f"Venda finalizada. Total R$ {total:.2f} | Valor pago R$ {valor_pago:.2f} | Troco R$ {troco:.2f}", "success")
    return redirect(url_for("venda"))


# ---------------------- HISTÓRICO ----------------------

def periodo_utc(inicio_str, fim_str):
    tz_local = pytz.timezone("America/Sao_Paulo")
    if not inicio_str:
        inicio_str = datetime.now().strftime("%Y-%m-%d")
    if not fim_str:
        fim_str = datetime.now().strftime("%Y-%m-%d")

    inicio_local = datetime.strptime(inicio_str + " 00:00:00", "%Y-%m-%d %H:%M:%S")
    fim_local = datetime.strptime(fim_str + " 23:59:59", "%Y-%m-%d %H:%M:%S")
    inicio_utc = tz_local.localize(inicio_local).astimezone(pytz.utc).strftime('%Y-%m-%d %H:%M:%S')
    fim_utc = tz_local.localize(fim_local).astimezone(pytz.utc).strftime('%Y-%m-%d %H:%M:%S')
    return inicio_str, fim_str, inicio_utc, fim_utc


@app.route("/historico")
@login_required
def historico():
    inicio = request.args.get("inicio")
    fim = request.args.get("fim")
    cliente = request.args.get("cliente", "").strip()
    obs = request.args.get("obs", "").strip()

    # Se não tiver filtro de data, define como hoje
    if not inicio or not fim:
        hoje = datetime.now().strftime("%Y-%m-%d")
        inicio = hoje
        fim = hoje

    conn = get_conn()
    cur = conn.cursor()

    # Monta a query com filtros
    query = """
        SELECT os.id, os.data_hora, i.nome AS item_nome, os.quantidade, os.total,
               os.cliente_nome, os.metodo_pagamento, os.observacao
        FROM ordens_de_servico os
        JOIN itens i ON os.item_id = i.id
        WHERE os.data_hora BETWEEN ? AND ?
    """
    params = [f"{inicio} 00:00:00", f"{fim} 23:59:59"]

    if cliente:
        query += " AND LOWER(os.cliente_nome) LIKE LOWER(?)"
        params.append(f"%{cliente}%")

    if obs:
        query += " AND LOWER(os.observacao) LIKE LOWER(?)"
        params.append(f"%{obs}%")

    query += " ORDER BY os.data_hora DESC"

    cur.execute(query, params)
    vendas_raw = cur.fetchall()

    tz_local = pytz.timezone("America/Sao_Paulo")
    vendas = []
    total_periodo = 0.0

    for v in vendas_raw:
        data_utc = datetime.strptime(v["data_hora"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
        data_local = data_utc.astimezone(tz_local).strftime("%d/%m/%Y %H:%M:%S")
        total_periodo += float(v["total"])
        vendas.append(dict(v))
        vendas[-1]["data_local"] = data_local

    conn.close()

    return render_template("historico.html", vendas=vendas, total_periodo=total_periodo,
                           inicio=inicio, fim=fim, cliente=cliente, obs=obs)

@app.route("/historico/obs/<int:venda_id>", methods=["GET", "POST"])
@login_required
def historico_obs(venda_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT os.*, i.nome AS item_nome
        FROM ordens_de_servico os
        JOIN itens i ON os.item_id = i.id
        WHERE os.id = ?
    """, (venda_id,))
    venda = cur.fetchone()

    if not venda:
        flash("Venda não encontrada.", "danger")
        conn.close()
        return redirect(url_for("historico"))

    if request.method == "POST":
        observacao = request.form.get("observacao", "").strip()
        cur.execute("UPDATE ordens_de_servico SET observacao=? WHERE id=?", (observacao, venda_id))
        conn.commit()
        conn.close()
        flash("Observação salva com sucesso.", "success")

        # === CORREÇÃO FINAL: Pega TODOS os filtros, mesmo vazios ===
        inicio = request.args.get("inicio", "")
        fim = request.args.get("fim", "")
        cliente = request.args.get("cliente", "")
        obs = request.args.get("obs", "")

        return redirect(url_for("historico", inicio=inicio or None, fim=fim or None, cliente=cliente or None, obs=obs or None))

    tz_local = pytz.timezone("America/Sao_Paulo")
    data_utc = datetime.strptime(venda["data_hora"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
    data_local = data_utc.astimezone(tz_local).strftime("%d/%m/%Y %H:%M:%S")
    venda = dict(venda)
    venda["data_local"] = data_local

    conn.close()
    return render_template("historico_obs.html", venda=venda,
                           inicio=request.args.get("inicio"), fim=request.args.get("fim"))


@app.route("/historico/excluir/<int:venda_id>", methods=["POST"])
@login_required
def historico_excluir(venda_id):
    conn = get_conn()
    cur = conn.cursor()

    try:
        # Primeiro, busca os dados da venda antes de excluir
        cur.execute("""
            SELECT os.item_id, os.quantidade, i.tipo
            FROM ordens_de_servico os
            JOIN itens i ON os.item_id = i.id
            WHERE os.id = ?
        """, (venda_id,))
        venda = cur.fetchone()

        if not venda:
            flash("Venda não encontrada.", "danger")
            conn.close()
            # Mantém filtros ao voltar
            inicio = request.form.get("inicio") or request.args.get("inicio", "")
            fim = request.form.get("fim") or request.args.get("fim", "")
            cliente = request.form.get("cliente") or request.args.get("cliente", "")
            obs = request.form.get("obs") or request.args.get("obs", "")
            return redirect(url_for("historico", inicio=inicio or None, fim=fim or None, cliente=cliente or None, obs=obs or None))

        # Se for produto, devolve ao estoque
        if venda["tipo"] == "produto":
            cur.execute("""
                UPDATE itens 
                SET estoque = estoque + ?
                WHERE id = ?
            """, (venda["quantidade"], venda["item_id"]))

        # Agora exclui a venda
        cur.execute("DELETE FROM ordens_de_servico WHERE id=?", (venda_id,))
        conn.commit()
        flash("Venda cancelada com sucesso. Estoque ajustado (se aplicável).", "success")

    except sqlite3.Error as e:
        conn.rollback()
        flash(f"Erro ao cancelar venda: {e}", "danger")
    finally:
        conn.close()

    # Mantém todos os filtros ao voltar
    inicio = request.form.get("inicio") or request.args.get("inicio", "")
    fim = request.form.get("fim") or request.args.get("fim", "")
    cliente = request.form.get("cliente") or request.args.get("cliente", "")
    obs = request.form.get("obs") or request.args.get("obs", "")

    return redirect(url_for("historico", inicio=inicio or None, fim=fim or None, cliente=cliente or None, obs=obs or None))



# ---------------------- RELATÓRIOS ----------------------

@app.route("/relatorios")
@login_required
@gerente_required
def relatorios():
    inicio = request.args.get("inicio")
    fim = request.args.get("fim")

    if not inicio or not fim:
        return render_template("relatorios.html", linhas=None, inicio=inicio, fim=fim)

    inicio_utc = f"{inicio} 00:00:00"
    fim_utc = f"{fim} 23:59:59"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT os.*, i.nome, i.valor_custo
        FROM ordens_de_servico os
        JOIN itens i ON os.item_id = i.id
        WHERE os.data_hora BETWEEN ? AND ?
        ORDER BY os.data_hora ASC
    """, (inicio_utc, fim_utc))
    linhas_raw = cur.fetchall()
    conn.close()

    if not linhas_raw:
        return render_template("relatorios.html", linhas=None, inicio=inicio, fim=fim)

    tz_local = pytz.timezone("America/Sao_Paulo")
    linhas = []
    total_vendas = 0.0
    total_custo = 0.0

    for l in linhas_raw:
        data_utc = datetime.strptime(l["data_hora"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
        data_local = data_utc.astimezone(tz_local).strftime("%d/%m/%Y %H:%M")
        total_vendas += float(l["total"])
        custo = int(l["quantidade"]) * float(l["valor_custo"])
        total_custo += custo

        linha = dict(l)
        linha["data_local"] = data_local
        linhas.append(linha)

    lucro = total_vendas - total_custo

    return render_template("relatorios.html", linhas=linhas, total_vendas=total_vendas,
                           total_custo=total_custo, lucro=lucro, inicio=inicio, fim=fim)

@app.route("/relatorios/pdf")
@login_required
@gerente_required
def relatorios_pdf():
    inicio = request.args.get("inicio")
    fim = request.args.get("fim")
    inicio, fim, inicio_utc, fim_utc = periodo_utc(inicio, fim)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT os.data_hora, i.nome AS item_nome, os.quantidade, os.total,
               os.cliente_nome, os.metodo_pagamento, os.observacao, i.valor_custo
        FROM ordens_de_servico os
        JOIN itens i ON os.item_id = i.id
        WHERE os.data_hora BETWEEN ? AND ?
        ORDER BY os.data_hora ASC
    """, (inicio_utc, fim_utc))
    vendas = cur.fetchall()
    conn.close()

    buffer = BytesIO()

    from reportlab.lib.pagesizes import A4, landscape
    c = canvas.Canvas(buffer, pagesize=landscape(A4))
    width, height = landscape(A4)

    # Cabeçalho com nome da lanchonete
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(width / 2, height - 25, "Sabor Mix")

    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(width / 2, height - 42, f"Relatório de Vendas: {inicio} a {fim}")

    y = height - 60

    # Cabeçalho da tabela
    c.setFont("Helvetica", 9)
    headers = ["Dia", "Item", "Quant.", "Custo", "Total", "Lucro", "Cliente", "Método", "Obs"]
    x_positions = [20, 120, 250, 290, 340, 390, 440, 520, 580]

    for x, htxt in zip(x_positions, headers):
        c.drawString(x, y, htxt)
    c.line(20, y - 3, 570, y - 3)
    y -= 14

    tz_local = pytz.timezone("America/Sao_Paulo")
    total_periodo = 0.0
    custo_total = 0.0
    lucro_total = 0.0

    for v in vendas:
        if y < 50:
            c.showPage()
            # Redesenha o cabeçalho em nova página
            c.setFont("Helvetica-Bold", 16)
            c.drawCentredString(width / 2, height - 25, "Sabor Mix")
            c.setFont("Helvetica-Bold", 11)
            c.drawCentredString(width / 2, height - 42, f"Relatório de Vendas: {inicio} a {fim}")

            y = height - 60
            c.setFont("Helvetica", 9)
            for x, htxt in zip(x_positions, headers):
                c.drawString(x, y, htxt)
            c.line(20, y - 3, 570, y - 3)
            y -= 14

        data_utc = datetime.strptime(v["data_hora"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
        data_local = data_utc.astimezone(tz_local).strftime("%d/%m/%Y %H:%M:%S")
        item_nome = v["item_nome"][:25]
        quantidade = int(v["quantidade"])
        total = float(v["total"])
        cliente = (v["cliente_nome"] or "")[:12]
        metodo = (v["metodo_pagamento"] or "")[:10]
        obs = (v["observacao"] or "")[:10]
        valor_custo_unit = float(v["valor_custo"])
        custo = quantidade * valor_custo_unit
        lucro = total - custo

        total_periodo += total
        custo_total += custo
        lucro_total += lucro

        values = [
            data_local, item_nome, str(quantidade),
            f"R$ {custo:.2f}", f"R$ {total:.2f}", f"R$ {lucro:.2f}",
            cliente, metodo, obs
        ]
        for x, val in zip(x_positions, values):
            c.drawString(x, y, val)
        y -= 13

    c.line(20, y - 3, 570, y - 3)
    y -= 15
    c.drawString(20, y, f"Total do Período: R$ {total_periodo:.2f}")
    y -= 12
    c.drawString(20, y, f"Custo Total: R$ {custo_total:.2f}")
    y -= 12
    c.drawString(20, y, f"Lucro Total: R$ {lucro_total:.2f}")

    c.save()
    buffer.seek(0)

    filename = f"relatorio_sabormix_{inicio}_a_{fim}.pdf"
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename
    )


@app.route("/relatorios/excel")
@login_required
@gerente_required
def relatorios_excel():
    inicio = request.args.get("inicio")
    fim = request.args.get("fim")
    inicio, fim, inicio_utc, fim_utc = periodo_utc(inicio, fim)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT os.data_hora, i.nome AS item_nome, os.quantidade, os.total,
               os.cliente_nome, os.metodo_pagamento, os.observacao, i.valor_custo
        FROM ordens_de_servico os
        JOIN itens i ON os.item_id = i.id
        WHERE os.data_hora BETWEEN ? AND ?
        ORDER BY os.data_hora ASC
    """, (inicio_utc, fim_utc))
    vendas = cur.fetchall()
    conn.close()

    tz_local = pytz.timezone("America/Sao_Paulo")

    data_rows = []
    total_periodo = 0.0
    custo_total = 0.0
    lucro_total = 0.0

    for v in vendas:
        data_utc = datetime.strptime(v["data_hora"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
        data_local = data_utc.astimezone(tz_local).strftime("%d/%m/%Y %H:%M:%S")
        quantidade = int(v["quantidade"])
        total = float(v["total"])
        valor_custo_unit = float(v["valor_custo"])
        custo = quantidade * valor_custo_unit
        lucro = total - custo
        total_periodo += total
        custo_total += custo
        lucro_total += lucro

        data_rows.append([
            data_local, v["item_nome"], quantidade, custo, total, lucro,
            v["cliente_nome"], v["metodo_pagamento"], v["observacao"]
        ])

    df = pd.DataFrame(data_rows, columns=[
        "Data/Hora", "Item", "Quantidade", "Custo", "Total", "Lucro",
        "Cliente", "Método Pagamento", "Observação"
    ])
    df.loc[len(df)] = ["", "", "", custo_total, total_periodo, lucro_total, "", "", ""]

    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Relatório")
    output.seek(0)

    filename = f"relatorio_{inicio}_a_{fim}.xlsx"
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename
    )


# ---------------------- USUÁRIOS ----------------------

@app.route("/usuarios")
@login_required
@gerente_required
def usuarios():
    edit_user = None
    edit_id = request.args.get("edit_id")

    conn = get_conn()
    cur = conn.cursor()
    if edit_id:
        cur.execute("SELECT * FROM usuarios WHERE id=?", (edit_id,))
        edit_user = cur.fetchone()

    cur.execute("SELECT id, nome, tipo FROM usuarios")
    usuarios = cur.fetchall()
    conn.close()

    return render_template("usuarios.html", usuarios=usuarios, edit_user=edit_user)


@app.route("/usuarios/salvar", methods=["POST"])
@login_required
@gerente_required
def usuarios_salvar():
    user_id = request.form.get("id") or None
    nome = request.form.get("nome", "").strip()
    senha = request.form.get("senha", "")
    tipo = request.form.get("tipo")

    conn = get_conn()
    cur = conn.cursor()
    try:
        if user_id:
            if senha:
                cur.execute(
                    "UPDATE usuarios SET nome=?, senha=?, tipo=? WHERE id=?",
                    (nome, hash_password(senha), tipo, user_id)
                )
            else:
                cur.execute(
                    "UPDATE usuarios SET nome=?, tipo=? WHERE id=?",
                    (nome, tipo, user_id)
                )
        else:
            cur.execute(
                "INSERT INTO usuarios (nome, senha, tipo) VALUES (?, ?, ?)",
                (nome, hash_password(senha), tipo)
            )
        conn.commit()
        flash("Usuário salvo com sucesso.", "success")
    except sqlite3.Error as e:
        conn.rollback()
        flash(f"Erro ao salvar usuário: {e}", "danger")
    finally:
        conn.close()

    return redirect(url_for("usuarios"))


@app.route("/usuarios/excluir/<int:user_id>", methods=["POST"])
@login_required
@gerente_required
def usuarios_excluir(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM usuarios WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    flash("Usuário excluído com sucesso.", "success")
    return redirect(url_for("usuarios"))

@app.route("/backup")
@login_required
@gerente_required
def backup():
    db_path = DB_PATH
    if not os.path.exists(db_path):
        flash("Arquivo do banco não encontrado.", "danger")
        return redirect(url_for("index"))

    # Nome do arquivo com data/hora
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"backup_sabormix_{timestamp}.db"

    return send_file(
        db_path,
        as_attachment=True,
        download_name=filename,
        mimetype="application/octet-stream"
    )
@app.route("/restaurar", methods=["GET", "POST"])
@login_required
@gerente_required
def restaurar_backup():
    if request.method == "POST":
        if 'file' not in request.files:
            flash("Nenhum arquivo selecionado.", "danger")
            return redirect(url_for("restaurar_backup"))

        file = request.files['file']

        if file.filename == '':
            flash("Nenhum arquivo selecionado.", "danger")
            return redirect(url_for("restaurar_backup"))

        if file and file.filename.endswith('.db'):
            try:
                # Salva temporariamente
                temp_path = os.path.join(os.path.dirname(DB_PATH), "temp_backup.db")
                file.save(temp_path)

                # Para o Flask de usar o banco atual (fecha conexões abertas)
                # Copia o arquivo temporário para o lugar do banco atual
                import shutil
                shutil.copy(temp_path, DB_PATH)

                # Remove o temporário
                os.remove(temp_path)

                flash("Backup restaurado com sucesso! O sistema foi atualizado.", "success")
            except Exception as e:
                flash(f"Erro ao restaurar backup: {e}", "danger")
        else:
            flash("Arquivo inválido. Selecione um arquivo .db", "danger")

        return redirect(url_for("index"))

    return render_template("restaurar.html")


# -------------------------------------------------
# MAIN
# -------------------------------------------------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
