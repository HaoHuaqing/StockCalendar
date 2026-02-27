const state = {
  allEvents: [],
  category: "all",
  markets: new Set(["A股", "港股", "美股"]),
  stocks: [],
  activeStockTab: "A",
};

const MARKET_GROUPS = [
  { key: "A", label: "A股" },
  { key: "HK", label: "港股" },
  { key: "US", label: "美股" },
];

const VALID_GROUPS = new Set(MARKET_GROUPS.map((item) => item.key));
const resolveCache = new Map();

const statusBadge = document.getElementById("statusBadge");
const detailPanel = document.getElementById("detailPanel");
const refreshBtn = document.getElementById("refreshBtn");
const saveStocksBtn = document.getElementById("saveStocksBtn");
const stockStatus = document.getElementById("stockStatus");
const stockList = document.getElementById("stockList");
const stockTabs = document.getElementById("stockTabs");
const addStockBtn = document.getElementById("addStockBtn");
const importStocksBtn = document.getElementById("importStocksBtn");
const exportStocksBtn = document.getElementById("exportStocksBtn");
const importStocksInput = document.getElementById("importStocksInput");

let calendar;
let stockIdSeed = 1;

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatUpdatedAt(value) {
  if (!value) return "未更新";
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return value;
  return dt.toLocaleString("zh-CN", { hour12: false });
}

function marketClass(market) {
  if (market === "A股") return "mkt-a";
  if (market === "港股") return "mkt-hk";
  return "mkt-us";
}

function groupInfoByKey(group) {
  return MARKET_GROUPS.find((item) => item.key === group) || MARKET_GROUPS[0];
}

function groupLabel(group) {
  return groupInfoByKey(group).label;
}

function updateAddStockBtnLabel() {
  if (!addStockBtn) return;
  addStockBtn.textContent = `新增${groupLabel(state.activeStockTab)}`;
}

function toCalendarEvent(payload) {
  return {
    id: payload.id,
    title: payload.title,
    start: payload.start,
    allDay: payload.allDay !== false,
    classNames: [
      payload.category === "macro" ? "evt-macro" : "evt-stock",
      marketClass(payload.market),
      payload.isForecast ? "evt-forecast" : "",
    ].filter(Boolean),
    extendedProps: {
      payload,
    },
  };
}

function passesFilter(event) {
  if (state.category !== "all" && event.category !== state.category) {
    return false;
  }

  if (!state.markets.has(event.market)) {
    return false;
  }

  return true;
}

function applyFilters() {
  const events = state.allEvents.filter(passesFilter).map(toCalendarEvent);
  calendar.removeAllEvents();
  calendar.addEventSource(events);
}

function renderDetail(payload) {
  const lines = [
    `<p class="detail-line"><strong>标题：</strong>${escapeHtml(payload.title || "")}</p>`,
    `<p class="detail-line"><strong>日期：</strong>${escapeHtml(String(payload.start || "").replace("T", " "))}</p>`,
    `<p class="detail-line"><strong>市场：</strong>${escapeHtml(payload.market || "-")}</p>`,
    `<p class="detail-line"><strong>类型：</strong>${escapeHtml(payload.eventType || "-")}</p>`,
  ];

  if (payload.description) {
    lines.push(
      `<p class="detail-line"><strong>说明：</strong>${escapeHtml(payload.description)}</p>`
    );
  }

  let linkBlock = "";
  if (payload.sourceUrl) {
    linkBlock = `<a class="detail-link" href="${escapeHtml(
      payload.sourceUrl
    )}" target="_blank" rel="noopener noreferrer">打开来源：${escapeHtml(
      payload.sourceLabel || "原文链接"
    )}</a>`;
  }

  detailPanel.innerHTML = `
    <h2>事件详情</h2>
    ${lines.join("")}
    ${linkBlock}
  `;
}

async function fetchEvents() {
  const response = await fetch("/api/events", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`拉取事件失败: ${response.status}`);
  }
  return response.json();
}

async function loadEvents() {
  statusBadge.textContent = "正在同步数据...";
  const data = await fetchEvents();
  state.allEvents = Array.isArray(data.events) ? data.events : [];
  statusBadge.textContent = `已同步 ${state.allEvents.length} 条 | 更新于 ${formatUpdatedAt(
    data.updatedAt
  )}`;
  applyFilters();
}

function setStockStatus(message, isError = false) {
  stockStatus.textContent = message;
  stockStatus.style.color = isError ? "#9c2d2d" : "#6b7a7f";
}

function nextStockId() {
  const id = String(stockIdSeed);
  stockIdSeed += 1;
  return id;
}

function marketGroupFromServer(stock) {
  const marketCode = String(stock.market_code || "").trim();
  if (marketCode === "116") return "HK";
  if (marketCode === "105") return "US";

  const market = String(stock.market || "").trim();
  if (market.includes("港")) return "HK";
  if (market.includes("美")) return "US";

  return "A";
}

function inferGroupFromCode(rawCode) {
  const code = String(rawCode || "").trim().toUpperCase();
  if (!code) return "A";

  if (code.endsWith(".XHKG")) return "HK";
  if (code.endsWith(".US") || code.endsWith(".XNAS") || code.endsWith(".XNYS")) return "US";
  if (code.endsWith(".XSHE") || code.endsWith(".XSHG")) return "A";

  if (/^\d{5}$/.test(code)) return "HK";
  if (/^\d{6}$/.test(code)) return "A";
  if (/^[A-Z][A-Z0-9._-]*$/.test(code)) return "US";

  return "A";
}

function toDisplayCode(stock) {
  const code = String(stock.code || "").trim().toUpperCase();
  const marketCode = String(stock.market_code || "").trim();

  if (code.includes(".")) return code;
  if (/^\d{6}$/.test(code) && marketCode === "1") return `${code}.XSHG`;
  if (/^\d{6}$/.test(code) && marketCode === "0") return `${code}.XSHE`;
  if (/^\d{1,5}$/.test(code) && marketCode === "116") return `${code.padStart(5, "0")}.XHKG`;
  if (marketCode === "105" && /^[A-Z][A-Z0-9._-]*$/.test(code)) return `${code}.US`;
  return code;
}

function normalizeCodeForSave(group, rawCode) {
  const value = String(rawCode || "").trim().toUpperCase();
  if (!value) return "";

  if (value.includes(".")) return value;

  if (group === "A") {
    if (!/^\d{6}$/.test(value)) return value;
    const suffix = /^[569]/.test(value) ? "XSHG" : "XSHE";
    return `${value}.${suffix}`;
  }

  if (group === "HK") {
    const digits = value.replace(/\D/g, "");
    if (!digits) return value;
    return `${digits.padStart(5, "0")}.XHKG`;
  }

  return `${value}.US`;
}

function collectStocksFromForm(keepEmpty = false) {
  const rows = Array.from(stockList.querySelectorAll(".stock-row"));
  const data = rows.map((row) => {
    const name = row.querySelector("input[data-field='name']")?.value.trim() || "";
    const code = row.querySelector("input[data-field='code']")?.value.trim() || "";
    const group = row.dataset.group || state.activeStockTab;
    const id = row.dataset.id || nextStockId();
    return { id, group, name, code };
  });
  return keepEmpty ? data : data.filter((item) => item.name || item.code);
}

function syncStateFromForm(keepEmpty = true) {
  const activeTabRows = collectStocksFromForm(keepEmpty);
  const others = state.stocks.filter((item) => item.group !== state.activeStockTab);
  state.stocks = [...others, ...activeTabRows];
}

function updateStockTabs() {
  if (!stockTabs) return;

  const counts = { A: 0, HK: 0, US: 0 };
  state.stocks.forEach((item) => {
    if (counts[item.group] !== undefined) {
      counts[item.group] += 1;
    }
  });

  stockTabs.querySelectorAll("button[data-tab]").forEach((btn) => {
    const key = btn.dataset.tab || "A";
    btn.classList.toggle("active", key === state.activeStockTab);
    btn.textContent = `${groupLabel(key)} (${counts[key] || 0})`;
  });
}

function renderStockRows() {
  stockList.innerHTML = "";

  const activeGroup = groupInfoByKey(state.activeStockTab).key;
  const section = document.createElement("div");
  section.className = "market-group";

  const head = document.createElement("div");
  head.className = "market-group-head";
  head.textContent = groupLabel(activeGroup);
  section.appendChild(head);

  const rows = state.stocks.filter((item) => item.group === activeGroup);

  if (rows.length === 0) {
    const empty = document.createElement("p");
    empty.className = "market-empty";
    empty.textContent = `暂无${groupLabel(activeGroup)}股票`;
    section.appendChild(empty);
  } else {
    rows.forEach((stock) => {
      const row = document.createElement("div");
      row.className = "stock-row";
      row.dataset.id = stock.id;
      row.dataset.group = stock.group;

      const nameInput = document.createElement("input");
      nameInput.dataset.field = "name";
      nameInput.placeholder = "股票名称（可只填名称）";
      nameInput.value = stock.name || "";

      const codeInput = document.createElement("input");
      codeInput.dataset.field = "code";
      codeInput.placeholder = "股票代码（可只填代码）";
      codeInput.value = stock.code || "";

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "del-btn";
      delBtn.dataset.action = "remove";
      delBtn.textContent = "删";

      row.appendChild(nameInput);
      row.appendChild(codeInput);
      row.appendChild(delBtn);
      section.appendChild(row);
    });
  }

  stockList.appendChild(section);
  updateStockTabs();
  updateAddStockBtnLabel();
}

async function fetchStocks() {
  const response = await fetch("/api/stocks", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`拉取股票配置失败: ${response.status}`);
  }
  return response.json();
}

function pickFirstNonEmptyGroup() {
  const found = MARKET_GROUPS.find((groupInfo) =>
    state.stocks.some((item) => item.group === groupInfo.key)
  );
  return found ? found.key : "A";
}

async function loadStocks() {
  setStockStatus("正在加载股票配置...");
  const data = await fetchStocks();
  const stocks = Array.isArray(data.stocks) ? data.stocks : [];

  state.stocks = stocks.map((item) => ({
    id: nextStockId(),
    group: marketGroupFromServer(item),
    name: item.name || "",
    code: toDisplayCode(item),
  }));

  state.activeStockTab = pickFirstNonEmptyGroup();
  renderStockRows();
  setStockStatus("支持只填名称或代码，保存时会自动识别补全");
}

function addStockRow(group = state.activeStockTab) {
  syncStateFromForm(true);
  state.stocks.push({
    id: nextStockId(),
    group,
    name: "",
    code: "",
  });
  renderStockRows();
  setStockStatus(`已新增${groupLabel(group)}空行，填写后保存`);
}

function resolveCacheKey(query, group) {
  return `${group}::${String(query || "").trim().toUpperCase()}`;
}

async function resolveStockInput(query, group) {
  const cleanQuery = String(query || "").trim();
  if (!cleanQuery) return null;

  const useGroup = VALID_GROUPS.has(group) ? group : inferGroupFromCode(cleanQuery);
  const key = resolveCacheKey(cleanQuery, useGroup);

  if (resolveCache.has(key)) {
    return resolveCache.get(key);
  }

  const promise = (async () => {
    try {
      const url = `/api/stocks/resolve?q=${encodeURIComponent(cleanQuery)}&group=${encodeURIComponent(
        useGroup
      )}`;
      const response = await fetch(url, { cache: "no-store" });
      let data = {};
      try {
        data = await response.json();
      } catch {
        return null;
      }

      if (!response.ok || !data.ok || !data.stock) {
        return null;
      }
      return data.stock;
    } catch {
      return null;
    }
  })();

  resolveCache.set(key, promise);
  return promise;
}

async function autofillRow(row, { silent = true } = {}) {
  if (!row) return false;

  const nameInput = row.querySelector("input[data-field='name']");
  const codeInput = row.querySelector("input[data-field='code']");
  if (!nameInput || !codeInput) return false;

  const name = nameInput.value.trim();
  const code = codeInput.value.trim();
  if (!name && !code) return false;
  if (name && code) return false;
  if (row.dataset.resolving === "1") return false;

  const query = name || code;
  const group = row.dataset.group || state.activeStockTab;

  row.dataset.resolving = "1";
  row.classList.add("is-resolving");
  const resolved = await resolveStockInput(query, group);
  row.classList.remove("is-resolving");
  delete row.dataset.resolving;

  if (!resolved) {
    if (!silent) {
      setStockStatus(`未识别到“${query}”，可补充另一列后再保存`, true);
    }
    return false;
  }

  if (!name) {
    nameInput.value = resolved.name || "";
  }
  if (!code) {
    codeInput.value = resolved.code || "";
  }

  if (!silent) {
    setStockStatus(`已自动补全：${resolved.name || ""} ${resolved.code || ""}`.trim());
  }
  return true;
}

async function autofillIncompleteStocks() {
  for (const item of state.stocks) {
    const name = String(item.name || "").trim();
    const code = String(item.code || "").trim();

    if (!name && !code) continue;
    if (name && code) continue;

    const resolved = await resolveStockInput(name || code, item.group);
    if (!resolved) continue;

    if (!name) {
      item.name = resolved.name || item.name;
    }
    if (!code) {
      item.code = resolved.code || item.code;
    }
  }
}

function listStocksFromPayload(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload && typeof payload === "object" && Array.isArray(payload.stocks)) {
    return payload.stocks;
  }
  return [];
}

function parseImportedStocks(payload) {
  const list = listStocksFromPayload(payload);
  const imported = [];

  list.forEach((item) => {
    if (!item || typeof item !== "object") return;

    const name = String(item.name || "").trim();
    const rawCode = String(item.code || "").trim();
    if (!name && !rawCode) return;

    let group = "";
    const groupRaw = String(item.group || "").trim().toUpperCase();
    if (VALID_GROUPS.has(groupRaw)) {
      group = groupRaw;
    }

    if (!group) {
      const hasMarketMeta =
        item.market_code !== undefined || item.marketCode !== undefined || item.market !== undefined;
      if (hasMarketMeta) {
        group = marketGroupFromServer(item);
      }
    }

    if (!group) {
      group = inferGroupFromCode(rawCode);
    }

    if (!group || !VALID_GROUPS.has(group)) {
      group = "A";
    }

    const code = rawCode ? normalizeCodeForSave(group, rawCode) : "";

    imported.push({
      id: nextStockId(),
      group,
      name,
      code,
    });
  });

  return imported;
}

function exportStocksToFile() {
  syncStateFromForm(true);

  const stocks = state.stocks
    .filter((item) => item.name || item.code)
    .map((item) => ({
      name: item.name,
      code: normalizeCodeForSave(item.group, item.code),
    }));

  if (stocks.length === 0) {
    setStockStatus("当前没有可导出的股票", true);
    return;
  }

  const payload = {
    exportedAt: new Date().toISOString(),
    stocks,
  };

  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\..+$/, "");
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);

  const link = document.createElement("a");
  link.href = url;
  link.download = `stocks-${stamp}.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();

  URL.revokeObjectURL(url);
  setStockStatus(`已导出 ${stocks.length} 只股票`);
}

async function importStocksFromFile(file) {
  if (!file) return;

  const text = await file.text();
  const payload = JSON.parse(text);
  const imported = parseImportedStocks(payload);

  if (imported.length === 0) {
    setStockStatus("导入失败：未识别到有效股票数据", true);
    return;
  }

  if (!window.confirm(`确认用导入文件覆盖当前列表吗？\n导入数量：${imported.length}`)) {
    return;
  }

  state.stocks = imported;
  state.activeStockTab = pickFirstNonEmptyGroup();
  renderStockRows();

  const incompleteCount = imported.filter((item) => !item.name || !item.code).length;
  if (incompleteCount > 0) {
    setStockStatus(`已导入 ${imported.length} 条，其中 ${incompleteCount} 条待自动识别，点击保存生效`);
  } else {
    setStockStatus(`已导入 ${imported.length} 条，点击保存生效`);
  }
}

async function saveStocks() {
  syncStateFromForm(true);
  state.stocks = state.stocks.filter((item) => item.name || item.code);

  if (state.stocks.length === 0) {
    setStockStatus("请至少保留 1 只股票", true);
    return;
  }

  const hasPartial = state.stocks.some((item) => !item.name || !item.code);
  if (hasPartial) {
    setStockStatus("正在自动识别未完整填写的行...");
    await autofillIncompleteStocks();
    renderStockRows();
  }

  const incomplete = state.stocks.some((item) => !item.name || !item.code);
  if (incomplete) {
    setStockStatus("仍有未识别条目，请补全名称或代码后再保存", true);
    return;
  }

  const stocks = state.stocks.map((item) => ({
    name: item.name,
    code: normalizeCodeForSave(item.group, item.code),
  }));

  saveStocksBtn.disabled = true;
  const oldText = saveStocksBtn.textContent;
  saveStocksBtn.textContent = "保存中...";
  setStockStatus("正在保存并刷新数据...");

  try {
    const response = await fetch("/api/stocks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stocks }),
    });

    const data = await response.json();
    if (!response.ok || !data.ok) {
      throw new Error(data.error || `保存失败: ${response.status}`);
    }

    const saved = Array.isArray(data.stocks) ? data.stocks : [];
    state.stocks = saved.map((item) => ({
      id: nextStockId(),
      group: marketGroupFromServer(item),
      name: item.name || "",
      code: toDisplayCode(item),
    }));

    state.activeStockTab = pickFirstNonEmptyGroup();
    renderStockRows();
    setStockStatus(`保存成功，当前 ${state.stocks.length} 只股票`);
    await loadEvents();
  } catch (error) {
    setStockStatus(`保存失败：${error.message}`, true);
  } finally {
    saveStocksBtn.disabled = false;
    saveStocksBtn.textContent = oldText;
  }
}

function bindCategoryFilters() {
  const group = document.getElementById("categoryFilters");
  group.addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-kind]");
    if (!btn) return;

    state.category = btn.dataset.kind;
    for (const node of group.querySelectorAll("button")) {
      node.classList.toggle("active", node === btn);
    }

    applyFilters();
  });
}

function bindMarketFilters() {
  const group = document.getElementById("marketFilters");
  group.addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-market]");
    if (!btn) return;

    const market = btn.dataset.market;
    if (state.markets.has(market)) {
      state.markets.delete(market);
      btn.classList.remove("active");
    } else {
      state.markets.add(market);
      btn.classList.add("active");
    }

    applyFilters();
  });
}

function bindStockEditor() {
  stockList.addEventListener("click", (event) => {
    const removeBtn = event.target.closest("button[data-action='remove']");
    if (!removeBtn) return;

    const row = removeBtn.closest(".stock-row");
    if (!row) return;

    const rowId = row.dataset.id;
    if (!rowId) return;
    if (!window.confirm("确认删除这只股票吗？")) return;

    syncStateFromForm(true);
    state.stocks = state.stocks.filter((item) => item.id !== rowId);
    renderStockRows();
    setStockStatus("已删除，点击保存后生效");
  });

  stockList.addEventListener("change", async (event) => {
    const input = event.target.closest("input[data-field]");
    if (!input) return;

    const row = input.closest(".stock-row");
    if (!row) return;

    await autofillRow(row, { silent: false });
    syncStateFromForm(true);
    updateStockTabs();
  });

  if (stockTabs) {
    stockTabs.addEventListener("click", (event) => {
      const btn = event.target.closest("button[data-tab]");
      if (!btn) return;

      const tab = btn.dataset.tab || "A";
      if (!VALID_GROUPS.has(tab) || tab === state.activeStockTab) return;

      syncStateFromForm(true);
      state.activeStockTab = tab;
      renderStockRows();
      setStockStatus(`已切换到${groupLabel(tab)}页签`);
    });
  }

  if (addStockBtn) {
    addStockBtn.addEventListener("click", () => {
      addStockRow(state.activeStockTab);
    });
  }

  if (importStocksBtn && importStocksInput) {
    importStocksBtn.addEventListener("click", () => {
      importStocksInput.click();
    });

    importStocksInput.addEventListener("change", async () => {
      const file = importStocksInput.files && importStocksInput.files[0];
      if (!file) return;

      try {
        await importStocksFromFile(file);
      } catch (error) {
        setStockStatus(`导入失败：${error.message}`, true);
      } finally {
        importStocksInput.value = "";
      }
    });
  }

  if (exportStocksBtn) {
    exportStocksBtn.addEventListener("click", exportStocksToFile);
  }

  saveStocksBtn.addEventListener("click", saveStocks);
}

async function triggerRefresh() {
  refreshBtn.disabled = true;
  const oldText = refreshBtn.textContent;
  refreshBtn.textContent = "刷新中...";
  statusBadge.textContent = "后台抓取中...";

  try {
    const response = await fetch("/api/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });

    if (!response.ok) {
      throw new Error(`刷新失败: ${response.status}`);
    }

    await loadEvents();
  } catch (error) {
    statusBadge.textContent = `刷新失败：${error.message}`;
  } finally {
    refreshBtn.disabled = false;
    refreshBtn.textContent = oldText;
  }
}

function initCalendar() {
  const calendarEl = document.getElementById("calendar");

  calendar = new FullCalendar.Calendar(calendarEl, {
    locale: "zh-cn",
    initialView: "dayGridMonth",
    height: "auto",
    firstDay: 1,
    dayMaxEvents: false,
    dayMaxEventRows: false,
    displayEventTime: false,
    headerToolbar: {
      left: "prev,next today",
      center: "title",
      right: "dayGridMonth,timeGridWeek,listWeek",
    },
    buttonText: {
      today: "今天",
      month: "月",
      week: "周",
      list: "列表",
    },
    eventContent(arg) {
      const box = document.createElement("div");
      box.className = "evt-title-wrap";
      box.textContent = arg.event.title;
      return { domNodes: [box] };
    },
    eventClick(info) {
      info.jsEvent.preventDefault();
      const payload = info.event.extendedProps.payload;
      renderDetail(payload);
    },
  });

  calendar.render();
}

function init() {
  initCalendar();
  bindCategoryFilters();
  bindMarketFilters();
  bindStockEditor();
  refreshBtn.addEventListener("click", triggerRefresh);

  loadEvents().catch((error) => {
    statusBadge.textContent = `加载失败：${error.message}`;
  });

  loadStocks().catch((error) => {
    setStockStatus(`加载股票配置失败：${error.message}`, true);
  });
}

init();
