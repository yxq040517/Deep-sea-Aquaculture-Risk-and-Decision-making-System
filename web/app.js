"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  datasets: [],
  models: [],
  selected: { datasetId: "", modelId: "", site: "", depth: "" },
  analysis: null,
  forecast: null,
  analysisKey: "",
  forecastKey: "",
  jobId: "",
  pollTimer: null,
  analysisRequest: 0,
  forecastRequest: 0,
};
const fields = [
  {
    key: "temperature_c",
    label: "水温",
    unit: "°C",
    actual: "actual_temperature_c",
    predicted: "predicted_temperature_c",
  },
  {
    key: "dissolved_oxygen_mg_l",
    label: "溶解氧",
    unit: "mg/L",
    actual: "actual_dissolved_oxygen_mg_l",
    predicted: "predicted_dissolved_oxygen_mg_l",
  },
];

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}
function empty(parent, message) {
  parent.replaceChildren(node("p", message, "empty-state"));
}
function status(id, message, isError = false) {
  const element = $(id);
  element.textContent = message || "";
  element.classList.toggle("error", isError);
}
function notice(message, isError = false) {
  const element = $("global-message");
  element.textContent = message || "";
  element.hidden = !message;
  element.classList.toggle("error", isError);
}
function detailMessage(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail))
    return detail
      .map((item) =>
        typeof item?.msg === "string" ? item.msg : JSON.stringify(item),
      )
      .join("；");
  return "请求未完成，请检查数据和服务后重试。";
}
async function request(url, options) {
  let response;
  try {
    response = await fetch(url, options);
  } catch (error) {
    throw new Error("无法连接本地服务：" + error.message);
  }
  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    throw new Error("服务返回的内容无法解析，请检查本地服务状态。");
  }
  if (!response.ok) throw new Error(detailMessage(data.detail || data.error));
  return data;
}
function postJSON(url, body) {
  return request(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
function finite(value) {
  return typeof value === "number" && Number.isFinite(value)
    ? value
    : Number(value);
}
function number(value, digits = 3) {
  return value === null ||
    value === undefined ||
    value === "" ||
    !Number.isFinite(finite(value))
    ? "—"
    : finite(value).toFixed(digits);
}
function utcDate(value) {
  const text = String(value || "");
  return new Date(/(?:[zZ]|[+-]\d{2}:\d{2})$/.test(text) ? text : text + "Z");
}
function time(value) {
  if (!value) return "—";
  const date = utcDate(value);
  return Number.isNaN(date.getTime())
    ? String(value)
    : new Intl.DateTimeFormat("zh-CN", {
        timeZone: "UTC",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(date);
}
function shortTime(value) {
  const date = utcDate(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 16);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "UTC",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    hour12: false,
  }).format(date);
}
function currentDataset() {
  return state.datasets.find((item) => item.id === state.selected.datasetId);
}
function currentModel() {
  return state.models.find((item) => item.id === state.selected.modelId);
}
function activeSeries() {
  return (currentDataset()?.series || []).find(
    (item) =>
      item.site === state.selected.site &&
      String(item.depth_m) === state.selected.depth,
  );
}
function seriesList() {
  return Array.isArray(currentDataset()?.series) ? currentDataset().series : [];
}
function selectionKey() {
  return JSON.stringify(state.selected);
}
function selectOptions(element, items, chosen, placeholder) {
  const options = [new Option(placeholder, "")];
  for (const item of items)
    options.push(new Option(String(item.label), String(item.value)));
  element.replaceChildren(...options);
  element.value = chosen;
}
function syncSelections() {
  for (const prefix of ["train", "analysis", "forecast"]) {
    selectOptions(
      $(prefix + "-dataset"),
      state.datasets.map((item) => ({
        value: item.id,
        label: `${item.name} · ${item.rows} 条`,
      })),
      state.selected.datasetId,
      "请选择已导入数据",
    );
  }
  for (const prefix of ["train", "analysis", "forecast"]) {
    selectOptions(
      $(prefix + "-model"),
      state.models.map((item) => ({
        value: item.id,
        label: `${item.id} · ${item.summary?.model?.type || "已保存模型"}`,
      })),
      state.selected.modelId,
      "请选择已保存模型",
    );
  }
  const sites = [...new Set(seriesList().map((item) => item.site))];
  const depths = seriesList().filter(
    (item) => item.site === state.selected.site,
  );
  for (const prefix of ["analysis", "forecast"]) {
    selectOptions(
      $(prefix + "-site"),
      sites.map((site) => ({ value: site, label: site })),
      state.selected.site,
      "请选择站点",
    );
    selectOptions(
      $(prefix + "-depth"),
      depths.map((item) => ({
        value: item.depth_m,
        label: `${item.depth_m} m · ${item.observations} 条`,
      })),
      state.selected.depth,
      "请选择水深",
    );
  }
  renderDatasetPreview();
  renderModelPreview();
}
function clearResults() {
  state.analysisRequest++;
  state.forecastRequest++;
  state.analysis = state.forecast = null;
  state.analysisKey = state.forecastKey = "";
  empty(
    $("analysis-output"),
    "选择已导入的观测数据及模型后运行回放，这里将显示目标时刻的实测值、模型值及误差。",
  );
  empty(
    $("forecast-output"),
    "选择数据集、模型与观测序列后生成预测；这里会并排呈现历史观测和未来数值。",
  );
  $("analysis-export").disabled = true;
  for (const id of ["forecast-csv", "forecast-xlsx"]) {
    $(id).removeAttribute("href");
    $(id).classList.add("disabled-link");
    $(id).setAttribute("aria-disabled", "true");
  }
  status("analysis-status", "");
  status("forecast-status", "");
}
function choose(key, value) {
  if (state.selected[key] === value) return;
  state.selected[key] = value;
  if (key === "datasetId") {
    const first = seriesList()[0];
    state.selected.site = first?.site || "";
    state.selected.depth = first ? String(first.depth_m) : "";
  }
  if (key === "site") {
    const first = seriesList().find((item) => item.site === value);
    state.selected.depth = first ? String(first.depth_m) : "";
  }
  clearResults();
  syncSelections();
}
function makeStats(container, stats) {
  const grid = node("div", null, "mini-stats");
  for (const [label, value] of stats) {
    const box = node("div", null, "mini-stat");
    box.append(
      node("span", label, "mini-stat-label"),
      node("span", value, "mini-stat-value"),
    );
    grid.append(box);
  }
  container.append(grid);
}
function makeTable(columns, rows) {
  const wrap = node("div", null, "table-scroll");
  const table = node("table", null, "data-table");
  const header = node("tr");
  for (const column of columns) {
    const th = node("th", column);
    th.scope = "col";
    header.append(th);
  }
  const thead = node("thead");
  thead.append(header);
  const tbody = node("tbody");
  for (const row of rows) {
    const tr = node("tr");
    for (const value of row) tr.append(node("td", value));
    tbody.append(tr);
  }
  table.append(thead, tbody);
  wrap.append(table);
  return wrap;
}
function renderDatasetPreview() {
  const target = $("dataset-preview");
  target.replaceChildren();
  const item = currentDataset();
  if (!item) {
    empty(
      target,
      "尚无可预览的数据。请上传 CSV / XLSX 观测文件，或选择已导入的数据集。",
    );
    return;
  }
  makeStats(target, [
    ["文件名", item.name],
    ["观测行数", `${item.rows} 条`],
    ["时间范围", `${time(item.start)} — ${time(item.end)}`],
    ["采样间隔", item.cadence || "—"],
    ["序列数", `${item.series?.length || 0} 组`],
    ["必需字段", "5 列"],
  ]);
  target.append(
    node(
      "p",
      "必需字段 · timestamp / site / depth_m / temperature_c / dissolved_oxygen_mg_l",
      "field-hint",
    ),
  );
  target.append(node("p", "按站点与水深分组", "small-heading"));
  target.append(
    makeTable(
      ["站点", "水深 (m)", "观测条数"],
      (item.series || []).map((entry) => [
        entry.site,
        number(entry.depth_m, 2),
        String(entry.observations),
      ]),
    ),
  );
}
function metricCards(parent, metrics) {
  const grid = node("div", null, "metric-grid");
  for (const field of fields) {
    const box = node("div", null, "metric-card");
    box.append(
      node("p", `${field.label} · ${field.unit}`, "metric-title"),
      node("span", `MAE ${number(metrics?.mae?.[field.key])}`, "metric-value"),
      node(
        "span",
        `持久性基线 MAE ${number(metrics?.naive_mae?.[field.key])}`,
        "metric-sub",
      ),
    );
    grid.append(box);
  }
  parent.append(grid);
}
function renderModelPreview() {
  const target = $("model-preview");
  target.replaceChildren();
  const model = currentModel();
  if (!model) {
    empty(
      target,
      state.models.length
        ? "选择一个已保存模型，查看独立时间区间的验证与测试误差。"
        : "目前没有已保存模型。导入观测数据并完成训练后会出现在这里。",
    );
    return;
  }
  const summary = model.summary || {};
  target.append(node("p", `模型编号：${model.id}`, "model-id"));
  makeStats(target, [
    ["历史窗口", summary.lookback ?? "—"],
    ["预测步数", summary.horizon ?? "—"],
    ["训练轮数", summary.epochs ?? "—"],
    ["采样间隔", summary.cadence || "—"],
    ["保存时间", time(model.created_at)],
    ["模型类型", summary.model?.type || "—"],
  ]);
  for (const [key, label] of [
    ["validation", "验证集"],
    ["test", "测试集"],
  ]) {
    const section = node("div", null, "metric-section");
    section.append(node("p", `${label} · 模型与持久性基线`, "small-heading"));
    if (summary.metrics?.[key]) metricCards(section, summary.metrics[key]);
    else section.append(node("p", "未提供此区间的误差指标。", "field-hint"));
    target.append(section);
  }
}
async function loadCollections() {
  const outcomes = await Promise.allSettled([
    request("/api/datasets"),
    request("/api/models"),
  ]);
  const errors = [];
  if (outcomes[0].status === "fulfilled")
    state.datasets = outcomes[0].value.datasets || [];
  else errors.push(`观测数据列表：${outcomes[0].reason.message}`);
  if (outcomes[1].status === "fulfilled")
    state.models = outcomes[1].value.models || [];
  else errors.push(`模型列表：${outcomes[1].reason.message}`);
  if (!state.datasets.some((item) => item.id === state.selected.datasetId)) {
    state.selected.datasetId = state.datasets[0]?.id || "";
    const first = seriesList()[0];
    state.selected.site = first?.site || "";
    state.selected.depth = first ? String(first.depth_m) : "";
  }
  if (!state.models.some((item) => item.id === state.selected.modelId))
    state.selected.modelId = "";
  syncSelections();
  if (errors.length) notice(errors.join("；"), true);
  else notice("");
}
async function upload(formId, inputId, statusId) {
  const form = $(formId),
    button = form.querySelector("button[type=submit]"),
    file = $(inputId).files[0];
  if (!file) {
    status(statusId, "请先选择 CSV 或 XLSX 观测文件。", true);
    return;
  }
  button.disabled = true;
  status(statusId, "正在上传并校验观测文件…");
  try {
    const body = new FormData();
    body.append("file", file);
    const dataset = await request("/api/datasets", { method: "POST", body });
    state.datasets = [
      dataset,
      ...state.datasets.filter((item) => item.id !== dataset.id),
    ];
    choose("datasetId", dataset.id);
    syncSelections();
    status(
      statusId,
      `导入完成：${dataset.name} · ${dataset.rows} 条观测，${dataset.series?.length || 0} 组序列。`,
    );
    form.reset();
  } catch (error) {
    status(statusId, error.message, true);
  } finally {
    button.disabled = false;
  }
}
function trainingProgress(job) {
  const wrap = $("train-progress");
  wrap.hidden = false;
  const match = String(job.progress || "").match(/Epoch\s+(\d+)\s*\/\s*(\d+)/i);
  const value =
    job.status === "completed"
      ? 100
      : match
        ? Math.min(99, Math.round((Number(match[1]) / Number(match[2])) * 100))
        : 0;
  $("train-progress-fill").style.width = `${value}%`;
  $("train-progress-text").textContent =
    job.progress ||
    {
      queued: "等待开始训练…",
      running: "模型训练中…",
      completed: "训练完成，模型已保存。",
      failed: "训练未完成。",
    }[job.status] ||
    "正在获取训练状态…";
}
async function pollJob(jobId) {
  if (state.jobId !== jobId) return;
  try {
    const job = await request(`/api/train/${encodeURIComponent(jobId)}`);
    if (state.jobId !== jobId) return;
    trainingProgress(job);
    if (job.status === "completed") {
      state.jobId = "";
      $("train-button").disabled = false;
      status(
        "train-status",
        "模型训练完成并保存。验证集与测试集的 MAE 可在模型档案中查看。",
      );
      try {
        const models = await request("/api/models");
        state.models = models.models || [];
        if (job.model_id) choose("modelId", job.model_id);
        syncSelections();
      } catch (error) {
        status(
          "train-status",
          `模型已保存，但读取模型列表失败：${error.message}`,
          true,
        );
      }
      return;
    }
    if (job.status === "failed") {
      state.jobId = "";
      $("train-button").disabled = false;
      status("train-status", job.error || "训练未能完成。", true);
      return;
    }
    if (job.status !== "queued" && job.status !== "running")
      throw new Error("未知训练状态，请检查本地服务。");
    status(
      "train-status",
      job.status === "queued"
        ? "任务已加入队列，等待训练开始…"
        : "正在训练 CPU LSTM；完成后将自动载入保存的模型。",
    );
    state.pollTimer = window.setTimeout(() => pollJob(jobId), 1400);
  } catch (error) {
    state.jobId = "";
    $("train-button").disabled = false;
    status(
      "train-status",
      `读取训练状态失败：${error.message}（任务编号：${jobId}）`,
      true,
    );
  }
}
async function train(event) {
  event.preventDefault();
  if (!currentDataset()) {
    status("train-status", "请先导入并选择观测数据。", true);
    return;
  }
  if (state.jobId) return;
  const values = ["lookback", "horizon", "epochs"].map((id) =>
    Number($(id).value),
  );
  if (values.some((value) => !Number.isSafeInteger(value) || value <= 0)) {
    status(
      "train-status",
      "历史窗口、预测步数和训练轮数都必须是正整数。",
      true,
    );
    return;
  }
  $("train-button").disabled = true;
  $("train-progress").hidden = false;
  $("train-progress-fill").style.width = "0%";
  $("train-progress-text").textContent = "正在提交训练任务…";
  status("train-status", "正在提交训练任务…");
  try {
    const data = await postJSON("/api/train", {
      dataset_id: state.selected.datasetId,
      lookback: values[0],
      horizon: values[1],
      epochs: values[2],
    });
    if (!data.job_id) throw new Error("服务未返回训练任务编号。");
    state.jobId = data.job_id;
    pollJob(data.job_id);
  } catch (error) {
    $("train-button").disabled = false;
    $("train-progress").hidden = true;
    status("train-status", error.message, true);
  }
}
function bodyForSelection() {
  if (!currentDataset()) throw new Error("请先导入并选择观测数据。");
  if (!currentModel()) throw new Error("请先训练并选择已保存的模型。");
  if (!activeSeries()) throw new Error("请选择该数据集中的站点和水深。");
  return {
    dataset_id: state.selected.datasetId,
    model_id: state.selected.modelId,
    site: state.selected.site,
    depth_m: Number(state.selected.depth),
  };
}
const svgNS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}, label) {
  const el = document.createElementNS(svgNS, tag);
  for (const [key, value] of Object.entries(attrs))
    el.setAttribute(key, String(value));
  if (label != null) el.textContent = label;
  return el;
}
function chart(
  field,
  first,
  second,
  firstLabel,
  secondLabel,
  forecast = false,
) {
  const card = node("div", null, "chart-card");
  card.append(node("h3", `${field.label} / ${field.unit}`));
  const figure = svg("svg", {
    viewBox: "0 0 600 248",
    role: "img",
    "aria-label": `${field.label}时序曲线：${firstLabel}为实线，${secondLabel}为虚线；具体数值见下方数据表`,
  });
  const left = 58,
    right = 585,
    top = 19,
    bottom = 200;
  const all = [...first, ...second].filter(
    (p) => Number.isFinite(p.value) && Number.isFinite(p.x),
  );
  if (!all.length) {
    card.append(node("p", "没有可绘制的数值。", "field-hint"));
    return card;
  }
  let minimum = Infinity,
    maximum = -Infinity,
    xMin = Infinity,
    xMax = -Infinity;
  for (const point of all) {
    minimum = Math.min(minimum, point.value);
    maximum = Math.max(maximum, point.value);
    xMin = Math.min(xMin, point.x);
    xMax = Math.max(xMax, point.x);
  }
  const padding = Math.max(
    (maximum - minimum) * 0.12,
    Math.max(Math.abs(minimum), Math.abs(maximum)) * 0.01,
    0.01,
  );
  const yMin = minimum - padding,
    yMax = maximum + padding;
  const xx = (value) =>
    left +
    (xMax === xMin ? 0.5 : (value - xMin) / (xMax - xMin)) * (right - left);
  const yy = (value) =>
    bottom - ((value - yMin) / (yMax - yMin)) * (bottom - top);
  for (let i = 0; i <= 4; i++) {
    const y = top + (i * (bottom - top)) / 4;
    figure.append(
      svg("line", {
        x1: left,
        x2: right,
        y1: y,
        y2: y,
        stroke: "#e6eeeb",
        "stroke-width": 1,
      }),
    );
    figure.append(
      svg(
        "text",
        {
          x: left - 9,
          y: y + 4,
          "text-anchor": "end",
          fill: "#657d81",
          "font-size": 11,
        },
        number(yMax - (i * (yMax - yMin)) / 4, 2),
      ),
    );
  }
  for (let i = 0; i <= 3; i++) {
    const x = left + (i * (right - left)) / 3;
    const date = new Date(xMin + (i * (xMax - xMin)) / 3);
    const label = Number.isNaN(date.getTime())
      ? String(Math.round(xMin + (i * (xMax - xMin)) / 3))
      : shortTime(date.toISOString());
    figure.append(
      svg("line", {
        x1: x,
        x2: x,
        y1: bottom,
        y2: bottom + 4,
        stroke: "#9db7b4",
      }),
    );
    figure.append(
      svg(
        "text",
        {
          x,
          y: 222,
          "text-anchor": i === 0 ? "start" : i === 3 ? "end" : "middle",
          fill: "#667e83",
          "font-size": 10,
        },
        label,
      ),
    );
  }
  if (forecast && first.length && second.length) {
    const boundary = xx(first[first.length - 1].x);
    figure.append(
      svg("line", {
        x1: boundary,
        x2: boundary,
        y1: top,
        y2: bottom,
        stroke: "#bed5ce",
        "stroke-width": 1,
        "stroke-dasharray": "3 4",
      }),
    );
  }
  function draw(points, color, dashed) {
    const sampled =
      points.length > 420
        ? points.filter(
            (_, index) =>
              index === 0 ||
              index === points.length - 1 ||
              index % Math.ceil(points.length / 420) === 0,
          )
        : points;
    let path = "",
      active = false;
    for (const p of sampled) {
      if (!Number.isFinite(p.x) || !Number.isFinite(p.value)) {
        active = false;
        continue;
      }
      path += `${active ? " L" : " M"}${xx(p.x).toFixed(2)} ${yy(p.value).toFixed(2)}`;
      active = true;
    }
    figure.append(
      svg("path", {
        d: path,
        fill: "none",
        stroke: color,
        "stroke-width": 2.5,
        "stroke-linejoin": "round",
        "stroke-linecap": "round",
        ...(dashed ? { "stroke-dasharray": "6 5" } : {}),
      }),
    );
    if (points.length === 1)
      figure.append(
        svg("circle", {
          cx: xx(points[0].x),
          cy: yy(points[0].value),
          r: 3.5,
          fill: color,
        }),
      );
  }
  draw(first, "#11817d", false);
  draw(second, "#d28246", true);
  card.append(figure);
  const legend = node("div", null, "chart-legend");
  for (const [label, secondary] of [
    [firstLabel, false],
    [secondLabel, true],
  ]) {
    const key = node("span", null, "legend-key");
    key.append(
      node("span", null, secondary ? "legend-line secondary" : "legend-line"),
      node("span", label),
    );
    legend.append(key);
  }
  card.append(legend);
  return card;
}
function chartGrid(data, forecast = false) {
  const grid = node("div", null, "chart-grid");
  for (const field of fields) {
    const observed = (forecast ? data.history : data.rows).map(
      (item, index) => ({
        x: utcDate(item.timestamp).getTime(),
        value: finite(item[forecast ? field.key : field.actual]),
      }),
    );
    const modeled = (forecast ? data.forecast : data.rows).map(
      (item, index) => ({
        x: utcDate(item.timestamp).getTime(),
        value: finite(item[forecast ? field.key : field.predicted]),
      }),
    );
    if (forecast && observed.length)
      modeled.unshift(observed[observed.length - 1]);
    grid.append(
      chart(
        field,
        observed,
        modeled,
        "历史观测",
        forecast ? "未来预测" : "模型回放",
        forecast,
      ),
    );
  }
  return grid;
}
function resultTable(target, columns, rows, limit = 200) {
  const latest = rows.slice(-limit);
  target.append(makeTable(columns, latest));
  if (rows.length > latest.length)
    target.append(
      node(
        "p",
        `表格显示最近 ${latest.length} / ${rows.length} 条；完整数据可导出 CSV。`,
        "table-caption",
      ),
    );
}
function renderAnalysis(data) {
  const target = $("analysis-output");
  target.replaceChildren();
  makeStats(target, [
    ["回放窗口数", data.sample_count ?? data.rows?.length ?? "—"],
    ["预测目标步数", data.horizon ?? "—"],
    ["采样间隔", data.cadence || "—"],
    ["对照方式", "目标时刻实测 vs 模型"],
  ]);
  const metrics = node("div", null, "metric-section");
  metrics.append(node("p", "历史回放误差 · 与持久性基线比较", "small-heading"));
  metricCards(metrics, data.metrics);
  target.append(metrics);
  target.append(
    node(
      "p",
      data.disclaimer || "若回放数据与训练数据重叠，结果并非独立验证。",
      "result-note caution",
    ),
  );
  target.append(chartGrid(data));
  target.append(node("p", "目标时刻明细 · UTC 时间", "small-heading"));
  resultTable(
    target,
    [
      "目标时间 (UTC)",
      "实测水温 (°C)",
      "模型水温 (°C)",
      "实测溶解氧 (mg/L)",
      "模型溶解氧 (mg/L)",
    ],
    data.rows.map((r) => [
      time(r.timestamp),
      number(r.actual_temperature_c),
      number(r.predicted_temperature_c),
      number(r.actual_dissolved_oxygen_mg_l),
      number(r.predicted_dissolved_oxygen_mg_l),
    ]),
    24,
  );
  $("analysis-export").disabled = false;
}
function renderForecast(data) {
  const target = $("forecast-output");
  target.replaceChildren();
  makeStats(target, [
    ["历史观测", `${data.history?.length || 0} 条`],
    ["未来预测", `${data.forecast?.length || 0} 条`],
    ["预测目标步数", data.horizon ?? "—"],
    ["采样间隔", data.cadence || "—"],
  ]);
  target.append(
    node(
      "p",
      data.disclaimer || "预测结果仅供研究参考，并非实测数据。",
      "result-note",
    ),
  );
  target.append(chartGrid(data, true));
  target.append(node("p", "观测及预测明细 · UTC 时间", "small-heading"));
  const rows = [
    ...data.history.map((item) => [
      "历史观测",
      time(item.timestamp),
      number(item.temperature_c),
      number(item.dissolved_oxygen_mg_l),
    ]),
    ...data.forecast.map((item) => [
      "未来预测",
      time(item.timestamp),
      number(item.temperature_c),
      number(item.dissolved_oxygen_mg_l),
    ]),
  ];
  resultTable(
    target,
    ["类型", "时间 (UTC)", "水温 (°C)", "溶解氧 (mg/L)"],
    rows,
  );
  const params = new URLSearchParams(bodyForSelection());
  for (const [id, format] of [
    ["forecast-csv", "csv"],
    ["forecast-xlsx", "xlsx"],
  ]) {
    const link = $(id);
    link.href = `/api/forecast/export.${format}?${params}`;
    link.classList.remove("disabled-link");
    link.setAttribute("aria-disabled", "false");
  }
}
async function runAnalysis(event) {
  event.preventDefault();
  let body;
  try {
    body = bodyForSelection();
  } catch (error) {
    status("analysis-status", error.message, true);
    return;
  }
  const key = selectionKey(),
    requestId = ++state.analysisRequest;
  $("analysis-button").disabled = true;
  status("analysis-status", "正在计算逐窗口历史回放…");
  try {
    const data = await postJSON("/api/analyze", body);
    if (requestId !== state.analysisRequest || key !== selectionKey()) return;
    if (!Array.isArray(data.rows) || !data.rows.length)
      throw new Error(
        "没有可比较的历史目标时刻；请检查文件长度、模型窗口与采样间隔。",
      );
    state.analysis = data;
    state.analysisKey = key;
    renderAnalysis(data);
    status(
      "analysis-status",
      `已完成 ${data.sample_count ?? data.rows.length} 个目标时刻的历史回放。`,
    );
  } catch (error) {
    if (requestId === state.analysisRequest)
      status("analysis-status", error.message, true);
  } finally {
    $("analysis-button").disabled = false;
  }
}
async function runForecast(event) {
  event.preventDefault();
  let body;
  try {
    body = bodyForSelection();
  } catch (error) {
    status("forecast-status", error.message, true);
    return;
  }
  const key = selectionKey(),
    requestId = ++state.forecastRequest;
  $("forecast-button").disabled = true;
  status("forecast-status", "正在根据最近观测计算未来时序…");
  try {
    const data = await postJSON("/api/forecast", body);
    if (requestId !== state.forecastRequest || key !== selectionKey()) return;
    if (
      !Array.isArray(data.history) ||
      !Array.isArray(data.forecast) ||
      !data.forecast.length
    )
      throw new Error("预测响应缺少有效的时序结果。");
    state.forecast = data;
    state.forecastKey = key;
    renderForecast(data);
    status(
      "forecast-status",
      `已生成 ${data.forecast.length} 个未来时刻；可在下方下载结果。`,
    );
  } catch (error) {
    if (requestId === state.forecastRequest)
      status("forecast-status", error.message, true);
  } finally {
    $("forecast-button").disabled = false;
  }
}
function exportAnalysis() {
  if (!state.analysis || state.analysisKey !== selectionKey()) return;
  const lines = [
    [
      "timestamp",
      "actual_temperature_c",
      "predicted_temperature_c",
      "actual_dissolved_oxygen_mg_l",
      "predicted_dissolved_oxygen_mg_l",
    ],
  ];
  for (const row of state.analysis.rows)
    lines.push([
      row.timestamp,
      row.actual_temperature_c,
      row.predicted_temperature_c,
      row.actual_dissolved_oxygen_mg_l,
      row.predicted_dissolved_oxygen_mg_l,
    ]);
  const csv =
    "\ufeff" +
    lines
      .map((row) =>
        row
          .map((value) => `"${String(value ?? "").replaceAll('"', '""')}"`)
          .join(","),
      )
      .join("\r\n");
  download(
    new Blob([csv], { type: "text/csv;charset=utf-8" }),
    "historical-analysis.csv",
  );
}
function download(blob, name) {
  const url = URL.createObjectURL(blob),
    anchor = node("a");
  anchor.href = url;
  anchor.download = name;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 60000);
}
async function exportForecast(event, format) {
  event.preventDefault();
  if (!state.forecast || state.forecastKey !== selectionKey()) {
    status("forecast-status", "请先生成当前选定数据与模型的预测。", true);
    return;
  }
  const link = event.currentTarget;
  if (link.getAttribute("aria-disabled") === "true") return;
  link.setAttribute("aria-disabled", "true");
  status("forecast-status", `正在准备 ${format.toUpperCase()} 文件…`);
  try {
    const response = await fetch(link.href);
    if (!response.ok) {
      let detail;
      try {
        detail = (await response.json()).detail;
      } catch {
        /* HTTP status below remains useful. */
      }
      throw new Error(
        detailMessage(detail || `文件下载失败 (HTTP ${response.status})`),
      );
    }
    const blob = await response.blob();
    download(blob, `ocean-forecast.${format}`);
    status("forecast-status", `${format.toUpperCase()} 下载已开始。`);
  } catch (error) {
    status("forecast-status", error.message, true);
  } finally {
    link.setAttribute("aria-disabled", "false");
  }
}
function showPage(page) {
  for (const name of ["train", "analyze", "forecast"]) {
    const selected = name === page;
    $("page-" + name).hidden = !selected;
    const button = document.querySelector(`[data-page="${name}"]`);
    button.classList.toggle("active", selected);
    if (selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  document.title =
    { train: "模型训练", analyze: "数据分析", forecast: "未来预测" }[page] +
    " · 深海观测";
  window.scrollTo({ top: 0, behavior: "instant" });
}
for (const button of document.querySelectorAll(".nav-item"))
  button.addEventListener("click", () => showPage(button.dataset.page));
for (const prefix of ["train", "analysis", "forecast"]) {
  $(prefix + "-dataset").addEventListener("change", (event) =>
    choose("datasetId", event.target.value),
  );
  $(prefix + "-model").addEventListener("change", (event) =>
    choose("modelId", event.target.value),
  );
}
for (const prefix of ["analysis", "forecast"]) {
  $(prefix + "-site").addEventListener("change", (event) =>
    choose("site", event.target.value),
  );
  $(prefix + "-depth").addEventListener("change", (event) =>
    choose("depth", event.target.value),
  );
}
$("train-upload-form").addEventListener("submit", (event) => {
  event.preventDefault();
  upload("train-upload-form", "train-file", "train-upload-status");
});
$("analysis-upload-form").addEventListener("submit", (event) => {
  event.preventDefault();
  upload("analysis-upload-form", "analysis-file", "analysis-upload-status");
});
$("train-form").addEventListener("submit", train);
$("analysis-form").addEventListener("submit", runAnalysis);
$("forecast-form").addEventListener("submit", runForecast);
$("analysis-export").addEventListener("click", exportAnalysis);
$("forecast-csv").addEventListener("click", (event) =>
  exportForecast(event, "csv"),
);
$("forecast-xlsx").addEventListener("click", (event) =>
  exportForecast(event, "xlsx"),
);
loadCollections();
