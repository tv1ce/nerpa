// ── Items editor (orders & invoices) ────────────────────────────────────────

let _itemsData = [];

function initItemsEditor(initialItems, productsMap) {
  _itemsData = initialItems || [];
  window._productsMap = productsMap || {};
  renderItems();
}

function addItem() {
  _itemsData.push({ product_id: "", name: "", quantity: 1, unit: "шт", price: 0, discount_pct: 0, vat_rate: 20, amount: 0 });
  renderItems();
  // Open picker for the new (last) row automatically
  setTimeout(function () { openItemPicker(_itemsData.length - 1); }, 50);
}

function openItemPicker(idx) {
  // Build picker products array from productsMap on first call
  if (!ProductPicker._ready) {
    var arr = Object.entries(window._productsMap || {}).map(function (_ref) {
      var id = _ref[0], p = _ref[1];
      return { id: parseInt(id), name: p.name, article: p.article || '', unit: p.unit,
               category: p.category || '', price: p.price, vat_rate: p.vat_rate, min_stock: 0 };
    });
    ProductPicker.init(arr, {});
    ProductPicker._ready = true;
  }
  ProductPicker.open(function (p) {
    _itemsData[idx].product_id = p.id;
    _itemsData[idx].name       = p.name;
    _itemsData[idx].unit       = p.unit;
    _itemsData[idx].price      = p.price;
    _itemsData[idx].vat_rate   = p.vat_rate;
    _itemsData[idx].amount     = calcAmount(_itemsData[idx]);
    renderItems();
  });
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
    _itemsData[idx].amount = calcAmount(_itemsData[idx]);
  } else {
    _itemsData[idx].product_id = "";
  }
  renderItems();
}

function onFieldChange(idx, field, value) {
  _itemsData[idx][field] = (field === "name" || field === "unit") ? value : parseFloat(value) || 0;
  _itemsData[idx].amount = calcAmount(_itemsData[idx]);
  const item = _itemsData[idx];
  const discVal = item.discount_pct || 0;
  const amountCell = document.getElementById(`item-amount-${idx}`);
  if (amountCell) {
    amountCell.innerHTML =
      (discVal > 0 ? `<div style="font-size:.72rem;text-decoration:line-through;color:#aaa">${fmtMoney((item.quantity||0)*(item.price||0))}</div>` : "") +
      fmtMoney(item.amount);
  }
  updateTotals();
  syncHidden();
}

function calcAmount(item) {
  const gross = (item.quantity || 0) * (item.price || 0);
  const disc  = Math.min(Math.max(item.discount_pct || 0, 0), 100);
  return gross * (1 - disc / 100);
}

function renderItems() {
  const tbody = document.getElementById("items-tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  _itemsData.forEach((item, idx) => {
    const discVal = item.discount_pct || 0;
    const tr = document.createElement("tr");
    const prodName = item.product_id && window._productsMap[item.product_id]
      ? window._productsMap[item.product_id].name : '';
    tr.innerHTML = `
      <td>
        <button type="button" class="btn-picker${prodName ? '' : ' empty'}" onclick="openItemPicker(${idx})" style="min-width:120px">
          <span class="pbl">${prodName || '— выбрать —'}</span>
          <i class="bi bi-grid-3x3-gap pbi"></i>
        </button>
      </td>
      <td><input type="text" class="form-control form-control-sm" value="${esc(item.name)}" oninput="onFieldChange(${idx},'name',this.value)"></td>
      <td><input type="number" class="form-control form-control-sm" value="${item.quantity}" step="0.001" min="0" oninput="onFieldChange(${idx},'quantity',this.value)"></td>
      <td><input type="text" class="form-control form-control-sm" value="${esc(item.unit)}" oninput="onFieldChange(${idx},'unit',this.value)"></td>
      <td><input type="number" class="form-control form-control-sm" value="${item.price}" step="0.01" min="0" oninput="onFieldChange(${idx},'price',this.value)"></td>
      <td>
        <div class="input-group input-group-sm">
          <input type="number" class="form-control form-control-sm ${discVal > 0 ? 'text-danger fw-semibold' : ''}"
                 value="${discVal}" step="0.1" min="0" max="100"
                 oninput="onFieldChange(${idx},'discount_pct',this.value)"
                 style="max-width:60px">
          <span class="input-group-text">%</span>
        </div>
      </td>
      <td><input type="number" class="form-control form-control-sm" value="${item.vat_rate}" step="1" min="0" max="100" oninput="onFieldChange(${idx},'vat_rate',this.value)"></td>
      <td class="text-end fw-semibold align-middle" id="item-amount-${idx}">
        ${discVal > 0 ? `<div style="font-size:.72rem;text-decoration:line-through;color:#aaa">${fmtMoney((item.quantity||0)*(item.price||0))}</div>` : ''}
        ${fmtMoney(item.amount)}
      </td>
      <td class="align-middle"><button type="button" class="btn btn-sm btn-outline-danger" onclick="removeItem(${idx})"><i class="bi bi-trash"></i></button></td>
    `;
    tbody.appendChild(tr);
  });
  updateTotals();
  syncHidden();
}

function updateTotals() {
  const grossTotal = _itemsData.reduce((s, i) => s + (i.quantity || 0) * (i.price || 0), 0);
  const subtotal   = _itemsData.reduce((s, i) => s + (i.amount || 0), 0);
  const discount   = grossTotal - subtotal;
  const vat        = _itemsData.reduce((s, i) => s + (i.amount || 0) * (i.vat_rate || 0) / 100, 0);
  const total      = subtotal + vat;

  setEl("total-subtotal", fmtMoney(subtotal));
  setEl("total-vat",      fmtMoney(vat));
  setEl("total-amount",   fmtMoney(total));

  // строка скидки — показываем только если есть
  const discRow = document.getElementById("total-discount-row");
  if (discRow) {
    discRow.style.display = discount > 0.001 ? "" : "none";
    setEl("total-discount", fmtMoney(discount));
  }
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

// ── Product Picker ────────────────────────────────────────────────────────────

const ProductPicker = (function () {
  let _products = [];
  let _balances = {};
  let _onSelect = null;
  let _bsModal  = null;

  function init(products, balances) {
    _products = products || [];
    _balances = balances || {};
  }

  function open(onSelect) {
    _onSelect = onSelect;
    const el = document.getElementById('productPickerModal');
    if (!el) { console.warn('ProductPicker: modal not found'); return; }
    _bsModal = bootstrap.Modal.getOrCreateInstance(el);
    _bsModal.show();
    setTimeout(function () {
      var s = document.getElementById('pickerSearch');
      if (s) { s.value = ''; s.focus(); }
      _render('');
    }, 80);
  }

  function _render(query) {
    var q = (query || '').trim().toLowerCase();
    var filtered = q
      ? _products.filter(function (p) {
          return (p.name     || '').toLowerCase().includes(q) ||
                 (p.article  || '').toLowerCase().includes(q) ||
                 (p.category || '').toLowerCase().includes(q);
        })
      : _products;

    var groups = {};
    filtered.forEach(function (p) {
      var cat = p.category || 'Все товары';
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push(p);
    });

    var body = document.getElementById('pickerBody');
    if (!body) return;

    if (!filtered.length) {
      body.innerHTML = '<div class="text-center text-muted py-5"><i class="bi bi-search fs-3 d-block mb-2 opacity-50"></i>Ничего не найдено</div>';
      return;
    }

    var catKeys = Object.keys(groups).sort(function (a, b) {
      if (a === 'Все товары') return 1;
      if (b === 'Все товары') return -1;
      return a.localeCompare(b, 'ru');
    });
    var multiCat = catKeys.length > 1;

    var html = '';
    catKeys.forEach(function (cat) {
      if (multiCat) {
        html += '<p class="picker-cat-label">' + esc(cat) +
          ' <span style="font-weight:400;opacity:.55">(' + groups[cat].length + ')</span></p>';
      }
      html += '<div class="picker-grid">';
      groups[cat].forEach(function (p) {
        var bal = _balances[p.id];
        var hasBalance = (bal !== undefined && bal !== null);
        var balHtml = '';
        if (hasBalance) {
          var balVal = (bal % 1 === 0) ? bal : parseFloat(bal).toFixed(1);
          var cls = (bal <= 0) ? 'picker-bal-zero'
                  : (p.min_stock && bal <= p.min_stock) ? 'picker-bal-low'
                  : 'picker-bal-ok';
          balHtml = '<div class="picker-bal ' + cls + '">' + balVal + ' ' + esc(p.unit || '') + '</div>';
        }
        html += '<div class="picker-card" onclick="ProductPicker._select(' + p.id + ')">' +
          (p.article ? '<div class="picker-art">' + esc(p.article) + '</div>' : '') +
          '<div class="picker-pname">' + esc(p.name) + '</div>' +
          balHtml +
          '</div>';
      });
      html += '</div>';
    });

    body.innerHTML = html;
  }

  function _select(pid) {
    var p = _products.find(function (x) { return x.id == pid; });
    if (p && _onSelect) {
      _onSelect(p);
      if (_bsModal) _bsModal.hide();
    }
  }

  return { init: init, open: open, _render: _render, _select: _select };
})();

// ── Confirm delete ───────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form[data-confirm]").forEach(form => {
    form.addEventListener("submit", e => {
      if (!confirm(form.dataset.confirm || "Удалить?")) e.preventDefault();
    });
  });
});
