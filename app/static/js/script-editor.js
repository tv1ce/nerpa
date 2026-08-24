/* Конструктор скриптов: блок-схема, дерево шагов, редактор шага.
 *
 * Состояние — один объект графа (G) той же формы, что отдаёт и принимает
 * сервер (см. graph_dict/apply_graph в app/routers/scripts.py). Любая правка
 * меняет G, помечает документ грязным и через паузу уходит на сервер целиком.
 * Отсюда бесплатно получается undo/redo: это снимки G, а не журнал операций.
 *
 * Новые блоки и ответы до первого сохранения живут с отрицательными id —
 * сервер отвечает картой «временный id → настоящий», и мы её применяем.
 */
(function () {
  'use strict';

  var cfg = window.SC_INIT;
  if (!cfg) return;

  var G = cfg.graph;
  var selectedId = null;
  var tmpSeq = -1;
  var view = { x: 40, y: 40, k: 1 };
  var undoStack = [], redoStack = [];
  var saveTimer = null, dirty = false;
  var linking = null;   // {answerId, nodeId} — тянем связь мышью

  var canvas = document.getElementById('scCanvas');
  var world = document.getElementById('scWorld');
  var edgesSvg = document.getElementById('scEdges');
  var treeBox = document.getElementById('scTree');
  var inspector = document.getElementById('scInspector');
  var statusEl = document.getElementById('scStatus');
  var miniSvg = document.getElementById('scMinimap');

  var COLORS = cfg.answerColors || {};
  var KINDS = cfg.nodeKinds || {};

  // ── Утилиты ───────────────────────────────────────────────────────────────

  function nodeById(id) {
    for (var i = 0; i < G.nodes.length; i++) if (G.nodes[i].id === id) return G.nodes[i];
    return null;
  }

  function answerById(id) {
    for (var i = 0; i < G.nodes.length; i++) {
      var answers = G.nodes[i].answers;
      for (var j = 0; j < answers.length; j++) if (answers[j].id === id) return answers[j];
    }
    return null;
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function stripHtml(html) {
    var d = document.createElement('div');
    d.innerHTML = html || '';
    return (d.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function colorOf(key) { return COLORS[key] || COLORS.gray || '#64748b'; }

  /** Порядок шагов в документе — тот же обход, что на сервере (ordered_nodes). */
  function orderedNodes() {
    if (!G.nodes.length) return [];
    var byId = {}, i;
    for (i = 0; i < G.nodes.length; i++) byId[G.nodes[i].id] = G.nodes[i];
    var start = byId[G.script.start_node_id] || G.nodes[0];
    var out = [], seen = {}, queue = [start.id];
    while (queue.length) {
      var id = queue.shift();
      if (seen[id] || !byId[id]) continue;
      seen[id] = true;
      out.push(byId[id]);
      byId[id].answers.forEach(function (a) {
        if (a.next && !seen[a.next]) queue.push(a.next);
      });
    }
    G.nodes.forEach(function (n) { if (!seen[n.id]) out.push(n); });
    return out;
  }

  function numbers() {
    var map = {}, ord = orderedNodes();
    for (var i = 0; i < ord.length; i++) map[ord[i].id] = i + 1;
    return map;
  }

  // ── Undo / redo / автосохранение ──────────────────────────────────────────

  function pushUndo() {
    undoStack.push(JSON.stringify(G));
    if (undoStack.length > 60) undoStack.shift();
    redoStack.length = 0;
    refreshHistoryButtons();
  }

  function refreshHistoryButtons() {
    var u = document.getElementById('scUndo'), r = document.getElementById('scRedo');
    if (u) u.disabled = !undoStack.length;
    if (r) r.disabled = !redoStack.length;
  }

  function applySnapshot(json) {
    G = JSON.parse(json);
    if (!nodeById(selectedId)) selectedId = G.nodes.length ? G.nodes[0].id : null;
    renderAll();
    scheduleSave();
  }

  function undo() {
    if (!undoStack.length) return;
    redoStack.push(JSON.stringify(G));
    applySnapshot(undoStack.pop());
    refreshHistoryButtons();
  }

  function redo() {
    if (!redoStack.length) return;
    undoStack.push(JSON.stringify(G));
    applySnapshot(redoStack.pop());
    refreshHistoryButtons();
  }

  function setStatus(text, cls) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = 'sc-save-status ' + (cls || '');
  }

  function scheduleSave() {
    if (!cfg.canEdit) return;
    dirty = true;
    setStatus('Сохранение…', 'saving');
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 1200);
  }

  function save() {
    if (!cfg.canEdit || !dirty) return;
    var payload = JSON.stringify(G);
    fetch('/scripts/' + cfg.scriptId + '/graph', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': cfg.csrf },
      body: payload
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (!res.ok) { setStatus(res.error || 'Ошибка сохранения', 'error'); return; }
      applyIdMap(res.id_map || {});
      dirty = false;
      setStatus('Сохранено' + (res.saved_at ? ' в ' + res.saved_at : ''), 'saved');
    }).catch(function () {
      setStatus('Нет связи с сервером — изменения не сохранены', 'error');
    });
  }

  /** Меняет временные отрицательные id на присвоенные сервером. */
  function applyIdMap(map) {
    var keys = Object.keys(map);
    if (!keys.length) return;
    function real(id) {
      var key = String(id);
      return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : id;
    }
    G.nodes.forEach(function (n) {
      n.id = real(n.id);
      n.answers.forEach(function (a) {
        a.id = real(a.id);
        if (a.next) a.next = real(a.next);
      });
    });
    G.script.start_node_id = real(G.script.start_node_id);
    G.script.quick_jumps = (G.script.quick_jumps || []).map(function (j) {
      return { id: real(j.id), label: j.label || '' };
    });
    selectedId = real(selectedId);
    renderTree();
    renderCanvas();
    renderInspector();
  }

  // Не теряем последние правки, если вкладку закрывают сразу после ввода
  window.addEventListener('beforeunload', function (e) {
    if (!dirty) return;
    clearTimeout(saveTimer);
    save();
    e.preventDefault();
    e.returnValue = '';
  });

  // ── Операции над графом ───────────────────────────────────────────────────

  function addNode(x, y, kind) {
    pushUndo();
    var node = {
      id: tmpSeq--, title: 'Новый вопрос', body_html: '', kind: kind || 'question',
      x: x, y: y, order: G.nodes.length, fields: [], answers: []
    };
    G.nodes.push(node);
    if (!G.script.start_node_id) G.script.start_node_id = node.id;
    selectedId = node.id;
    renderAll();
    scheduleSave();
    return node;
  }

  function deleteNode(id) {
    var node = nodeById(id);
    if (!node) return;
    if (!confirm('Удалить блок «' + node.title + '»? Ведущие в него переходы станут концом ветки.')) return;
    pushUndo();
    G.nodes = G.nodes.filter(function (n) { return n.id !== id; });
    G.nodes.forEach(function (n) {
      n.answers.forEach(function (a) { if (a.next === id) a.next = null; });
    });
    G.script.quick_jumps = (G.script.quick_jumps || []).filter(function (j) { return j.id !== id; });
    if (G.script.start_node_id === id) G.script.start_node_id = G.nodes.length ? G.nodes[0].id : null;
    if (selectedId === id) selectedId = G.nodes.length ? G.nodes[0].id : null;
    renderAll();
    scheduleSave();
  }

  function duplicateNode(id) {
    var node = nodeById(id);
    if (!node) return;
    pushUndo();
    var copy = JSON.parse(JSON.stringify(node));
    copy.id = tmpSeq--;
    copy.title = node.title + ' — копия';
    copy.x = (node.x || 0) + 40;
    copy.y = (node.y || 0) + 60;
    copy.answers = copy.answers.map(function (a) {
      return { id: tmpSeq--, text: a.text, color: a.color, next: a.next, order: a.order };
    });
    G.nodes.push(copy);
    selectedId = copy.id;
    renderAll();
    scheduleSave();
  }

  function addAnswer(nodeId, text, next) {
    var node = nodeById(nodeId);
    if (!node) return null;
    pushUndo();
    var ans = {
      id: tmpSeq--, text: text || 'Новый ответ', color: 'gray',
      next: next || null, order: node.answers.length
    };
    node.answers.push(ans);
    renderAll();
    scheduleSave();
    return ans;
  }

  function autoLayout() {
    pushUndo();
    var ord = orderedNodes();
    if (!ord.length) return;
    var byId = {};
    ord.forEach(function (n) { byId[n.id] = n; });
    var depth = {}, start = G.script.start_node_id || ord[0].id;
    depth[start] = 0;
    var queue = [start], seen = {};
    while (queue.length) {
      var id = queue.shift();
      if (seen[id]) continue;
      seen[id] = true;
      var node = byId[id];
      if (!node) continue;
      node.answers.forEach(function (a) {
        if (a.next && byId[a.next] && depth[a.next] === undefined) {
          depth[a.next] = depth[id] + 1;
          queue.push(a.next);
        }
      });
    }
    var rows = {};
    ord.forEach(function (n) {
      var d = depth[n.id] === undefined ? 0 : depth[n.id];
      rows[d] = rows[d] || [];
      rows[d].push(n);
    });
    Object.keys(rows).forEach(function (d) {
      rows[d].forEach(function (n, i) {
        n.x = 60 + Number(d) * 340;
        n.y = 60 + i * 230;
      });
    });
    renderCanvas();
    renderMinimap();
    scheduleSave();
  }

  // ── Дерево шагов (левая панель) ───────────────────────────────────────────

  function renderTree() {
    if (!treeBox) return;
    var filter = (document.getElementById('scTreeSearch') || {}).value || '';
    filter = filter.toLowerCase().trim();
    var nums = numbers();
    var html = '';
    orderedNodes().forEach(function (n) {
      var text = stripHtml(n.body_html);
      if (filter && (n.title + ' ' + text).toLowerCase().indexOf(filter) === -1) return;
      var icon = n.kind === 'end' ? 'bi-flag' : (n.kind === 'info' ? 'bi-chat-left-text' : 'bi-question-circle');
      html += '<button class="sc-tree-item' + (n.id === selectedId ? ' active' : '') +
        '" data-node="' + n.id + '">' +
        '<span class="sc-num">' + nums[n.id] + '.</span>' +
        '<i class="bi ' + icon + ' me-1 text-muted"></i>' + esc(n.title) +
        (text ? '<span class="sc-sub">' + esc(text.slice(0, 70)) + '</span>' : '') +
        '</button>';
    });
    treeBox.innerHTML = html || '<div class="text-muted p-3" style="font-size:.82rem">Ничего не найдено</div>';
    treeBox.querySelectorAll('[data-node]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        select(Number(btn.dataset.node));
        focusNode(Number(btn.dataset.node));
      });
    });
  }

  // ── Блок-схема (центр) ────────────────────────────────────────────────────

  function applyView() {
    world.style.transform = 'translate(' + view.x + 'px,' + view.y + 'px) scale(' + view.k + ')';
    renderMinimap();
  }

  function renderCanvas() {
    var nums = numbers();
    var html = '';
    G.nodes.forEach(function (n) {
      var isStart = n.id === G.script.start_node_id;
      var cls = 'sc-node' + (n.id === selectedId ? ' selected' : '') +
        (isStart ? ' is-start' : '') + (n.kind === 'end' ? ' is-end' : '');
      html += '<div class="' + cls + '" data-node="' + n.id + '" style="left:' +
        (n.x || 0) + 'px;top:' + (n.y || 0) + 'px">' +
        '<div class="sc-port-in" data-in="' + n.id + '"></div>' +
        '<div class="sc-node-head" data-drag="' + n.id + '">' +
        '<span class="text-muted">' + nums[n.id] + '.</span>' +
        '<span class="flex-grow-1 text-truncate">' + esc(n.title) + '</span>' +
        (isStart ? '<span class="sc-node-badge start">старт</span>' : '') +
        (n.kind === 'end' ? '<span class="sc-node-badge end">конец</span>' : '') +
        '</div>';
      var text = stripHtml(n.body_html);
      if (text) html += '<div class="sc-node-text">' + esc(text.slice(0, 160)) + '</div>';
      if (n.answers.length) {
        html += '<div class="sc-answers">';
        n.answers.forEach(function (a) {
          var c = colorOf(a.color);
          html += '<div class="sc-answer" style="border-left-color:' + c + '" data-answer="' + a.id + '">' +
            esc(a.text) +
            '<span class="sc-port" style="color:' + c + '" data-port="' + a.id + '" ' +
            'data-node="' + n.id + '" title="Потяните к нужному вопросу"></span></div>';
        });
        html += '</div>';
      } else if (n.kind !== 'end') {
        html += '<div class="sc-answers"><div class="sc-answer text-muted" style="border-left-color:#cbd5e1">' +
          'Нет ответов<span class="sc-port" style="color:#94a3b8" data-port="new" data-node="' + n.id +
          '" title="Потяните, чтобы создать переход"></span></div></div>';
      }
      html += '</div>';
    });
    world.innerHTML = html;
    world.appendChild(edgesSvg);
    bindNodeEvents();
    drawEdges();
  }

  function bindNodeEvents() {
    world.querySelectorAll('[data-drag]').forEach(function (head) {
      head.addEventListener('mousedown', startNodeDrag);
    });
    world.querySelectorAll('.sc-node').forEach(function (el) {
      el.addEventListener('mousedown', function (e) {
        if (e.target.closest('.sc-port')) return;
        select(Number(el.dataset.node));
      });
      el.addEventListener('dblclick', function () {
        var input = document.getElementById('scNodeTitle');
        if (input) { input.focus(); input.select(); }
      });
    });
    world.querySelectorAll('.sc-port').forEach(function (port) {
      port.addEventListener('mousedown', startLink);
    });
  }

  /** Экранные координаты → координаты холста (с учётом зума и панорамы). */
  function toWorld(clientX, clientY) {
    var rect = canvas.getBoundingClientRect();
    return {
      x: (clientX - rect.left - view.x) / view.k,
      y: (clientY - rect.top - view.y) / view.k
    };
  }

  function startNodeDrag(e) {
    if (!cfg.canEdit || e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    var id = Number(e.currentTarget.dataset.drag);
    var node = nodeById(id);
    if (!node) return;
    select(id);
    var el = world.querySelector('.sc-node[data-node="' + id + '"]');
    var start = toWorld(e.clientX, e.clientY);
    var origin = { x: node.x || 0, y: node.y || 0 };
    var moved = false;

    function move(ev) {
      var p = toWorld(ev.clientX, ev.clientY);
      node.x = Math.round(origin.x + (p.x - start.x));
      node.y = Math.round(origin.y + (p.y - start.y));
      el.style.left = node.x + 'px';
      el.style.top = node.y + 'px';
      if (!moved) { pushUndo(); moved = true; }
      drawEdges();
    }
    function up() {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      if (moved) { renderMinimap(); scheduleSave(); }
    }
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  }

  function startLink(e) {
    if (!cfg.canEdit || e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    var portEl = e.currentTarget;
    var nodeId = Number(portEl.dataset.node);
    var answerId = portEl.dataset.port === 'new' ? null : Number(portEl.dataset.port);
    linking = { nodeId: nodeId, answerId: answerId, from: portCenter(portEl) };

    function move(ev) {
      var p = toWorld(ev.clientX, ev.clientY);
      drawEdges({ from: linking.from, to: p });
      var target = ev.target.closest ? ev.target.closest('.sc-node') : null;
      world.querySelectorAll('.sc-node.drop-target').forEach(function (n) {
        n.classList.remove('drop-target');
      });
      if (target) target.classList.add('drop-target');
    }
    function up(ev) {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      world.querySelectorAll('.sc-node.drop-target').forEach(function (n) {
        n.classList.remove('drop-target');
      });
      var target = ev.target.closest ? ev.target.closest('.sc-node') : null;
      var targetId = target ? Number(target.dataset.node) : null;
      if (!targetId) {
        // бросили на пустое место — предлагаем сразу создать следующий вопрос
        var p = toWorld(ev.clientX, ev.clientY);
        if (confirm('Создать здесь новый вопрос и связать с ним?')) {
          var created = addNode(Math.round(p.x), Math.round(p.y));
          targetId = created.id;
        }
      }
      if (targetId) linkTo(linking, targetId);
      linking = null;
      drawEdges();
    }
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  }

  function linkTo(link, targetId) {
    var node = nodeById(link.nodeId);
    if (!node || targetId === link.nodeId) return;
    pushUndo();
    if (link.answerId === null) {
      node.answers.push({
        id: tmpSeq--, text: 'Далее', color: 'gray', next: targetId, order: node.answers.length
      });
    } else {
      var ans = answerById(link.answerId);
      if (ans) ans.next = targetId;
    }
    renderAll();
    scheduleSave();
  }

  function portCenter(el) {
    var rect = el.getBoundingClientRect();
    return toWorld(rect.left + rect.width / 2, rect.top + rect.height / 2);
  }

  function inPortCenter(nodeId) {
    var el = world.querySelector('[data-in="' + nodeId + '"]');
    if (!el) return null;
    return portCenter(el);
  }

  function bezier(a, b) {
    var dx = Math.max(60, Math.abs(b.x - a.x) * 0.5);
    return 'M' + a.x + ',' + a.y + ' C' + (a.x + dx) + ',' + a.y + ' ' +
      (b.x - dx) + ',' + b.y + ' ' + b.x + ',' + b.y;
  }

  function drawEdges(temp) {
    var parts = ['<defs>'];
    var seenColors = {};
    G.nodes.forEach(function (n) {
      n.answers.forEach(function (a) { seenColors[colorOf(a.color)] = true; });
    });
    Object.keys(seenColors).forEach(function (c, i) {
      parts.push('<marker id="scArrow' + i + '" viewBox="0 0 10 10" refX="9" refY="5" ' +
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">' +
        '<path d="M0,0 L10,5 L0,10 z" fill="' + c + '"/></marker>');
    });
    parts.push('</defs>');
    var colorIndex = {};
    Object.keys(seenColors).forEach(function (c, i) { colorIndex[c] = i; });

    G.nodes.forEach(function (n) {
      n.answers.forEach(function (a) {
        if (!a.next) return;
        var portEl = world.querySelector('[data-port="' + a.id + '"]');
        var from = portEl ? portCenter(portEl) : null;
        var to = inPortCenter(a.next);
        if (!from || !to) return;
        var c = colorOf(a.color);
        var d = bezier(from, to);
        parts.push('<path d="' + d + '" fill="none" stroke="transparent" stroke-width="14" ' +
          'style="pointer-events:stroke;cursor:pointer" data-edge="' + a.id + '"></path>');
        parts.push('<path d="' + d + '" fill="none" stroke="' + c + '" stroke-width="2" ' +
          'marker-end="url(#scArrow' + colorIndex[c] + ')" opacity=".85"></path>');
      });
    });

    if (temp) {
      parts.push('<path d="' + bezier(temp.from, temp.to) + '" fill="none" stroke="#2563eb" ' +
        'stroke-width="2" stroke-dasharray="5 4"></path>');
    }
    edgesSvg.innerHTML = parts.join('');
    edgesSvg.querySelectorAll('[data-edge]').forEach(function (path) {
      path.addEventListener('click', function () {
        if (!cfg.canEdit) return;
        var ans = answerById(Number(path.dataset.edge));
        if (ans && confirm('Удалить переход по ответу «' + ans.text + '»?')) {
          pushUndo();
          ans.next = null;
          renderAll();
          scheduleSave();
        }
      });
    });
  }

  function focusNode(id) {
    var node = nodeById(id);
    if (!node) return;
    var rect = canvas.getBoundingClientRect();
    view.x = rect.width / 2 - (node.x + 130) * view.k;
    view.y = rect.height / 2 - (node.y + 70) * view.k;
    applyView();
  }

  // ── Мини-карта и зум ──────────────────────────────────────────────────────

  function graphBounds() {
    if (!G.nodes.length) return { x1: 0, y1: 0, x2: 800, y2: 600 };
    var x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
    G.nodes.forEach(function (n) {
      x1 = Math.min(x1, n.x || 0); y1 = Math.min(y1, n.y || 0);
      x2 = Math.max(x2, (n.x || 0) + 260); y2 = Math.max(y2, (n.y || 0) + 200);
    });
    return { x1: x1 - 60, y1: y1 - 60, x2: x2 + 60, y2: y2 + 60 };
  }

  function renderMinimap() {
    if (!miniSvg) return;
    var b = graphBounds();
    var w = b.x2 - b.x1, h = b.y2 - b.y1;
    miniSvg.setAttribute('viewBox', b.x1 + ' ' + b.y1 + ' ' + w + ' ' + h);
    var parts = [];
    G.nodes.forEach(function (n) {
      var fill = n.id === selectedId ? '#2563eb' : (n.kind === 'end' ? '#fdba74' : '#cbd5e1');
      parts.push('<rect x="' + (n.x || 0) + '" y="' + (n.y || 0) + '" width="260" height="120" rx="14" fill="' + fill + '"/>');
    });
    var rect = canvas.getBoundingClientRect();
    var vx = -view.x / view.k, vy = -view.y / view.k;
    parts.push('<rect x="' + vx + '" y="' + vy + '" width="' + (rect.width / view.k) +
      '" height="' + (rect.height / view.k) + '" fill="none" stroke="#2563eb" stroke-width="' +
      Math.max(2, w / 220) + '"/>');
    miniSvg.innerHTML = parts.join('');
  }

  function zoomAt(factor, clientX, clientY) {
    var rect = canvas.getBoundingClientRect();
    var cx = clientX === undefined ? rect.width / 2 : clientX - rect.left;
    var cy = clientY === undefined ? rect.height / 2 : clientY - rect.top;
    var k = Math.min(2.2, Math.max(0.25, view.k * factor));
    view.x = cx - (cx - view.x) * (k / view.k);
    view.y = cy - (cy - view.y) * (k / view.k);
    view.k = k;
    applyView();
  }

  function fitToScreen() {
    var b = graphBounds();
    var rect = canvas.getBoundingClientRect();
    var k = Math.min(rect.width / (b.x2 - b.x1), rect.height / (b.y2 - b.y1), 1.4);
    view.k = Math.max(0.25, k);
    view.x = -b.x1 * view.k + 10;
    view.y = -b.y1 * view.k + 10;
    applyView();
  }

  // ── Редактор шага (правая панель) ─────────────────────────────────────────

  function select(id) {
    if (selectedId === id) return;
    flushRichText();
    selectedId = id;
    renderTree();
    world.querySelectorAll('.sc-node').forEach(function (el) {
      el.classList.toggle('selected', Number(el.dataset.node) === id);
    });
    renderInspector();
    renderMinimap();
  }

  var rteEl = null;

  function flushRichText() {
    if (!rteEl) return;
    var node = nodeById(Number(rteEl.dataset.node));
    if (node && node.body_html !== rteEl.innerHTML) {
      node.body_html = rteEl.innerHTML;
      scheduleSave();
    }
  }

  function renderInspector() {
    var node = nodeById(selectedId);
    if (!node) {
      inspector.innerHTML = '<div class="text-muted p-3" style="font-size:.85rem">' +
        'Выберите блок на схеме или добавьте новый.</div>';
      rteEl = null;
      return;
    }
    var nums = numbers();
    var options = orderedNodes().map(function (n) {
      return '<option value="' + n.id + '"' + '>' + nums[n.id] + '. ' + esc(n.title) + '</option>';
    }).join('');

    var html = '<div class="p-3">';
    html += '<div class="sc-editor-label">Название шага</div>' +
      '<input id="scNodeTitle" class="form-control form-control-sm mb-3" value="' + esc(node.title) + '">';

    html += '<div class="row g-2 mb-3">';
    html += '<div class="col-7"><div class="sc-editor-label">Тип</div><select id="scNodeKind" class="form-select form-select-sm">';
    Object.keys(KINDS).forEach(function (k) {
      html += '<option value="' + k + '"' + (node.kind === k ? ' selected' : '') + '>' + esc(KINDS[k]) + '</option>';
    });
    html += '</select></div>';
    html += '<div class="col-5 d-flex align-items-end">' +
      '<div class="form-check form-switch mb-1"><input class="form-check-input" type="checkbox" id="scIsStart"' +
      (G.script.start_node_id === node.id ? ' checked' : '') +
      '><label class="form-check-label small" for="scIsStart">Старт</label></div></div>';
    html += '</div>';

    var jump = (G.script.quick_jumps || []).filter(function (j) { return j.id === node.id; })[0];
    html += '<div class="form-check form-switch mb-2"><input class="form-check-input" type="checkbox" id="scQuickJump"' +
      (jump ? ' checked' : '') +
      '><label class="form-check-label small" for="scQuickJump">В быстрые переходы менеджера</label></div>';
    html += '<input id="scQuickLabel" class="form-control form-control-sm mb-3" ' +
      (jump ? '' : 'style="display:none" ') +
      'placeholder="Подпись кнопки: Цена, Доставка, Возражения…" value="' +
      esc(jump ? jump.label : '') + '">';

    html += '<div class="sc-editor-label">Текст шага</div>';
    html += '<div class="sc-rte-toolbar" id="scRteToolbar">' +
      '<button type="button" data-cmd="bold" title="Жирный"><b>Ж</b></button>' +
      '<button type="button" data-cmd="italic" title="Курсив"><i>К</i></button>' +
      '<button type="button" data-cmd="underline" title="Подчёркнутый"><u>Ч</u></button>' +
      '<button type="button" data-cmd="strikeThrough" title="Зачёркнутый"><s>З</s></button>' +
      '<button type="button" data-block="h3" title="Заголовок">H</button>' +
      '<button type="button" data-block="blockquote" title="Цитата"><i class="bi bi-quote"></i></button>' +
      '<button type="button" data-cmd="insertUnorderedList" title="Список"><i class="bi bi-list-ul"></i></button>' +
      '<button type="button" data-cmd="insertOrderedList" title="Нумерованный список"><i class="bi bi-list-ol"></i></button>' +
      '<button type="button" data-color="#dc2626" title="Красный текст" style="color:#dc2626">A</button>' +
      '<button type="button" data-color="#16a34a" title="Зелёный текст" style="color:#16a34a">A</button>' +
      '<button type="button" data-hilite="#fef08a" title="Выделить цветом"><i class="bi bi-highlighter"></i></button>' +
      '<button type="button" data-link="1" title="Ссылка"><i class="bi bi-link-45deg"></i></button>' +
      '<button type="button" data-table="1" title="Таблица"><i class="bi bi-table"></i></button>' +
      '<button type="button" data-cmd="removeFormat" title="Убрать форматирование"><i class="bi bi-eraser"></i></button>' +
      '</div>';
    html += '<div class="sc-rte" id="scRte" contenteditable="' + (cfg.canEdit ? 'true' : 'false') +
      '" data-node="' + node.id + '">' + (node.body_html || '') + '</div>';
    html += '<div class="form-text mb-3">Подстановки из карточки клиента: {{Имя}}, {{Компания}}, {{Телефон}}.</div>';

    // ── Ответы ──
    html += '<div class="d-flex align-items-center justify-content-between mt-3 mb-2">' +
      '<div class="sc-editor-label mb-0">Ответы и переходы</div>' +
      '<button class="btn btn-sm btn-outline-secondary py-0" id="scAddAnswer">' +
      '<i class="bi bi-plus-lg"></i></button></div>';
    node.answers.forEach(function (a) {
      html += '<div class="border rounded p-2 mb-2" data-answer-row="' + a.id + '">' +
        '<div class="d-flex gap-1 mb-1">' +
        '<input class="form-control form-control-sm" data-a-text="' + a.id + '" value="' + esc(a.text) + '">' +
        '<select class="form-select form-select-sm" style="width:74px" data-a-color="' + a.id + '">';
      Object.keys(COLORS).forEach(function (c) {
        html += '<option value="' + c + '"' + (a.color === c ? ' selected' : '') + '>■</option>';
      });
      html += '</select>' +
        '<button class="btn btn-sm btn-link text-danger p-0 px-1" data-a-del="' + a.id + '" title="Удалить ответ">' +
        '<i class="bi bi-trash"></i></button></div>' +
        '<div class="d-flex gap-1 align-items-center">' +
        '<span class="text-muted" style="font-size:.75rem;white-space:nowrap">Ведёт →</span>' +
        '<select class="form-select form-select-sm" data-a-next="' + a.id + '">' +
        '<option value="">— конец ветки —</option>' + options +
        '<option value="__new">＋ Создать новый вопрос</option></select></div></div>';
    });
    if (!node.answers.length) {
      html += '<div class="text-muted mb-2" style="font-size:.8rem">Ответов нет — ветка на этом шаге заканчивается.</div>';
    }

    // ── Поля для заполнения ──
    html += '<div class="d-flex align-items-center justify-content-between mt-4 mb-2">' +
      '<div class="sc-editor-label mb-0">Поля для заполнения</div>' +
      '<button class="btn btn-sm btn-outline-secondary py-0" id="scAddField"><i class="bi bi-plus-lg"></i></button></div>';
    (node.fields || []).forEach(function (f, i) {
      html += '<div class="d-flex gap-1 mb-1">' +
        '<input class="form-control form-control-sm" data-f-label="' + i + '" value="' + esc(f.label || '') +
        '" placeholder="Подпись">' +
        '<select class="form-select form-select-sm" style="width:110px" data-f-type="' + i + '">';
      [['text', 'Текст'], ['textarea', 'Комментарий'], ['number', 'Число'],
       ['checkbox', 'Чекбокс'], ['select', 'Список'], ['date', 'Дата']].forEach(function (t) {
        html += '<option value="' + t[0] + '"' + (f.type === t[0] ? ' selected' : '') + '>' + t[1] + '</option>';
      });
      html += '</select>' +
        '<button class="btn btn-sm btn-link text-danger p-0 px-1" data-f-del="' + i + '">' +
        '<i class="bi bi-trash"></i></button></div>';
      html += '<input class="form-control form-control-sm mb-2" data-f-crm="' + i + '" value="' +
        esc(f.crm_field || '') + '" placeholder="Поле CRM (напр. UF_CRM_BUDGET) — необязательно">';
    });

    html += '<div class="d-flex gap-2 mt-4">' +
      '<button class="btn btn-sm btn-outline-secondary flex-grow-1" id="scDupNode">' +
      '<i class="bi bi-files me-1"></i>Копировать блок</button>' +
      '<button class="btn btn-sm btn-outline-danger" id="scDelNode"><i class="bi bi-trash"></i></button>' +
      '</div>';
    html += '</div>';

    inspector.innerHTML = html;
    bindInspector(node);
  }

  function bindInspector(node) {
    var title = document.getElementById('scNodeTitle');
    title.addEventListener('input', function () {
      node.title = title.value;
      renderTree();
      var head = world.querySelector('.sc-node[data-node="' + node.id + '"] .sc-node-head span:nth-child(2)');
      if (head) head.textContent = node.title;
      scheduleSave();
    });
    title.addEventListener('focus', pushUndo);

    document.getElementById('scNodeKind').addEventListener('change', function (e) {
      pushUndo();
      node.kind = e.target.value;
      renderAll();
      scheduleSave();
    });

    document.getElementById('scIsStart').addEventListener('change', function (e) {
      pushUndo();
      G.script.start_node_id = e.target.checked ? node.id : G.script.start_node_id;
      renderAll();
      scheduleSave();
    });

    var labelInput = document.getElementById('scQuickLabel');
    document.getElementById('scQuickJump').addEventListener('change', function (e) {
      pushUndo();
      var jumps = G.script.quick_jumps || [];
      if (e.target.checked) {
        if (!jumps.some(function (j) { return j.id === node.id; })) {
          jumps.push({ id: node.id, label: '' });
        }
        labelInput.style.display = '';
        labelInput.focus();
      } else {
        jumps = jumps.filter(function (j) { return j.id !== node.id; });
        labelInput.style.display = 'none';
      }
      G.script.quick_jumps = jumps;
      scheduleSave();
    });
    // Пустая подпись — на кнопке останется заголовок шага (так решает сервер)
    labelInput.addEventListener('input', function () {
      var j = (G.script.quick_jumps || []).filter(function (x) { return x.id === node.id; })[0];
      if (j) { j.label = labelInput.value; scheduleSave(); }
    });

    // rich-text
    rteEl = document.getElementById('scRte');
    rteEl.addEventListener('input', function () {
      node.body_html = rteEl.innerHTML;
      renderTree();
      var preview = world.querySelector('.sc-node[data-node="' + node.id + '"] .sc-node-text');
      if (preview) preview.textContent = stripHtml(node.body_html).slice(0, 160);
      scheduleSave();
    });
    rteEl.addEventListener('focus', pushUndo);
    // вставка только текстом — иначе из Word приезжает чужая вёрстка со шрифтами
    rteEl.addEventListener('paste', function (e) {
      e.preventDefault();
      var text = (e.clipboardData || window.clipboardData).getData('text/plain');
      document.execCommand('insertText', false, text);
    });

    document.getElementById('scRteToolbar').addEventListener('click', function (e) {
      var btn = e.target.closest('button');
      if (!btn) return;
      e.preventDefault();
      rteEl.focus();
      if (btn.dataset.cmd) document.execCommand(btn.dataset.cmd, false, null);
      else if (btn.dataset.block) document.execCommand('formatBlock', false, btn.dataset.block);
      else if (btn.dataset.color) document.execCommand('foreColor', false, btn.dataset.color);
      else if (btn.dataset.hilite) document.execCommand('hiliteColor', false, btn.dataset.hilite);
      else if (btn.dataset.link) {
        var url = prompt('Адрес ссылки:', 'https://');
        if (url) document.execCommand('createLink', false, url);
      } else if (btn.dataset.table) {
        document.execCommand('insertHTML', false,
          '<table><tr><th>Колонка</th><th>Колонка</th></tr>' +
          '<tr><td>&nbsp;</td><td>&nbsp;</td></tr></table><p><br></p>');
      }
      node.body_html = rteEl.innerHTML;
      scheduleSave();
    });

    document.getElementById('scAddAnswer').addEventListener('click', function () {
      addAnswer(node.id);
    });

    inspector.querySelectorAll('[data-a-text]').forEach(function (input) {
      input.addEventListener('focus', pushUndo);
      input.addEventListener('input', function () {
        var a = answerById(Number(input.dataset.aText));
        if (a) { a.text = input.value; renderCanvas(); scheduleSave(); }
      });
    });
    inspector.querySelectorAll('[data-a-color]').forEach(function (sel) {
      sel.addEventListener('change', function () {
        pushUndo();
        var a = answerById(Number(sel.dataset.aColor));
        if (a) { a.color = sel.value; renderCanvas(); scheduleSave(); }
      });
    });
    inspector.querySelectorAll('[data-a-next]').forEach(function (sel) {
      var a = answerById(Number(sel.dataset.aNext));
      if (a) sel.value = a.next ? String(a.next) : '';
      sel.addEventListener('change', function () {
        pushUndo();
        var ans = answerById(Number(sel.dataset.aNext));
        if (!ans) return;
        if (sel.value === '__new') {
          var base = nodeById(node.id);
          var created = addNode((base.x || 0) + 340, (base.y || 0) + 40);
          ans.next = created.id;
        } else {
          ans.next = sel.value ? Number(sel.value) : null;
        }
        renderAll();
        scheduleSave();
      });
    });
    inspector.querySelectorAll('[data-a-del]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        pushUndo();
        var id = Number(btn.dataset.aDel);
        node.answers = node.answers.filter(function (a) { return a.id !== id; });
        renderAll();
        scheduleSave();
      });
    });

    document.getElementById('scAddField').addEventListener('click', function () {
      pushUndo();
      node.fields = node.fields || [];
      node.fields.push({ key: 'field_' + (node.fields.length + 1), label: '', type: 'text', crm_field: '' });
      renderInspector();
      scheduleSave();
    });
    inspector.querySelectorAll('[data-f-label]').forEach(function (input) {
      input.addEventListener('input', function () {
        var f = node.fields[Number(input.dataset.fLabel)];
        if (f) { f.label = input.value; scheduleSave(); }
      });
    });
    inspector.querySelectorAll('[data-f-type]').forEach(function (sel) {
      sel.addEventListener('change', function () {
        var f = node.fields[Number(sel.dataset.fType)];
        if (f) { f.type = sel.value; scheduleSave(); }
      });
    });
    inspector.querySelectorAll('[data-f-crm]').forEach(function (input) {
      input.addEventListener('input', function () {
        var f = node.fields[Number(input.dataset.fCrm)];
        if (f) { f.crm_field = input.value.trim(); scheduleSave(); }
      });
    });
    inspector.querySelectorAll('[data-f-del]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        pushUndo();
        node.fields.splice(Number(btn.dataset.fDel), 1);
        renderInspector();
        scheduleSave();
      });
    });

    document.getElementById('scDupNode').addEventListener('click', function () { duplicateNode(node.id); });
    document.getElementById('scDelNode').addEventListener('click', function () { deleteNode(node.id); });
  }

  // ── Панорама, зум, горячие клавиши ────────────────────────────────────────

  canvas.addEventListener('mousedown', function (e) {
    if (e.button !== 0 || e.target.closest('.sc-node') || e.target.closest('.sc-minimap')) return;
    var start = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y };
    canvas.classList.add('panning');
    function move(ev) {
      view.x = start.vx + (ev.clientX - start.x);
      view.y = start.vy + (ev.clientY - start.y);
      applyView();
    }
    function up() {
      canvas.classList.remove('panning');
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
    }
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  });

  canvas.addEventListener('wheel', function (e) {
    e.preventDefault();
    zoomAt(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX, e.clientY);
  }, { passive: false });

  canvas.addEventListener('dblclick', function (e) {
    if (!cfg.canEdit || e.target.closest('.sc-node')) return;
    var p = toWorld(e.clientX, e.clientY);
    addNode(Math.round(p.x), Math.round(p.y));
  });

  document.addEventListener('keydown', function (e) {
    var tag = (e.target.tagName || '').toLowerCase();
    var typing = tag === 'input' || tag === 'textarea' || e.target.isContentEditable;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z' && !e.shiftKey) {
      if (typing && e.target.isContentEditable) return;   // внутри текста — родной undo
      e.preventDefault(); undo();
    } else if ((e.ctrlKey || e.metaKey) && (e.key.toLowerCase() === 'y' ||
      (e.shiftKey && e.key.toLowerCase() === 'z'))) {
      e.preventDefault(); redo();
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault(); clearTimeout(saveTimer); save();
    } else if (e.key === 'Delete' && !typing && selectedId) {
      deleteNode(selectedId);
    }
  });

  // ── Панель инструментов ───────────────────────────────────────────────────

  function on(id, handler) {
    var el = document.getElementById(id);
    if (el) el.addEventListener('click', handler);
  }
  on('scAddNode', function () {
    var rect = canvas.getBoundingClientRect();
    var p = toWorld(rect.left + rect.width / 2, rect.top + rect.height / 3);
    addNode(Math.round(p.x), Math.round(p.y));
  });
  on('scAddEnd', function () {
    var rect = canvas.getBoundingClientRect();
    var p = toWorld(rect.left + rect.width / 2, rect.top + rect.height / 2);
    var n = addNode(Math.round(p.x), Math.round(p.y), 'end');
    n.title = 'Завершение разговора';
    renderAll();
  });
  on('scUndo', undo);
  on('scRedo', redo);
  on('scLayout', autoLayout);
  on('scFit', fitToScreen);
  on('scZoomIn', function () { zoomAt(1.2); });
  on('scZoomOut', function () { zoomAt(1 / 1.2); });

  // размер холста меняется вместе с окном — пересчитываем связи и мини-карту,
  // иначе после разворачивания окна схема остаётся смещённой за кадр
  window.addEventListener('resize', function () { drawEdges(); renderMinimap(); });

  var treeSearch = document.getElementById('scTreeSearch');
  if (treeSearch) treeSearch.addEventListener('input', renderTree);

  if (miniSvg) {
    miniSvg.addEventListener('click', function (e) {
      var rect = miniSvg.getBoundingClientRect();
      var b = graphBounds();
      var px = b.x1 + (e.clientX - rect.left) / rect.width * (b.x2 - b.x1);
      var py = b.y1 + (e.clientY - rect.top) / rect.height * (b.y2 - b.y1);
      var c = canvas.getBoundingClientRect();
      view.x = c.width / 2 - px * view.k;
      view.y = c.height / 2 - py * view.k;
      applyView();
    });
  }

  // ── Старт ─────────────────────────────────────────────────────────────────

  function renderAll() {
    renderTree();
    renderCanvas();
    renderInspector();
    renderMinimap();
  }

  if (G.nodes.length) selectedId = G.script.start_node_id || G.nodes[0].id;
  renderAll();
  applyView();
  fitToScreen();
  setStatus(cfg.canEdit ? 'Все изменения сохраняются автоматически' : 'Только просмотр');
  refreshHistoryButtons();
})();
