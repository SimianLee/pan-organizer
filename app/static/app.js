// pan-organizer-web 前端逻辑
// 单页应用，无构建工具，原生 JS + Tailwind CDN

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

// 当前 /api/rules 里真实存在的规则 id（白名单）。
// 恢复旧 state.json / config.last_job 时，不在清单里的历史规则 id
// （如已移除的 rename_on_conflict 伪规则）会被过滤掉，避免幽灵勾选。
const RULE_IDS = [
  "extsort", "category", "by_date", "by_size",
  "skip_incomplete",
  "regex_match", "dedupe", "cleanup_empty", "cron",
];

// 尚未实现、/api/rules 里标了 planned 的规则：可在界面上展示，但不会被提交执行。
const PLANNED_RULE_IDS = ["dedupe", "cron"];

// ---- 状态 ----
const state = {
  config: null,            // 当前配置
  srcPath: "",
  dstPath: "",
  selectedRules: new Set(["extsort", "skip_incomplete"]),
  status: "idle",
  sse: null,
  lastPlan: null,          // 上次任务的计划文件名（页面恢复时预选下拉用）
};

// ---- 通用工具 ----
// HTML 转义：网盘里的目录名/文件名可能含 " < > & 等字符，直接拼进 innerHTML
// 会破坏页面结构（甚至被注入脚本）。凡是要塞进模板字符串的用户数据都先过这里。
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// 统一请求封装：非 2xx 时把后端返回的 {error|msg} 提取出来抛错。
// 旧实现只抛 `HTTP 400`，导致"请选择要执行的计划"这类真正有用的提示被吞掉，
// 用户只能看到一个状态码。
async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const ctype = r.headers.get("Content-Type") || "";
  let body = null;
  if (ctype.indexOf("application/json") >= 0) {
    try { body = await r.json(); } catch (e) { body = null; }
  }
  if (!r.ok) {
    // 兼容三种后端错误体：{"error":...} / {"msg":...} / {"output":...}
    const msg = (body && (body.error || body.msg || body.output))
      || `HTTP ${r.status}`;
    const err = new Error(msg);
    err.status = r.status;
    err.body = body;
    throw err;
  }
  return body === null ? {} : body;
}

// ---- 标签页切换 ----
$$(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    $$(".tab-btn").forEach(b => b.classList.remove("tab-active"));
    btn.classList.add("tab-active");
    const tab = btn.dataset.tab;
    $$("[data-pane]").forEach(p => {
      p.classList.toggle("hidden", p.dataset.pane !== tab);
    });
    // 进入"路径"标签时加载挂载点
    if (tab === "paths") loadMounts();
    if (tab === "rules") loadRules();
    if (tab === "run") { loadHistory(); refreshPlans(); }
  });
});
// 默认激活第一个
$(".tab-btn").classList.add("tab-active");

// 刷新计划下拉：尽量保留当前已选中的计划
function refreshPlans() {
  const sel = $("#planSelect");
  loadPlans(sel && sel.value ? sel.value : null);
}

// ---- 配置：加载/保存/测试 ----
// 页面打开/刷新时一次性从 /api/state 把所有恢复需要的数据都拿回来：
//   - config  : alist 连接配置
//   - state   : 最近任务的完整快照（src/dst/rules/skip_ext/on_conflict/started_at/...）
//   - last_run: 上次运行的 UI 摘要（含进度条百分比）
async function loadConfig() {
  try {
    const resp = await api("/api/state");
    const cfg = (resp.config || {});
    state.config = cfg;
    // alist 连接配置回填
    const al = cfg.alist || {};
    $("#f_base_url").value = al.base_url || "";
    $("#f_username").value = al.username || "";
    $("#f_timeout").value = al.timeout || 30;
    if (al.password_set) {
      $("#pwdSetBadge").classList.remove("hidden");
      $("#f_password").placeholder = "留空保持原密码";
    }
    // on_conflict 单选：支持 rename/skip/overwrite 三态，默认 rename（撞名自动编号，绝不覆盖）。
    // 旧 config.json 里其它合法值也能正确回填到对应 radio。
    const opt = cfg.options || {};
    const v = opt.on_conflict || "rename";
    const radio = $(`input[name="on_conflict"][value="${v}"]`);
    if (radio) radio.checked = true;

    // —— 任务参数回填 ——
    // state.json（最新源，结构化）覆盖 cfg.last_job（旧兼容字段）
    const st = resp.state || {};
    const lj = st.src ? st : (cfg.last_job || {});
    if (lj.src) {
      state.srcPath = lj.src;
      $("#srcPath").value = lj.src;
    }
    if (lj.dst) {
      state.dstPath = lj.dst;
      $("#dstPath").value = lj.dst;
    }
    if (Array.isArray(lj.rules) && lj.rules.length) {
      // 只恢复"当前仍存在且确实可执行"的规则：
      // 过滤掉历史遗留/已移除的 id（如 rename_on_conflict）以及"规划中"的规则
      state.selectedRules = new Set(
        lj.rules.filter(
          id => RULE_IDS.includes(id) && !PLANNED_RULE_IDS.includes(id)));
    }
    // 记录上次任务用到的计划文件（供"按计划移动"下拉预选）
    state.lastPlan = lj.plan || null;
    if (lj.skip_ext) {
      $("#skipExt").value = lj.skip_ext;
    }
    if (cfg.options && cfg.options.regex_pattern) {
      const rp = $("#regexPattern");
      if (rp) rp.value = cfg.options.regex_pattern;
    }

    // —— 上次运行状态恢复（idle 时直接渲染，已结束也展示）——
    if (resp.last_run && resp.last_run.log) {
      await restoreFromLastRun(resp.last_run, (resp.status || {}).status);
    }
  } catch (e) {
    $("#cfgMsg").textContent = "加载配置失败: " + e.message;
    $("#cfgMsg").className = "text-sm text-red-600";
  }
}

$("#cfgForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    alist: {
      base_url: $("#f_base_url").value.trim(),
      username: $("#f_username").value.trim(),
      password: $("#f_password").value,  // 留空表示保持原密码
      timeout: parseInt($("#f_timeout").value, 10) || 30,
    },
  };
  try {
    const r = await api("/api/config", {
      method: "POST", body: JSON.stringify(body),
    });
    $("#cfgMsg").textContent = "✓ 已保存";
    $("#cfgMsg").className = "text-sm text-green-600";
    $("#pwdSetBadge").classList.remove("hidden");
    setTimeout(() => $("#cfgMsg").textContent = "", 3000);
  } catch (err) {
    $("#cfgMsg").textContent = "保存失败: " + err.message;
    $("#cfgMsg").className = "text-sm text-red-600";
  }
});

$("#btnTest").addEventListener("click", async () => {
  $("#btnTest").disabled = true;
  $("#btnTest").textContent = "测试中…";
  $("#testOut").classList.remove("hidden");
  $("#testOutPre").textContent = "连接中…";
  try {
    // 先保存当前表单
    await api("/api/config", {
      method: "POST",
      body: JSON.stringify({
        alist: {
          base_url: $("#f_base_url").value.trim(),
          username: $("#f_username").value.trim(),
          password: $("#f_password").value,
          timeout: parseInt($("#f_timeout").value, 10) || 30,
        },
      }),
    });
    const r = await api("/api/test", { method: "POST" });
    $("#testOutPre").textContent = r.output || "(无输出)";
    $("#testOutPre").className = "log bg-slate-900 text-slate-100 p-3 " +
      "rounded-md max-h-80 overflow-auto scroll-thin " +
      (r.ok ? "" : "border-2 border-red-500");
  } catch (err) {
    $("#testOutPre").textContent = "测试失败: " + err.message;
  } finally {
    $("#btnTest").disabled = false;
    $("#btnTest").textContent = "测试连接";
  }
});

// ---- 挂载点 / 路径树 ----
async function loadMounts() {
  // 加载根目录挂载点
  const srcTree = $("#srcTree");
  const dstTree = $("#dstTree");
  try {
    const r = await api("/api/mounts");
    if (!r.ok || !r.mounts || r.mounts.length === 0) {
      srcTree.innerHTML = '<p class="text-red-500 text-center py-8">' +
        '连接失败，请检查【① 连接】配置</p>';
      return;
    }
    // 把挂载点渲染为可展开节点（名称统一转义，避免目录名里的特殊字符破版）
    const html = r.mounts.map(m => `
      <div class="tree-mount" data-path="${esc(m)}">
        <div class="tree-item flex items-center" data-path="${esc(m)}">
          <span class="mr-1 cursor-wait toggle">▸</span>
          <span class="mr-1">📁</span>
          <span class="font-mono">${esc(m)}</span>
        </div>
        <div class="ml-5 children hidden"></div>
      </div>
    `).join("");
    srcTree.innerHTML = html;
    dstTree.innerHTML = html;
    bindTreeEvents(srcTree, "src");
    bindTreeEvents(dstTree, "dst");
  } catch (err) {
    srcTree.innerHTML = '<p class="text-red-500 text-center py-8">' +
      '加载失败: ' + err.message + '</p>';
  }
}

function bindTreeEvents(root, which) {
  root.addEventListener("click", async (e) => {
    const item = e.target.closest(".tree-item");
    if (!item) return;
    const mount = item.closest(".tree-mount");
    const path = item.dataset.path;
    const children = mount.querySelector(".children");
    const toggle = item.querySelector(".toggle");

    // 选中
    $$(`#${which}Tree .tree-item.selected`).forEach(el =>
      el.classList.remove("selected"));
    item.classList.add("selected");

    // 设置输入框
    if (which === "src") {
      state.srcPath = path;
      $("#srcPath").value = path;
    } else {
      state.dstPath = path;
      $("#dstPath").value = path;
    }

    // 展开/收起
    if (e.target.classList.contains("toggle") ||
        e.target === item.firstElementChild) {
      if (children.classList.contains("hidden")) {
        // 展开
        toggle.textContent = "▾";
        children.classList.remove("hidden");
        if (!children.dataset.loaded) {
          children.innerHTML = '<p class="text-slate-400 py-1">加载中…</p>';
          try {
            const r = await api(`/api/tree?path=${encodeURIComponent(path)}`);
            if (r.subdirs && r.subdirs.length > 0) {
              children.innerHTML = r.subdirs.map(sub => `
                <div class="tree-mount" data-path="${path}/${sub}">
                  <div class="tree-item flex items-center"
                       data-path="${path}/${sub}">
                    <span class="mr-1 toggle">▸</span>
                    <span class="mr-1">📁</span>
                    <span class="font-mono">${sub}</span>
                  </div>
                  <div class="ml-5 children hidden"></div>
                </div>
              `).join("");
              children.dataset.loaded = "1";
            } else {
              children.innerHTML = '<p class="text-slate-400 py-1 text-xs">' +
                '空目录</p>';
            }
          } catch (err) {
            children.innerHTML = '<p class="text-red-500 py-1 text-xs">' +
              '加载失败: ' + err.message + '</p>';
          }
        }
      } else {
        toggle.textContent = "▸";
        children.classList.add("hidden");
      }
    }
  });
}

// 浏览按钮：滚动到对应输入框位置 + 打开路径标签
$("#btnSrcBrowse").addEventListener("click", () => {
  $('[data-tab="paths"]').click();
  setTimeout(() => $("#srcPath").focus(), 100);
});
$("#btnDstBrowse").addEventListener("click", () => {
  $('[data-tab="paths"]').click();
  setTimeout(() => $("#dstPath").focus(), 100);
});
$("#btnDstClear").addEventListener("click", () => {
  state.dstPath = "";
  $("#dstPath").value = "";
});

// 输入框回车或失焦同步到 state
$("#srcPath").addEventListener("change", (e) => { state.srcPath = e.target.value; });
$("#dstPath").addEventListener("change", (e) => { state.dstPath = e.target.value; });

// ---- 规则 ----
async function loadRules() {
  try {
    const r = await api("/api/rules");
    const html = r.rules.map(rule => `
      <label class="flex items-start gap-3 p-3 border border-slate-200
                    rounded-md hover:bg-slate-50 cursor-pointer
                    ${rule.planned ? "opacity-60" : ""}">
        <input type="checkbox" data-rule="${rule.id}"
               ${state.selectedRules.has(rule.id) ? "checked" : ""}
               ${rule.planned ? "disabled" : ""}
               class="mt-1" />
        <div class="flex-1">
          <div class="font-medium text-slate-800">
            ${rule.name}
            ${rule.planned ? `<span class="ml-2 text-xs px-1.5 py-0.5
                              bg-amber-100 text-amber-700 rounded">${rule.planned}</span>` : ""}
          </div>
          <div class="text-xs text-slate-500 mt-0.5">${rule.description}</div>
        </div>
      </label>
    `).join("");
    $("#rulesList").innerHTML = html;
    $$("input[data-rule]").forEach(cb => {
      cb.addEventListener("change", (e) => {
        const id = e.target.dataset.rule;
        if (e.target.checked) state.selectedRules.add(id);
        else state.selectedRules.delete(id);
      });
    });
  } catch (err) {
    $("#rulesList").innerHTML = '<p class="text-red-500">加载失败</p>';
  }
}

// ---- 执行：查询(预览) / 查询并移动 / 按计划移动 ----
const RUN_BTN_IDS = ["btnQuery", "btnQueryMove", "btnPlanMove"];

// 统一同步三个执行按钮 + 停止按钮的可用性。
// running=true 时全禁；空闲时按计划移动还要求下拉里已有可选计划。
function syncRunButtons(running = false) {
  RUN_BTN_IDS.forEach(id => { $("#" + id).disabled = running; });
  $("#btnStop").disabled = !running;
  if (!running) {
    const sel = $("#planSelect");
    if (sel) $("#btnPlanMove").disabled = !sel.value;
  }
}

// 组装本次任务的公共参数（撞名策略 / 跳过后缀 / 正则筛选 / 勾选规则）
function collectRunParams() {
  const rp = document.getElementById("regexPattern");
  return {
    src: state.srcPath || $("#srcPath").value.trim(),
    dst: state.dstPath || $("#dstPath").value.trim(),
    // 撞名策略：三态单选（rename/skip/overwrite）。理论上总有一个选中，
    // 这里仍兜底默认 rename，避免极端情况下取 null.value 抛错。
    on_conflict: ($('input[name="on_conflict"]:checked') || {}).value || "rename",
    skip_ext: $("#skipExt").value.trim(),
    regex_pattern: rp ? rp.value.trim() : "",
    // 提交前剔除"规划中"的规则（它们在界面上不可勾选，但可能残留在旧 state 里）
    rules: [...state.selectedRules].filter(id => !PLANNED_RULE_IDS.includes(id)),
  };
}

async function doRun(mode) {
  const p = collectRunParams();
  const body = { ...p, mode };
  let label;
  if (mode === "move") {
    // 模式3：按选中的历史计划移动（不再扫描）
    const plan = $("#planSelect").value;
    if (!plan) {
      alert("请先选择一份查询计划。\n没有可选计划？先点「查询(预览)」生成一份。");
      return;
    }
    body.plan = plan;
    label = `按计划移动 ${plan}`;
  } else {
    // 模式1/2：查询需要源目录
    if (mode !== "query" && mode !== "query_move") return;
    if (!p.src) {
      alert("请先在【② 路径】选择源目录");
      return;
    }
    // 一条规则都不勾时，引擎会退化成"按后缀归档"，与用户预期不符 → 直接拦下
    if (!p.rules.length) {
      alert("请先在【③ 规则】至少勾选一条整理规则（例如「按后缀归档」）。");
      return;
    }
    label = (mode === "query" ? "查询(预览)" : "查询并移动") +
      ` ${p.src}` + (p.dst && p.dst !== p.src ? ` → ${p.dst}` : "");
  }

  // 「覆盖」是破坏性策略：真正要动文件前再确认一次。
  // （程序内部走"备份式覆盖"，失败会自动回滚、不会丢文件；但被替换掉的那份内容找不回来）
  if (mode !== "query" && p.on_conflict === "overwrite") {
    if (!confirm(
      "⚠ 撞名策略已选择「覆盖」\n\n" +
      "移动时若目标位置已有同名文件，将用源文件替换它。\n" +
      "程序会先把原文件备份让位再移入，中途失败会自动回滚（不丢文件）；\n" +
      "但被替换掉的那份内容无法找回。\n\n" +
      "确认继续执行？")) return;
  }

  // —— UI 准备 ——
  syncRunButtons(true);
  $("#logBox").textContent = "";
  // 重置进度条（若页面是从"上次运行"恢复的，清掉旧的百分比 / 颜色 / indet）
  $("#jobProg").classList.remove("hidden");
  const bar = $("#progBar");
  bar.classList.remove("indet", "prog-amber");  // ★ 清掉历史终态的颜色残留
  bar.classList.add("indet");                  // 启动即扫描阶段 → 流动条
  bar.style.width = "";
  $("#progPct").textContent = "启动中";
  $("#progLabel").textContent = "准备中…";
  $("#runInfo").classList.remove("hidden");
  $("#runLabel").textContent = label;
  $("#runLogFile").textContent = "—";
  $("#runStarted").textContent = new Date().toLocaleTimeString();
  $("#runStatus").textContent = "启动中…";
  $("#runStatus").className = "font-medium text-blue-600";

  try {
    const r = await api("/api/run", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      // 兼容后端万一返回 200 但 ok=false 的情况
      alert("启动失败: " + (r.error || r.msg || "未知原因"));
      syncRunButtons(false);
      $("#runStatus").textContent = "启动失败";
      $("#runStatus").className = "font-medium text-red-600";
      return;
    }
    syncRunButtons(true);
    $("#runStatus").textContent = "运行中";
    $("#runStatus").className = "font-medium text-green-600 pulse";
    connectSSE();
    pollStatus();
  } catch (err) {
    // err.message 已经是后端给出的可读原因（见 api() 里对非 2xx 的处理）
    alert("启动失败: " + err.message);
    syncRunButtons(false);
    $("#runStatus").textContent = "启动失败";
    $("#runStatus").className = "font-medium text-red-600";
  }
}

$("#btnQuery").addEventListener("click", () => doRun("query"));
$("#btnQueryMove").addEventListener("click", () => doRun("query_move"));
$("#btnPlanMove").addEventListener("click", () => doRun("move"));

// 下拉选择变化 → 刷新"按计划移动"可用性
$("#planSelect").addEventListener("change", () => syncRunButtons(false));

// 加载历史计划到下拉框：
// - 默认有一个 disabled placeholder 强制用户主动选择（避免误点"按计划移动"用了错的计划）
// - preferName 存在时优先选中它（页面恢复上次任务）
async function loadPlans(preferName = null) {
  const sel = $("#planSelect");
  if (!sel) return;
  try {
    const r = await api("/api/plans");
    const placeholder = '<option value="" disabled selected>'
      + '（请选择计划 · 或先点「查询(预览)」生成）</option>';
    if (!r.plans || r.plans.length === 0) {
      sel.innerHTML = placeholder;
      sel.disabled = true;
    } else {
      sel.disabled = false;
      sel.innerHTML = placeholder + r.plans.map(p => {
        const meta = p.meta || {};
        const n = meta.count != null ? ` · ${meta.count} 个` : "";
        const src = meta.src ? ` · ${meta.src}` : "";
        return `<option value="${p.name}">${p.name}${n}${src}</option>`;
      }).join("");
      // 仅当 preferName 真的在列表里时才预选（页面恢复场景）
      if (preferName && [...sel.options].some(o => o.value === preferName)) {
        sel.value = preferName;
      } else {
        sel.value = "";   // ★ 强制默认不选择（覆盖浏览器默认选第一项的行为）
      }
    }
    syncRunButtons(false);
    renderPlansList(r.plans || []);   // 同步刷新折叠区列表
  } catch (err) {
    sel.innerHTML = '<option value="">（计划列表加载失败）</option>';
    sel.disabled = true;
  }
}

// 把后端返回的计划列表渲染到"📋 计划管理"折叠区：
// 显示 meta（源目录 / 文件数 / 生成时间）+ 查看/下载/删除/应用四个动作
async function renderPlansList(plans) {
  const box = $("#plansList");
  const cnt = $("#plansCount");
  if (!box) return;
  if (!plans.length) {
    cnt.textContent = "（暂无）";
    box.innerHTML = '<p class="text-slate-400 text-center py-4">'
      + '暂无计划 · 点「查询(预览)」生成第一份</p>';
    return;
  }
  cnt.textContent = `（${plans.length} 份）`;
  box.innerHTML = plans.map(p => {
    const m = p.meta || {};
    const src = m.src || "";
    const count = m.count != null ? `${m.count} 个` : "";
    const ts = m.created_at
      ? new Date(m.created_at * 1000).toLocaleString() : "";
    return `
      <div class="border border-slate-200 rounded p-2 hover:bg-slate-50"
           data-plan-row="${esc(p.name)}">
        <div class="flex items-start justify-between gap-2">
          <div class="flex-1 min-w-0">
            <div class="font-mono text-sm text-slate-800 truncate"
                 title="${esc(p.name)}">${esc(p.name)}</div>
            <div class="text-xs text-slate-500 mt-0.5">
              ${src ? `源 <span class="font-mono">${esc(src)}</span>` : ""}
              ${count ? ` · ${count}` : ""}
              ${ts ? ` · ${ts}` : ""}
            </div>
          </div>
          <div class="flex flex-wrap gap-1.5 shrink-0">
            <button class="px-2 py-0.5 text-xs bg-blue-100 hover:bg-blue-200
                           text-blue-700 rounded"
                    data-action="apply" data-name="${esc(p.name)}"
                    title="把这份计划填到上方下拉框（需手动点「按计划移动」才执行）">
              应用
            </button>
            <button class="px-2 py-0.5 text-xs bg-slate-100 hover:bg-slate-200
                           text-slate-700 rounded"
                    data-action="view" data-name="${esc(p.name)}">
              查看
            </button>
            <button class="px-2 py-0.5 text-xs bg-slate-100 hover:bg-slate-200
                           text-slate-700 rounded"
                    data-action="download" data-name="${esc(p.name)}">
              下载
            </button>
            <button class="px-2 py-0.5 text-xs bg-red-100 hover:bg-red-200
                           text-red-700 rounded"
                    data-action="delete" data-name="${esc(p.name)}">
              删除
            </button>
          </div>
        </div>
      </div>
    `;
  }).join("");

  // 事件代理：四个按钮
  box.querySelectorAll("button[data-action]").forEach(btn => {
    btn.addEventListener("click", () => handlePlanAction(
      btn.dataset.action, btn.dataset.name));
  });
}

// 计划行按钮的统一处理
async function handlePlanAction(action, name) {
  const sel = $("#planSelect");
  const planUrl = `/api/plans/${encodeURIComponent(name)}`;
  if (action === "apply") {
    // 把这份计划填到上方下拉框（不会自动启动）
    // 计划可能刚被别处删掉 → 不在下拉里就明确提示，而不是静默清空选择
    if (![...sel.options].some(o => o.value === name)) {
      alert("这份计划已不在列表中，请先刷新（重新打开【④ 执行与日志】）再试。");
      return;
    }
    sel.value = name;
    syncRunButtons(false);
    // 滚到下拉位置给个视觉反馈
    sel.scrollIntoView({ behavior: "smooth", block: "center" });
    sel.focus();
    return;
  }
  if (action === "view") {
    try {
      const data = await api(planUrl);
      // 用 textarea 弹窗显示（保持 JSON 缩进，方便复制）
      const w = window.open("", "_blank", "width=720,height=520");
      if (!w) { alert("浏览器拦截了弹窗"); return; }
      w.document.title = name;
      w.document.body.style.cssText =
        "margin:0;font-family:ui-monospace,Menlo,monospace;background:#0f172a;color:#e2e8f0;";
      const ta = w.document.createElement("textarea");
      ta.value = JSON.stringify(data, null, 2);
      ta.style.cssText = "width:100vw;height:100vh;border:0;outline:0;"
        + "background:#0f172a;color:#e2e8f0;padding:12px;"
        + "font-family:inherit;font-size:13px;resize:none;box-sizing:border-box;";
      w.document.body.appendChild(ta);
      ta.focus(); ta.select();
    } catch (err) {
      alert("查看失败: " + err.message);
    }
    return;
  }
  if (action === "download") {
    try {
      const data = await api(planUrl);
      const blob = new Blob([JSON.stringify(data, null, 2)],
        { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = name;
      document.body.appendChild(a); a.click();
      setTimeout(() => { document.body.removeChild(a); URL.revokeObjectURL(url); }, 0);
    } catch (err) {
      alert("下载失败: " + err.message);
    }
    return;
  }
  if (action === "delete") {
    if (!confirm(`删除计划 ${name} ？（删除后「按计划移动」不再可用此计划）`)) return;
    try {
      await api(planUrl, { method: "DELETE" });
      // 如果当前下拉正好选的是它 → 清空
      if (sel.value === name) { sel.value = ""; syncRunButtons(false); }
      await loadPlans();   // 刷新下拉 + 折叠区
    } catch (err) {
      alert("删除失败: " + err.message);
    }
    return;
  }
}

$("#btnStop").addEventListener("click", async () => {
  if (!confirm("确定停止当前任务？")) return;
  try {
    const r = await api("/api/stop", { method: "POST" });
    $("#logBox").textContent += `\n[${new Date().toLocaleTimeString()}] ` +
      `${r.msg}\n`;
  } catch (err) {
    alert("停止失败: " + err.message);
  }
});

$("#btnClearLog").addEventListener("click", () => {
  $("#logBox").textContent = "";
});

// ---- 状态轮询 ----
const STATUS_TEXT = {
  idle: "空闲", running: "运行中", done: "完成",
  failed: "失败", stopped: "已停止",
};

// 任务进入终态（done/failed/stopped/idle）时同步进度条：
// - 移除 indet 不确定态类（CSS 里的无限流动条动画要靠它驱动，没了就停）
// - done → 固定 100%
// - failed/stopped → 停在最后的百分比（不强行覆盖，仅替换文案）
// 触发场景：点 ■ 停止 / 引擎异常退出 / 任务自然完成 / 刷新页面拉到 stopped 状态
function finishProgress(s) {
  const bar  = $("#progBar");
  const pct  = $("#progPct");
  const lbl  = $("#progLabel");
  if (!bar) return;
  bar.classList.remove("indet");   // 关键：停掉扫描阶段的无限流动动画
  if (s.status === "done") {
    bar.style.width = "100%";
    pct.textContent = "100%";
    // 文案由 handleJobProgress 里"完成：成功移动…"那行覆盖；兜底也写一下
    if (!lbl.textContent || lbl.textContent.indexOf("执行完成") < 0) {
      lbl.textContent = "执行完成";
    }
  } else if (s.status === "failed") {
    lbl.textContent = "✗ 执行失败，已中断（进度条停在最后的百分比）";
  } else if (s.status === "stopped") {
    lbl.textContent = "■ 已停止（进度条停在最后的百分比）";
  }
  // idle 不动（无任务或刚启动前）
}

// 把后端状态渲染到页面（顶部状态 + 任务信息卡 + 按钮可用性）
function applyStatusUI(s) {
  $("#statusText").textContent = STATUS_TEXT[s.status] || s.status;
  $("#runLogFile").textContent = s.log_file || "—";
  if (s.status === "running") {
    syncRunButtons(true);
    $("#runStatus").textContent = "运行中";
    $("#runStatus").className = "font-medium text-green-600 pulse";
  } else {
    syncRunButtons(false);
    if (s.status === "done") {
      $("#runStatus").textContent = "✓ 完成";
      $("#runStatus").className = "font-medium text-green-600";
    } else if (s.status === "failed") {
      $("#runStatus").textContent = "✗ 失败";
      $("#runStatus").className = "font-medium text-red-600";
    } else if (s.status === "stopped") {
      $("#runStatus").textContent = "■ 已停止";
      $("#runStatus").className = "font-medium text-amber-600";
    }
    // ★ 关键：终态必须收尾进度条，否则扫描阶段的流动条会无限循环
    finishProgress(s);
  }
}

let pollTimer = null;
function pollStatus() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const s = await api("/api/status");
      applyStatusUI(s);
      if (s.status !== "running") {
        clearInterval(pollTimer);
        pollTimer = null;
        // 任务进入终态：刷新计划列表（新生成的 plan-*.json 立刻出现在下拉 + 管理区）
        // 但跳过 idle（用户还没启动过任务时也会拉到 idle，不要每 1.5s 重复刷）
        if (["done", "failed", "stopped"].includes(s.status)) {
          loadPlans();
        }
      }
    } catch (e) { /* ignore */ }
  }, 1500);
}

// 页面打开时自动恢复任务界面：不点"开始"也能看到正在跑的任务与进度
// （loadConfig 已经从 /api/state.last_run 渲染了"上次运行"面板，
//  这里主要负责"当前正在跑"的实时回放——拉日志、补 SSE、刷新轮询）
async function restoreJobUI() {
  try {
    const s = await api("/api/state");
    if (s.status && s.status.status !== "idle") {
      // 进程内存里仍知道有活进程在跑（SSE 接管即可）
      applyStatusUI(s.status);
      $("#runInfo").classList.remove("hidden");
      $("#runLabel").textContent = s.status.label || "—";
      $("#runStarted").textContent = s.status.started_at
        ? new Date(s.status.started_at * 1000).toLocaleString() : "—";
      if (s.status.status === "running") {
        $("#jobProg").classList.remove("hidden");
        $("#progBar").classList.add("indet");
        $("#progLabel").textContent = "扫描/执行中…";
        $("#progPct").textContent = "…";   // 恢复时别显示误导性的 0%，配合流动条表示"在动"
        connectSSE();
        pollStatus();
      }
    }
  } catch (e) { /* 后端不可达时保持默认 */ }
}

// 从最近一次日志还原"上次运行"面板：任务进程已不在（idle）或刚结束，
// 但日志记录着它跑到哪——据此恢复状态、进度条与日志内容。
//
// memStatus = web 进程内存里的当前任务状态，用来区分两种语境：
//   - "idle"                 → 容器重建/进程重启后看到的"上次运行"（措辞用"上次运行"）
//   - done/failed/stopped    → 刚跑完就刷新页面（F5）：此时说的是"本次运行"，
//                              并且必须把日志内容重新载入，否则会出现
//                              "状态显示完成、日志区却是空白"的割裂观感
async function restoreFromLastRun(lr, memStatus) {
  const sum = lr.summary || {};
  const live = !!memStatus && memStatus !== "idle";
  const lastWord = live ? "本次运行" : "上次运行";
  // 任务卡
  $("#runInfo").classList.remove("hidden");
  $("#runLabel").textContent = lr.label || "—";
  $("#runLogFile").textContent = lr.log;
  $("#runStarted").textContent = lr.started_at
    ? new Date(lr.started_at * 1000).toLocaleString() : "—";
  // 状态徽标 + 进度条
  $("#jobProg").classList.remove("hidden");
  const bar = $("#progBar"), pct = $("#progPct"),
        label = $("#progLabel"), rs = $("#runStatus");
  if (sum.done) {
    rs.textContent = live ? "✓ 完成" : "✓ 上次运行已完成";
    rs.className = "font-medium text-green-600";
    bar.classList.remove("indet");
    bar.style.width = "100%";
    pct.textContent = "100%";
    const warn = sum.failed ? `（失败 ${sum.failed} 个）` : "";
    label.textContent =
      `${lastWord}完成 · 成功 ${sum.success} / 失败 ${sum.failed} / 跳过 ${sum.skipped} ${warn}`;
  } else if (sum.phase === "exec") {
    rs.textContent = live ? "✗ 已中断" : "⟳ 上次运行中断";
    rs.className = "font-medium text-amber-600";
    bar.classList.remove("indet");
    bar.style.width = Math.min(100, sum.pct) + "%";
    pct.textContent = sum.pct + "%";
    label.textContent =
      `${lastWord}中断于 ${sum.pct}%（进程已退出，可重新开始续跑）`;
  } else if (sum.phase === "scan_done" || sum.phase === "scan") {
    rs.textContent = live ? "✗ 已中断（扫描阶段）" : "⟳ 上次运行中断（扫描阶段）";
    rs.className = "font-medium text-amber-600";
    bar.classList.remove("indet");        // ★ 历史恢复：不要流动条（视觉上像在跑）
    bar.style.width = "15%";              // 静态短条 + amber 颜色
    bar.classList.add("prog-amber");
    pct.textContent = "未完成";
    label.textContent = sum.phase === "scan_done"
      ? `扫描完成 · ${sum.scanned_dirs} 目录 / ${sum.scanned_files} 文件，执行前中断`
      : `${lastWord}扫描中 · 已处理 ${sum.scanned_dirs} 目录`;
  } else {
    rs.textContent = live ? "本次运行异常结束" : "上次运行异常结束";
    rs.className = "font-medium text-slate-600";
    bar.classList.remove("indet");
    bar.style.width = "15%";
    bar.classList.add("prog-amber");
    label.textContent = "日志未能识别到进度（可能刚启动就被中断）";
  }
  // 顶部状态条
  $("#statusText").textContent = "空闲";
  // 载入日志尾部供查看（全量可到【历史日志】里翻）
  const txt = await (await fetch("/api/logs/" + lr.log + "?tail=2000")).text();
  $("#logBox").textContent = txt;
  txt.split("\n").forEach((ln) => handleJobProgress(ln));
  $("#sseStatus").textContent = "离线 · 已从最近日志恢复";
  // 按钮：可重新执行（查询/查询并移动/按计划移动按各自条件启用）
  syncRunButtons(false);
}

// ---- SSE 实时日志 ----
// 从日志行中识别进度信息并驱动页面上方的进度条。
// 扫描阶段总量未知 → 流动条动画；执行阶段 → 精确百分比；收尾 → 固定 100%。
function handleJobProgress(line) {
  const wrap = $("#jobProg"), bar = $("#progBar");
  const label = $("#progLabel"), pct = $("#progPct");
  if (!wrap) return;
  // 1) 执行阶段精确进度：[进度] 已完成 38% (2914/7630)，成功 2911，失败 2…
  let m = line.match(
    /\[进度\] 已完成 (\d+)% \((\d+)\/(\d+)\)，成功 (\d+)，失败 (\d+)，跳过 (\d+)/);
  if (m) {
    wrap.classList.remove("hidden");
    bar.classList.remove("indet");
    const v = Math.min(100, +m[1]);
    bar.style.width = v + "%";
    pct.textContent = v + "%";
    label.textContent = `执行中 · ${m[2]}/${m[3]} 个 · 成功 ${m[4]} / 失败 ${m[5]} / 跳过 ${m[6]}`;
    return;
  }
  // 2) 扫描阶段（总量未知 → 流动条动画）
  m = line.match(/\[扫描中\] 已处理 (\d+) 个目录，发现 (\d+) 个文件/);
  if (m) {
    wrap.classList.remove("hidden");
    bar.classList.add("indet");
    bar.style.width = "";
    pct.textContent = "扫描中";
    label.textContent = `正在扫描 · 已处理 ${m[1]} 个目录，发现 ${m[2]} 个文件`;
    return;
  }
  // 3) 扫描完成 → 保持流动条，提示即将进入执行
  m = line.match(/\[扫描完成\] 共处理 (\d+) 个目录，发现 (\d+) 个文件/);
  if (m) {
    bar.classList.add("indet");
    bar.style.width = "";
    pct.textContent = "待执行";
    label.textContent = `扫描完成 · ${m[1]} 个目录 / ${m[2]} 个文件，即将开始整理`;
    return;
  }
  // 4) 开始执行
  if (line.indexOf("开始执行") >= 0) {
    label.textContent = "开始执行 …";
    return;
  }
  // 5) 执行收尾 → 固定 100%
  m = line.match(/完成：成功移动 (\d+) 个，失败 (\d+) 个，跳过 (\d+) 个/);
  if (m) {
    bar.classList.remove("indet");
    bar.style.width = "100%";
    pct.textContent = "100%";
    label.textContent = `执行完成 · 成功 ${m[1]} / 失败 ${m[2]} / 跳过 ${m[3]}`;
  }
}

function connectSSE() {
  if (state.sse) state.sse.close();
  $("#sseStatus").textContent = "连接中…";
  const es = new EventSource("/api/logs/stream");
  state.sse = es;
  es.onopen = () => { $("#sseStatus").textContent = "已连接 ✓"; };
  es.onmessage = (ev) => {
    // 逐行解析进度：一次性推送的多行尾部（页面恢复时）也能正确恢复百分比
    ev.data.split("\n").forEach((ln) => handleJobProgress(ln));
    const box = $("#logBox");
    box.textContent += ev.data + "\n";
    box.scrollTop = box.scrollHeight;
    // 限制最大行数（防止无限增长占满内存）
    const lines = box.textContent.split("\n");
    if (lines.length > 5000) {
      box.textContent = "...(省略早期日志)...\n" +
        lines.slice(-4000).join("\n");
    }
    // 服务端任务结束后会发 [SSE 流结束]：主动关闭，避免 EventSource 无限自动重连
    if (ev.data.includes("[SSE 流结束]")) {
      es.close();
      state.sse = null;
      $("#sseStatus").textContent = "连接已结束";
    }
  };
  es.onerror = () => {
    $("#sseStatus").textContent = "连接断开";
    // EventSource 会自动重连，状态变 running 后也会重连
  };
}

// ---- 历史日志 ----
async function loadHistory() {
  try {
    const r = await api("/api/logs");
    if (!r.logs || r.logs.length === 0) {
      $("#historyLogs").innerHTML = '<p class="text-slate-400">无历史日志</p>';
      return;
    }
    const html = r.logs.map(log => {
      const dt = new Date(log.mtime * 1000);
      const sizeKB = (log.size / 1024).toFixed(1);
      const url = "/api/logs/" + encodeURIComponent(log.name);
      return `<div class="flex items-center justify-between
                          hover:bg-slate-100 p-1 rounded">
        <span class="font-mono">${esc(log.name)}</span>
        <span class="text-xs text-slate-500">
          ${dt.toLocaleString()} · ${sizeKB} KB
        </span>
        <span>
          <a href="${esc(url)}" target="_blank"
             class="text-xs text-blue-600 hover:underline mr-2">查看</a>
          <a href="#" data-name="${esc(log.name)}"
             class="del-log text-xs text-red-600 hover:underline">删除</a>
        </span>
      </div>`;
    }).join("");
    $("#historyLogs").innerHTML = html;
    $$(".del-log").forEach(a => {
      a.addEventListener("click", async (e) => {
        e.preventDefault();
        const name = e.currentTarget.dataset.name;
        if (!confirm("删除日志 " + name + "？")) return;
        try {
          await api("/api/logs/" + encodeURIComponent(name), { method: "DELETE" });
          loadHistory();
        } catch (err) {
          alert("删除失败: " + err.message);
        }
      });
    });
  } catch (err) {
    $("#historyLogs").innerHTML = '<p class="text-red-500">加载失败</p>';
  }
}

// ---- 启动 ----
(async () => {
  // 先把"持久化配置 + 上次任务状态"全部回填完，再判断当前是否有活任务要接管
  await loadConfig();
  await restoreJobUI();
  // 预载计划列表（优先选中上次任务用到的计划）
  await loadPlans(state.lastPlan);
  syncRunButtons(false);
})();