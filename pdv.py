"""PDV simples: vendas, estoque e cadastro de produtos (SQLite + Tkinter) com emissão de NF-e (SEFAZ-GO)."""
import json
import re
import sqlite3
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import messagebox, simpledialog, ttk

import fiscal

DB = str(fiscal.BASE / "pdv.db")
# forma de pagamento -> código tPag da NF-e
FORMAS = {"Dinheiro": "01", "Cartão de Crédito": "03", "Cartão de Débito": "04", "PIX": "17"}


# ---------------------------------------------------------------- banco de dados
def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS produtos(
            id INTEGER PRIMARY KEY, codigo TEXT UNIQUE NOT NULL, descricao TEXT NOT NULL,
            ncm TEXT DEFAULT '', unidade TEXT DEFAULT 'UN', preco REAL NOT NULL,
            estoque REAL DEFAULT 0, estoque_min REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS vendas(
            id INTEGER PRIMARY KEY, data TEXT, total REAL, cliente_doc TEXT, cliente_nome TEXT,
            numero_nf INTEGER, chave TEXT, protocolo TEXT, status_nfe TEXT DEFAULT 'NAO_EMITIDA');
        CREATE TABLE IF NOT EXISTS itens_venda(
            id INTEGER PRIMARY KEY, venda_id INTEGER REFERENCES vendas(id),
            produto_id INTEGER REFERENCES produtos(id), qtd REAL, preco REAL);
        CREATE TABLE IF NOT EXISTS movimentos(
            id INTEGER PRIMARY KEY, produto_id INTEGER REFERENCES produtos(id),
            tipo TEXT, qtd REAL, data TEXT, obs TEXT);
        CREATE TABLE IF NOT EXISTS pagamentos(
            id INTEGER PRIMARY KEY, venda_id INTEGER REFERENCES vendas(id), forma TEXT, valor REAL);
        """)
        for col in ("troco REAL DEFAULT 0", "estornada INTEGER DEFAULT 0", "estorno_data TEXT",
                    "estorno_motivo TEXT", "nfe_data TEXT"):  # bancos criados em versões anteriores
            try:
                c.execute(f"ALTER TABLE vendas ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass


def num(s):
    return float(str(s).strip().replace(",", ".") or 0)


def brl(v):
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def mov(c, pid, tipo, qtd, obs=""):
    c.execute("UPDATE produtos SET estoque = estoque + ? WHERE id=?", (qtd, pid))
    c.execute("INSERT INTO movimentos(produto_id,tipo,qtd,data,obs) VALUES(?,?,?,?,?)",
              (pid, tipo, qtd, datetime.now().isoformat(timespec="seconds"), obs))


def finalizar_venda(cart, doc, nome, pags):
    with conn() as c:
        for it in cart:  # confere estoque antes de gravar
            est = c.execute("SELECT estoque, descricao FROM produtos WHERE id=?", (it["id"],)).fetchone()
            if est["estoque"] < it["qtd"]:
                raise ValueError(f"Estoque insuficiente: {est['descricao']} (disponível {est['estoque']:g})")
        total = round(sum(i["qtd"] * i["preco"] for i in cart), 2)
        pago = round(sum(v for _, v in pags), 2)
        dinheiro = sum(v for f, v in pags if f == "Dinheiro")
        troco = round(pago - total, 2)
        if troco < 0:
            raise ValueError(f"Pagamento insuficiente: faltam R$ {brl(-troco)}")
        if troco > dinheiro:
            raise ValueError("O troco só pode sair do valor pago em dinheiro; reduza o cartão/PIX.")
        vid = c.execute("INSERT INTO vendas(data,total,cliente_doc,cliente_nome,troco) VALUES(?,?,?,?,?)",
                        (datetime.now().isoformat(timespec="seconds"), total, doc, nome, troco)).lastrowid
        for forma, valor in pags:
            c.execute("INSERT INTO pagamentos(venda_id,forma,valor) VALUES(?,?,?)", (vid, forma, valor))
        for it in cart:
            c.execute("INSERT INTO itens_venda(venda_id,produto_id,qtd,preco) VALUES(?,?,?,?)",
                      (vid, it["id"], it["qtd"], it["preco"]))
            mov(c, it["id"], "VENDA", -it["qtd"], f"venda {vid}")
    return vid


def emitir_para_venda(vid):
    with conn() as c:
        v = dict(c.execute("SELECT * FROM vendas WHERE id=?", (vid,)).fetchone())
        itens = [dict(r) for r in c.execute(
            """SELECT p.codigo, p.descricao, p.ncm, p.unidade, i.qtd, i.preco FROM itens_venda i
               JOIN produtos p ON p.id=i.produto_id WHERE i.venda_id=?""", (vid,))]
        pags = [(FORMAS[r["forma"]], r["valor"]) for r in c.execute(
            "SELECT forma, valor FROM pagamentos WHERE venda_id=?", (vid,))]
        numero = c.execute("SELECT COALESCE(MAX(numero_nf),0)+1 FROM vendas").fetchone()[0]
        c.execute("UPDATE vendas SET numero_nf=? WHERE id=?", (numero, vid))
    try:
        r = fiscal.emitir_nfe(v, itens, numero, pags)
    except Exception as e:  # falha de certificado, rede, config etc.
        r = {"status": "ERRO", "motivo": str(e), "chave": "", "protocolo": "", "cstat": ""}
    with conn() as c:
        c.execute("UPDATE vendas SET status_nfe=?, chave=?, protocolo=?, nfe_data=? WHERE id=?",
                  (r["status"], r["chave"], r["protocolo"],
                   datetime.now().isoformat(timespec="seconds") if r["status"] == "AUTORIZADA" else None, vid))
    return r


def estornar_venda(vid, motivo):
    """Devolve os itens ao estoque e marca a venda como estornada.
    Se a NF-e estiver autorizada, ela é cancelada na SEFAZ antes (e o estorno só segue se der certo)."""
    motivo = motivo.strip()
    with conn() as c:
        v = c.execute("SELECT * FROM vendas WHERE id=?", (vid,)).fetchone()
    if not v:
        raise ValueError("Venda não encontrada.")
    if v["estornada"]:
        raise ValueError("Esta venda já foi estornada.")
    if not motivo:
        raise ValueError("Informe o motivo do estorno.")
    status, aviso = v["status_nfe"], ""
    if status == "AUTORIZADA":
        if len(motivo) < 15:
            raise ValueError("Com NF-e autorizada, o motivo precisa ter ao menos 15 caracteres.")
        emitida = datetime.fromisoformat(v["nfe_data"] or v["data"])
        if datetime.now() - emitida > timedelta(hours=24):
            raise ValueError("A NF-e foi autorizada há mais de 24 horas e não pode mais ser cancelada "
                             "por aqui. Em Goiás, depois do prazo é preciso emitir NF-e de estorno/devolução "
                             "ou pedir cancelamento extemporâneo à SEFAZ-GO.")
        try:
            r = fiscal.cancelar_nfe(v["chave"], v["protocolo"], motivo)
        except Exception as e:
            raise ValueError(f"Falha ao cancelar a NF-e (estorno não realizado): {e}")
        if r["status"] != "CANCELADA":
            raise ValueError(f"A SEFAZ recusou o cancelamento (estorno não realizado): "
                             f"{r['cstat']} - {r['motivo']}")
        status, aviso = "CANCELADA", f"\nNF-e cancelada na SEFAZ (protocolo {r['protocolo']})."
    elif status == "ERRO":
        aviso = ("\nAtenção: a emissão da NF-e desta venda terminou em erro. Consulte a chave na "
                 "consulta pública da SEFAZ para confirmar se ela chegou a ser autorizada.")
    with conn() as c:
        for it in c.execute("SELECT produto_id, qtd FROM itens_venda WHERE venda_id=?", (vid,)).fetchall():
            mov(c, it["produto_id"], "ESTORNO", it["qtd"], f"estorno venda {vid}")
        c.execute("UPDATE vendas SET estornada=1, estorno_data=?, estorno_motivo=?, status_nfe=? WHERE id=?",
                  (datetime.now().isoformat(timespec="seconds"), motivo, status, vid))
    return aviso


# ---------------------------------------------------------------- interface
BG, CARD, DARK, TXT, MUTED = "#f1f5f9", "#ffffff", "#0f172a", "#1e293b", "#64748b"
BLUE, GREEN, RED, AMBER, LINE = "#2563eb", "#16a34a", "#dc2626", "#d97706", "#e2e8f0"
FONT = "Segoe UI"


def aplicar_estilo(root):
    s = ttk.Style(root)
    s.theme_use("clam")
    root.configure(bg=BG)
    s.configure(".", font=(FONT, 10), background=BG, foreground=TXT)
    s.configure("TFrame", background=BG)
    s.configure("Card.TFrame", background=CARD)
    s.configure("TLabel", background=BG, foreground=TXT)
    s.configure("Card.TLabel", background=CARD)
    s.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=(FONT, 9))
    s.configure("H.TLabel", background=CARD, font=(FONT, 11, "bold"))
    s.configure("Title.TLabel", background=DARK, foreground="white", font=(FONT, 17, "bold"))
    s.configure("Sub.TLabel", background=DARK, foreground="#94a3b8", font=(FONT, 10))
    s.configure("TEntry", fieldbackground="white", padding=6)
    s.configure("TCombobox", padding=6)
    s.map("TCombobox", fieldbackground=[("readonly", "white")], background=[("readonly", "white")])
    s.configure("TCheckbutton", background=CARD)
    s.configure("TNotebook", background=BG, borderwidth=0)
    s.configure("TNotebook.Tab", padding=(26, 10), font=(FONT, 10, "bold"), background=LINE)
    s.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", BLUE)])
    s.configure("Treeview", rowheight=30, background="white", fieldbackground="white",
                borderwidth=0, font=(FONT, 10))
    s.configure("Treeview.Heading", background=LINE, foreground=TXT, font=(FONT, 10, "bold"),
                padding=7, relief="flat")
    s.map("Treeview", background=[("selected", BLUE)], foreground=[("selected", "white")])
    for nome, cor, hover in (("Accent", BLUE, "#1d4ed8"), ("Success", GREEN, "#15803d"),
                             ("Danger", RED, "#b91c1c"), ("Ghost", "#64748b", "#475569")):
        s.configure(f"{nome}.TButton", background=cor, foreground="white", font=(FONT, 10, "bold"),
                    padding=(14, 8), borderwidth=0, focuscolor=cor)
        s.map(f"{nome}.TButton", background=[("active", hover), ("pressed", hover)])
    s.configure("Big.Success.TButton", font=(FONT, 14, "bold"), padding=(14, 14))


def card(parent):
    return ttk.Frame(parent, style="Card.TFrame", padding=12)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PDV")
        self.geometry("1200x800")
        self.minsize(1100, 720)
        aplicar_estilo(self)
        self.cart, self.pags, self.sel_id = [], [], None
        self.build_header()
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=(10, 12))
        self.t_v, self.t_p, self.t_e, self.t_h = (ttk.Frame(nb, padding=12) for _ in range(4))
        for t, n in ((self.t_v, "Vendas"), (self.t_p, "Produtos"), (self.t_e, "Estoque"),
                     (self.t_h, "Histórico / Estorno")):
            nb.add(t, text=n)
        self.build_vendas()
        self.build_produtos()
        self.build_estoque()
        self.build_historico()
        self.bind("<F2>", lambda e: (nb.select(self.t_v), self.v_cod_e.focus()))
        self.bind("<F9>", lambda e: self.finalizar())
        self.refresh()
        self.v_cod_e.focus()

    # ---- cabeçalho
    def build_header(self):
        h = tk.Frame(self, bg=DARK)
        h.pack(fill="x")
        ttk.Label(h, text="PDV VIEIRA", style="Title.TLabel").pack(side="left", padx=(20, 10), pady=14)
        ttk.Label(h, text="Vendas  ·  Estoque  ·  NF-e SEFAZ-GO", style="Sub.TLabel").pack(side="left")
        self.lbl_clock = ttk.Label(h, style="Sub.TLabel")
        self.lbl_clock.pack(side="right", padx=20)
        try:
            cfg = json.loads(fiscal.CONFIG_PATH.read_text(encoding="utf-8"))
            txt, cor = ("NF-e: HOMOLOGAÇÃO", AMBER) if cfg.get("homologacao", True) else ("NF-e: PRODUÇÃO", GREEN)
        except Exception:
            txt, cor = "NF-e: não configurada", MUTED
        tk.Label(h, text=txt, bg=cor, fg="white", font=(FONT, 9, "bold"), padx=10, pady=3).pack(side="right")
        self.tick()

    def tick(self):
        self.lbl_clock.config(text=datetime.now().strftime("%d/%m/%Y   %H:%M:%S"))
        self.after(1000, self.tick)

    # ---- helpers
    def tree(self, parent, cols, widths, h=12, right=(), expand=True):
        box = ttk.Frame(parent)
        box.pack(fill="both", expand=expand, pady=(8, 0))
        t = ttk.Treeview(box, columns=cols, show="headings", height=h)
        sb = ttk.Scrollbar(box, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        for c_, w in zip(cols, widths):
            t.heading(c_, text=c_, anchor="e" if c_ in right else "w")
            t.column(c_, width=w, anchor="e" if c_ in right else "w")
        t.tag_configure("odd", background="#f8fafc")
        t.tag_configure("baixo", foreground=RED)
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return t

    def campo(self, parent, label, var, row, col, span=1, width=10, font=None):
        ttk.Label(parent, text=label, style="Muted.TLabel").grid(
            row=row * 2, column=col, columnspan=span, sticky="w", padx=6, pady=(6, 0))
        kw = {"font": font} if font else {}
        e = ttk.Entry(parent, textvariable=var, width=width, **kw)
        e.grid(row=row * 2 + 1, column=col, columnspan=span, sticky="ew", padx=6, pady=(2, 4))
        return e

    # ---- vendas
    def build_vendas(self):
        self.t_v.columnconfigure(0, weight=1)
        self.t_v.rowconfigure(0, weight=1)
        esq = ttk.Frame(self.t_v)
        esq.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        dp = card(self.t_v)
        dp.grid(row=0, column=1, sticky="ns")
        dp.configure(width=390)
        dp.pack_propagate(False)

        # esquerda: leitura do produto + carrinho
        c = card(esq)
        c.pack(fill="x")
        c.columnconfigure(0, weight=1)
        self.v_cod, self.v_qtd = tk.StringVar(), tk.StringVar(value="1")
        self.v_cod_e = self.campo(c, "Código de barras / código do produto  (F2)", self.v_cod, 0, 0,
                                  font=(FONT, 14))
        self.campo(c, "Qtd", self.v_qtd, 0, 1, width=7, font=(FONT, 14))
        ttk.Button(c, text="Adicionar", style="Accent.TButton", command=self.add_item).grid(
            row=1, column=2, padx=(6, 0), pady=(2, 4))
        self.v_cod_e.bind("<Return>", lambda e: self.add_item())
        self.cart_t = self.tree(esq, ("Código", "Descrição", "Qtd", "Preço", "Subtotal"),
                                (125, 270, 60, 95, 105), right=("Qtd", "Preço", "Subtotal"))
        bt = ttk.Frame(esq)
        bt.pack(fill="x", pady=(10, 0))
        ttk.Button(bt, text="Remover item", style="Danger.TButton", command=self.rem_item).pack(side="left")
        ttk.Button(bt, text="Cancelar venda", style="Ghost.TButton", command=self.cancelar).pack(side="left", padx=8)

        # direita: total, pagamento, cliente
        ttk.Button(dp, text="FINALIZAR VENDA  (F9)", style="Big.Success.TButton",
                   command=self.finalizar).pack(side="bottom", fill="x", pady=(12, 0))
        tb = tk.Frame(dp, bg=DARK)
        tb.pack(fill="x")
        tk.Label(tb, text="TOTAL A PAGAR", bg=DARK, fg="#94a3b8", font=(FONT, 9, "bold")).pack(
            anchor="w", padx=14, pady=(8, 0))
        self.lbl_total = tk.Label(tb, text="R$ 0,00", bg=DARK, fg="#4ade80", font=(FONT, 28, "bold"))
        self.lbl_total.pack(anchor="e", padx=14, pady=(0, 8))

        ttk.Label(dp, text="Pagamento", style="H.TLabel").pack(anchor="w", pady=(10, 4))
        lin = ttk.Frame(dp, style="Card.TFrame")
        lin.pack(fill="x")
        self.p_forma, self.p_valor = tk.StringVar(value="Dinheiro"), tk.StringVar()
        ttk.Combobox(lin, textvariable=self.p_forma, values=list(FORMAS), state="readonly",
                     width=17).pack(side="left", fill="x", expand=True)
        pe = ttk.Entry(lin, textvariable=self.p_valor, width=9, justify="right")
        pe.pack(side="left", padx=6)
        pe.bind("<Return>", lambda e: self.add_pag())
        ttk.Button(lin, text="+", style="Accent.TButton", width=3, command=self.add_pag).pack(side="left")
        self.pag_t = self.tree(dp, ("Forma", "Valor"), (210, 120), h=3, right=("Valor",), expand=False)
        rp = ttk.Frame(dp, style="Card.TFrame")
        rp.pack(fill="x", pady=(6, 0))
        ttk.Button(rp, text="Remover", style="Ghost.TButton", command=self.rem_pag).pack(side="right")
        self.lbl_pag = tk.Label(rp, text="", bg=CARD, font=(FONT, 13, "bold"), anchor="w")
        self.lbl_pag.pack(side="left", fill="x", expand=True)

        self.v_doc, self.v_nome, self.v_nfe = tk.StringVar(), tk.StringVar(), tk.BooleanVar()
        ch = ttk.Frame(dp, style="Card.TFrame")
        ch.pack(fill="x", pady=(10, 0))
        ttk.Label(ch, text="Cliente", style="H.TLabel").pack(side="left")
        ttk.Checkbutton(ch, text="Emitir NF-e", variable=self.v_nfe).pack(side="right")
        for lbl, var in (("CPF / CNPJ (obrigatório p/ NF-e)", self.v_doc), ("Nome", self.v_nome)):
            ttk.Label(dp, text=lbl, style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
            ttk.Entry(dp, textvariable=var).pack(fill="x", pady=(1, 0))

    def total(self):
        return round(sum(i["qtd"] * i["preco"] for i in self.cart), 2)

    def add_item(self):
        cod = self.v_cod.get().strip()
        with conn() as c:
            p = c.execute("SELECT * FROM produtos WHERE codigo=?", (cod,)).fetchone()
        if not p:
            return messagebox.showwarning("PDV", "Produto não encontrado.")
        q = num(self.v_qtd.get() or 1)
        if q <= 0:
            return
        for it in self.cart:
            if it["id"] == p["id"]:
                it["qtd"] += q
                break
        else:
            self.cart.append({"id": p["id"], "codigo": p["codigo"], "descricao": p["descricao"],
                              "qtd": q, "preco": p["preco"]})
        self.v_cod.set("")
        self.v_qtd.set("1")
        self.refresh_cart()
        self.v_cod_e.focus()

    def rem_item(self):
        for s in sorted((self.cart_t.index(s) for s in self.cart_t.selection()), reverse=True):
            del self.cart[s]
        self.refresh_cart()

    def cancelar(self):
        if self.cart and messagebox.askyesno("PDV", "Cancelar a venda em andamento?"):
            self.cart.clear()
            self.pags.clear()
            self.refresh_cart()

    def add_pag(self):
        valor = num(self.p_valor.get())
        if not self.cart or valor <= 0:
            return
        self.pags.append((self.p_forma.get(), round(valor, 2)))
        self.refresh_pag()

    def rem_pag(self):
        for s in sorted((self.pag_t.index(s) for s in self.pag_t.selection()), reverse=True):
            del self.pags[s]
        self.refresh_pag()

    def refresh_pag(self):
        self.pag_t.delete(*self.pag_t.get_children())
        for i, (f, v) in enumerate(self.pags):
            self.pag_t.insert("", "end", tags=("odd",) if i % 2 else (), values=(f, "R$ " + brl(v)))
        falta = round(self.total() - sum(v for _, v in self.pags), 2)
        if falta > 0:
            self.lbl_pag.config(text=f"Falta:  R$ {brl(falta)}", fg=RED)
            self.p_valor.set(f"{falta:.2f}".replace(".", ","))  # sugere o restante
        else:
            self.lbl_pag.config(text=f"Troco:  R$ {brl(-falta)}" if self.cart else "", fg=GREEN)
            self.p_valor.set("")

    def refresh_cart(self):
        self.cart_t.delete(*self.cart_t.get_children())
        for n, i in enumerate(self.cart):
            self.cart_t.insert("", "end", tags=("odd",) if n % 2 else (),
                               values=(i["codigo"], i["descricao"], f"{i['qtd']:g}",
                                       brl(i["preco"]), brl(i["qtd"] * i["preco"])))
        self.lbl_total.config(text="R$ " + brl(self.total()))
        self.refresh_pag()

    def finalizar(self):
        if not self.cart:
            return
        doc = re.sub(r"\D", "", self.v_doc.get())
        if self.v_nfe.get() and len(doc) not in (11, 14):
            return messagebox.showwarning("PDV", "NF-e exige CPF (11) ou CNPJ (14 dígitos) do destinatário.")
        try:
            troco = max(0, round(sum(v for _, v in self.pags) - self.total(), 2))
            vid = finalizar_venda(self.cart, doc, self.v_nome.get().strip(), self.pags)
        except ValueError as e:
            return messagebox.showerror("PDV", str(e))
        msg = f"Venda {vid} registrada." + (f"\nTROCO: R$ {brl(troco)}" if troco else "")
        if self.v_nfe.get():
            self.config(cursor="watch")
            self.update()
            r = emitir_para_venda(vid)
            self.config(cursor="")
            msg += f"\nNF-e: {r['status']} {r.get('cstat', '')} - {r['motivo']}"
            if r["chave"]:
                msg += f"\nChave: {r['chave']}\nProtocolo: {r['protocolo']}"
        messagebox.showinfo("PDV", msg)
        self.cart.clear()
        self.pags.clear()
        self.v_doc.set("")
        self.v_nome.set("")
        self.v_nfe.set(False)
        self.refresh()
        self.v_cod_e.focus()

    # ---- produtos
    def build_produtos(self):
        c = card(self.t_p)
        c.pack(fill="x")
        for i in range(4):
            c.columnconfigure(i, weight=1, uniform="p")
        self.pv = {k: tk.StringVar() for k in ("codigo", "descricao", "ncm", "unidade", "preco", "estoque", "estoque_min")}
        self.pv["unidade"].set("UN")
        self.campo(c, "Código / código de barras", self.pv["codigo"], 0, 0)
        self.campo(c, "Descrição", self.pv["descricao"], 0, 1, span=2)
        self.campo(c, "NCM (8 dígitos)", self.pv["ncm"], 0, 3)
        self.campo(c, "Unidade", self.pv["unidade"], 1, 0)
        self.campo(c, "Preço (R$)", self.pv["preco"], 1, 1)
        self.e_estoque = self.campo(c, "Estoque inicial", self.pv["estoque"], 1, 2)
        self.campo(c, "Estoque mínimo", self.pv["estoque_min"], 1, 3)
        bt = ttk.Frame(c, style="Card.TFrame")
        bt.grid(row=4, column=0, columnspan=4, sticky="w", padx=6, pady=(8, 0))
        ttk.Button(bt, text="Salvar", style="Success.TButton", command=self.salvar_produto).pack(side="left")
        ttk.Button(bt, text="Novo", style="Accent.TButton", command=self.novo_produto).pack(side="left", padx=8)
        ttk.Button(bt, text="Excluir", style="Danger.TButton", command=self.excluir_produto).pack(side="left")
        bar = ttk.Frame(self.t_p)
        bar.pack(fill="x", pady=(12, 0))
        ttk.Label(bar, text="Buscar:").pack(side="left")
        self.busca = tk.StringVar()
        self.busca.trace_add("write", lambda *a: self.refresh_lists())
        ttk.Entry(bar, textvariable=self.busca, width=40).pack(side="left", padx=8)
        self.prod_t = self.tree(self.t_p, ("id", "Código", "Descrição", "NCM", "Un", "Preço", "Estoque"),
                                (50, 140, 380, 100, 60, 110, 100), right=("Preço", "Estoque"))
        self.prod_t.bind("<<TreeviewSelect>>", self.carrega_produto)

    def novo_produto(self):
        self.sel_id = None
        for k, v in self.pv.items():
            v.set("UN" if k == "unidade" else "")
        self.e_estoque.state(["!disabled"])
        self.prod_t.selection_remove(*self.prod_t.selection())

    def carrega_produto(self, _=None):
        s = self.prod_t.selection()
        if not s:
            return
        self.sel_id = int(self.prod_t.item(s[0])["values"][0])
        with conn() as c:
            p = c.execute("SELECT * FROM produtos WHERE id=?", (self.sel_id,)).fetchone()
        for k in self.pv:
            self.pv[k].set(p[k])
        self.e_estoque.state(["disabled"])  # estoque de produto existente só muda na aba Estoque

    def salvar_produto(self):
        v = {k: x.get().strip() for k, x in self.pv.items()}
        if not v["codigo"] or not v["descricao"] or not v["preco"]:
            return messagebox.showwarning("PDV", "Código, descrição e preço são obrigatórios.")
        if v["ncm"] and not re.fullmatch(r"\d{8}", v["ncm"]):
            return messagebox.showwarning("PDV", "NCM deve ter 8 dígitos.")
        try:
            with conn() as c:
                if self.sel_id:
                    c.execute("""UPDATE produtos SET codigo=?,descricao=?,ncm=?,unidade=?,preco=?,estoque_min=?
                                 WHERE id=?""", (v["codigo"], v["descricao"], v["ncm"], v["unidade"],
                                                 num(v["preco"]), num(v["estoque_min"]), self.sel_id))
                else:
                    pid = c.execute("""INSERT INTO produtos(codigo,descricao,ncm,unidade,preco,estoque_min)
                                       VALUES(?,?,?,?,?,?)""", (v["codigo"], v["descricao"], v["ncm"],
                                                                v["unidade"], num(v["preco"]),
                                                                num(v["estoque_min"]))).lastrowid
                    if num(v["estoque"]):
                        mov(c, pid, "ENTRADA", num(v["estoque"]), "estoque inicial")
        except sqlite3.IntegrityError:
            return messagebox.showerror("PDV", "Já existe produto com esse código.")
        self.novo_produto()
        self.refresh()

    def excluir_produto(self):
        if not self.sel_id or not messagebox.askyesno("PDV", "Excluir produto?"):
            return
        try:
            with conn() as c:
                c.execute("DELETE FROM produtos WHERE id=?", (self.sel_id,))
        except sqlite3.IntegrityError:
            return messagebox.showerror("PDV", "Produto já tem vendas/movimentos e não pode ser excluído.")
        self.novo_produto()
        self.refresh()

    # ---- estoque
    def build_estoque(self):
        c = card(self.t_e)
        c.pack(fill="x")
        c.columnconfigure(1, weight=1)
        self.e_qtd, self.e_obs = tk.StringVar(), tk.StringVar()
        self.campo(c, "Quantidade (+ entrada / - saída)", self.e_qtd, 0, 0, width=18)
        self.campo(c, "Observação", self.e_obs, 0, 1)
        ttk.Button(c, text="Lançar movimento", style="Accent.TButton", command=self.lancar).grid(
            row=1, column=2, padx=6, pady=(2, 4))
        self.lbl_baixo = ttk.Label(self.t_e, text="", font=(FONT, 10, "bold"))
        self.lbl_baixo.pack(anchor="w", pady=(10, 0))
        self.est_t = self.tree(self.t_e, ("id", "Código", "Descrição", "Estoque", "Mínimo"),
                               (50, 140, 440, 110, 110), right=("Estoque", "Mínimo"))

    def lancar(self):
        s = self.est_t.selection()
        q = num(self.e_qtd.get())
        if not s or not q:
            return messagebox.showwarning("PDV", "Selecione um produto e informe a quantidade.")
        pid = int(self.est_t.item(s[0])["values"][0])
        with conn() as c:
            atual = c.execute("SELECT estoque FROM produtos WHERE id=?", (pid,)).fetchone()[0]
            if atual + q < 0:
                return messagebox.showerror("PDV", "Estoque ficaria negativo.")
            mov(c, pid, "ENTRADA" if q > 0 else "AJUSTE", q, self.e_obs.get())
        self.e_qtd.set("")
        self.e_obs.set("")
        self.refresh()

    # ---- histórico / estorno
    def build_historico(self):
        c = card(self.t_h)
        c.pack(fill="x")
        ttk.Label(c, text="Período", style="Muted.TLabel").pack(side="left", padx=(6, 6))
        self.h_filtro = tk.StringVar(value="Hoje")
        cb = ttk.Combobox(c, textvariable=self.h_filtro, values=("Hoje", "Últimos 7 dias", "Todas"),
                          state="readonly", width=16)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_hist())
        ttk.Button(c, text="Estornar venda", style="Danger.TButton", command=self.estornar).pack(side="right")
        ttk.Button(c, text="Ver itens", style="Accent.TButton", command=self.ver_itens).pack(side="right", padx=8)
        self.hist_t = self.tree(self.t_h, ("Nº", "Data", "Cliente", "Pagamento", "Total", "NF-e", "Situação"),
                                (60, 120, 220, 260, 110, 120, 120), right=("Total",))
        self.hist_t.tag_configure("estornada", foreground="#94a3b8")
        self.hist_t.bind("<Double-1>", lambda e: self.ver_itens())

    def refresh_hist(self):
        filtro = {"Hoje": "WHERE substr(v.data,1,10) = date('now','localtime')",
                  "Últimos 7 dias": "WHERE v.data >= datetime('now','localtime','-7 days')",
                  "Todas": ""}[self.h_filtro.get()]
        with conn() as c:
            rows = c.execute(f"""SELECT v.*, (SELECT group_concat(forma, ' + ') FROM pagamentos
                                 WHERE venda_id=v.id) AS pgto FROM vendas v {filtro}
                                 ORDER BY v.id DESC LIMIT 500""").fetchall()
        self.hist_t.delete(*self.hist_t.get_children())
        for n, v in enumerate(rows):
            nfe = {"NAO_EMITIDA": "-"}.get(v["status_nfe"], v["status_nfe"])
            tags = ("estornada",) if v["estornada"] else (("odd",) if n % 2 else ())
            self.hist_t.insert("", "end", tags=tags, values=(
                v["id"], datetime.fromisoformat(v["data"]).strftime("%d/%m %H:%M"),
                v["cliente_nome"] or v["cliente_doc"] or "-", v["pgto"] or "-",
                "R$ " + brl(v["total"]), nfe, "ESTORNADA" if v["estornada"] else "Concluída"))

    def venda_selecionada(self):
        s = self.hist_t.selection()
        if not s:
            messagebox.showwarning("PDV", "Selecione uma venda na lista.")
            return None
        return int(self.hist_t.item(s[0])["values"][0])

    def ver_itens(self):
        vid = self.venda_selecionada()
        if vid is None:
            return
        with conn() as c:
            itens = c.execute("""SELECT p.descricao, i.qtd, i.preco FROM itens_venda i
                                 JOIN produtos p ON p.id=i.produto_id WHERE i.venda_id=?""", (vid,)).fetchall()
        linhas = [f"{i['qtd']:g} x {i['descricao']}  -  R$ {brl(i['qtd'] * i['preco'])}" for i in itens]
        messagebox.showinfo(f"Venda {vid}", "\n".join(linhas))

    def estornar(self):
        vid = self.venda_selecionada()
        if vid is None:
            return
        motivo = simpledialog.askstring(
            "Estornar venda", f"Motivo do estorno da venda {vid}\n(se houver NF-e autorizada: mínimo de 15 caracteres)",
            parent=self)
        if motivo is None or not messagebox.askyesno(
                "PDV", f"Confirma o estorno da venda {vid}?\nOs itens voltam ao estoque e, se houver NF-e "
                       "autorizada, ela será cancelada na SEFAZ."):
            return
        self.config(cursor="watch")
        self.update()
        try:
            aviso = estornar_venda(vid, motivo)
        except ValueError as e:
            self.config(cursor="")
            return messagebox.showerror("PDV", str(e))
        self.config(cursor="")
        messagebox.showinfo("PDV", f"Venda {vid} estornada; itens devolvidos ao estoque.{aviso}\n\n"
                                   "Lembre-se de devolver o valor ao cliente: o app não movimenta "
                                   "dinheiro, PIX ou cartão.")
        self.refresh()

    # ---- atualização
    def refresh_lists(self):
        with conn() as c:
            rows = c.execute("SELECT * FROM produtos ORDER BY descricao").fetchall()
        termo = self.busca.get().strip().lower()
        self.prod_t.delete(*self.prod_t.get_children())
        self.est_t.delete(*self.est_t.get_children())
        n_prod = n_est = baixos = 0
        for p in rows:
            baixo = p["estoque"] <= p["estoque_min"]
            if not termo or termo in p["descricao"].lower() or termo in p["codigo"].lower():
                self.prod_t.insert("", "end", tags=("odd",) if n_prod % 2 else (),
                                   values=(p["id"], p["codigo"], p["descricao"], p["ncm"], p["unidade"],
                                           brl(p["preco"]), f"{p['estoque']:g}"))
                n_prod += 1
            tag = ("baixo",) if baixo else (("odd",) if n_est % 2 else ())
            self.est_t.insert("", "end", tags=tag, values=(p["id"], p["codigo"], p["descricao"],
                                                           f"{p['estoque']:g}", f"{p['estoque_min']:g}"))
            n_est += 1
            baixos += baixo
        self.lbl_baixo.config(text=f"{baixos} produto(s) com estoque no mínimo ou abaixo" if baixos
                              else "Todos os produtos acima do estoque mínimo",
                              foreground=RED if baixos else GREEN)

    def refresh(self):
        self.refresh_lists()
        self.refresh_hist()
        self.refresh_cart()


if __name__ == "__main__":
    try:  # texto nítido em telas com escala > 100% no Windows
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    init_db()
    App().mainloop()
