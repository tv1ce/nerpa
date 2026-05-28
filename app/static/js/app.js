// ── Items editor (orders & invoices) ────────────────────────────────────────

let _itemsData = [];

function initItemsEditor(initialItems, productsMap) {
  _itemsData = initialItems || [];
  window._productsMap = productsMap || {};
  renderItems();
}

function addItem() {
  _itemsData.push({ product_id: "", name: "", quantity: 1, unit: "кг", price: 0, vat_rate: 20, amount: 0 });
  renderItems();
}

function removeItem(idx) {
  _itemsData.splice(idx, 1);
  renderItems();
}

function onProductChange(idx, selectEl) {
  const pid = selectEl.value;
  if (pid && window._productsMap[pid]) {
    const p = window._productsMap[pid];
    _itemsData[idx].product_id = pid;
    _itemsData[idx].name = p.name;
    _itemsData[idx].unit = p.unit;
    _itemsData[idx].price = p.price;
    _itemsData[idx].vat_rate = p.vat_rate;
    _itemsData[idx].amount = _itemsData[idx].quantity * p.price;
  } else {
    _itemsData[idx].product_id = "";
  }
  renderItems();
}

function onFieldChange(idx, field, value) {
  _itemsData[idx][field] = field === "name" || field === "unit" ? value : parseFloat(value) || 0;
  _itemsData[idx].amount = (_itemsData[idx].quantity || 0) * (_itemsData[idx].price || 0);
  updateTotals();
  syncHidden();
}

function renderItems() {
  const tbody = document.getElementById("items-tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  _itemsData.forEach((item, idx) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>
        <select class="form-select form-select-sm" onchange="onProductChange(${idx}, this)">
          <option value="">— выбрать —</option>
          ${Object.entries(window._productsMap || {}).map(([id, p]) =>
            `<option value="${id}" ${item.product_id == id ? "selected" : ""}>${p.name}</option>`
          ).join("")}
        </select>
      </td>
      <td><input type="text" class="form-control" value="${esc(item.name)}" oninput="onFieldChange(${idx},'name',this.value)"></td>
      <td><input type="number" class="form-control" value="${item.quantity}" step="0.001" min="0" oninput="onFieldChange(${idx},'quantity',this.value)"></td>
      <td><input type="text" class="form-control" value="${esc(item.unit)}" oninput="onFieldChange(${idx},'unit',this.value)"></td>
      <td><input type="number" class="form-control" value="${item.price}" step="0.01" min="0" oninput="onFieldChange(${idx},'price',this.value)"></td>
      <td><input type="number" class="form-control" value="${item.vat_rate}" step="1" min="0" max="100" oninput="onFieldChange(${idx},'vat_rate',this.value)"></td>
      <td class="text-end fw-semibold">${fmtMoney(item.amount)}</td>
      <td><button type="button" class="btn btn-sm btn-outline-danger" onclick="removeItem(${idx})"><i class="bi bi-trash"></i></button></td>
    `;
    tbody.appendChild(tr);
  });
  updateTotals();
  syncHidden();
}

function updateTotals() {
  const subtotal = _itemsData.reduce((s, i) => s + (i.amount || 0), 0);
  const vat = _itemsData.reduce((s, i) => s + (i.amount || 0) * (i.vat_rate || 0) / 100, 0);
  const total = subtotal + vat;
  setEl("total-subtotal", fmtMoney(subtotal));
  setEl("total-vat", fmtMoney(vat));
  setEl("total-amount", fmtMoney(total));
}

function syncHidden() {
  const hidden = document.getElementById("items_json");
  if (hidden) hidden.value = JSON.stringify(_itemsData);
}

function setEl(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function fmtMoney(v) {
  return (v || 0).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, " ").replace(".", ",");
}

function esc(s) {
  return (s || "").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

// ── Confirm delete ───────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form[data-confirm]").forEach(form => {
    form.addEventListener("submit", e => {
      if (!confirm(form.dataset.confirm || "Удалить?")) e.preventDefault();
    });
  });
});
