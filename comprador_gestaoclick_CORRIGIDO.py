import csv
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

PYTHON_DIR = Path(os.environ.get("PYTHONHOME", "") or Path(os.sys.executable).resolve().parent)
TCL_DIR = PYTHON_DIR / "tcl" / "tcl8.6"
TK_DIR = PYTHON_DIR / "tcl" / "tk8.6"
if TCL_DIR.exists():
    os.environ.setdefault("TCL_LIBRARY", str(TCL_DIR))
if TK_DIR.exists():
    os.environ.setdefault("TK_LIBRARY", str(TK_DIR))

from tkinter import BOTH, END, LEFT, RIGHT, VERTICAL, X, Y, BooleanVar, StringVar, Tk, Toplevel, messagebox, filedialog
from tkinter import ttk


API_BASE = "https://api.gestaoclick.com"
APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "comprador_gestaoclick_config.json"
EQUIVALENCES_FILE = APP_DIR / "equivalencias_compras.json"


def money_or_number(value):
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def text(value):
    return "" if value is None else str(value).strip()


def product_key(product):
    produto_id = text(product.get("produto_id"))
    variacao_id = text(product.get("variacao_id"))
    unidade = text(product.get("sigla_unidade")).upper()
    nome = text(product.get("nome_produto"))
    detalhes = text(product.get("detalhes"))
    if produto_id:
        return ("id", produto_id, variacao_id, unidade)
    return ("texto", nome, detalhes, unidade)


@dataclass
class Origin:
    budget_id: str
    budget_code: str
    customer: str
    seller: str
    date: str
    item_id: str
    product_id: str
    variation_id: str
    quantity: Decimal
    details: str


@dataclass
class PurchaseGroup:
    key: tuple
    product_id: str = ""
    variation_id: str = ""
    name: str = ""
    unit: str = ""
    total_quantity: Decimal = Decimal("0")
    stock: Decimal | None = None
    suggestion: str = ""
    origins: list[Origin] = field(default_factory=list)


class GestaoClickAPI:
    def __init__(self, access_token, secret_token):
        self.headers = {
            "Content-Type": "application/json",
            "access-token": access_token,
            "secret-access-token": secret_token,
        }
        self.last_request = 0.0

    def request(self, path, params=None, method="GET", body=None):
        elapsed = time.monotonic() - self.last_request
        if elapsed < 0.35:
            time.sleep(0.35 - elapsed)

        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GestaoClick retornou erro {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Nao foi possivel acessar o GestaoClick: {exc.reason}") from exc
        finally:
            self.last_request = time.monotonic()

        if payload.get("status") != "success":
            raise RuntimeError(payload.get("message") or "Resposta inesperada do GestaoClick.")
        return payload

    def list_all(self, path, params=None, limit=100, max_pages=200):
        records = []
        page = 1
        while True:
            query = dict(params or {})
            query.update({"pagina": page, "limite": limit})
            payload = self.request(path, query)
            page_records = payload.get("data") or []
            records.extend(page_records)
            meta = payload.get("meta") or {}
            if not meta.get("proxima_pagina") and len(page_records) < limit:
                break
            page += 1
            if page > max_pages:
                raise RuntimeError(f"A consulta {path} excedeu {max_pages} paginas.")
        return records

    def stores(self):
        return self.list_all("/lojas")

    def budget_statuses(self, store_id=None):
        params = {"loja_id": store_id} if store_id else None
        return self.list_all("/situacoes_orcamentos", params)

    def open_budgets(self, status_id, store_id=None, days_back=120):
        params = {
            "situacao_id": status_id,
            "data_inicio": (date.today() - timedelta(days=days_back)).isoformat(),
            "data_fim": date.today().isoformat(),
        }
        if store_id:
            params["loja_id"] = store_id
        return self.list_all("/orcamentos", params)

    def product_search(self, name=None, product_id=None, store_id=None):
        params = {"ativo": 1}
        if name:
            params["nome"] = name
        if store_id:
            params["loja_id"] = store_id
        products = self.list_all("/produtos", params, limit=100, max_pages=20)
        if product_id:
            product_id = str(product_id)
            for item in products:
                if str(item.get("id")) == product_id:
                    return item
        return products[0] if product_id and products else products

    def get_budget(self, budget_id):
        return self.request(f"/orcamentos/{budget_id}").get("data") or {}

    def update_budget(self, budget_id, body):
        # O GestaoClick não permite alterar a loja de um orçamento/pedido já cadastrado.
        # Em alguns retornos da API, o campo da loja pode vir como objeto, vazio ou com
        # formato diferente do esperado pelo PUT. Antes de enviar a atualização, mantemos
        # somente o ID original da loja do próprio orçamento e nunca usamos a Loja ID da tela
        # para sobrescrever esse campo.
        payload = dict(body or {})

        loja = payload.get("loja")
        loja_id = payload.get("loja_id")

        if isinstance(loja, dict) and loja.get("id"):
            payload["loja_id"] = text(loja.get("id"))
        elif isinstance(loja_id, dict) and loja_id.get("id"):
            payload["loja_id"] = text(loja_id.get("id"))
        elif text(loja_id):
            payload["loja_id"] = text(loja_id)
        else:
            # Se a API não devolveu loja_id válido, não enviamos loja_id no PUT.
            # Assim evitamos mandar loja_id vazio/errado e disparar o erro de loja divergente.
            payload.pop("loja_id", None)

        return self.request(f"/orcamentos/{budget_id}", method="PUT", body=payload).get("data") or {}


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_equivalences():
    default = {
        "regras": [
            {
                "produto_id": "",
                "nome_exato": "Caneta Bic",
                "sugestoes": [
                    {
                        "produto_id": "",
                        "nome": "Caneta Bic Cristal",
                        "observacao": "Produto aprovado como alternativa de compra."
                    }
                ]
            }
        ]
    }
    if not EQUIVALENCES_FILE.exists():
        save_json(EQUIVALENCES_FILE, default)
    return load_json(EQUIVALENCES_FILE, default)


class PurchasePlanner:
    def __init__(self, api, store_id, status_id, days_back, include_stock, include_equivalences):
        self.api = api
        self.store_id = store_id
        self.status_id = status_id
        self.days_back = int(days_back)
        self.include_stock = include_stock
        self.include_equivalences = include_equivalences
        self.equivalences = load_equivalences()

    def build(self):
        budgets = self.api.open_budgets(self.status_id, self.store_id, self.days_back)
        groups = {}
        for budget in budgets:
            for wrapped in budget.get("produtos") or []:
                product = wrapped.get("produto") or {}
                key = product_key(product)
                if key not in groups:
                    groups[key] = PurchaseGroup(
                        key=key,
                        product_id=text(product.get("produto_id")),
                        variation_id=text(product.get("variacao_id")),
                        name=text(product.get("nome_produto")),
                        unit=text(product.get("sigla_unidade")).upper(),
                    )
                group = groups[key]
                quantity = money_or_number(product.get("quantidade"))
                group.total_quantity += quantity
                group.origins.append(Origin(
                    budget_id=text(budget.get("id")),
                    budget_code=text(budget.get("codigo")),
                    customer=text(budget.get("nome_cliente")),
                    seller=text(budget.get("nome_vendedor")),
                    date=text(budget.get("data")),
                    item_id=text(product.get("id")),
                    product_id=text(product.get("produto_id")),
                    variation_id=text(product.get("variacao_id")),
                    quantity=quantity,
                    details=text(product.get("detalhes")),
                ))

        if self.include_stock:
            self._fill_stock_and_suggestions(groups.values())
        else:
            for group in groups.values():
                group.suggestion = self._equivalence_suggestion(group)
        return sorted(groups.values(), key=lambda g: g.name.lower())

    def _fill_stock_and_suggestions(self, groups):
        for group in groups:
            try:
                product = self.api.product_search(group.name, group.product_id, self.store_id)
            except Exception as exc:
                group.suggestion = f"Nao foi possivel consultar estoque: {exc}"
                continue
            group.stock = self._stock_for_group(product, group)
            parts = []
            if group.stock is not None:
                if group.stock >= group.total_quantity:
                    parts.append(f"Estoque atual atende: {group.stock:g} disponivel para {group.total_quantity:g} solicitado.")
                elif group.stock > 0:
                    missing = group.total_quantity - group.stock
                    parts.append(f"Estoque parcial: {group.stock:g} disponivel; faltam {missing:g}.")
                else:
                    parts.append("Sem estoque informado/disponivel para este item.")
            equivalence = self._equivalence_suggestion(group)
            if equivalence:
                parts.append(equivalence)
            group.suggestion = " ".join(parts).strip()

    def _stock_for_group(self, product, group):
        if not isinstance(product, dict):
            return None
        if group.variation_id:
            for wrapped in product.get("variacoes") or []:
                variation = wrapped.get("variacao") or {}
                if text(variation.get("id")) == group.variation_id:
                    return money_or_number(variation.get("estoque"))
        if "estoque" in product:
            return money_or_number(product.get("estoque"))
        return None

    def _equivalence_suggestion(self, group):
        if not self.include_equivalences:
            return ""
        suggestions = []
        for rule in self.equivalences.get("regras") or []:
            same_id = text(rule.get("produto_id")) and text(rule.get("produto_id")) == group.product_id
            same_name = text(rule.get("nome_exato")).lower() == group.name.lower()
            if not same_id and not same_name:
                continue
            for suggestion in rule.get("sugestoes") or []:
                name = text(suggestion.get("nome")) or text(suggestion.get("produto_id"))
                note = text(suggestion.get("observacao"))
                if name:
                    suggestions.append(f"Sugestao aprovada: {name}. {note}".strip())
        return " ".join(suggestions)


class BuyerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Gestao de Compras - Orcamentos em Aberto")
        self.root.geometry("1280x760")
        self.config = load_json(CONFIG_FILE, {})
        self.groups = []
        self.group_by_row = {}

        self.access_token = StringVar(value="")
        self.secret_token = StringVar(value="")
        self.store_id = StringVar(value=self.config.get("store_id", ""))
        self.status_id = StringVar(value=self.config.get("status_id", ""))
        self.days_back = StringVar(value=str(self.config.get("days_back", 120)))
        self.include_stock = BooleanVar(value=self.config.get("include_stock", True))
        self.include_equivalences = BooleanVar(value=self.config.get("include_equivalences", True))
        self.status_text = StringVar(value="Informe os tokens e clique em Buscar.")

        self._build_ui()

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=X)

        ttk.Label(top, text="Access token").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.access_token, width=36, show="*").grid(row=0, column=1, padx=5, sticky="ew")
        ttk.Label(top, text="Secret token").grid(row=0, column=2, sticky="w")
        ttk.Entry(top, textvariable=self.secret_token, width=36, show="*").grid(row=0, column=3, padx=5, sticky="ew")
        ttk.Label(top, text="Loja ID").grid(row=0, column=4, sticky="w")
        ttk.Entry(top, textvariable=self.store_id, width=10).grid(row=0, column=5, padx=5)

        ttk.Label(top, text="Situacao em aberto ID").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(top, textvariable=self.status_id, width=12).grid(row=1, column=1, sticky="w", padx=5, pady=(8, 0))
        ttk.Label(top, text="Dias para buscar").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(top, textvariable=self.days_back, width=10).grid(row=1, column=3, sticky="w", padx=5, pady=(8, 0))
        ttk.Checkbutton(top, text="Consultar estoque", variable=self.include_stock).grid(row=1, column=4, sticky="w", pady=(8, 0))
        ttk.Checkbutton(top, text="Usar equivalencias", variable=self.include_equivalences).grid(row=1, column=5, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        buttons.pack(fill=X)
        ttk.Button(buttons, text="Buscar situacao em aberto", command=self.find_open_status).pack(side=LEFT, padx=(0, 6))
        ttk.Button(buttons, text="Buscar e agrupar", command=self.refresh).pack(side=LEFT, padx=(0, 6))
        ttk.Button(buttons, text="Ver origens", command=self.show_origins).pack(side=LEFT, padx=(0, 6))
        ttk.Button(buttons, text="Gravar sugestao nos detalhes", command=self.write_selected_suggestion).pack(side=LEFT, padx=(0, 6))
        ttk.Button(buttons, text="Exportar CSV", command=self.export_csv).pack(side=LEFT, padx=(0, 6))
        ttk.Button(buttons, text="Abrir equivalencias", command=self.open_equivalences).pack(side=RIGHT)

        columns = ("produto", "quantidade", "unidade", "estoque", "origens", "sugestao")
        self.tree = ttk.Treeview(self.root, columns=columns, show="headings", height=20)
        self.tree.heading("produto", text="Produto")
        self.tree.heading("quantidade", text="Qtd. total")
        self.tree.heading("unidade", text="Unidade")
        self.tree.heading("estoque", text="Estoque")
        self.tree.heading("origens", text="Orcamentos")
        self.tree.heading("sugestao", text="Sugestao")
        self.tree.column("produto", width=280)
        self.tree.column("quantidade", width=90, anchor="e")
        self.tree.column("unidade", width=80)
        self.tree.column("estoque", width=90, anchor="e")
        self.tree.column("origens", width=90, anchor="center")
        self.tree.column("sugestao", width=620)
        scroll = ttk.Scrollbar(self.root, orient=VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=LEFT, fill=BOTH, expand=True, padx=(10, 0), pady=(0, 8))
        scroll.pack(side=RIGHT, fill=Y, pady=(0, 8), padx=(0, 10))

        footer = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        footer.pack(fill=X)
        ttk.Label(footer, textvariable=self.status_text).pack(side=LEFT)

    def api(self):
        if not self.access_token.get().strip() or not self.secret_token.get().strip():
            raise RuntimeError("Informe access token e secret token.")
        return GestaoClickAPI(self.access_token.get().strip(), self.secret_token.get().strip())

    def save_config(self):
        save_json(CONFIG_FILE, {
            "store_id": self.store_id.get().strip(),
            "status_id": self.status_id.get().strip(),
            "days_back": self.days_back.get().strip(),
            "include_stock": self.include_stock.get(),
            "include_equivalences": self.include_equivalences.get(),
        })

    def run_background(self, label, func, done):
        self.status_text.set(label)
        def worker():
            try:
                result = func()
                self.root.after(0, lambda: done(result, None))
            except Exception as exc:
                self.root.after(0, lambda: done(None, exc))
        threading.Thread(target=worker, daemon=True).start()

    def find_open_status(self):
        def task():
            api = self.api()
            statuses = api.budget_statuses(self.store_id.get().strip() or None)
            for item in statuses:
                if text(item.get("nome")).lower() == "em aberto":
                    return item
            for item in statuses:
                if "aberto" in text(item.get("nome")).lower():
                    return item
            return None

        def done(result, error):
            if error:
                messagebox.showerror("Erro", str(error))
                self.status_text.set("Erro ao buscar situacoes.")
                return
            if not result:
                messagebox.showwarning("Nao encontrado", "Nao encontrei uma situacao com nome Em aberto.")
                self.status_text.set("Situacao em aberto nao encontrada.")
                return
            self.status_id.set(text(result.get("id")))
            self.save_config()
            self.status_text.set(f"Situacao encontrada: {text(result.get('nome'))} (ID {text(result.get('id'))}).")

        self.run_background("Buscando situacao em aberto...", task, done)

    def refresh(self):
        if not self.status_id.get().strip():
            messagebox.showwarning("Situacao obrigatoria", "Informe o ID da situacao Em aberto ou clique em Buscar situacao em aberto.")
            return
        self.save_config()

        def task():
            planner = PurchasePlanner(
                self.api(),
                self.store_id.get().strip() or None,
                self.status_id.get().strip(),
                self.days_back.get().strip() or 120,
                self.include_stock.get(),
                self.include_equivalences.get(),
            )
            return planner.build()

        def done(result, error):
            if error:
                messagebox.showerror("Erro", str(error))
                self.status_text.set("Erro ao buscar orcamentos.")
                return
            self.groups = result
            self.populate_tree()
            total_items = sum(len(group.origins) for group in self.groups)
            self.status_text.set(f"{len(self.groups)} produtos agrupados a partir de {total_items} linhas de orcamento.")

        self.run_background("Buscando orcamentos e agrupando itens...", task, done)

    def populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.group_by_row.clear()
        for group in self.groups:
            stock = "" if group.stock is None else f"{group.stock:g}"
            row = self.tree.insert("", END, values=(
                group.name,
                f"{group.total_quantity:g}",
                group.unit,
                stock,
                len(group.origins),
                group.suggestion,
            ))
            self.group_by_row[row] = group

    def selected_group(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("Selecione um produto", "Selecione uma linha primeiro.")
            return None
        return self.group_by_row.get(selected[0])

    def show_origins(self):
        group = self.selected_group()
        if not group:
            return
        win = Toplevel(self.root)
        win.title(f"Origens - {group.name}")
        win.geometry("900x420")
        cols = ("orcamento", "cliente", "vendedor", "data", "quantidade", "detalhes")
        tree = ttk.Treeview(win, columns=cols, show="headings")
        for col in cols:
            tree.heading(col, text=col.capitalize())
        tree.column("orcamento", width=90)
        tree.column("cliente", width=180)
        tree.column("vendedor", width=160)
        tree.column("data", width=90)
        tree.column("quantidade", width=90, anchor="e")
        tree.column("detalhes", width=360)
        tree.pack(fill=BOTH, expand=True, padx=10, pady=10)
        for origin in group.origins:
            tree.insert("", END, values=(
                origin.budget_code or origin.budget_id,
                origin.customer,
                origin.seller,
                origin.date,
                f"{origin.quantity:g}",
                origin.details,
            ))

    def write_selected_suggestion(self):
        group = self.selected_group()
        if not group:
            return
        if not group.suggestion:
            messagebox.showinfo("Sem sugestao", "Este produto ainda nao tem sugestao para gravar.")
            return
        if not messagebox.askyesno(
            "Confirmar gravacao",
            "Vou gravar a sugestao no campo detalhes dos itens de origem deste produto. Continuar?"
        ):
            return

        def task():
            api = self.api()
            updated = 0
            marker = "[SUGESTAO COMPRA]"
            for origin in group.origins:
                budget = api.get_budget(origin.budget_id)
                changed = False
                for wrapped in budget.get("produtos") or []:
                    product = wrapped.get("produto") or {}
                    same_item_id = origin.item_id and text(product.get("id")) == origin.item_id
                    same_product = (
                        text(product.get("produto_id")) == origin.product_id
                        and text(product.get("variacao_id")) == origin.variation_id
                        and money_or_number(product.get("quantidade")) == origin.quantity
                    )
                    if not same_item_id and not same_product:
                        continue
                    current = text(product.get("detalhes"))
                    clean = current.split(marker)[0].strip()
                    product["detalhes"] = f"{clean}\n{marker} {group.suggestion}".strip()
                    changed = True
                    break
                if changed:
                    api.update_budget(origin.budget_id, budget)
                    updated += 1
            return updated

        def done(result, error):
            if error:
                messagebox.showerror("Erro", str(error))
                self.status_text.set("Erro ao gravar sugestoes.")
                return
            self.status_text.set(f"Sugestao gravada em {result} item(ns) de orcamento.")
            messagebox.showinfo("Concluido", f"Sugestao gravada em {result} item(ns).")

        self.run_background("Gravando sugestoes no GestaoClick...", task, done)

    def export_csv(self):
        if not self.groups:
            messagebox.showwarning("Nada para exportar", "Busque os orcamentos primeiro.")
            return
        path = filedialog.asksaveasfilename(
            title="Salvar CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=f"compras_agrupadas_{date.today().isoformat()}.csv",
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file, delimiter=";")
            writer.writerow(["produto", "produto_id", "variacao_id", "quantidade_total", "unidade", "estoque", "orcamentos", "sugestao"])
            for group in self.groups:
                writer.writerow([
                    group.name,
                    group.product_id,
                    group.variation_id,
                    f"{group.total_quantity:g}",
                    group.unit,
                    "" if group.stock is None else f"{group.stock:g}",
                    ", ".join(origin.budget_code or origin.budget_id for origin in group.origins),
                    group.suggestion,
                ])
        self.status_text.set(f"CSV salvo em {path}.")

    def open_equivalences(self):
        load_equivalences()
        os.startfile(str(EQUIVALENCES_FILE))


def main():
    root = Tk()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    BuyerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
