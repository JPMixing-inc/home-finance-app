// Small hand-drawn SVG charts. A chart is <div class="chart" data-chart="path|trend|spark" data-src="id">,
// where #id is a <script type="application/json"> holding its data. Charts redraw on resize and theme change.
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const money = (c) => (c < 0 ? "−" : "") + "$" + Math.abs(c / 100).toLocaleString("en-US",
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const shortMonth = (label, withYear) => label.slice(0, 3) + (withYear ? " '" + label.slice(-2) : "");

  function svgEl(tag, attrs, parent) {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    if (parent) parent.appendChild(node);
    return node;
  }

  function niceTicks(max, count) {
    if (max <= 0) return [0, 100000];
    const raw = max / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
    const ticks = [];
    for (let v = 0; v < max + step * 0.999; v += step) ticks.push(v);
    return ticks;
  }

  // Y-axis labels: full dollars under 10,000, compact above; one style per axis.
  function axisFormatter(top) {
    const compact = top / 100 >= 10000;
    return (c) => compact ? "$" + Math.round(c / 100000) + "K" : "$" + Math.round(c / 100).toLocaleString("en-US");
  }

  function makeSvg(box, height) {
    box.querySelector("svg")?.remove();
    const width = Math.max(260, box.clientWidth);
    const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height, role: "img" });
    box.prepend(svg);
    return { svg, width };
  }

  function tip(box) {
    let t = box.querySelector(".chart-tip");
    if (!t) { t = document.createElement("div"); t.className = "chart-tip"; box.appendChild(t); }
    return t;
  }

  // Tooltip: the value leads, the series name follows; series keyed by a short line in its color.
  function showTip(box, x, y, title, rows) {
    const t = tip(box);
    t.replaceChildren();
    const h = document.createElement("div"); h.className = "tip-title"; h.textContent = title; t.appendChild(h);
    for (const row of rows) {
      const r = document.createElement("div"); r.className = "tip-row";
      if (row.color) { const k = document.createElement("span"); k.className = "tip-key"; k.style.background = row.color; r.appendChild(k); }
      const v = document.createElement("strong"); v.textContent = row.value; r.appendChild(v);
      if (row.label) { const l = document.createElement("span"); l.className = "tip-label"; l.textContent = row.label; r.appendChild(l); }
      t.appendChild(r);
    }
    t.classList.add("show");
    const bw = box.clientWidth, tw = t.offsetWidth;
    t.style.left = Math.min(Math.max(0, x + 14), bw - tw) + "px";
    t.style.top = Math.max(0, y - t.offsetHeight - 10) + "px";
  }
  const hideTip = (box) => box.querySelector(".chart-tip")?.classList.remove("show");

  function grid(svg, ticks, y, pad, width, fmt) {
    for (const v of ticks) {
      svgEl("line", { x1: pad.l, x2: width - pad.r, y1: y(v), y2: y(v), class: "grid" }, svg);
      const t = svgEl("text", { x: pad.l - 8, y: y(v) + 4, "text-anchor": "end", class: "axis" }, svg);
      t.textContent = fmt(v);
    }
  }

  function gradient(svg, id, color) {
    const defs = svgEl("defs", {}, svg);
    const g = svgEl("linearGradient", { id, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
    svgEl("stop", { offset: "0%", "stop-color": color, "stop-opacity": 0.32 }, g);
    svgEl("stop", { offset: "100%", "stop-color": color, "stop-opacity": 0 }, g);
  }

  // ---------------------------------------------------------------- path to debt-free
  function pathChart(box, d) {
    const height = +box.dataset.height || 300;
    const { svg, width } = makeSvg(box, height);
    const pad = { l: 64, r: 20, t: 16, b: 30 };
    const n = d.labels.length;
    const val = (arr, i) => (i < arr.length ? arr[i] : 0);
    // Scale to the plan; a runaway minimums-only line is clipped at the top (the tooltip has its values).
    const snowMax = Math.max(...d.snow), baseMax = Math.max(...d.base.slice(0, n));
    const ticks = niceTicks(baseMax > snowMax * 1.6 ? snowMax * 1.4 : Math.max(snowMax, baseMax), 4);
    const top = ticks[ticks.length - 1];
    const x = (i) => pad.l + (i * (width - pad.l - pad.r)) / Math.max(1, n - 1);
    const y = (v) => pad.t + (1 - v / top) * (height - pad.t - pad.b);
    const c1 = token("--series-1"), cb = token("--baseline"), ring = token("--surface-solid");

    grid(svg, ticks, y, pad, width, axisFormatter(top));
    const plotW = width - pad.l - pad.r;
    const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(plotW / 70))));  // ~70px per label
    for (let i = 0; i < n; i += every) {
      const t = svgEl("text", { x: x(i), y: height - 8, "text-anchor": i === 0 ? "start" : "middle", class: "axis" }, svg);
      t.textContent = i === 0 ? "Now" : shortMonth(d.labels[i], true);
    }
    gradient(svg, "snowFill", c1);
    const pts = (arr) => Array.from({ length: n }, (_, i) => `${x(i)},${y(val(arr, i))}`);
    const snowPts = pts(d.snow);
    svgEl("path", { d: `M${x(0)},${y(0)} L${snowPts.join(" L")} L${x(n - 1)},${y(0)} Z`, fill: "url(#snowFill)" }, svg);
    const clip = svgEl("clipPath", { id: "plotClip" }, svgEl("defs", {}, svg));
    svgEl("rect", { x: 0, y: pad.t - 2, width, height: height - pad.t - pad.b + 4 }, clip);
    svgEl("polyline", { "clip-path": "url(#plotClip)", points: pts(d.base).join(" "), fill: "none", stroke: cb, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    svgEl("polyline", { points: snowPts.join(" "), fill: "none", stroke: c1, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", class: "glow-line" }, svg);
    for (const e of d.events) {
      if (e.m < n) svgEl("circle", { cx: x(e.m), cy: y(val(d.snow, e.m)), r: 4, fill: c1, stroke: ring, "stroke-width": 2 }, svg);
    }
    if (d.snow_months != null && d.snow_months < n) {
      const t = svgEl("text", { x: Math.min(x(d.snow_months) + 8, width - pad.r), y: y(0) - 10,
        "text-anchor": x(d.snow_months) > width - 140 ? "end" : "start", class: "end-label" }, svg);
      t.textContent = "Debt-free " + d.labels[d.snow_months];
    }

    const hair = svgEl("line", { y1: pad.t, y2: height - pad.b, class: "crosshair", visibility: "hidden" }, svg);
    const dotS = svgEl("circle", { r: 4, fill: c1, stroke: ring, "stroke-width": 2, visibility: "hidden" }, svg);
    const dotB = svgEl("circle", { r: 4, fill: cb, stroke: ring, "stroke-width": 2, visibility: "hidden" }, svg);
    const hit = svgEl("rect", { x: pad.l, y: 0, width: width - pad.l - pad.r, height, fill: "transparent" }, svg);
    let current = 0;
    const show = (i) => {
      current = Math.max(0, Math.min(n - 1, i));
      for (const node of [hair, dotS, dotB]) node.setAttribute("visibility", "visible");
      hair.setAttribute("x1", x(current)); hair.setAttribute("x2", x(current));
      dotS.setAttribute("cx", x(current)); dotS.setAttribute("cy", y(val(d.snow, current)));
      dotB.setAttribute("cx", x(current)); dotB.setAttribute("cy", Math.max(pad.t, y(val(d.base, current))));
      const rows = [
        { color: c1, value: money(val(d.snow, current)), label: "Snowball plan" },
        { color: cb, value: current < d.base.length ? money(d.base[current]) : "$0.00", label: "Minimums only" },
      ];
      for (const e of d.events) if (e.m === current) rows.push({ value: "✓ Paid off", label: e.name });
      showTip(box, x(current), y(val(d.snow, current)), current === 0 ? "Now" : d.labels[current], rows);
    };
    const hide = () => { for (const node of [hair, dotS, dotB]) node.setAttribute("visibility", "hidden"); hideTip(box); };
    hit.addEventListener("pointermove", (e) => {
      const r = svg.getBoundingClientRect();
      show(Math.round(((e.clientX - r.left) * (width / r.width) - pad.l) / ((width - pad.l - pad.r) / Math.max(1, n - 1))));
    });
    hit.addEventListener("pointerleave", hide);
    svg.setAttribute("tabindex", "0");
    svg.addEventListener("focus", () => show(current));
    svg.addEventListener("blur", hide);
    svg.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight") { show(current + 1); e.preventDefault(); }
      if (e.key === "ArrowLeft") { show(current - 1); e.preventDefault(); }
    });
    svg.setAttribute("aria-label", `Total debt: ${money(d.snow[0])} now` +
      (d.snow_months != null ? `, paid off by ${d.labels[d.snow_months]} on the snowball plan` : ""));
  }

  // ---------------------------------------------------------------- income vs spending by month
  function columnTop(x, y, w, h) {
    const r = Math.min(4, w / 2, h);
    return `M${x},${y + h} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + h} Z`;
  }

  function trendChart(box, rows) {
    const height = +box.dataset.height || 260;
    const { svg, width } = makeSvg(box, height);
    const pad = { l: 64, r: 12, t: 16, b: 30 };
    const n = rows.length;
    const ticks = niceTicks(Math.max(1, ...rows.map((r) => Math.max(r.income, r.spent))), 4);
    const top = ticks[ticks.length - 1];
    const band = (width - pad.l - pad.r) / n;
    const barW = Math.max(3, Math.min(24, (band - 10) / 2));
    const y = (v) => pad.t + (1 - v / top) * (height - pad.t - pad.b);
    const c1 = token("--series-2"), c2 = token("--series-1");  // income cool, spending brand pink
    grid(svg, ticks, y, pad, width, axisFormatter(top));
    rows.forEach((r, i) => {
      const cx = pad.l + band * i + band / 2;
      const g = svgEl("g", { class: "col-group", tabindex: 0 }, svg);
      const bars = [[r.income, c1, cx - barW - 1], [r.spent, c2, cx + 1]];
      for (const [v, color, bx] of bars) {
        const h = Math.max(0, y(0) - y(v));
        if (h > 0) svgEl("path", { d: columnTop(bx, y(v), barW, h), fill: color, class: "col" }, g);
      }
      const label = svgEl("text", { x: cx, y: height - 8, "text-anchor": "middle", class: "axis" }, svg);
      const wide = i === 0 || r.label.startsWith("Jan");
      label.textContent = shortMonth(r.label, wide && band >= 46);
      const step = Math.max(1, Math.ceil(44 / band));  // keep ~44px between labels
      if ((n - 1 - i) % step !== 0) label.setAttribute("visibility", "hidden");
      const hit = svgEl("rect", { x: pad.l + band * i, y: 0, width: band, height, fill: "transparent" }, g);
      const show = () => {
        g.classList.add("hover");
        const net = r.income - r.spent;
        showTip(box, cx, y(Math.max(r.income, r.spent)), r.label, [
          { color: c1, value: money(r.income), label: "Income" },
          { color: c2, value: money(r.spent), label: "Spending" },
          { value: (net >= 0 ? "+" : "") + money(net), label: "Net" },
        ]);
      };
      const hide = () => { g.classList.remove("hover"); hideTip(box); };
      hit.addEventListener("pointerenter", show);
      hit.addEventListener("pointerleave", hide);
      g.addEventListener("focus", show);
      g.addEventListener("blur", hide);
    });
    svg.setAttribute("aria-label", "Income and spending for the last " + n + " months");
  }

  // ---------------------------------------------------------------- sparkline (dashboard card)
  function sparkChart(box, d) {
    const height = +box.dataset.height || 56;
    const { svg, width } = makeSvg(box, height);
    const vals = d.snow.slice(0, (d.snow_months ?? d.snow.length - 1) + 1);
    const max = Math.max(1, ...vals);
    const x = (i) => 4 + (i * (width - 8)) / Math.max(1, vals.length - 1);
    const y = (v) => 4 + (1 - v / max) * (height - 8);
    const c1 = token("--series-1");
    gradient(svg, "sparkFill", c1);
    const pts = vals.map((v, i) => `${x(i)},${y(v)}`);
    svgEl("path", { d: `M${x(0)},${y(0)} L${pts.join(" L")} L${x(vals.length - 1)},${y(0)} Z`, fill: "url(#sparkFill)" }, svg);
    svgEl("polyline", { points: pts.join(" "), fill: "none", stroke: c1, "stroke-width": 2, "stroke-linejoin": "round", class: "glow-line" }, svg);
    svgEl("circle", { cx: x(0), cy: y(vals[0]), r: 4, fill: c1, stroke: token("--surface-solid"), "stroke-width": 2 }, svg);
    svg.setAttribute("aria-label", "Projected total debt from " + money(vals[0]) + " down to $0");
  }

  const kinds = { path: pathChart, trend: trendChart, spark: sparkChart };
  document.querySelectorAll(".chart[data-chart]").forEach((box) => {
    const src = document.getElementById(box.dataset.src);
    if (!src || !kinds[box.dataset.chart]) return;
    const data = JSON.parse(src.textContent);
    let last = 0;
    const draw = () => kinds[box.dataset.chart](box, data);
    draw();
    new ResizeObserver(() => { if (Math.abs(box.clientWidth - last) > 2) { last = box.clientWidth; draw(); } }).observe(box);
    document.addEventListener("themechange", draw);
  });
})();
