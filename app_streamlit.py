import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from datetime import date, timedelta

import streamlit as st


API_BASE = "https://api.gestaoclick.com"


def as_text(value):
    return "" if value is None else str(value).strip()


def as_number(value):
    try:
        return float(str(value or "0").replace(",", "."))
    except ValueError:
        return 0.0


def normalized(value):
    text = unicodedata.normalize("NFD", as_text(value))
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text.lower().strip()


def searchable_name(value):
    return " ".join(
        "".join(ch if ch.isalnum() else " " for ch in normalized(value)).split()
    )


def product_key(product):
    product_id = as_text(product.get("produto_id"))
    variation_id = as_text(product.get("variacao_id"))
    unit = as_text(product.get("sigla_unidade")).upper()
    if product_id:
        return ("id", product_id, variation_id, unit)
    return ("texto", as_text(product.get("nome_produto")), as_text(product.get("detalhes")), unit)


def effective_unit_price(product):
    quantity = as_number(product.get("quantidade"))
    total = as_number(product.get("valor_total"))
    sale = as_number(product.get("valor_venda"))
    if total > 0 and quantity > 0:
        return total / quantity
    return sale


def remove_forbidden_budget_fields(value):
    if isinstance(value, list):
        for item in value:
            remove_forbidden_budget_fields(item)
        return value
    if not isinstance(value, dict):
        return value
    for key in list(value.keys()):
        if normalized(key) in {"loja_id", "nome_loja"}:
            value.pop(key, None)
    for child in list(value.values()):
        remove_forbidden_budget_fields(child)
    return value


def clean_wrapped_items(items, item_key):
    cleaned = []
    for wrapped in items or []:
        item = deepcopy((wrapped or {}).get(item_key) or wrapped or {})
        remove_forbidden_budget_fields(item)
        cleaned.append({item_key: item})
    return cleaned


def budget_edit_payload(budget):
    allowed = [
        "tipo", "codigo", "cliente_id", "nome_cliente", "vendedor_id", "nome_vendedor",
        "tecnico_id", "nome_tecnico", "data", "previsao_entrega", "situacao_id",
        "nome_situacao", "transportadora_id", "nome_transportadora", "centro_custo_id",
        "nome_centro_custo", "aos_cuidados_de", "validade", "introducao",
        "observacoes", "observacoes_interna", "valor_frete", "desconto_valor",
        "desconto_porcentagem", "tipo_desconto", "condicao_pagamento",
        "forma_pagamento_id", "data_primeira_parcela", "numero_parcelas", "intervalo_dias",
    ]
    payload = {key: budget[key] for key in allowed if budget.get(key) is not None}
    payload["tipo"] = payload.get("tipo") or "produto"
    payload["produtos"] = clean_wrapped_items(budget.get("produtos"), "produto")
    payload["servicos"] = clean_wrapped_items(budget.get("servicos"), "servico")
    if budget.get("pagamentos") is not None:
        payload["pagamentos"] = clean_wrapped_items(budget.get("pagamentos"), "pagamento")
    return remove_forbidden_budget_fields(payload)


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
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
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

    def list_all(self, path, params=None, max_pages=200):
        records = []
        for page in range(1, max_pages + 1):
            query = dict(params or {})
            query.update({"pagina": page, "limite": 100})
            payload = self.request(path, query)
            data = payload.get("data") or []
            records.extend(data)
            meta = payload.get("meta") or {}
            if not meta.get("proxima_pagina") and len(data) < 100:
                break
        return records

    def stores(self):
        return self.list_all("/lojas")

    def statuses(self, store_id):
        return self.list_all("/situacoes_orcamentos", {"loja_id": store_id} if store_id else None)

    def open_budgets(self, status_id, store_id, days_back):
        params = {"situacao_id": status_id}
        days = int(days_back or 0)
        if days > 0:
            params["data_inicio"] = (date.today() - timedelta(days=days)).isoformat()
            params["data_fim"] = date.today().isoformat()
        if store_id:
            params["loja_id"] = store_id
        return self.list_all("/orcamentos", params)

    def product_search(self, name=None, product_id=None, store_id=None):
        params = {"ativo": 1}
        if name:
            params["nome"] = name
        if store_id:
            params["loja_id"] = store_id
        products = self.list_all("/produtos", params, max_pages=20)
        if product_id:
            return next((item for item in products if as_text(item.get("id")) == as_text(product_id)), products[0] if products else None)
        return products[0] if products else None

    def product_list_by_name(self, name, store_id, max_pages=3):
        params = {"ativo": 1}
        if name:
            params["nome"] = name
        if store_id:
            params["loja_id"] = store_id
        return self.list_all("/produtos", params, max_pages=max_pages)

    def get_budget(self, budget_id, store_id=None):
        try:
            return self.request(f"/orcamentos/{budget_id}", {"loja_id": store_id} if store_id else None).get("data") or {}
        except Exception:
            if not store_id:
                raise
            return self.request(f"/orcamentos/{budget_id}").get("data") or {}

    def update_budget(self, budget_id, budget, store_id=None):
        body = budget_edit_payload(budget)
        if "loja_id" in json.dumps(body, ensure_ascii=False):
            raise RuntimeError("Payload bloqueado porque ainda continha loja_id.")
        params = {"loja_id": store_id} if store_id else None
        return self.request(f"/orcamentos/{budget_id}", params, method="PUT", body=body).get("data") or {}


def resolve_novaprint_store(api):
    stores = api.stores()
    store = next((item for item in stores if "novaprint" in normalized(item.get("nome"))), None)
    if not store:
        raise RuntimeError("A loja NOVAPRINT nao foi encontrada para estes tokens.")
    return {"id": as_text(store.get("id")), "name": as_text(store.get("nome"))}


def resolve_open_status(statuses):
    return next((s for s in statuses if normalized(s.get("nome")) == "em aberto"), None) or next(
        (s for s in statuses if "aberto" in normalized(s.get("nome"))), None
    )


def stock_for(product, group):
    if not product:
        return None
    if group.get("variationId"):
        for wrapped in product.get("variacoes") or []:
            variation = wrapped.get("variacao") or {}
            if as_text(variation.get("id")) == group.get("variationId"):
                return as_number(variation.get("estoque"))
    return as_number(product.get("estoque")) if product.get("estoque") is not None else None


def similarity(a, b):
    a = searchable_name(a)
    b = searchable_name(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        dp[i][0] = i
    for j in range(len(b) + 1):
        dp[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            dp[i][j] = min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + cost)
    return 1 - dp[-1][-1] / max(len(a), len(b))


def suggestion_query(name):
    blocked = {"para", "com", "sem", "tipo", "ref", "equivalente"}
    words = [w for w in searchable_name(name).split() if len(w) >= 4 and w not in blocked]
    return " ".join(words[:2]) or (words[0] if words else searchable_name(name).split(" ")[0])


def product_candidates(product):
    candidates = []
    product_id = as_text(product.get("id"))
    base_name = as_text(product.get("nome"))
    unit = as_text(product.get("sigla_unidade")).upper()
    variations = product.get("variacoes") or []
    if variations:
        for wrapped in variations:
            variation = wrapped.get("variacao") or {}
            variation_name = as_text(variation.get("nome"))
            candidates.append({
                "productId": product_id,
                "variationId": as_text(variation.get("id")),
                "name": f"{base_name} - {variation_name}" if variation_name else base_name,
                "stock": as_number(variation.get("estoque")),
                "unit": unit,
            })
    else:
        candidates.append({
            "productId": product_id,
            "variationId": "",
            "name": base_name,
            "stock": as_number(product.get("estoque")),
            "unit": unit,
        })
    return candidates


def registered_product_search(api, query, store_id, context_name=""):
    attempts = []

    def add_attempt(value):
        value = as_text(value)
        if value and value not in attempts:
            attempts.append(value)

    add_attempt(query)
    clean_query = searchable_name(query)
    add_attempt(clean_query)
    add_attempt(re.sub(r"([a-zA-Z]+)(\d+)", r"\1 \2", clean_query))
    add_attempt(re.sub(r"\d+", "", clean_query).strip())

    for word in clean_query.split():
        if len(word) >= 4:
            add_attempt(word)

    # If the typed search is too specific or misspelled, use the item being quoted
    # as a fallback because it usually contains the correct catalog keywords.
    add_attempt(suggestion_query(context_name))
    for word in searchable_name(context_name).split():
        if len(word) >= 4:
            add_attempt(word)

    seen_products = set()
    candidates = []
    for attempt in attempts:
        if not attempt:
            continue
        products = api.product_list_by_name(attempt, store_id, max_pages=3)
        for product in products:
            for candidate in product_candidates(product):
                key = (candidate["productId"], candidate["variationId"], candidate["name"])
                if key in seen_products:
                    continue
                seen_products.add(key)
                candidate["busca"] = attempt
                candidates.append(candidate)
        if len(candidates) >= 25:
            break
    return candidates[:25]


def automatic_suggestion(api, group, store_id):
    query = suggestion_query(group["name"])
    if not query:
        return "", []
    products = api.product_list_by_name(query, store_id, max_pages=3)
    matches = []
    for product in products:
        for candidate in product_candidates(product):
            if candidate["stock"] <= 0:
                continue
            if candidate["productId"] == group.get("productId") and candidate["variationId"] == group.get("variationId"):
                continue
            score = similarity(group["name"], candidate["name"])
            if score >= 0.8:
                candidate["score"] = score
                matches.append(candidate)
    matches.sort(key=lambda item: (-item["score"], -item["stock"], item["name"]))
    if not matches:
        return "", []
    best = matches[0]
    return f'Sugestao: substituir por "{best["name"]}" ({round(best["score"] * 100)}% similar, estoque {best["stock"]:g}).', matches[:3]


def product_is_priced(product):
    return effective_unit_price(product) > 0


def all_products_priced(budget):
    products = budget.get("produtos") or []
    return bool(products) and all(product_is_priced((wrapped or {}).get("produto") or {}) for wrapped in products)


def build_groups(api, store_id, open_status_id, days_back, include_stock=True, include_suggestions=True):
    budgets = api.open_budgets(open_status_id, store_id, days_back)
    note = ""
    if not budgets and int(days_back or 0) > 0:
        budgets = api.open_budgets(open_status_id, store_id, 0)
        if budgets:
            note = f"Nenhum orçamento encontrado nos últimos {days_back} dia(s). Mostrando todos em aberto."

    groups = {}
    for budget in budgets:
        for wrapped in budget.get("produtos") or []:
            product = wrapped.get("produto") or {}
            key = product_key(product)
            if key not in groups:
                groups[key] = {
                    "key": key,
                    "productId": as_text(product.get("produto_id")),
                    "variationId": as_text(product.get("variacao_id")),
                    "name": as_text(product.get("nome_produto")),
                    "unit": as_text(product.get("sigla_unidade")).upper(),
                    "totalQuantity": 0.0,
                    "stock": None,
                    "suggestion": "",
                    "equivalences": [],
                    "unitPrice": "",
                    "prices": [],
                    "origins": [],
                }
            group = groups[key]
            quantity = as_number(product.get("quantidade"))
            group["totalQuantity"] += quantity
            price = effective_unit_price(product)
            if price > 0 and not group["unitPrice"]:
                group["unitPrice"] = f"{price:.2f}"
            if price > 0:
                group["prices"].append(round(price, 4))
            group["origins"].append({
                "budgetId": as_text(budget.get("id")),
                "budgetCode": as_text(budget.get("codigo")),
                "itemId": as_text(product.get("id")),
                "productId": as_text(product.get("produto_id")),
                "variationId": as_text(product.get("variacao_id")),
                "quantity": quantity,
                "details": as_text(product.get("detalhes")),
            })

    for group in groups.values():
        parts = []
        unique_prices = sorted(set(group.get("prices") or []))
        if len(unique_prices) > 1:
            parts.append("Atenção: este item tem preços diferentes nos orçamentos agrupados.")
        if include_stock:
            product = api.product_search(group["name"], group["productId"], store_id)
            group["stock"] = stock_for(product, group)
            if group["stock"] is not None:
                if group["stock"] >= group["totalQuantity"]:
                    parts.append(f'Estoque atende: {group["stock"]:g} disponível.')
                elif group["stock"] > 0:
                    parts.append(f'Estoque parcial: {group["stock"]:g}; faltam {group["totalQuantity"] - group["stock"]:g}.')
                else:
                    parts.append("Sem estoque disponível.")
        if include_suggestions:
            suggestion, matches = automatic_suggestion(api, group, store_id)
            group["equivalences"] = matches
            if suggestion:
                parts.append(suggestion)
        group["suggestion"] = " ".join(parts)

    return sorted(groups.values(), key=lambda item: item["name"].lower()), note


def find_matching_product(budget, origin):
    for wrapped in budget.get("produtos") or []:
        product = wrapped.get("produto") or {}
        same_item = origin.get("itemId") and as_text(product.get("id")) == origin.get("itemId")
        same_product = (
            as_text(product.get("produto_id")) == origin.get("productId")
            and as_text(product.get("variacao_id")) == origin.get("variationId")
            and as_number(product.get("quantidade")) == as_number(origin.get("quantity"))
        )
        if same_item or same_product:
            return product
    return None


def grouped_origins(group):
    by_budget = {}
    for origin in group.get("origins") or []:
        by_budget.setdefault(origin["budgetId"], []).append(origin)
    return by_budget


def write_price(api, group, unit_price, store_id, final_status):
    updated = 0
    status_changed = 0
    for budget_id, origins in grouped_origins(group).items():
        budget = api.get_budget(budget_id, store_id)
        changed = False
        for origin in origins:
            product = find_matching_product(budget, origin)
            if not product:
                continue
            product["valor_venda"] = f"{unit_price:.2f}"
            product["valor_total"] = f"{unit_price * as_number(origin.get('quantity')):.2f}"
            updated += 1
            changed = True
        if changed:
            if final_status and all_products_priced(budget):
                budget["situacao_id"] = as_text(final_status["id"])
                budget["nome_situacao"] = as_text(final_status["nome"])
                status_changed += 1
            api.update_budget(budget_id, budget, as_text(budget.get("loja_id")) or store_id)
    return updated, status_changed


def replace_product_text(api, group, description, store_id):
    updated = 0
    for budget_id, origins in grouped_origins(group).items():
        budget = api.get_budget(budget_id, store_id)
        changed = False
        for origin in origins:
            product = find_matching_product(budget, origin)
            if not product:
                continue
            previous = as_text(product.get("nome_produto")) or group["name"]
            product["tipo"] = "S"
            product.pop("produto_id", None)
            product.pop("variacao_id", None)
            product["nome_produto"] = description
            note = f"[SUBSTITUICAO MANUAL] Produto anterior: {previous}. Novo item informado: {description}."
            details = as_text(product.get("detalhes"))
            product["detalhes"] = f"{details}\n{note}".strip()
            updated += 1
            changed = True
        if changed:
            api.update_budget(budget_id, budget, as_text(budget.get("loja_id")) or store_id)
    return updated


def replace_product_registered(api, group, replacement, store_id):
    updated = 0
    for budget_id, origins in grouped_origins(group).items():
        budget = api.get_budget(budget_id, store_id)
        changed = False
        for origin in origins:
            product = find_matching_product(budget, origin)
            if not product:
                continue
            previous = as_text(product.get("nome_produto")) or group["name"]
            product["produto_id"] = replacement["productId"]
            product["variacao_id"] = replacement["variationId"]
            product["nome_produto"] = replacement["name"]
            if replacement.get("unit"):
                product["sigla_unidade"] = replacement["unit"]
            note = f'[SUBSTITUICAO COMPRA] Produto anterior: {previous}. Substituido por: {replacement["name"]}.'
            details = as_text(product.get("detalhes"))
            product["detalhes"] = f"{details}\n{note}".strip()
            updated += 1
            changed = True
        if changed:
            api.update_budget(budget_id, budget, as_text(budget.get("loja_id")) or store_id)
    return updated


def init_state():
    defaults = {
        "groups": [],
        "statuses": [],
        "store": None,
        "open_status": None,
        "manual_options": {},
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def get_api():
    access = st.session_state.get("access_token") or st.secrets.get("GESTAOCLICK_ACCESS_TOKEN", "")
    secret = st.session_state.get("secret_token") or st.secrets.get("GESTAOCLICK_SECRET_TOKEN", "")
    if not access or not secret:
        raise RuntimeError("Informe os tokens do GestãoClick.")
    return GestaoClickAPI(access, secret)


def load_context(api):
    store = resolve_novaprint_store(api)
    statuses = [{"id": as_text(s.get("id")), "nome": as_text(s.get("nome"))} for s in api.statuses(store["id"])]
    open_status = resolve_open_status(statuses)
    if not open_status:
        raise RuntimeError("Não encontrei a situação Em aberto no GestãoClick.")
    st.session_state.store = store
    st.session_state.statuses = statuses
    st.session_state.open_status = open_status


st.set_page_config(page_title="Gestão de Compras", layout="wide")
init_state()

st.title("Gestão de Compras - Orçamentos em Aberto")
st.caption("Streamlit | v busca-tolerante | itens agrupados, preço, substituição manual e sugestão automática")

with st.sidebar:
    st.subheader("Conexão")
    st.text_input("Access token", type="password", key="access_token")
    st.text_input("Secret token", type="password", key="secret_token")
    days_back = st.number_input("Dias para buscar", min_value=0, max_value=3650, value=120, step=10)
    include_stock = st.checkbox("Consultar estoque", value=True)
    include_suggestions = st.checkbox("Sugestão automática", value=True)

    if st.button("Carregar situações"):
        try:
            load_context(get_api())
            st.success("Situações carregadas.")
        except Exception as exc:
            st.error(str(exc))

if st.session_state.statuses:
    status_names = [item["nome"] for item in st.session_state.statuses]
    default_index = 0
    if st.session_state.open_status:
        default_index = next((i for i, s in enumerate(st.session_state.statuses) if s["id"] == st.session_state.open_status["id"]), 0)
    final_status_name = st.selectbox("Status quando terminar", status_names, index=default_index)
else:
    final_status_name = st.text_input("Status quando terminar", value="Carregue as situações")

col_a, col_b = st.columns([1, 5])
with col_a:
    if st.button("Buscar e agrupar", type="primary"):
        try:
            api = get_api()
            if not st.session_state.store or not st.session_state.open_status:
                load_context(api)
            groups, note = build_groups(
                api,
                st.session_state.store["id"],
                st.session_state.open_status["id"],
                days_back,
                include_stock,
                include_suggestions,
            )
            st.session_state.groups = groups
            st.session_state.manual_options = {}
            if note:
                st.info(note)
            st.success(f"{len(groups)} produto(s) agrupado(s).")
        except Exception as exc:
            st.error(str(exc))

with col_b:
    if st.session_state.store:
        st.write(f"Loja: **{st.session_state.store['name']}**")

groups = st.session_state.groups
if not groups:
    st.info("Nenhum item carregado. Clique em Buscar e agrupar.")
else:
    selected_final_status = next((s for s in st.session_state.statuses if s["nome"] == final_status_name), None)
    for index, group in list(enumerate(groups)):
        with st.container(border=True):
            codes = sorted({origin["budgetCode"] or origin["budgetId"] for origin in group["origins"]})
            c1, c2, c3, c4, c5, c6 = st.columns([1.2, 3.5, 0.8, 0.8, 1.1, 1])
            c1.write("**Orçamento**")
            c1.write("\n".join(codes))
            c2.write("**Produto**")
            c2.write(group["name"])
            c3.write("**Qtd.**")
            c3.write(f"{group['totalQuantity']:g}")
            c4.write("**Estoque**")
            c4.write("" if group["stock"] is None else f"{group['stock']:g}")
            price = c5.text_input("Preço unit.", value=group.get("unitPrice", ""), key=f"price_{index}")
            if c6.button("Gravar", key=f"save_{index}"):
                try:
                    api = get_api()
                    updated, status_changed = write_price(
                        api,
                        group,
                        as_number(price),
                        st.session_state.store["id"],
                        selected_final_status,
                    )
                    if updated <= 0:
                        st.warning("Nenhum item foi atualizado. Recarregue a lista e tente novamente.")
                        st.stop()
                    st.session_state.groups.pop(index)
                    st.success(f"Preço gravado em {updated} item(ns). Status alterado em {status_changed} orçamento(s).")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

            if group.get("suggestion"):
                st.info(group["suggestion"])

            m1, m2, m3 = st.columns([3, 1, 2])
            description = m1.text_input("Substituição manual livre", key=f"desc_{index}", placeholder="Digite a descrição exata do item")
            if m2.button("Aplicar", key=f"apply_text_{index}"):
                try:
                    updated = replace_product_text(get_api(), group, description, st.session_state.store["id"])
                    st.success(f"Descrição substituída em {updated} item(ns).")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

            search = m3.text_input("Buscar produto cadastrado", key=f"search_{index}", placeholder="Opcional")
            if m3.button("Buscar cad.", key=f"search_btn_{index}"):
                try:
                    api = get_api()
                    options = registered_product_search(api, search, st.session_state.store["id"], group["name"])
                    st.session_state.manual_options[index] = options[:25]
                    if not options:
                        st.warning("Nenhum produto cadastrado encontrado para essa busca.")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

            options = st.session_state.manual_options.get(index) or []
            if options:
                labels = [f'{item["name"]} | estoque {item["stock"]:g} | busca {item.get("busca", "")}' for item in options]
                selected_label = st.selectbox("Op??es cadastradas", labels, key=f"registered_{index}")
                selected = options[labels.index(selected_label)]
                if st.button("Substituir por cadastrado", key=f"replace_registered_{index}"):
                    try:
                        updated = replace_product_registered(get_api(), group, selected, st.session_state.store["id"])
                        st.success(f"Produto substituído em {updated} item(ns).")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
