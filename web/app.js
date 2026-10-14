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
  analysisRule: null,
  forecastRule: null,
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
  if (typeof detail?.message === "string") return detail.message;
  if (Array.isArray(detail))
    return detail
      .map((item) =>
        typeof item?.msg === "string" ? item.msg : JSON.stringify(item),
      )
      .join("；");
  return "请求未完成，请检查数据和服务后重试。";
}
function given(value) {
  return value === null || value === undefined || String(value).trim() === ""
    ? "未提供"
    : String(value);
}
function qualityLines(quality) {
  if (!quality || typeof quality !== "object") return [];
  const lines = [];
  if (quality.duplicate_count !== undefined && quality.duplicate_count !== null)
    lines.push(`重复记录：${quality.duplicate_count} 条`);
  if (quality.missing_count !== undefined && quality.missing_count !== null)
    lines.push(`缺测时间点：${quality.missing_count} 个`);
  if (Array.isArray(quality.missing_periods)) {
    lines.push(
      quality.missing_periods.length
        ? "缺测区间：" +
            quality.missing_periods
              .map(
                (gap) =>
                  `${gap.site ?? "站点未知"} / ${gap.depth_m ?? "水深未知"} m ${time(gap.from)} — ${time(gap.to)}（${gap.missing_steps ?? "?"} 步）`,
              )
              .join("；")
        : "缺测区间：未发现",
    );
  }
  return lines;
}
function uploadError(error) {
  const lines = qualityLines(error.quality);
  return lines.length ? `${error.message}\n${lines.join("\n")}` : error.message;
}
function formRule(form, forecast = false) {
  const rawTemp = form.elements.namedItem("max_temp_c").value.trim();
  const rawOxygen = form.elements.namedItem("min_do_mg_l").value.trim();
  const source = form.elements.namedItem("rule_source").value.trim();
  const rawAction = forecast
    ? form.elements.namedItem("action_minutes").value.trim()
    : "";
  if (!rawTemp && !rawOxygen) {
    if (rawAction)
      throw new Error("请先设置至少一个管理限值，再填写操作所需时间。");
    return null;
  }
  if (!source) throw new Error("已设置限值时必须填写限值来源。");
  const rule = { source };
  if (rawTemp) rule.max_temp_c = Number(rawTemp);
  if (rawOxygen) rule.min_do_mg_l = Number(rawOxygen);
  if (rawAction) rule.action_minutes = Number(rawAction);
  if (
    Object.entries(rule).some(
      ([key, value]) => key !== "source" && !Number.isFinite(value),
    )
  )
    throw new Error("限值和操作时间必须是有效数字。");
  if (rawAction && rule.action_minutes < 0)
    throw new Error("操作所需时间不能为负数。");
  return rule;
}
function ruleKey(form) {
  return JSON.stringify([
    form.elements.namedItem("max_temp_c").value.trim(),
    form.elements.namedItem("min_do_mg_l").value.trim(),
    form.elements.namedItem("rule_source").value.trim(),
    form.elements.namedItem("action_minutes")?.value.trim() ?? "",
  ]);
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
  if (!response.ok) {
    const error = new Error(detailMessage(data.detail || data.error));
    error.quality = data.quality || data.detail?.quality;
    throw error;
  }
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
  state.analysisRule = state.forecastRule = null;
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
  for (const id of ["dataset-preview", "analysis-dataset-preview"]) {
    const target = $(id);
    target.replaceChildren();
    const item = currentDataset();
    if (!item) {
      empty(
        target,
        "尚无可预览的数据。请上传 CSV / XLSX 观测文件，或选择已导入的数据集。",
      );
      continue;
    }
    const quality = item.quality || {};
    const provenance = item.provenance || {};
    const basis =
      {
        measured: "用户声明逐时实测",
        nominal: "名义分组深度（非逐时实测）",
        unknown: "未知（不能视为实测）",
      }[provenance.depth_basis] || "未知（不能视为实测）";
    makeStats(target, [
      ["文件名", item.name],
      ["观测行数", `${item.rows} 条`],
      ["时间范围 · UTC", `${time(item.start)} — ${time(item.end)}`],
      ["采样间隔", item.cadence || "未知"],
      ["序列数", `${item.series?.length || 0} 组`],
      ["水深依据", basis],
      ["来源名称", given(provenance.source_name)],
      ["来源引用", given(provenance.source_reference)],
      ["文件 SHA-256", given(item.sha256)],
      [
        "重复记录",
        quality.duplicate_count == null
          ? "未知"
          : `${quality.duplicate_count} 条`,
      ],
      [
        "缺测时间点",
        quality.missing_count == null ? "未知" : `${quality.missing_count} 个`,
      ],
    ]);
    target.append(
      node(
        "p",
        "时间解释：" +
          given(quality.timezone_interpretation) +
          "；来源所述时区：" +
          given(provenance.timezone_note) +
          "。来源说明不自动转换文件时间；未注明时区的时间按 UTC 解读。",
        "result-note caution",
      ),
    );
    target.append(
      node(
        "p",
        "文件值仅按 °C / mg/L 使用；溶解氧 % 饱和度不可直接作为 mg/L。" +
          (provenance.depth_basis !== "measured"
            ? " 当前水深并未证实为逐时实测。"
            : ""),
        "field-hint",
      ),
    );
    target.append(
      node("p", "来源质控说明：" + given(provenance.qc_note), "field-hint"),
    );
    const gaps = quality.missing_periods;
    target.append(
      node(
        "p",
        "缺测区间：" +
          (Array.isArray(gaps)
            ? gaps.length
              ? `${gaps.length} 段`
              : "未发现"
            : "未知（旧数据未记录）"),
        "small-heading",
      ),
    );
    if (Array.isArray(gaps) && gaps.length)
      target.append(
        makeTable(
          ["站点", "水深 (m)", "从 · UTC", "到 · UTC", "缺测步数"],
          gaps.map((gap) => [
            gap.site ?? "—",
            number(gap.depth_m, 2),
            time(gap.from),
            time(gap.to),
            gap.missing_steps ?? "—",
          ]),
        ),
      );
    const flags = quality.qc_flags;
    target.append(
      node(
        "p",
        "原始质控标记：" +
          (flags && Object.keys(flags).length
            ? "文件保留值及频次"
            : quality.missing_count == null
              ? "未知（旧数据未记录）"
              : "文件未提供"),
        "small-heading",
      ),
    );
    if (flags && Object.keys(flags).length)
      target.append(
        makeTable(
          ["原始标记列", "值与频次"],
          Object.entries(flags).map(([column, values]) => [
            column,
            Object.entries(values || {})
              .map(([value, count]) => `${value}：${count}`)
              .join("；") || "—",
          ]),
        ),
      );
    if (Array.isArray(quality.warnings))
      for (const warning of quality.warnings)
        target.append(node("p", warning, "result-note caution"));
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
function baselineCaution(metrics, context) {
  const weaker = fields
    .filter(({ key }) => {
      const value = metrics?.mae?.[key];
      const baseline = metrics?.naive_mae?.[key];
      return (
        typeof value === "number" &&
        Number.isFinite(value) &&
        typeof baseline === "number" &&
        Number.isFinite(baseline) &&
        value >= baseline
      );
    })
    .map(({ label }) => label);
  return weaker.length
    ? node(
        "p",
        `${context}的${weaker.join("、")} MAE 未优于末值基线；不能仅凭这次模型输出认定预警可靠。`,
        "result-note caution",
      )
    : null;
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
    [
      "切分策略",
      summary.split_mode === "year_holdout"
        ? "完整日历年独立留出"
        : summary.split_mode === "chronological"
          ? "按时间顺序留出"
          : "未知（旧模型）",
    ],
    ["训练来源 SHA-256", given(summary.training_source_sha256)],
    ["模型类型", summary.model?.type || "—"],
  ]);
  if (Array.isArray(summary.sites)) {
    for (const site of summary.sites) {
      const periods = site.split_periods;
      if (!periods) continue;
      target.append(
        node(
          "p",
          `站点 ${given(site.site)} / ${number(site.depth_m, 2)} m · 各时间区间`,
          "small-heading",
        ),
      );
      target.append(
        makeTable(
          ["区间", "开始 · UTC", "结束 · UTC", "涵盖年份"],
          [
            ["训练", periods.train],
            ["验证", periods.validation],
            ["测试", periods.test],
          ].map(([label, period]) => [
            label,
            time(period?.start),
            time(period?.end),
            Array.isArray(period?.years)
              ? period.years.join("、") || "未知"
              : "未知",
          ]),
        ),
      );
    }
  }
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
  const caution = baselineCaution(summary.metrics?.test, "留出测试段");
  if (caution) target.append(caution);
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
    const provenance = {
      depth_basis: form.elements.namedItem("depth_basis").value,
      temperature_unit: "°C",
      oxygen_unit: "mg/L",
    };
    for (const key of [
      "source_name",
      "source_reference",
      "timezone_note",
      "qc_note",
    ])
      provenance[key] = form.elements.namedItem(key).value.trim();
    body.append("provenance", JSON.stringify(provenance));
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
    status(statusId, uploadError(error), true);
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
      split_mode: $("split-mode").value,
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
function ruleDescription(rule) {
  return `限值来源：${rule.source}；${rule.max_temp_c === undefined ? "水温未配置" : `水温高于 ${rule.max_temp_c} °C`}；${rule.min_do_mg_l === undefined ? "溶解氧未配置" : `溶解氧低于 ${rule.min_do_mg_l} mg/L`}。`;
}
function evaluation(parent, result, rule) {
  parent.append(node("p", "按用户限值评价事件 · 目标时刻", "small-heading"));
  if (!rule) {
    parent.append(
      node(
        "p",
        "未配置事件定义；无法评价事件命中、漏报或持续时间。",
        "result-note caution",
      ),
    );
    return;
  }
  parent.append(node("p", ruleDescription(rule), "result-note"));
  if (!result?.by_variable) {
    parent.append(
      node("p", "事件评价不可用；不能推断未发生事件。", "result-note caution"),
    );
    return;
  }
  const titles = {
    temperature: "水温",
    oxygen: "溶解氧",
    combined: "任一限值",
  };
  const rows = Object.entries(result.by_variable).map(([key, value]) => [
    titles[key] || key,
    value.tp ?? "—",
    value.fp ?? "—",
    value.fn ?? "—",
    value.actual_event_episodes ?? "—",
    value.predicted_event_episodes ?? "—",
    value.missed_event_episodes ?? "—",
    value.false_alarm_episodes ?? "—",
    number(value.actual_duration_minutes, 1),
    number(value.predicted_duration_minutes, 1),
    number(value.duration_bias_minutes, 1),
    value.precision == null
      ? "无法计算"
      : `${number(value.precision * 100, 1)}%`,
    value.recall == null ? "无法计算" : `${number(value.recall * 100, 1)}%`,
  ]);
  if (rows.length)
    parent.append(
      makeTable(
        [
          "变量",
          "命中步",
          "误报步",
          "漏报步",
          "实测事件段",
          "预测事件段",
          "漏报事件段",
          "误报事件段",
          "实测超限分钟",
          "预测超限分钟",
          "时长偏差分钟",
          "精确率",
          "召回率",
        ],
        rows,
      ),
    );
  else
    parent.append(
      node(
        "p",
        "已配置限值的事件结果未返回，无法计算。",
        "result-note caution",
      ),
    );
  for (const warning of result.warnings || [])
    parent.append(node("p", warning, "result-note caution"));
}
function leadDisplay(parent, lead, rule) {
  parent.append(node("p", "用户限值 · 操作提前量", "small-heading"));
  if (!rule) {
    parent.append(
      node("p", "未设管理限值，无法给出提前量。", "result-note caution"),
    );
    return;
  }
  parent.append(node("p", ruleDescription(rule), "result-note"));
  if (!lead) {
    parent.append(
      node("p", "限值提前量不可用，不能据此判断安全。", "result-note caution"),
    );
    return;
  }
  if (lead.status === "crossing") {
    makeStats(parent, [
      ["首次预测越限 · UTC", time(lead.first_crossing_timestamp)],
      ["相对末次观测的提前量", `${number(lead.minutes_until, 1)} 分钟`],
      [
        "预测生成时可用时间",
        lead.expired
          ? "已过期"
          : `${number(lead.remaining_minutes_at_generation, 1)} 分钟`,
      ],
      [
        "触发变量",
        (lead.triggered_variables || [])
          .map(
            (key) =>
              ({ temperature_c: "水温", dissolved_oxygen_mg_l: "溶氧" })[key] ||
              key,
          )
          .join("、") || "未知",
      ],
      [
        "执行所需时间",
        lead.action_minutes == null
          ? "未提供"
          : `${number(lead.action_minutes, 1)} 分钟`,
      ],
      [
        "执行时间比较",
        lead.expired
          ? "历史预测已过期，不能用于当前行动"
          : lead.action_feasible == null
            ? "未提供所需时间，无法比较"
            : lead.action_feasible
              ? "生成时剩余时间不少于执行所需时间"
              : "生成时剩余时间短于执行所需时间",
      ],
    ]);
    if (lead.expired)
      parent.append(
        node(
          "p",
          "这份观测的预测时间已经过去；仅用于流程演示，不能作实时预警。",
          "result-note caution",
        ),
      );
  } else if (lead.status === "no_crossing")
    parent.append(
      node(
        "p",
        "在本次短时预测范围内未发现所设限值的越限；不表示未来安全或无风险。",
        "result-note caution",
      ),
    );
  else
    parent.append(
      node("p", "提前量状态未知，不能据此判断安全。", "result-note caution"),
    );
  if (lead.disclaimer) parent.append(node("p", lead.disclaimer, "field-hint"));
}
function datasetSource(parent) {
  const source = currentDataset()?.provenance;
  parent.append(
    node(
      "p",
      `数据来源：${given(source?.source_name)}；来源引用：${given(source?.source_reference)}；水深依据：${{ measured: "用户声明逐时实测", nominal: "名义分组深度", unknown: "未知" }[source?.depth_basis] || "未知"}。原文件时区说明：${given(source?.timezone_note)}（填写说明不自动转换时间）。`,
      "field-hint",
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
  datasetSource(target);
  const metrics = node("div", null, "metric-section");
  metrics.append(node("p", "历史回放误差 · 与持久性基线比较", "small-heading"));
  metricCards(metrics, data.metrics);
  target.append(metrics);
  const caution = baselineCaution(data.metrics, "当前历史回放");
  if (caution) target.append(caution);
  const validation = data.validation_context;
  const validity =
    {
      independent_time: "独立时间段",
      overlap: "与模型来源重叠，非独立验证",
      unknown: "独立性未知，不可当作独立验证",
    }[validation?.status] || "独立性未知，不可当作独立验证";
  target.append(
    node(
      "p",
      `回放验证关系：${validity}。${validation?.message || "模型来源或时间范围信息不足。"}`,
      "result-note caution",
    ),
  );
  target.append(
    node(
      "p",
      data.disclaimer || "若回放数据与训练数据重叠，结果并非独立验证。",
      "result-note caution",
    ),
  );
  evaluation(target, data.event_evaluation, state.analysisRule);
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
  datasetSource(target);
  target.append(
    node(
      "p",
      data.disclaimer || "预测结果仅供研究参考，并非实测数据。",
      "result-note",
    ),
  );
  leadDisplay(target, data.lead_time, state.forecastRule);
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
  const xlsxParams = new URLSearchParams(params);
  if (state.forecastRule)
    xlsxParams.set("rule", JSON.stringify(state.forecastRule));
  for (const [id, format] of [
    ["forecast-csv", "csv"],
    ["forecast-xlsx", "xlsx"],
  ]) {
    const link = $(id);
    link.href = `/api/forecast/export.${format}?${format === "xlsx" ? xlsxParams : params}`;
    link.classList.remove("disabled-link");
    link.setAttribute("aria-disabled", "false");
  }
}
async function runAnalysis(event) {
  event.preventDefault();
  let body;
  try {
    body = bodyForSelection();
    body.rule = formRule($("analysis-form"));
  } catch (error) {
    status("analysis-status", error.message, true);
    return;
  }
  const key = selectionKey() + ruleKey($("analysis-form")),
    requestId = ++state.analysisRequest;
  $("analysis-button").disabled = true;
  status("analysis-status", "正在计算逐窗口历史回放…");
  try {
    const data = await postJSON("/api/analyze", body);
    if (
      requestId !== state.analysisRequest ||
      key !== selectionKey() + ruleKey($("analysis-form"))
    )
      return;
    if (!Array.isArray(data.rows) || !data.rows.length)
      throw new Error(
        "没有可比较的历史目标时刻；请检查文件长度、模型窗口与采样间隔。",
      );
    state.analysis = data;
    state.analysisKey = key;
    state.analysisRule = body.rule;
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
    body.rule = formRule($("forecast-form"), true);
  } catch (error) {
    status("forecast-status", error.message, true);
    return;
  }
  const key = selectionKey() + ruleKey($("forecast-form")),
    requestId = ++state.forecastRequest;
  $("forecast-button").disabled = true;
  status("forecast-status", "正在根据最近观测计算未来时序…");
  try {
    const data = await postJSON("/api/forecast", body);
    if (
      requestId !== state.forecastRequest ||
      key !== selectionKey() + ruleKey($("forecast-form"))
    )
      return;
    if (
      !Array.isArray(data.history) ||
      !Array.isArray(data.forecast) ||
      !data.forecast.length
    )
      throw new Error("预测响应缺少有效的时序结果。");
    state.forecast = data;
    state.forecastKey = key;
    state.forecastRule = body.rule;
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
  if (
    !state.analysis ||
    state.analysisKey !== selectionKey() + ruleKey($("analysis-form"))
  )
    return;
  const lines = [
    [
      "timestamp",
      "actual_temperature_c",
      "predicted_temperature_c",
      "actual_dissolved_oxygen_mg_l",
      "predicted_dissolved_oxygen_mg_l",
      "validation_status",
      "validation_message",
      "max_temp_c",
      "min_do_mg_l",
      "rule_source",
      "dataset_source_name",
      "dataset_source_reference",
    ],
  ];
  for (const row of state.analysis.rows)
    lines.push([
      row.timestamp,
      row.actual_temperature_c,
      row.predicted_temperature_c,
      row.actual_dissolved_oxygen_mg_l,
      row.predicted_dissolved_oxygen_mg_l,
      state.analysis.validation_context?.status ?? "unknown",
      state.analysis.validation_context?.message ?? "",
      state.analysisRule?.max_temp_c ?? "",
      state.analysisRule?.min_do_mg_l ?? "",
      state.analysisRule?.source ?? "",
      currentDataset()?.provenance?.source_name ?? "",
      currentDataset()?.provenance?.source_reference ?? "",
    ]);
  const csv =
    "\ufeff" +
    lines
      .map((row) =>
        row
          .map((value) => {
            const raw =
              typeof value === "string" &&
              /^[\s\u0000-\u001f]*[=+\-@]/.test(value)
                ? "'" + value
                : String(value ?? "");
            return `"${raw.replaceAll('"', '""')}"`;
          })
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
  if (
    !state.forecast ||
    state.forecastKey !== selectionKey() + ruleKey($("forecast-form"))
  ) {
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
    if (state.forecastKey !== selectionKey() + ruleKey($("forecast-form")))
      return;
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
for (const id of ["train-file", "analysis-file"])
  $(id).addEventListener("change", clearResults);
for (const id of ["analysis-form", "forecast-form"]) {
  for (const name of [
    "max_temp_c",
    "min_do_mg_l",
    "rule_source",
    "action_minutes",
  ]) {
    const input = $(id).elements.namedItem(name);
    if (input) input.addEventListener("input", clearResults);
  }
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
