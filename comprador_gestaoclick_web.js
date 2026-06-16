const http = require("http");
const fs = require("fs");
const path = require("path");
const { URL } = require("url");

const API_BASE = "https://api.gestaoclick.com";
const START_PORT = 8787;
const APP_DIR = __dirname;
const CONFIG_FILE = path.join(APP_DIR, "comprador_gestaoclick_config.json");
const EQUIV_FILE = path.join(APP_DIR, "equivalencias_compras.json");

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return fallback;
  }
}

function writeJson(file, data) {
  fs.writeFileSync(file, JSON.stringify(data, null, 2), "utf8");
}

function ensureEquivalences() {
  if (!fs.existsSync(EQUIV_FILE)) {
    writeJson(EQUIV_FILE, {
      regras: [
        {
          produto_id: "",
          nome_exato: "Caneta Bic",
          sugestoes: [
            { produto_id: "", nome: "Caneta Bic Cristal", observacao: "Produto aprovado como alternativa de compra." }
          ]
        }
      ]
    });
  }
  return readJson(EQUIV_FILE, { regras: [] });
}

function asText(value) {
  return value == null ? "" : String(value).trim();
}

function asNumber(value) {
  const n = Number(String(value ?? "0").replace(",", "."));
  return Number.isFinite(n) ? n : 0;
}

function removeForbiddenBudgetFields(value) {
  if (Array.isArray(value)) {
    for (const item of value) removeForbiddenBudgetFields(item);
    return value;
  }
  if (!value || typeof value !== "object") return value;
  for (const key of Object.keys(value)) {
    if (normalized(key) === "loja_id" || normalized(key) === "nome_loja") {
      delete value[key];
    }
  }
  for (const child of Object.values(value)) removeForbiddenBudgetFields(child);
  return value;
}

function cleanWrappedItems(items, itemKey) {
  return (items || []).map((wrapped) => {
    const item = JSON.parse(JSON.stringify(wrapped[itemKey] || wrapped || {}));
    removeForbiddenBudgetFields(item);
    return { [itemKey]: item };
  });
}

function budgetEditPayload(budget) {
  const allowed = [
    "tipo",
    "codigo",
    "cliente_id",
    "nome_cliente",
    "vendedor_id",
    "nome_vendedor",
    "tecnico_id",
    "nome_tecnico",
    "data",
    "previsao_entrega",
    "situacao_id",
    "nome_situacao",
    "transportadora_id",
    "nome_transportadora",
    "centro_custo_id",
    "nome_centro_custo",
    "aos_cuidados_de",
    "validade",
    "introducao",
    "observacoes",
    "observacoes_interna",
    "valor_frete",
    "desconto_valor",
    "desconto_porcentagem",
    "tipo_desconto",
    "condicao_pagamento",
    "forma_pagamento_id",
    "data_primeira_parcela",
    "numero_parcelas",
    "intervalo_dias"
  ];
  const payload = {};
  for (const key of allowed) {
    if (budget[key] !== undefined && budget[key] !== null) payload[key] = budget[key];
  }
  payload.tipo = payload.tipo || "produto";
  payload.produtos = cleanWrappedItems(budget.produtos, "produto");
  payload.servicos = cleanWrappedItems(budget.servicos, "servico");
  if (budget.pagamentos) payload.pagamentos = cleanWrappedItems(budget.pagamentos, "pagamento");
  removeForbiddenBudgetFields(payload);
  return payload;
}

function productKey(product) {
  const produtoId = asText(product.produto_id);
  const variacaoId = asText(product.variacao_id);
  const unidade = asText(product.sigla_unidade).toUpperCase();
  if (produtoId) return ["id", produtoId, variacaoId, unidade].join("|");
  return ["texto", asText(product.nome_produto), asText(product.detalhes), unidade].join("|");
}

class GestaoClickAPI {
  constructor(accessToken, secretToken) {
    this.headers = {
      "Content-Type": "application/json",
      "access-token": accessToken,
      "secret-access-token": secretToken
    };
    this.lastRequest = 0;
  }

  async waitRate() {
    const elapsed = Date.now() - this.lastRequest;
    if (elapsed < 350) await new Promise((resolve) => setTimeout(resolve, 350 - elapsed));
    this.lastRequest = Date.now();
  }

  async request(apiPath, params = {}, method = "GET", body = undefined) {
    await this.waitRate();
    const url = new URL(API_BASE + apiPath);
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
    });
    const response = await fetch(url, {
      method,
      headers: this.headers,
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    const raw = await response.text();
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch {
      throw new Error(`Resposta invalida do GestaoClick: ${raw.slice(0, 300)}`);
    }
    if (!response.ok || payload.status !== "success") {
      throw new Error(payload.message || `GestaoClick retornou erro ${response.status}: ${raw.slice(0, 300)}`);
    }
    return payload;
  }

  async listAll(apiPath, params = {}, maxPages = 200) {
    const records = [];
    for (let page = 1; page <= maxPages; page += 1) {
      const payload = await this.request(apiPath, { ...params, pagina: page, limite: 100 });
      const data = payload.data || [];
      records.push(...data);
      const meta = payload.meta || {};
      if (!meta.proxima_pagina && data.length < 100) break;
    }
    return records;
  }

  stores() {
    return this.listAll("/lojas");
  }

  statuses(storeId) {
    return this.listAll("/situacoes_orcamentos", storeId ? { loja_id: storeId } : {});
  }

  openBudgets(statusId, storeId, daysBack) {
    const params = { situacao_id: statusId };
    const days = Number(daysBack || 0);
    if (days > 0) {
      const end = new Date();
      const start = new Date();
      start.setDate(start.getDate() - days);
      params.data_inicio = start.toISOString().slice(0, 10);
      params.data_fim = end.toISOString().slice(0, 10);
    }
    if (storeId) params.loja_id = storeId;
    return this.listAll("/orcamentos", params);
  }

  async productSearch(name, productId, storeId) {
    const params = { ativo: 1 };
    if (name) params.nome = name;
    if (storeId) params.loja_id = storeId;
    const products = await this.listAll("/produtos", params, 20);
    if (productId) return products.find((item) => String(item.id) === String(productId)) || products[0] || null;
    return products[0] || null;
  }

  async productListByName(name, storeId, maxPages = 3) {
    const params = { ativo: 1 };
    if (name) params.nome = name;
    if (storeId) params.loja_id = storeId;
    return this.listAll("/produtos", params, maxPages);
  }

  products(storeId) {
    const params = { ativo: 1 };
    if (storeId) params.loja_id = storeId;
    return this.listAll("/produtos", params, 200);
  }

  async getBudget(id, storeId) {
    const params = storeId ? { loja_id: storeId } : {};
    try {
      return (await this.request(`/orcamentos/${id}`, params)).data || {};
    } catch (error) {
      if (!storeId) throw error;
      return (await this.request(`/orcamentos/${id}`)).data || {};
    }
  }

  async updateBudget(id, body, storeId) {
    const cleanBody = budgetEditPayload(body);
    removeForbiddenBudgetFields(cleanBody);
    if (JSON.stringify(cleanBody).includes("loja_id")) {
      throw new Error("Bloqueei a gravacao porque ainda havia loja_id no payload limpo.");
    }
    const params = storeId ? { loja_id: storeId } : {};
    return (await this.request(`/orcamentos/${id}`, params, "PUT", cleanBody)).data || {};
  }
}

function normalized(value) {
  return asText(value)
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
}

async function resolveStatusByName(api, storeId, wantedName) {
  const wanted = normalized(wantedName);
  if (!wanted) return null;
  const statuses = await api.statuses(storeId);
  return statuses.find((item) => normalized(item.nome) === wanted)
    || statuses.find((item) => normalized(item.nome).includes(wanted))
    || null;
}

async function resolveStatusByIdOrName(api, storeId, wantedId, wantedName) {
  const statuses = await api.statuses(storeId);
  if (wantedId) {
    const foundById = statuses.find((item) => asText(item.id) === asText(wantedId));
    if (foundById) return foundById;
  }
  const wanted = normalized(wantedName);
  if (!wanted) return null;
  return statuses.find((item) => normalized(item.nome) === wanted)
    || statuses.find((item) => normalized(item.nome).includes(wanted))
    || null;
}

async function resolveNovaprintStore(api) {
  const stores = await api.stores();
  const found = stores.find((item) => normalized(item.nome).includes("novaprint"));
  if (!found) throw new Error("A loja NOVAPRINT nao foi encontrada para estes tokens.");
  return { id: asText(found.id), name: asText(found.nome) };
}

function stockFor(product, group) {
  if (!product) return null;
  if (group.variationId) {
    for (const wrapped of product.variacoes || []) {
      const variation = wrapped.variacao || {};
      if (asText(variation.id) === group.variationId) return asNumber(variation.estoque);
    }
  }
  return product.estoque == null ? null : asNumber(product.estoque);
}

function searchableName(value) {
  return normalized(value).replace(/[^a-z0-9]+/g, " ").replace(/\s+/g, " ").trim();
}

function similarity(a, b) {
  a = searchableName(a);
  b = searchableName(b);
  if (!a || !b) return 0;
  if (a === b) return 1;
  const rows = a.length + 1;
  const cols = b.length + 1;
  const dp = Array.from({ length: rows }, () => Array(cols).fill(0));
  for (let i = 0; i < rows; i += 1) dp[i][0] = i;
  for (let j = 0; j < cols; j += 1) dp[0][j] = j;
  for (let i = 1; i < rows; i += 1) {
    for (let j = 1; j < cols; j += 1) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      dp[i][j] = Math.min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + cost);
    }
  }
  return 1 - dp[a.length][b.length] / Math.max(a.length, b.length);
}

function productCandidates(product) {
  const candidates = [];
  const productId = asText(product.id);
  const baseName = asText(product.nome);
  const baseStock = product.estoque == null ? null : asNumber(product.estoque);
  const variations = product.variacoes || [];
  if (variations.length) {
    for (const wrapped of variations) {
      const variation = wrapped.variacao || {};
      const variationName = asText(variation.nome);
      candidates.push({
        productId,
        variationId: asText(variation.id),
        name: variationName ? `${baseName} - ${variationName}` : baseName,
        stock: asNumber(variation.estoque),
        unit: asText(product.sigla_unidade).toUpperCase()
      });
    }
    return candidates;
  }
  candidates.push({
    productId,
    variationId: "",
    name: baseName,
    stock: baseStock == null ? 0 : baseStock,
    unit: asText(product.sigla_unidade).toUpperCase()
  });
  return candidates;
}

function automaticEquivalences(group, products, enabled) {
  if (!enabled) return [];
  const matches = [];
  for (const product of products || []) {
    for (const candidate of productCandidates(product)) {
      if (!candidate.name || candidate.stock <= 0) continue;
      if (candidate.productId === group.productId && candidate.variationId === group.variationId) continue;
      const score = similarity(group.name, candidate.name);
      if (score < 0.8) continue;
      matches.push({ ...candidate, score });
    }
  }
  return matches
    .sort((a, b) => b.score - a.score || b.stock - a.stock || a.name.localeCompare(b.name, "pt-BR"))
    .slice(0, 3);
}

function suggestionQuery(name) {
  const words = searchableName(name)
    .split(" ")
    .filter((word) => word.length >= 4 && !["para", "com", "sem", "tipo", "ref", "equivalente"].includes(word));
  return words.slice(0, 2).join(" ") || words[0] || searchableName(name).split(" ")[0] || "";
}

function productIsPriced(product) {
  return asNumber(product.valor_venda) > 0;
}

function allProductsPriced(budget) {
  const products = budget.produtos || [];
  if (!products.length) return false;
  return products.every((wrapped) => productIsPriced(wrapped.produto || {}));
}

async function buildGroups(api, params) {
  const store = await resolveNovaprintStore(api);
  params.storeId = store.id;
  if (!params.statusId) {
    const statuses = await api.statuses(params.storeId);
    const found = statuses.find((s) => asText(s.nome).toLowerCase() === "em aberto")
      || statuses.find((s) => asText(s.nome).toLowerCase().includes("aberto"));
    if (!found) throw new Error("Nao encontrei a situacao Em aberto.");
    params.statusId = asText(found.id);
    params.statusName = asText(found.nome);
  }
  let budgets = await api.openBudgets(params.statusId, params.storeId, params.daysBack);
  let note = "";
  if (!budgets.length && Number(params.daysBack || 0) > 0) {
    budgets = await api.openBudgets(params.statusId, params.storeId, 0);
    if (budgets.length) {
      note = `Nenhum orcamento encontrado nos ultimos ${params.daysBack} dia(s). Mostrando todos os orcamentos em aberto.`;
    }
  }
  const map = new Map();
  for (const budget of budgets) {
    for (const wrapped of budget.produtos || []) {
      const product = wrapped.produto || {};
      const key = productKey(product);
      if (!map.has(key)) {
        map.set(key, {
          key,
          productId: asText(product.produto_id),
          variationId: asText(product.variacao_id),
          name: asText(product.nome_produto),
          unit: asText(product.sigla_unidade).toUpperCase(),
          totalQuantity: 0,
          stock: null,
          suggestion: "",
          unitPrice: "",
          origins: []
        });
      }
      const group = map.get(key);
      const quantity = asNumber(product.quantidade);
      group.totalQuantity += quantity;
      const itemPrice = asNumber(product.valor_venda);
      if (itemPrice > 0 && !group.unitPrice) group.unitPrice = itemPrice.toFixed(2);
      group.origins.push({
        budgetId: asText(budget.id),
        budgetCode: asText(budget.codigo),
        customer: asText(budget.nome_cliente),
        seller: asText(budget.nome_vendedor),
        date: asText(budget.data),
        itemId: asText(product.id),
        productId: asText(product.produto_id),
        variationId: asText(product.variacao_id),
        quantity,
        details: asText(product.detalhes)
      });
    }
  }

  for (const group of map.values()) {
    const parts = [];
    if (params.includeStock) {
      try {
        const product = await api.productSearch(group.name, group.productId, params.storeId);
        group.stock = stockFor(product, group);
        if (group.stock !== null) {
          if (group.stock >= group.totalQuantity) parts.push(`Estoque atual atende: ${group.stock} disponivel para ${group.totalQuantity} solicitado.`);
          else if (group.stock > 0) parts.push(`Estoque parcial: ${group.stock} disponivel; faltam ${group.totalQuantity - group.stock}.`);
          else parts.push("Sem estoque disponivel para este item.");
        }
      } catch (error) {
        parts.push(`Nao foi possivel consultar estoque: ${error.message}`);
      }
    }
    group.equivalences = [];
    if (params.includeEquivalences) {
      try {
        const query = suggestionQuery(group.name);
        const products = query ? await api.productListByName(query, params.storeId, 3) : [];
        group.equivalences = automaticEquivalences(group, products, true);
        if (group.equivalences.length) {
          const best = group.equivalences[0];
          parts.push(`Sugestao: substituir por "${best.name}" (${Math.round(best.score * 100)}% similar, estoque ${best.stock}).`);
        }
      } catch (error) {
        parts.push(`Sugestao automatica indisponivel: ${error.message}`);
      }
    }
    group.suggestion = parts.join(" ");
  }
  return {
    store,
    status: { id: params.statusId, nome: params.statusName || "Em aberto" },
    statuses: (await api.statuses(params.storeId)).map((item) => ({ id: asText(item.id), nome: asText(item.nome) })),
    note,
    groups: [...map.values()].sort((a, b) => a.name.localeCompare(b.name, "pt-BR"))
  };
}

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
}

function sendJson(res, status, data) {
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(data));
}

function html() {
  return `<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Gestao de Compras</title>
  <style>
    :root { color-scheme: light; --line:#d8dee8; --ink:#172033; --muted:#657084; --brand:#0f766e; --soft:#eef7f5; --warn:#9a3412; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: Segoe UI, Arial, sans-serif; color:var(--ink); background:#f6f8fb; }
    header { background:#fff; border-bottom:1px solid var(--line); padding:14px 18px; display:flex; align-items:center; justify-content:space-between; gap:12px; }
    h1 { margin:0; font-size:20px; }
    main { padding:16px; }
    .toolbar { display:grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap:10px; align-items:end; background:#fff; border:1px solid var(--line); padding:12px; border-radius:8px; }
    label { display:block; font-size:12px; color:var(--muted); margin-bottom:4px; }
    input { width:100%; height:34px; border:1px solid var(--line); border-radius:6px; padding:6px 8px; font-size:14px; }
    button { height:34px; border:1px solid #0d5f59; background:var(--brand); color:#fff; border-radius:6px; padding:0 12px; cursor:pointer; font-weight:600; }
    button.secondary { background:#fff; color:var(--ink); border-color:var(--line); }
    button.warn { background:#fff7ed; color:var(--warn); border-color:#fed7aa; }
    .checks { display:flex; gap:12px; align-items:center; height:34px; }
    .checks label { margin:0; color:var(--ink); display:flex; gap:5px; align-items:center; white-space:nowrap; }
    .actions { display:flex; gap:8px; margin:12px 0; flex-wrap:wrap; }
    .status { color:var(--muted); font-size:13px; margin-left:auto; align-self:center; }
    table { width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
    th, td { border-bottom:1px solid var(--line); padding:9px 10px; text-align:left; vertical-align:top; font-size:13px; }
    th { background:#eef2f7; color:#334155; font-size:12px; text-transform:uppercase; letter-spacing:.02em; }
    tbody tr { cursor:pointer; }
    tbody td { user-select:text; cursor:text; }
    tr.selected { background:var(--soft); }
    td.num { text-align:right; white-space:nowrap; }
    .price-input { width:110px; text-align:right; }
    .replace-input { width:180px; }
    .replace-select { width:220px; height:30px; }
    .row-action { height:30px; padding:0 10px; }
    .budget-codes { line-height:1.45; white-space:normal; min-width:90px; }
    .equivalence-row td { background:#fffaf0; color:#4b5563; border-bottom:1px solid #f4d7a1; }
    .equivalence-name { padding-left:22px; font-style:italic; }
    .score { color:#0f766e; font-weight:700; }
    .hidden { display:none; }
    .suggestion { max-width:620px; }
    dialog { border:1px solid var(--line); border-radius:8px; width:min(920px, 92vw); padding:0; }
    dialog header { border-bottom:1px solid var(--line); }
    dialog .content { padding:12px; max-height:65vh; overflow:auto; }
    @media (max-width: 900px) { .toolbar { grid-template-columns: 1fr 1fr; } }
  </style>
</head>
<body>
  <header><h1>Gestao de Compras - Orcamentos em Aberto <small style="font-size:12px;color:#657084">v remove-ao-gravar</small></h1><span id="status" class="status">Pronto</span></header>
  <main>
    <section class="toolbar">
      <div><label>Access token</label><input id="accessToken" type="password"></div>
      <div><label>Secret token</label><input id="secretToken" type="password"></div>
      <div><label>Dias para buscar</label><input id="daysBack" value="120"></div>
      <div><label>Status quando terminar</label><select id="finalStatusId"><option value="">Carregue as situacoes</option></select></div>
      <div><label>Loja</label><input value="NOVAPRINT automatico" disabled></div>
      <input id="statusId" class="hidden">
      <div class="checks">
        <label><input id="includeStock" type="checkbox" checked> Estoque</label>
        <label><input id="includeEquiv" type="checkbox" checked> Equivalencias</label>
      </div>
    </section>
    <div class="actions">
      <button onclick="findStatus()">Buscar situacao em aberto</button>
      <button onclick="loadGroups()">Buscar e agrupar</button>
      <button class="secondary" onclick="exportCsv()">Exportar CSV</button>
    </div>
    <table>
      <thead><tr><th>Orcamento</th><th>Produto</th><th>Qtd. total</th><th>Un.</th><th>Estoque</th><th>Preco unit.</th><th>Gravar</th><th>Substituicao manual</th><th>Sugestao</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </main>
<script>
let groups = [];
let selected = -1;
const $ = (id) => document.getElementById(id);
function params() {
  return {
    accessToken: $("accessToken").value.trim(),
    secretToken: $("secretToken").value.trim(),
    statusId: $("statusId").value.trim(),
    daysBack: $("daysBack").value.trim() || "120",
    finalStatusId: $("finalStatusId").value,
    finalStatusName: $("finalStatusId").selectedOptions[0]?.textContent || "",
    includeStock: $("includeStock").checked,
    includeEquivalences: $("includeEquiv").checked
  };
}
function setStatus(text) { $("status").textContent = text; }
async function post(url, body) {
  const res = await fetch(url, { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body) });
  const data = await res.json();
  if (!res.ok || !data.ok) throw new Error(data.error || "Erro inesperado");
  return data;
}
async function findStatus() {
  try {
    setStatus("Buscando situacao...");
    const data = await post("/api/find-status", params());
    $("statusId").value = data.status.id;
    fillFinalStatuses(data.statuses, data.status.id);
    setStatus("Loja " + data.store.name + " | Situacao: " + data.status.nome + " (ID " + data.status.id + ")");
  } catch (e) { alert(e.message); setStatus("Erro"); }
}
async function loadGroups() {
  try {
    setStatus("Buscando e agrupando...");
    const data = await post("/api/groups", params());
    $("statusId").value = data.status?.id || $("statusId").value;
    fillFinalStatuses(data.statuses, $("finalStatusId").value || data.status?.id);
    groups = data.groups;
    selected = -1;
    render();
    setStatus((data.note ? data.note + " | " : "") + "Loja " + data.store.name + " | " + groups.length + " produtos agrupados.");
  } catch (e) { alert(e.message); setStatus("Erro"); }
}
function fillFinalStatuses(statuses, selectedId) {
  if (!statuses || !statuses.length) return;
  const current = selectedId || $("finalStatusId").value;
  $("finalStatusId").innerHTML = statuses.map(s => \`<option value="\${escapeHtml(s.id)}" \${String(s.id) === String(current) ? "selected" : ""}>\${escapeHtml(s.nome)}</option>\`).join("");
}
function render() {
  if (!groups.length) {
    $("rows").innerHTML = '<tr><td colspan="9" style="text-align:center;color:#657084;padding:28px">Nenhum item encontrado. Aumente os dias ou confirme se existem orcamentos com situacao Em aberto.</td></tr>';
    return;
  }
  $("rows").innerHTML = groups.map((g, i) => \`
    <tr data-index="\${i}" class="\${i === selected ? "selected" : ""}">
      <td class="budget-codes">\${budgetCodes(g).map(escapeHtml).join("<br>")}</td>
      <td>\${escapeHtml(g.name)}</td>
      <td class="num">\${g.totalQuantity}</td>
      <td>\${escapeHtml(g.unit)}</td>
      <td class="num">\${g.stock ?? ""}</td>
      <td><input class="price-input" value="\${escapeHtml(g.unitPrice || "")}" onclick="event.stopPropagation()" oninput="groups[\${i}].unitPrice=this.value"></td>
      <td><button class="row-action" onclick="event.stopPropagation(); writePrice(\${i})">Gravar</button></td>
      <td>
        <input class="replace-input" placeholder="Descricao exata" value="\${escapeHtml(g.manualQuery || "")}" onclick="event.stopPropagation()" oninput="groups[\${i}].manualQuery=this.value">
        <button class="row-action" onclick="event.stopPropagation(); replaceFreeTextProduct(\${i})">Aplicar</button>
        <button class="row-action" onclick="event.stopPropagation(); searchManualReplacement(\${i})">Buscar cad.</button>
        \${manualReplacementSelect(g, i)}
      </td>
      <td class="suggestion">\${escapeHtml(g.suggestion || "")}</td>
    </tr>
  \`).join("");
  for (const row of $("rows").querySelectorAll("tr[data-index]")) {
    row.addEventListener("click", () => {
      const selection = window.getSelection();
      if (selection && selection.toString()) return;
      selected = Number(row.dataset.index);
      render();
    });
  }
}
function budgetCodes(g) {
  return [...new Set((g.origins || []).map(o => o.budgetCode || o.budgetId).filter(Boolean))];
}
function equivalenceRows(g) {
  return (g.equivalences || []).map((eq, eqIndex) => \`
    <tr class="equivalence-row">
      <td></td>
      <td class="equivalence-name">Equivalencia: \${escapeHtml(eq.name)}</td>
      <td></td>
      <td>\${escapeHtml(eq.unit || "")}</td>
      <td class="num">\${eq.stock}</td>
      <td></td>
      <td><button class="row-action" onclick="event.stopPropagation(); replaceProduct(\${groups.indexOf(g)}, \${eqIndex})">Substituir</button></td>
      <td>Similaridade <span class="score">\${Math.round(eq.score * 100)}%</span>, com estoque disponivel.</td>
    </tr>
  \`).join("");
}
function manualReplacementSelect(g, i) {
  if (!g.manualOptions || !g.manualOptions.length) return "";
  return \`
    <select class="replace-select" onclick="event.stopPropagation()" onchange="groups[\${i}].manualSelected=this.value">
      \${g.manualOptions.map((option, index) => \`<option value="\${index}" \${String(index) === String(g.manualSelected || "0") ? "selected" : ""}>\${escapeHtml(option.name)} | est. \${option.stock}</option>\`).join("")}
    </select>
    <button class="row-action" onclick="event.stopPropagation(); replaceManualProduct(\${i})">Substituir</button>
  \`;
}
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function currentGroup() {
  if (selected < 0) { alert("Selecione uma linha."); return null; }
  return groups[selected];
}
async function writeSuggestion() {
  const g = currentGroup(); if (!g) return;
  if (!g.suggestion) return alert("Este produto nao tem sugestao.");
  if (!confirm("Gravar a sugestao nos detalhes dos itens de origem no GestaoClick?")) return;
  try {
    setStatus("Gravando sugestoes...");
    const data = await post("/api/write-suggestion", { ...params(), group: g });
    setStatus("Sugestao gravada em " + data.updated + " item(ns).");
    alert("Sugestao gravada em " + data.updated + " item(ns).");
  } catch (e) { alert(e.message); setStatus("Erro"); }
}
async function writePrice(index = selected) {
  if (index < 0) { alert("Selecione uma linha."); return; }
  selected = index;
  const g = groups[index];
  const price = String(g.unitPrice || "").trim().replace(",", ".");
  if (!price || Number.isNaN(Number(price)) || Number(price) < 0) return alert("Informe um preço unitário válido na linha selecionada.");
  if (!confirm("Gravar o preço unitário R$ " + price + " nos itens de origem deste produto no GestãoClick?")) return;
  try {
    setStatus("Gravando preço...");
    const data = await post("/api/write-price", { ...params(), group: g, unitPrice: price });
    const statusText = data.statusChanged
      ? " Status alterado em " + data.statusChanged + " orçamento(s)."
      : "";
    const warning = data.warning ? "\\n\\nAviso: " + data.warning : "";
    groups.splice(index, 1);
    selected = -1;
    render();
    setStatus("Preço gravado em " + data.updated + " item(ns)." + statusText);
    alert("Preço gravado em " + data.updated + " item(ns)." + statusText + warning);
  } catch (e) { alert(e.message); setStatus("Erro"); }
}
async function replaceProduct(groupIndex, eqIndex) {
  const g = groups[groupIndex];
  const eq = g?.equivalences?.[eqIndex];
  if (!g || !eq) return alert("Equivalencia nao encontrada.");
  if (!confirm('Substituir o produto "' + g.name + '" por "' + eq.name + '" nos orcamentos de origem?')) return;
  try {
    setStatus("Substituindo produto...");
    const data = await post("/api/replace-product", { ...params(), group: g, replacement: eq });
    setStatus("Produto substituido em " + data.updated + " item(ns).");
    alert("Produto substituido em " + data.updated + " item(ns). Clique em Buscar e agrupar para atualizar a tela.");
  } catch (e) { alert(e.message); setStatus("Erro"); }
}
async function searchManualReplacement(groupIndex) {
  const g = groups[groupIndex];
  const query = String(g?.manualQuery || "").trim();
  if (!query) return alert("Digite parte do nome do produto substituto.");
  try {
    setStatus("Buscando produto substituto...");
    const data = await post("/api/search-products", { ...params(), query });
    g.manualOptions = data.products;
    g.manualSelected = "0";
    render();
    setStatus(data.products.length + " produto(s) encontrado(s).");
    if (!data.products.length) alert("Nenhum produto com estoque encontrado para essa busca.");
  } catch (e) { alert(e.message); setStatus("Erro"); }
}
async function replaceManualProduct(groupIndex) {
  const g = groups[groupIndex];
  const optionIndex = Number(g?.manualSelected || 0);
  const replacement = g?.manualOptions?.[optionIndex];
  if (!g || !replacement) return alert("Busque e selecione um produto substituto primeiro.");
  if (!confirm('Substituir o produto "' + g.name + '" por "' + replacement.name + '" nos orcamentos de origem?')) return;
  try {
    setStatus("Substituindo produto...");
    const data = await post("/api/replace-product", { ...params(), group: g, replacement });
    setStatus("Produto substituido em " + data.updated + " item(ns).");
    alert("Produto substituido em " + data.updated + " item(ns). Clique em Buscar e agrupar para atualizar a tela.");
  } catch (e) { alert(e.message); setStatus("Erro"); }
}
async function replaceFreeTextProduct(groupIndex) {
  const g = groups[groupIndex];
  const description = String(g?.manualQuery || "").trim();
  if (!description) return alert("Digite a descricao exata do produto substituto.");
  if (!confirm('Substituir o produto "' + g.name + '" pela descricao "' + description + '" nos orcamentos de origem?')) return;
  try {
    setStatus("Aplicando substituicao manual...");
    const data = await post("/api/replace-product-text", { ...params(), group: g, description });
    setStatus("Descricao substituida em " + data.updated + " item(ns).");
    alert("Descricao substituida em " + data.updated + " item(ns). Clique em Buscar e agrupar para atualizar a tela.");
  } catch (e) { alert(e.message); setStatus("Erro"); }
}
function exportCsv() {
  if (!groups.length) return alert("Busque os orcamentos primeiro.");
  const rows = [["produto","produto_id","variacao_id","quantidade_total","unidade","estoque","preco_unitario","orcamentos","sugestao"]];
  for (const g of groups) rows.push([g.name,g.productId,g.variationId,g.totalQuantity,g.unit,g.stock ?? "",g.unitPrice || "",budgetCodes(g).join(", "),g.suggestion || ""]);
  const csv = rows.map(r => r.map(v => '"' + String(v ?? "").replaceAll('"','""') + '"').join(";")).join("\\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], {type:"text/csv;charset=utf-8"}));
  a.download = "compras_agrupadas.csv";
  a.click();
}
</script>
</body>
</html>`;
}

async function handle(req, res) {
  try {
    const host = req.headers.host || "127.0.0.1";
    const url = new URL(req.url, `http://${host}`);
    if (req.method === "GET" && url.pathname === "/") {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      res.end(html());
      return;
    }
    if (req.method !== "POST") {
      sendJson(res, 404, { ok: false, error: "Nao encontrado" });
      return;
    }
    const body = await readBody(req);
    const api = body.accessToken && body.secretToken ? new GestaoClickAPI(body.accessToken, body.secretToken) : null;
    if (url.pathname === "/api/find-status") {
      if (!api) throw new Error("Informe access token e secret token.");
      const store = await resolveNovaprintStore(api);
      body.storeId = store.id;
      const statuses = await api.statuses(body.storeId);
      const found = statuses.find((s) => asText(s.nome).toLowerCase() === "em aberto")
        || statuses.find((s) => asText(s.nome).toLowerCase().includes("aberto"));
      if (!found) throw new Error("Nao encontrei a situacao Em aberto.");
      sendJson(res, 200, {
        ok: true,
        store,
        status: { id: asText(found.id), nome: asText(found.nome) },
        statuses: statuses.map((item) => ({ id: asText(item.id), nome: asText(item.nome) }))
      });
      return;
    }
    if (url.pathname === "/api/groups") {
      if (!api) throw new Error("Informe access token e secret token.");
      const result = await buildGroups(api, body);
      sendJson(res, 200, { ok: true, store: result.store, status: result.status, statuses: result.statuses, note: result.note, groups: result.groups });
      return;
    }
    if (url.pathname === "/api/search-products") {
      if (!api) throw new Error("Informe access token e secret token.");
      const store = await resolveNovaprintStore(api);
      const products = await api.productListByName(asText(body.query), store.id);
      const candidates = [];
      for (const product of products) {
        for (const candidate of productCandidates(product)) {
          if (candidate.stock > 0) candidates.push(candidate);
        }
      }
      sendJson(res, 200, {
        ok: true,
        products: candidates
          .sort((a, b) => b.stock - a.stock || a.name.localeCompare(b.name, "pt-BR"))
          .slice(0, 25)
      });
      return;
    }
    if (url.pathname === "/api/write-price") {
      if (!api) throw new Error("Informe access token e secret token.");
      const store = await resolveNovaprintStore(api);
      body.storeId = store.id;
      const group = body.group;
      const unitPrice = Number(String(body.unitPrice || "").replace(",", "."));
      if (!Number.isFinite(unitPrice) || unitPrice < 0) throw new Error("Preco unitario invalido.");
      const finalStatusName = asText(body.finalStatusName);
      const finalStatus = await resolveStatusByIdOrName(api, body.storeId, body.finalStatusId, finalStatusName);
      const originsByBudget = new Map();
      for (const origin of group.origins || []) {
        if (!originsByBudget.has(origin.budgetId)) originsByBudget.set(origin.budgetId, []);
        originsByBudget.get(origin.budgetId).push(origin);
      }
      let updated = 0;
      let statusChanged = 0;
      let completedWithoutStatus = 0;
      for (const [budgetId, origins] of originsByBudget.entries()) {
        const budget = await api.getBudget(budgetId, body.storeId);
        let changed = false;
        for (const origin of origins) {
          for (const wrapped of budget.produtos || []) {
            const product = wrapped.produto || {};
            const sameItem = origin.itemId && asText(product.id) === origin.itemId;
            const sameProduct = asText(product.produto_id) === origin.productId
              && asText(product.variacao_id) === origin.variationId
              && asNumber(product.quantidade) === Number(origin.quantity);
            if (!sameItem && !sameProduct) continue;
            product.valor_venda = unitPrice.toFixed(2);
            product.valor_total = (unitPrice * Number(origin.quantity || 0)).toFixed(2);
            changed = true;
            updated += 1;
            break;
          }
        }
        if (changed) {
          const budgetStoreId = asText(budget.loja_id) || body.storeId;
          if (allProductsPriced(budget)) {
            if (finalStatus) {
              budget.situacao_id = asText(finalStatus.id);
              budget.nome_situacao = asText(finalStatus.nome);
              statusChanged += 1;
            } else {
              completedWithoutStatus += 1;
            }
          }
          await api.updateBudget(budgetId, budget, budgetStoreId);
        }
      }
      const warning = completedWithoutStatus
        ? `Todos os produtos de ${completedWithoutStatus} orcamento(s) ficaram precificados, mas o status final selecionado nao foi encontrado no GestaoClick.`
        : "";
      sendJson(res, 200, { ok: true, updated, statusChanged, warning });
      return;
    }
    if (url.pathname === "/api/replace-product") {
      if (!api) throw new Error("Informe access token e secret token.");
      const store = await resolveNovaprintStore(api);
      body.storeId = store.id;
      const group = body.group;
      const replacement = body.replacement;
      if (!replacement || !replacement.productId) throw new Error("Produto substituto invalido.");
      const originsByBudget = new Map();
      for (const origin of group.origins || []) {
        if (!originsByBudget.has(origin.budgetId)) originsByBudget.set(origin.budgetId, []);
        originsByBudget.get(origin.budgetId).push(origin);
      }
      let updated = 0;
      for (const [budgetId, origins] of originsByBudget.entries()) {
        const budget = await api.getBudget(budgetId, body.storeId);
        let changed = false;
        for (const origin of origins) {
          for (const wrapped of budget.produtos || []) {
            const product = wrapped.produto || {};
            const sameItem = origin.itemId && asText(product.id) === origin.itemId;
            const sameProduct = asText(product.produto_id) === origin.productId
              && asText(product.variacao_id) === origin.variationId
              && asNumber(product.quantidade) === Number(origin.quantity);
            if (!sameItem && !sameProduct) continue;
            const previousName = asText(product.nome_produto);
            product.produto_id = asText(replacement.productId);
            product.variacao_id = asText(replacement.variationId);
            product.nome_produto = asText(replacement.name);
            if (replacement.unit) product.sigla_unidade = asText(replacement.unit);
            const note = `[SUBSTITUICAO COMPRA] Produto anterior: ${previousName || group.name}. Substituido por: ${replacement.name}.`;
            const currentDetails = asText(product.detalhes);
            product.detalhes = currentDetails ? `${currentDetails}\n${note}` : note;
            changed = true;
            updated += 1;
            break;
          }
        }
        if (changed) {
          const budgetStoreId = asText(budget.loja_id) || body.storeId;
          await api.updateBudget(budgetId, budget, budgetStoreId);
        }
      }
      sendJson(res, 200, { ok: true, updated });
      return;
    }
    if (url.pathname === "/api/replace-product-text") {
      if (!api) throw new Error("Informe access token e secret token.");
      const store = await resolveNovaprintStore(api);
      body.storeId = store.id;
      const group = body.group;
      const description = asText(body.description);
      if (!description) throw new Error("Descricao substituta invalida.");
      const originsByBudget = new Map();
      for (const origin of group.origins || []) {
        if (!originsByBudget.has(origin.budgetId)) originsByBudget.set(origin.budgetId, []);
        originsByBudget.get(origin.budgetId).push(origin);
      }
      let updated = 0;
      for (const [budgetId, origins] of originsByBudget.entries()) {
        const budget = await api.getBudget(budgetId, body.storeId);
        let changed = false;
        for (const origin of origins) {
          for (const wrapped of budget.produtos || []) {
            const product = wrapped.produto || {};
            const sameItem = origin.itemId && asText(product.id) === origin.itemId;
            const sameProduct = asText(product.produto_id) === origin.productId
              && asText(product.variacao_id) === origin.variationId
              && asNumber(product.quantidade) === Number(origin.quantity);
            if (!sameItem && !sameProduct) continue;
            const previousName = asText(product.nome_produto);
            product.tipo = "S";
            delete product.produto_id;
            delete product.variacao_id;
            product.nome_produto = description;
            const note = `[SUBSTITUICAO MANUAL] Produto anterior: ${previousName || group.name}. Novo item informado: ${description}.`;
            const currentDetails = asText(product.detalhes);
            product.detalhes = currentDetails ? `${currentDetails}\n${note}` : note;
            changed = true;
            updated += 1;
            break;
          }
        }
        if (changed) {
          const budgetStoreId = asText(budget.loja_id) || body.storeId;
          await api.updateBudget(budgetId, budget, budgetStoreId);
        }
      }
      sendJson(res, 200, { ok: true, updated });
      return;
    }
    if (url.pathname === "/api/write-suggestion") {
      if (!api) throw new Error("Informe access token e secret token.");
      const store = await resolveNovaprintStore(api);
      body.storeId = store.id;
      const group = body.group;
      const marker = "[SUGESTAO COMPRA]";
      let updated = 0;
      for (const origin of group.origins || []) {
        const budget = await api.getBudget(origin.budgetId, body.storeId);
        let changed = false;
        for (const wrapped of budget.produtos || []) {
          const product = wrapped.produto || {};
          const sameItem = origin.itemId && asText(product.id) === origin.itemId;
          const sameProduct = asText(product.produto_id) === origin.productId
            && asText(product.variacao_id) === origin.variationId
            && asNumber(product.quantidade) === Number(origin.quantity);
          if (!sameItem && !sameProduct) continue;
          const current = asText(product.detalhes);
          const clean = current.split(marker)[0].trim();
          product.detalhes = `${clean}\\n${marker} ${group.suggestion}`.trim();
          changed = true;
          break;
        }
        if (changed) {
          const budgetStoreId = asText(budget.loja_id) || body.storeId;
          await api.updateBudget(origin.budgetId, budget, budgetStoreId);
          updated += 1;
        }
      }
      sendJson(res, 200, { ok: true, updated });
      return;
    }
    if (url.pathname === "/api/open-equivalences") {
      ensureEquivalences();
      require("child_process").exec(`start "" "${EQUIV_FILE}"`);
      sendJson(res, 200, { ok: true });
      return;
    }
    sendJson(res, 404, { ok: false, error: "Nao encontrado" });
  } catch (error) {
    sendJson(res, 500, { ok: false, error: error.message });
  }
}

function startServer(port, attemptsLeft = 20) {
  const server = http.createServer(handle);
  server.on("error", (error) => {
    if (error.code === "EADDRINUSE" && attemptsLeft > 0) {
      startServer(port + 1, attemptsLeft - 1);
      return;
    }
    console.error(error);
    process.exit(1);
  });
  server.listen(port, "127.0.0.1", () => {
    const target = `http://127.0.0.1:${port}`;
    console.log(`Sistema aberto em ${target}`);
    require("child_process").exec(`start "" "${target}"`);
  });
}

startServer(START_PORT);
