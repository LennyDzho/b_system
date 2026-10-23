"use strict";

(() => {
  document.addEventListener("submit", event => {
    if (!event.target.matches(".filters")) return;
    for (const field of event.target.elements) {
      if (field.name && !field.value) field.disabled = true;
    }
  });

  // Preferences contain presentation state only; browser storage is optional.
  const preference = (key, value) => {
    try {
      if (value === undefined) return sessionStorage.getItem(key);
      sessionStorage.setItem(key, value);
    } catch { /* Controls also work when browser storage is unavailable. */ }
    return null;
  };
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };

  const panels = [];
  document.querySelectorAll("section.panel").forEach((panel, index) => {
    const heading = Array.from(panel.children).find(child => child.tagName === "H2");
    if (!heading) return;
    panel.id ||= `finance-panel-${index}`;
    const content = node("div");
    content.id = `${panel.id}-content`;
    while (panel.firstChild) content.append(panel.firstChild);
    heading.remove();
    const button = node("button", "Свернуть", "quiet panel-toggle");
    button.type = "button";
    button.setAttribute("aria-controls", content.id);
    const header = node("div", undefined, "panel-heading");
    header.append(heading, button);
    panel.append(header, content);
    const key = `vf:panel:${location.pathname}:${heading.textContent}`;
    const collapse = (collapsed, remember = true) => {
      content.hidden = collapsed;
      panel.classList.toggle("is-collapsed", collapsed);
      button.textContent = collapsed ? "Развернуть" : "Свернуть";
      button.setAttribute("aria-expanded", String(!collapsed));
      button.setAttribute("aria-label", `${button.textContent}: ${heading.textContent}`);
      if (remember) preference(key, String(collapsed));
    };
    const remembered = preference(key);
    collapse(remembered === null ? panel.dataset.defaultCollapsed === "true" : remembered === "true", false);
    button.addEventListener("click", () => collapse(!content.hidden));
    panels.push({panel, collapse});
  });
  const reveal = hash => {
    const target = document.getElementById(hash.replace(/^#/, ""));
    if (target) panels.forEach(({panel, collapse}) => {
      if (panel === target || panel.contains(target)) collapse(false);
    });
  };
  document.querySelectorAll("[data-collapse-all]").forEach(button => {
    button.addEventListener("click", () => panels.forEach(({collapse}) => collapse(button.dataset.collapseAll === "true")));
  });
  document.addEventListener("click", event => {
    const anchor = event.target.closest("a[href^='#']");
    if (anchor) reveal(anchor.getAttribute("href"));
  });
  window.addEventListener("hashchange", () => reveal(location.hash));
  if (location.hash) reveal(location.hash);
  // Preserve old upload bookmarks after separating the import representations.
  if (location.pathname === "/imports" && location.hash === "#import-upload" && !document.getElementById("import-upload")) {
    const destination = new URL(location.href);
    destination.searchParams.set("view", "upload");
    location.replace(destination);
  }

  const source = document.getElementById("dashboard-chart-data");
  if (!source) return;
  const data = JSON.parse(source.textContent);
  const labels = {
    gross: "Стоимость бронирований · Gross", refund: "Возвраты", net: "После возвратов · Net",
    ota: "Комиссии каналов · OTA", management: "Комиссии управления", expenses: "Расходы",
    owner_estimate: "Оценка собственника до неизвестных комиссий", cash: "Поступления", after_ota: "Начисления после OTA"
  };
  const colors = {
    gross: "#24699c", refund: "#ad5465", net: "#6d6fc4", ota: "#a77835", management: "#9b5a9a",
    expenses: "#be6249", owner_estimate: "#c08b26", cash: "#16876c", after_ota: "#24699c"
  };
  // Preserve exact decimal strings in labels. Number conversion is only for SVG coordinates.
  const money = value => {
    if (value === null || value === undefined) return "Не определено";
    const [whole, fraction = ""] = String(value).split(".");
    return whole.replace(/\B(?=(\d{3})+(?!\d))/g, " ") + (fraction && !/^0+$/.test(fraction) ? `,${fraction}` : "");
  };
  const metricText = metric => {
    let text = metric.value === null ? `Не определено; известная часть ${money(metric.known_subtotal)} IDR` : `${money(metric.value)} IDR`;
    if (metric.unknown_count) text += `; неизвестных комиссий: ${metric.unknown_count}`;
    if (metric.is_estimate) text += "; оценка";
    if (!metric.is_complete) text += "; неполные данные";
    return text;
  };
  const periodLabel = point => point.date_from === point.date_to ? point.date_from : `${point.date_from} — ${point.date_to}`;
  const stepLabels = {day: "день", week: "неделя с понедельника", month: "календарный месяц", year: "год"};
  document.querySelectorAll("[data-chart-step]").forEach(element => {
    const series = data.charts[element.dataset.chartStep];
    element.textContent = `Шаг: ${series.step > 1 ? `${series.step} лет` : stepLabels[series.granularity]} · IDR`;
  });
  document.querySelectorAll(".legend-toggle").forEach(button => {
    const legend = document.getElementById(button.getAttribute("aria-controls"));
    const key = `vf:legend:${legend.id}`;
    const update = hidden => {
      legend.hidden = hidden;
      button.textContent = hidden ? "Показать легенду" : "Скрыть легенду";
      button.setAttribute("aria-expanded", String(!hidden));
      preference(key, String(hidden));
    };
    update(preference(key) === "true");
    button.addEventListener("click", () => update(!legend.hidden));
  });
  const svgNode = (tag, attributes = {}, text) => {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attributes).forEach(([name, value]) => element.setAttribute(name, value));
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const frame = (id, height = 320, width = 960, left = 82, right = 28) => {
    const host = document.getElementById(id);
    const svg = svgNode("svg", {viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": host.closest("section").querySelector("h2").textContent});
    svg.style.minWidth = `${width}px`;
    host.replaceChildren(svg);
    const box = {svg, width, height, left, right, top: 25, bottom: 48};
    box.plotWidth = width - left - right;
    box.plotHeight = height - box.top - box.bottom;
    return box;
  };
  const scale = values => {
    const min = Math.min(0, ...values), max = Math.max(0, ...values);
    const margin = (max - min) * .1 || 1000000;
    return {min: min < 0 ? min - margin : 0, max: max > 0 ? max + margin : (min < 0 ? 0 : margin)};
  };
  const tickText = value => (value / 1000000).toLocaleString("ru-RU", {maximumFractionDigits: Math.abs(value) < 1000000 ? 6 : 1});
  const verticalAxis = (box, bounds) => {
    const y = value => box.top + (bounds.max - value) / (bounds.max - bounds.min) * box.plotHeight;
    for (let index = 0; index <= 4; index++) {
      const value = bounds.min + (bounds.max - bounds.min) * index / 4;
      const position = y(value);
      box.svg.append(svgNode("line", {x1: box.left, x2: box.width - box.right, y1: position, y2: position, class: "chart-gridline"}));
      box.svg.append(svgNode("text", {x: box.left - 12, y: position + 4, "text-anchor": "end", class: "chart-axis"}, tickText(value)));
    }
    box.svg.append(svgNode("text", {x: box.left, y: 13, class: "chart-axis"}, "млн IDR"));
    box.svg.append(svgNode("line", {x1: box.left, x2: box.width - box.right, y1: y(0), y2: y(0), class: "chart-zero"}));
    if (bounds.min < 0 && bounds.max > 0) box.svg.append(svgNode("text", {x: box.left - 12, y: y(0) + 4, "text-anchor": "end", class: "chart-axis"}, "0"));
    return y;
  };
  const legend = (id, keys, selected, draw) => {
    const host = document.getElementById(id);
    if (!host) return;
    keys.forEach(key => {
      const button = node("button");
      button.type = "button";
      button.dataset.series = key;
      button.setAttribute("aria-pressed", String(selected.has(key)));
      const swatch = node("span", undefined, "chart-swatch");
      swatch.style.backgroundColor = colors[key];
      button.append(swatch, node("span", labels[key]));
      button.addEventListener("click", () => {
        if (selected.has(key)) selected.delete(key); else selected.add(key);
        button.setAttribute("aria-pressed", String(selected.has(key)));
        draw();
      });
      host.append(button);
    });
    draw();
  };
  const noSeries = box => box.svg.append(svgNode("text", {x: box.width / 2, y: box.height / 2, "text-anchor": "middle", class: "chart-axis"}, "Выберите показатели в легенде"));

  if (document.getElementById("trend-chart")) {
  const points = data.charts.trend.points;
  const trendKeys = ["gross", "refund", "net", "ota", "management", "expenses", "owner_estimate", "cash"];
  const trendVisible = new Set(["gross", "owner_estimate", "cash"]);
  const picker = document.getElementById("trend-period");
  points.forEach((point, index) => {
    const option = node("option", periodLabel(point));
    option.value = index;
    picker.append(option);
  });
  const readout = index => {
    const point = points[index];
    const host = document.getElementById("trend-readout");
    host.replaceChildren(node("strong", periodLabel(point)));
    trendKeys.filter(key => trendVisible.has(key)).forEach(key => {
      const item = node("span", `${labels[key]}: ${metricText(point.metrics[key])}`);
      item.style.borderLeft = `3px solid ${colors[key]}`;
      item.style.paddingLeft = "8px";
      host.append(item);
    });
    if (point.excluded_count) host.append(node("span", `Исключённых записей: ${point.excluded_count}`));
  };
  picker.addEventListener("change", () => readout(Number(picker.value)));
  const drawTrend = () => {
    const box = frame("trend-chart", 310, Math.max(520, document.getElementById("trend-chart").clientWidth));
    const keys = trendKeys.filter(key => trendVisible.has(key));
    readout(Number(picker.value));
    if (!keys.length) { noSeries(box); return; }
    const values = points.flatMap(point => keys.map(key => point.metrics[key].value)).filter(value => value !== null).map(Number);
    if (!values.length) {
      box.svg.append(svgNode("text", {x: box.width / 2, y: box.height / 2, "text-anchor": "middle", class: "chart-axis"}, "Значения выбранных показателей не определены"));
      return;
    }
    const y = verticalAxis(box, scale(values));
    const x = index => box.left + (points.length === 1 ? box.plotWidth / 2 : index / (points.length - 1) * box.plotWidth);
    const labelCount = Math.min(7, points.length);
    const labelIndexes = new Set(Array.from({length: labelCount}, (_, index) => Math.round(index * (points.length - 1) / Math.max(1, labelCount - 1))));
    points.forEach((point, index) => {
      if (labelIndexes.has(index)) box.svg.append(svgNode("text", {x: x(index), y: box.height - 18, "text-anchor": index === 0 ? "start" : index === points.length - 1 ? "end" : "middle", class: "chart-axis"}, point.date_from));
    });
    keys.forEach(key => {
      let path = "", connected = false;
      points.forEach((point, index) => {
        const metric = point.metrics[key];
        if (metric.value === null) { connected = false; return; }
        path += `${connected ? "L" : "M"}${x(index)},${y(Number(metric.value))} `;
        connected = true;
      });
      const incomplete = points.some(point => !point.metrics[key].is_complete || point.metrics[key].is_estimate);
      box.svg.append(svgNode("path", {d: path, fill: "none", stroke: colors[key], "stroke-width": 2.2, "stroke-dasharray": incomplete ? "6 4" : "none", "data-series": key}));
      points.forEach((point, index) => {
        if (point.metrics[key].value === null) return;
        const dot = svgNode("circle", {cx: x(index), cy: y(Number(point.metrics[key].value)), r: 3.2, fill: colors[key]});
        dot.append(svgNode("title", {}, `${periodLabel(point)} · ${labels[key]}: ${metricText(point.metrics[key])}`));
        dot.addEventListener("mouseenter", () => { picker.value = index; readout(index); });
        box.svg.append(dot);
      });
    });
  };
  legend("trend-legend", trendKeys, trendVisible, drawTrend);
  }

  const comparisonKeys = ["after_ota", "cash"];
  const comparisonVisible = new Set(comparisonKeys);
  const drawComparison = () => {
    const box = frame("cash-chart", 340, Math.max(600, document.getElementById("cash-chart").clientWidth));
    const periods = data.charts.cash_accrual.points;
    const keys = comparisonKeys.filter(key => comparisonVisible.has(key));
    if (!keys.length) { noSeries(box); return; }
    const y = verticalAxis(box, scale(periods.flatMap(point => keys.map(key => point.metrics[key].value)).filter(value => value !== null).map(Number)));
    const groupWidth = box.plotWidth / periods.length;
    const barWidth = Math.min(55, groupWidth * .72 / keys.length);
    const labelStep = Math.max(1, Math.ceil(periods.length / 6));
    periods.forEach((point, index) => {
      const center = box.left + groupWidth * (index + .5);
      keys.forEach((key, position) => {
        const metric = point.metrics[key];
        if (metric.value === null) return;
        const value = Number(metric.value);
        const bar = svgNode("rect", {x: center - keys.length * barWidth / 2 + position * barWidth + 2, y: Math.min(y(value), y(0)), width: Math.max(1, barWidth - 4), height: Math.abs(y(value) - y(0)), rx: 3, fill: colors[key]});
        bar.append(svgNode("title", {}, `${periodLabel(point)} · ${labels[key]}: ${metricText(metric)}`));
        box.svg.append(bar);
      });
      if (index % labelStep === 0) box.svg.append(svgNode("text", {x: center, y: box.height - 18, "text-anchor": "middle", class: "chart-axis"}, point.date_from.slice(0, 7)));
    });
  };
  legend("cash-legend", comparisonKeys, comparisonVisible, drawComparison);

  const horizontal = (rows, keys) => {
    const box = frame("properties-chart", Math.max(240, rows.length * 54 + 65), Math.max(700, document.getElementById("properties-chart").clientWidth), 160, 150);
    if (!keys.length) { noSeries(box); return; }
    const values = rows.flatMap(row => keys.map(key => row.metrics[key].value)).filter(value => value !== null).map(Number);
    const bounds = scale(values);
    const x = value => box.left + (value - bounds.min) / (bounds.max - bounds.min) * box.plotWidth;
    for (let index = 0; index <= 4; index++) {
      const value = bounds.min + (bounds.max - bounds.min) * index / 4;
      box.svg.append(svgNode("line", {x1: x(value), x2: x(value), y1: 20, y2: box.height - 40, class: "chart-gridline"}));
      box.svg.append(svgNode("text", {x: x(value), y: box.height - 17, "text-anchor": "middle", class: "chart-axis"}, tickText(value)));
    }
    box.svg.append(svgNode("text", {x: box.left, y: 13, class: "chart-axis"}, "млн IDR"));
    box.svg.append(svgNode("line", {x1: x(0), x2: x(0), y1: 20, y2: box.height - 40, class: "chart-zero"}));
    rows.forEach((row, index) => {
      const top = 30 + index * 54;
      const label = svgNode("text", {x: box.left - 16, y: top + 18, "text-anchor": "end", class: "chart-axis"}, row.label.length > 35 ? `${row.label.slice(0, 33)}…` : row.label);
      label.append(svgNode("title", {}, row.label));
      box.svg.append(label);
      keys.forEach((key, position) => {
        const metric = row.metrics[key];
        if (metric.value === null) return;
        const value = Number(metric.value), topBar = top + position * 21;
        const bar = svgNode("rect", {x: Math.min(x(0), x(value)), y: topBar, width: Math.abs(x(value) - x(0)), height: 15, rx: 3, fill: colors[key], stroke: colors[key], "stroke-dasharray": !metric.is_complete || metric.is_estimate ? "4 3" : "none", "fill-opacity": !metric.is_complete || metric.is_estimate ? .65 : 1});
        bar.append(svgNode("title", {}, `${row.label} · ${labels[key]}: ${metricText(metric)}`));
        box.svg.append(bar, svgNode("text", {x: box.width - box.right + 18, y: topBar + 12, class: "chart-axis"}, `${money(metric.value)}${!metric.is_complete ? " *" : ""}`));
      });
    });
  };
  if (document.getElementById("deductions-chart")) {
  const deductions = frame("deductions-chart", 315, 600, 24, 24);
  const deductionValueX = deductions.width - 190;
  const deductionKeys = ["refund", "ota", "management", "expenses"];
  const deductionMax = Math.max(1, ...deductionKeys.map(key => Number(data.metrics[key].known_subtotal)));
  deductionKeys.forEach((key, index) => {
    const metric = data.metrics[key], y = 25 + index * 72;
    deductions.svg.append(svgNode("text", {x: 24, y, class: "chart-axis"}, labels[key] + (key === "management" ? " · известная часть" : "")));
    const bar = svgNode("rect", {x: 24, y: y + 12, width: Number(metric.known_subtotal) / deductionMax * (deductionValueX - 42), height: 20, rx: 4, fill: colors[key]});
    bar.append(svgNode("title", {}, `${labels[key]}: ${metricText(metric)}`));
    deductions.svg.append(bar, svgNode("text", {x: deductionValueX, y: y + 27, class: "chart-axis"}, `${money(metric.known_subtotal)} IDR${!metric.is_complete ? " *" : ""}`));
  });
  }
  const propertyKeys = ["owner_estimate", "cash"];
  const propertyVisible = new Set(propertyKeys);
  const propertyRows = data.properties.map(prop => ({label: `${prop.property_id} · ${prop.property_name}`, metrics: prop.metrics}));
  const propertyPicker = document.getElementById("property-chart-count");
  const drawProperties = () => horizontal(propertyPicker.value === "all" ? propertyRows : propertyRows.slice(0, 8), propertyKeys.filter(key => propertyVisible.has(key)));
  legend("properties-legend", propertyKeys, propertyVisible, drawProperties);
  if (propertyPicker) propertyPicker.addEventListener("change", drawProperties);
})();
