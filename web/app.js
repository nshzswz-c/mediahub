/* MediaHub 前端逻辑 */
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const state = {
  tab: "browse",
  mediaType: "电影",
  searchMode: false,
  statsView: "stats",
  page: 1,
  items: [],
  records: [],
  mineStatus: "want",
  current: null,
  currentRecord: null,
  rating: 0,
  setPane: "mp",
  cfg: {},
};

// ---------------- 工具 ----------------
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay
    ? `今天 ${p(d.getHours())}:${p(d.getMinutes())}`
    : `${d.getMonth() + 1}月${d.getDate()}日 ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function toast(msg, type = "") {
  const t = $("#toast");
  const icon = type === "ok" ? "✅ " : type === "err" ? "❌ " : "";
  t.textContent = icon + msg;
  t.className = "toast " + type;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), 3000);
}

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await resp.json().catch(() => ({}));
  if (resp.status === 401 && data.need_login) {
    showLogin();
    throw new Error("请先输入访问密码");
  }
  if (!resp.ok) throw new Error(data.detail || `请求失败 (${resp.status})`);
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------- 视图切换 ----------------
function switchTab(tab, type) {
  state.tab = tab;
  if (type) state.mediaType = type;
  $$(".tab").forEach((b) =>
    b.classList.toggle("active",
      b.dataset.tab === tab && (tab !== "browse" || b.dataset.type === type)));
  ["browse", "books", "mine", "settings", "stats"].forEach((v) =>
    $("#view-" + v).classList.toggle("hidden", v !== tab));
  if (tab === "browse") loadBrowse();
  if (tab === "books") loadBooks(true);
  if (tab === "mine") loadRecords();
  if (tab === "stats") { loadStats(); showStatsView(state.statsView || "stats"); }
  if (tab === "settings") {
    window.scrollTo({ top: 0 });
    loadSettings();
  }
}

// 统计页子视图切换：统计 / 年度报告
function showStatsView(v) {
  state.statsView = v;
  $("#stats-inside").classList.toggle("hidden", v !== "stats");
  $("#report-inside").classList.toggle("hidden", v !== "report");
  $("#sw-stats").classList.toggle("on", v === "stats");
  $("#sw-report").classList.toggle("on", v === "report");
  if (v === "report") loadReport();
}
$("#sw-stats").addEventListener("click", () => showStatsView("stats"));
$("#sw-report").addEventListener("click", () => showStatsView("report"));

$("#tabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (!btn) return;
  if (btn.dataset.tab === "browse") {
    state.searchMode = false;
    switchTab("browse", btn.dataset.type);
    $("#browse-title").textContent = "热门" + btn.dataset.type;
    loadHomeRows(true);
  } else {
    switchTab(btn.dataset.tab);
  }
});

// ---------------- 浏览/搜索 ----------------
function cardHTML(m, record, idx = -1) {
  const rate = record?.status === "done" && record.rating
    ? `<span class="rate-badge">★ ${record.rating}</span>` : "";
  const badge = record
    ? (record.status === "done"
        ? `<span class="badge done">看完</span>`
        : `<span class="badge want">想看</span>`)
    : "";
  const poster = m.poster
    ? `<img src="${esc(m.poster)}" loading="lazy" onerror="this.style.display='none'">`
    : `<div class="no-poster">🎞</div>`;
  // 悬停快捷按钮：没记录 → 一键想看；想看 → 已加入；看完 → 已看完
  const qa = !record
    ? `<button class="qa" data-qa="want" title="一键加入想看并订阅下载">＋ 想看</button>`
    : record.status === "done"
      ? `<button class="qa is-done" data-qa="none" title="已看完，点海报看详情">✓ 已看完</button>`
      : `<button class="qa is-want" data-qa="want" title="已在想看清单">✓ 想看</button>`;
  const vote = m.vote
    ? `<span class="badge vote" title="来源评分">★ ${Number(m.vote).toFixed(1)}</span>` : "";
  const anim = idx >= 0 ? ` enter" style="animation-delay:${Math.min(idx * 28, 340)}ms` : "";
  return `
  <div class="card${anim}" data-idx="${m._idx}" title="${esc(m.title)}${m.year ? ` (${esc(m.year)})` : ""}">
    <div class="poster-wrap">${poster}${rate}${vote}${badge}
      <span class="badge type">${esc(m.media_type)}</span>${qa}</div>
    <div class="info">
      <div class="title">${esc(m.title)}</div>
      <div class="year">${esc(m.year || "")}</div>
    </div>
  </div>`;
}

// 骨架屏：加载时先占位，避免白屏突兀
function showSkeleton(grid, n = 18) {
  grid.innerHTML = Array.from({ length: n }, () => `
    <div class="sk-card">
      <div class="sk-box"></div>
      <div class="sk-line"></div>
      <div class="sk-line short"></div>
    </div>`).join("");
}

function findRecord(m) {
  return state.records.find(
    (r) => r.title === m.title &&
      (m.tmdb_id ? r.tmdb_id === m.tmdb_id : r.year === (m.year || "")) &&
      r.media_type === m.media_type);
}

function renderGrid() {
  const grid = $("#media-grid");
  if (state.tab !== "browse") return;
  grid.innerHTML = state.items
    .map((m, i) => { m._idx = i; return cardHTML(m, findRecord(m), i); })
    .join("");
  $("#browse-empty").classList.toggle("hidden", state.items.length > 0);
  $("#more-btn").classList.toggle("hidden", state.items.length === 0 || state.searchMode);
}

async function loadBrowse(append = false) {
  if (!append) {
    state.page = 1; state.items = [];
    showSkeleton($("#media-grid"));
    $("#browse-empty").classList.add("hidden");
    $("#more-btn").classList.add("hidden");
  }
  try {
    let data;
    if (state.mediaType === "图书") {
      $("#browse-title").textContent = "图书";
      $("#browse-empty").textContent = "MoviePilot 暂不支持图书订阅。你可以在\"我的\"页管理本地图书记录，或通过搜索添加。";
      $("#browse-empty").classList.remove("hidden");
      return;
    }
    if (state.searchMode) {
      data = await api(`/api/media/search?title=${encodeURIComponent(state.query)}&media_type=${encodeURIComponent(state.mediaType)}&page=${state.page}`);
      $("#browse-title").textContent = `“${state.query}” 的搜索结果`;
    } else {
      data = await api(`/api/media/popular?media_type=${encodeURIComponent(state.mediaType)}&page=${state.page}`);
    }
    const list = Array.isArray(data) ? data : [];
    // 类型过滤（trending 会混入电影）
    const filtered = state.mediaType === "电影" || state.mediaType === "全部"
      ? list
      : list.filter((m) => m.media_type !== "电影" || ["图书", "音乐"].includes(state.mediaType));
    state.items = append ? state.items.concat(filtered) : filtered;
    renderGrid();
  } catch (e) {
    toast(e.message, "err");
    $("#browse-empty").classList.remove("hidden");
  }
}

$("#search-btn").addEventListener("click", doSearch);
$("#search-input").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
function doSearch() {
  const q = $("#search-input").value.trim();
  if (!q) return;
  state.searchMode = true;
  state.query = q;
  hideHomeRows();
  loadBrowse();
}
$("#more-btn").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  state.page++;
  btn.classList.add("loading"); btn.textContent = "加载中";
  try { await loadBrowse(true); }
  finally { btn.classList.remove("loading"); btn.textContent = "加载更多"; }
});

$("#media-grid").addEventListener("click", async (e) => {
  const qa = e.target.closest(".qa");
  if (qa) {
    e.stopPropagation();
    if (qa.dataset.qa !== "want") return;
    const card = qa.closest(".card");
    const m = state.items[+card.dataset.idx];
    if (!m || qa.classList.contains("loading")) return;
    qa.classList.add("loading"); qa.textContent = "提交中…";
    try {
      await api("/api/want", {
        method: "POST",
        body: JSON.stringify({
          source: m.source || "mp", media_type: m.media_type, title: m.title,
          year: m.year || "", overview: m.overview || "", poster: m.poster || "",
          tmdb_id: m.tmdb_id || null, douban_id: m.douban_id || null,
          media_source: m.media_source || "", media_id: m.media_id || "",
          subscribe_mp: true,
        }),
      });
      toast("已加入想看，并已通知 MP 订阅下载 ✅", "ok");
      await refreshRecords();
      renderGrid(); // 重渲染后该卡会带上 ✓ 徽标
    } catch (err) {
      toast(err.message, "err");
      qa.classList.remove("loading"); qa.textContent = "＋ 想看";
    }
    return;
  }
  const card = e.target.closest(".card");
  if (card) openDetail(state.items[+card.dataset.idx]);
});

// ---------------- 详情弹窗 ----------------
const QUICK_TAGS = ["神作", "好看", "上头", "还行", "一般", "失望", "烂尾", "下饭", "烧脑", "催泪"];

function openDetail(m, openRating = false) {
  state.current = m;
  state.currentRecord = findRecord(m) || null;
  state.rating = state.currentRecord?.rating ? Math.round(state.currentRecord.rating / 2) : 0;
  state.currentTags = new Set((state.currentRecord?.tags || "").split(",").map(t => t.trim()).filter(Boolean));
  $("#m-poster").src = m.poster || "";
  $("#m-poster").style.display = m.poster ? "" : "none";
  $("#m-title").textContent = m.title;
  $("#m-meta").textContent =
    [m.media_type, m.year, m.tmdb_id ? "TMDB:" + m.tmdb_id : "", m.douban_id ? "豆瓣:" + m.douban_id : ""]
      .filter(Boolean).join(" · ");
  $("#m-overview").textContent = m.overview || "暂无简介";
  const r = state.currentRecord;
  const tagHTML = (r?.tags || "")
    ? `<br>` + (r.tags || "").split(",").filter(Boolean)
        .map((t) => `<i class="tag-chip">${esc(t.trim())}</i>`).join(" ")
    : "";
  $("#m-status").innerHTML = r
    ? (r.status === "done"
        ? `<span class="ok">✔ 已看完</span>${r.rating ? " · 评分 " + r.rating + "/10" : ""}` +
          (r.comment ? `<br>📝 ${esc(r.comment)}` : "") + tagHTML
        : `<span class="ok">＋ 已标记想看</span>${r.mp_subscribed === false ? ' <span class="err">(MP 订阅失败)</span>' : ""}`)
    : "";
  // 按钮文案：想看过的显示"看完"，看完过的显示"修改评价"（已看完的隐藏"想看"按钮）
  $("#btn-want").style.display = (r && r.status === "done") ? "none" : "";
  $("#btn-want").textContent = (r && r.status === "want") ? "✔ 我看完了" : "＋ 想看（订阅下载）";
  $("#btn-finish").textContent = (r && r.status === "done") ? "★ 修改评价" : "✔ 看完 · 评价";
  $("#comment").value = r?.comment || "";
  // 已看完的记录再次评价时，可以标为「重看」
  $("#rewatch-row").classList.toggle("hidden", !(r && r.status === "done"));
  $("#rewatch-check").checked = false;
  $("#rating-area").classList.toggle("hidden", !openRating);
  $("#res-check-result").classList.add("hidden");
  $("#res-check-result").innerHTML = "";
  $("#list-pick").classList.add("hidden");
  $("#m-douban").classList.add("hidden");
  $("#m-douban").innerHTML = "";
  loadDouban(m);
  renderStars();
  renderQuickTags();
  updateStarDisplay();
  $("#modal").classList.remove("hidden");
}

function renderQuickTags() {
  $("#quick-tags").innerHTML = QUICK_TAGS.map((t) =>
    `<span class="${state.currentTags?.has(t) ? "on" : ""}" data-tag="${t}">${t}</span>`).join("");
}
$("#quick-tags").addEventListener("click", (e) => {
  const t = e.target.dataset.tag;
  if (!t) return;
  state.currentTags.has(t) ? state.currentTags.delete(t) : state.currentTags.add(t);
  renderQuickTags();
});

// ---------------- 资源库检查 ----------------
$("#btn-res-check").addEventListener("click", async () => {
  const m = state.current;
  const box = $("#res-check-result");
  box.classList.remove("hidden");
  box.innerHTML = `<div class="rc-hint">🔍 正在检查你的所有资源库…</div>`;
  try {
    const res = await api(`/api/resource/check?title=${encodeURIComponent(m.title)}`);
    if (!res.results.length) {
      box.innerHTML = `<div class="rc-hint">还没有配置任何媒体服务器或 115，去 ⚙️ 设置里添加吧</div>`;
      return;
    }
    box.innerHTML = res.results.map((s) => {
      const typeIcon = { emby: "🎬", jellyfin: "🐬", plex: "🌶", "115": "☁️" }[s.type] || "📁";
      const key = (t) => String(t || "")
        .replace(/[\s·:：!！?？,，.。\-—_()（）\[\]【】]/g, "").toLowerCase();
      let badge, extra = "";
      if (s.ok && s.count > 0) {
        const items = s.items || [];
        // 只看标题是否真的对得上，避免"希望之城"被算成"希望"
        const hit = s.type === "115" ? [] : items.filter((x) => key(x.title) === key(m.title));
        if (hit.length || s.type === "115") {
          badge = `<span class="rc-badge yes">已有</span>`;
        } else {
          badge = `<span class="rc-badge maybe">相关 ${s.count} 项</span>`;
        }
        const names = s.type === "115"
          ? items.map((f) => f.name).slice(0, 3).join(" / ")
          : (hit.length ? hit : items)
              .map((x) => `${x.title}${x.year ? "(" + x.year + ")" : ""}`).slice(0, 3).join(" / ");
        extra = names ? `<div class="rc-items">${esc(names)}</div>` : "";
        // 只有标题真匹配上才给播放入口，免得点进去是别的片
        const play = hit.find((x) => x.play_url);
        if (play) {
          extra += ` <a class="mini-btn rc-play" href="${esc(play.play_url)}" target="_blank">▶ 在 ${esc(s.name)} 播放</a>`;
        }
      } else if (s.ok) {
        badge = `<span class="rc-badge no">没有</span>`;
      } else {
        badge = `<span class="rc-badge err">失败</span>`;
        extra = `<div class="rc-items">${esc(s.error || "")}</div>`;
      }
      return `<div class="rc-row">${typeIcon} <span class="rc-name">${esc(s.name)}</span>${badge}${extra}</div>`;
    }).join("");
  } catch (e) {
    box.innerHTML = `<div class="rc-hint">检查失败：${esc(e.message)}</div>`;
  }
});

$("#modal-close").addEventListener("click", () => $("#modal").classList.add("hidden"));
$("#modal").addEventListener("click", (e) => {
  if (e.target === $("#modal")) $("#modal").classList.add("hidden");
});

// 想看
$("#btn-want").addEventListener("click", async () => {
  const m = state.current;
  $("#btn-want").disabled = true;
  try {
    const res = await api("/api/want", {
      method: "POST",
      body: JSON.stringify({
        source: m.source || "mp", media_type: m.media_type, title: m.title,
        year: m.year || "", overview: m.overview || "", poster: m.poster || "",
        tmdb_id: m.tmdb_id || null, douban_id: m.douban_id || null,
        media_source: m.media_source || "", media_id: m.media_id || "",
        subscribe_mp: true,
      }),
    });
    if (res.mp_subscribed) {
      toast("已加入想看，并已通知 MoviePilot 订阅下载 ✅", "ok");
      $("#m-status").innerHTML = `<span class="ok">＋ 已订阅，MP 正在处理下载</span>`;
    } else {
      toast("已加入想看，但 MP 订阅失败：" + (res.mp_result || "未知错误"), "err");
      $("#m-status").innerHTML = `<span class="ok">＋ 已加入想看</span> <span class="err">MP 订阅失败：${esc(res.mp_result || "")}</span>`;
    }
    await refreshRecords();
    renderGrid();
  } catch (e) {
    toast(e.message, "err");
  } finally {
    $("#btn-want").disabled = false;
  }
});

// 看完·评价
$("#btn-finish").addEventListener("click", () => {
  $("#rating-area").classList.toggle("hidden");
});

function renderStars() {
  $("#stars").innerHTML = Array.from({ length: 5 }, (_, i) =>
    `<span data-v="${i + 1}">★</span>`).join("");
}
function updateStarDisplay() {
  $$("#stars span").forEach((s) => s.classList.toggle("on", +s.dataset.v <= state.rating));
}
$("#stars").addEventListener("click", (e) => {
  if (e.target.dataset.v) { state.rating = +e.target.dataset.v; updateStarDisplay(); }
});

$("#btn-save-finish").addEventListener("click", async () => {
  const m = state.current;
  try {
    let rid = state.currentRecord?.id;
    if (!rid) {
      const res = await api("/api/want", {
        method: "POST",
        body: JSON.stringify({
          source: m.source || "mp", media_type: m.media_type, title: m.title,
          year: m.year || "", overview: m.overview || "", poster: m.poster || "",
          tmdb_id: m.tmdb_id || null, douban_id: m.douban_id || null,
          media_source: m.media_source || "", media_id: m.media_id || "",
          subscribe_mp: false,
        }),
      });
      rid = res.record_id;
    }
    const isRewatch = $("#rewatch-check")?.checked && state.currentRecord?.status === "done";
    await api("/api/finish", {
      method: "POST",
      body: JSON.stringify({
        record_id: rid,
        rating: state.rating ? state.rating * 2 : null,
        comment: $("#comment").value.trim(),
        tags: [...(state.currentTags || [])].join(","),
        rewatch: !!isRewatch,
      }),
    });
    toast(isRewatch ? "已记为「重看」一次 🔁" : "已标记看完，感谢评价 🎉", "ok");
    $("#m-status").innerHTML = `<span class="ok">✔ 已看完</span>${state.rating ? " · 评分 " + state.rating * 2 + "/10" : ""}`;
    $("#rating-area").classList.add("hidden");
    await refreshRecords();
    renderGrid();
  } catch (e) {
    toast(e.message, "err");
  }
});

// ---------------- 我的清单 ----------------
$$(".sub-tab").forEach((b) => b.addEventListener("click", () => {
  $$(".sub-tab").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  state.mineStatus = b.dataset.status;
  loadRecords();
}));

async function refreshRecords() {
  try {
    const want = await api("/api/records?status=want");
    const done = await api("/api/records?status=done");
    state.records = want.concat(done);
  } catch { /* ignore */ }
}

async function loadRecords() {
  if (state.mineStatus === "lists") {
    $("#records-grid").classList.add("hidden");
    $("#records-empty").classList.add("hidden");
    try { await renderListsView(); } catch (e) { toast(e.message, "err"); }
    return;
  }
  $("#lists-view").classList.add("hidden");
  $("#records-grid").classList.remove("hidden");
  await refreshRecords();
  const list = state.records.filter((r) => r.status === state.mineStatus);
  $("#records-grid").innerHTML = list
    .map((r, i) => { r._idx = i; return cardHTML(r, r); })
    .join("");
  $("#records-empty").classList.toggle("hidden", list.length > 0);
}

$("#records-grid").addEventListener("click", (e) => {
  const card = e.target.closest(".card");
  if (!card) return;
  const r = state.records.filter((x) => x.status === state.mineStatus)[+card.dataset.idx];
  if (r) openDetail({ ...r, source: r.source, media_type: r.media_type }, true);
});

// 媒体库同步（全部服务器）
$("#sync-emby-btn").addEventListener("click", async () => {
  $("#sync-emby-btn").disabled = true;
  try {
    const res = await api("/api/sync", { method: "POST" });
    const detail = (res.servers || [])
      .map((s) => `${s.server} ${s.error ? "❌" : s.count + " 条"}`).join(" · ");
    toast(`同步完成：共 ${res.imported} 条（${detail}）`, "ok");
    loadRecords();
  } catch (e) {
    toast(e.message, "err");
  } finally {
    $("#sync-emby-btn").disabled = false;
  }
});

// MP 订阅状态
$("#view-subs-btn").addEventListener("click", async () => {
  try {
    const res = await api("/api/subscriptions");
    if (!res.ok) throw new Error(res.detail || "获取失败");
    const subs = res.list || [];
    if (!subs.length) return toast("MoviePilot 当前没有进行中的订阅");
    const lines = subs.slice(0, 5).map((s) =>
      `《${s.name || "?"}》${s.year ? "(" + s.year + ")" : ""} 第${s.season || 1}季 · ${s.state || ""}`);
    toast(`MP 订阅 ${subs.length} 条：${lines.join(" / ")}`, "ok");
  } catch (e) {
    toast(e.message, "err");
  }
});

// ---------------- 统计 ----------------
async function loadStats() {
  let d;
  await refreshRecords();   // 让榜单里的条目能直接打开详情/评价
  try {
    d = await api("/api/stats");
  } catch (e) {
    toast(e.message, "err");
    return;
  }
  const byType = Object.entries(d.by_type_done || {})
    .sort((a, b) => b[1] - a[1]);
  $("#hero-done").textContent = d.done_count;
  $("#hero-bytype").innerHTML = byType.length
    ? byType.map(([k, v]) => `<span class="chip">${esc(k)} · ${v}</span>`).join("")
    : `<span class="chip">还没有看完记录，快去标记吧</span>`;

  const cards = [
    { ic: "📥", cls: "blue",  num: d.want_count,   lbl: "想看中 · 等着追" },
    { ic: "⭐", cls: "gold",  num: d.avg_rating ?? "–", lbl: `平均评分 · ${d.comment_count} 条评论` },
    { ic: "📆", cls: "cyan",  num: d.month_count,  lbl: "本月看完" },
  ];
  const act = d.activity || {};
  const days = Object.keys(act).length;
  const totalAct = Object.values(act).reduce((a, b) => a + b, 0);
  if (days) cards.push({ ic: "🔥", cls: "green", num: days, lbl: `活跃天数 · 共 ${totalAct} 次记录` });
  if (d.rewatch && d.rewatch.times > 0) {
    cards.push({ ic: "🔁", cls: "cyan", num: d.rewatch.times,
      lbl: `重看次数 · ${d.rewatch.titles} 部刷过` });
  }
  $("#stat-cards").innerHTML = cards.map((c) => `
    <div class="stat-card">
      <div class="ic ic-${c.cls}">${c.ic}</div>
      <div><div class="num c-${c.cls}">${c.num}</div><div class="lbl">${c.lbl}</div></div>
    </div>`).join("");
  renderHeatmap(act);

  const rankHTML = (list, showRating, fmtSub) => list.length
    ? list.map((r, i) => {
        const rk = showRating ? (i < 3 ? `rk${i + 1}` : "rkx") : "";
        return `
      <div class="rank-item" data-title="${esc(r.title)}" data-type="${esc(r.media_type)}">
        ${rk ? `<div class="rk ${rk}"></div>` : ""}
        ${r.poster ? `<img src="${esc(r.poster)}" loading="lazy">` : `<div class="no-img">🎞</div>`}
        <div class="info">
          <div class="t">${esc(r.title)}</div>
          <div class="s">${esc(r.media_type)} · ${fmtSub(r)}</div>
        </div>
        ${showRating && r.rating ? `<div class="r">★ ${r.rating}<small>/10</small></div>` : ""}
      </div>`;}).join("")
    : `<div class="rank-empty">还没有数据 · 去点几个"看完"吧</div>`;
  $("#top-rated").innerHTML = rankHTML(d.top_rated || [], true,
    (r) => `${r.year || ""}`);
  $("#recent-list").innerHTML = rankHTML(d.recent || [], false,
    (r) => new Date(r.finished_at * 1000).toLocaleDateString("zh-CN"));

  // 想看清单
  const wants = d.want_list || [];  $("#want-list-stats").innerHTML = wants.length
    ? wants.map((r) => `
      <div class="rank-item" data-title="${esc(r.title)}" data-type="${esc(r.media_type)}">
        ${r.poster ? `<img src="${esc(r.poster)}" loading="lazy">` : `<div class="no-img">🎯</div>`}
        <div class="info">
          <div class="t">${esc(r.title)}</div>
          <div class="s">${esc(r.media_type)}${r.year ? " · " + esc(r.year) : ""} · ${new Date(r.created_at * 1000).toLocaleDateString("zh-CN")} 加入</div>
        </div>
        <span class="want-badge">想看</span>
      </div>`).join("")
    : `<div class="rank-empty">还没有想看的 · 去浏览页点个＋</div>`;

  // 点击榜单条目 → 打开详情可改评价
  $$("#view-stats .rank-item").forEach((el) => {
    el.addEventListener("click", () => {
      const rec = state.records.find((x) => x.title === el.dataset.title);
      openDetail(rec ? { ...rec, media_type: rec.media_type }
                     : { title: el.dataset.title, media_type: el.dataset.type,
                         source: "local" }, true);
    });
  });

  // 观影时长 + 最爱导演/演员
  $("#total-hours").textContent = d.total_hours ?? 0;
  const personsHTML = (list) => list && list.length
    ? list.map((p) => `
      <div class="rank-item person">
        <div class="avatar">${esc(p.name.slice(0, 1))}</div>
        <div class="info"><div class="t">${esc(p.name)}</div></div>
        <span class="want-badge">${p.count} 部</span>
      </div>`).join("")
    : `<div class="rank-empty">同步已看或打开详情后自动积累</div>`;
  $("#top-directors").innerHTML = personsHTML(d.top_directors);
  $("#top-actors").innerHTML = personsHTML(d.top_actors);

  // 年度观影目标（面板永远显示；未设目标时展示内联设置）
  const goal = d.goal;
  if (goal && goal.target > 0) {
    $("#goal-label").textContent = `已看 ${goal.done} / ${goal.target} 部 · 完成 ${goal.pct}%`;
    $("#goal-fill").style.width = goal.pct + "%";
    $("#goal-bar").style.display = "";
    $("#goal-set").classList.add("hidden");
  } else {
    $("#goal-label").textContent = "";
    $("#goal-bar").style.display = "none";
    $("#goal-set").classList.remove("hidden");
  }

  loadTaste();
  loadGap();
  loadStorage();
}

// ---------------- 存储体检 ----------------
let storageLoaded = false;

async function loadStorage(force = false) {
  const body = $("#storage-body");
  if (!body) return;
  if (!force && storageLoaded && body.dataset.filled === "1") return;
  body.innerHTML = `<div class="rank-empty">正在扫描媒体库体积…（首次较慢，之后 10 分钟内走缓存）</div>`;

  let d;
  try {
    d = await api("/api/storage" + (force ? "?refresh=1" : ""));
  } catch (e) {
    body.innerHTML = `<div class="rank-empty">${esc(e.message || "扫描失败")}</div>`;
    return;
  }
  storageLoaded = true;

  if (!d.count) {
    body.innerHTML = `<div class="rank-empty">还没有可统计的媒体库（需要先在设置里添加 Emby / Jellyfin）</div>`;
    return;
  }
  const maxTop = Math.max(1, ...(d.top || []).map((t) => t.gb));
  const typeLine = (d.by_type || []).map((t) => `${t.name} ${t.gb} GB`).join(" · ");
  const topRows = (d.top || []).slice(0, 6).map((t) => `
    <div class="taste-row">
      <span class="taste-name" title="${esc(t.title)}">${esc(t.title)}</span>
      <span class="taste-bar"><i style="width:${Math.round(t.gb / maxTop * 100)}%"></i></span>
      <span class="taste-val">${t.gb} GB</span>
    </div>`).join("");
  const c = d.cleanable || { count: 0, gb: 0, items: [] };
  const cleanBlock = c.count
    ? `<div class="taste-sub">可清理（${c.count} 个 · 约 ${c.gb} GB）</div>` +
      c.items.map((i) => `
        <div class="gap-item">
          <span class="gap-t" title="${esc(i.title)}">${esc(i.title)}</span>
          <span class="gap-s">${i.gb} GB<i>${esc(i.reason)}</i></span>
        </div>`).join("")
    : `<div class="taste-sub">可清理</div>
       <div class="rank-empty" style="padding:12px 0">暂时没有明显该删的：看完的片评分都还不错，或最近还在看</div>`;

  body.innerHTML = `
    <div class="gap-hero">
      <span class="gap-num">${d.total_gb}</span>
      <span class="gap-verdict">GB · ${d.count} 部作品<br><small>${esc(typeLine)}</small></span>
    </div>
    <div class="taste-sub">占用最多</div>
    ${topRows}
    ${cleanBlock}`;
  body.dataset.filled = "1";
}

$("#storage-refresh").addEventListener("click", () => loadStorage(true));

// ---------------- 口味画像 ----------------
async function loadTaste() {
  let d;
  try { d = await api("/api/taste"); } catch { return; }
  const note = $("#taste-note"), body = $("#taste-body");
  if (!d.n) {
    note.textContent = "";
    body.innerHTML = `<div class="rank-empty">还没有看完记录</div>`;
    return;
  }
  note.textContent = `${d.n} 部已看完 · ${d.rated} 部打过分 · 元数据覆盖 ${d.cover}%`;

  const barRow = (name, val, max, right) => `
    <div class="taste-row">
      <span class="taste-name" title="${esc(name)}">${esc(name)}</span>
      <span class="taste-bar"><i style="width:${max ? Math.round(val / max * 100) : 0}%"></i></span>
      <span class="taste-val">${right}</span>
    </div>`;
  const maxOf = (list) => Math.max(1, ...list.map((x) => x.count));
  const blocks = [];

  if (d.by_type?.length) {
    const mx = maxOf(d.by_type);
    blocks.push(`<div class="taste-sub">类型</div>` + d.by_type.map((t) =>
      barRow(t.name, t.count, mx, `${t.count} 部${t.avg ? ` · ★${t.avg}` : ""}`)).join(""));
  }
  if (d.by_decade?.length) {
    const mx = maxOf(d.by_decade);
    blocks.push(`<div class="taste-sub">年代</div>` + d.by_decade.map((t) =>
      barRow(t.name, t.count, mx, `${t.count} 部`)).join(""));
  }
  if (d.genres?.length) {
    blocks.push(`<div class="taste-sub">题材</div><div class="chip-line">` +
      d.genres.map((g) => `<span>${esc(g.name)} · ${g.count}${g.avg ? ` ★${g.avg}` : ""}</span>`).join("") +
      `</div>`);
  }
  if (d.directors?.length) {
    blocks.push(`<div class="taste-sub">常看导演</div><div class="chip-line">` +
      d.directors.map((p) => `<span>${esc(p.name)} · ${p.count}${p.avg ? ` ★${p.avg}` : ""}</span>`).join("") +
      `</div>`);
  }
  body.innerHTML = blocks.join("") ||
    `<div class="rank-empty">数据还不够：多标记几部「看完」、打开详情积累豆瓣信息即可</div>`;
}

// ---------------- 我和豆瓣 ----------------
async function loadGap() {
  let d;
  try { d = await api("/api/rating-gap"); } catch { return; }
  const note = $("#gap-note"), body = $("#gap-body");
  if (!d.n) {
    note.textContent = "";
    body.innerHTML = `<div class="rank-empty">还没有可比数据：打开影片详情会自动抓豆瓣评分，慢慢就能对比了</div>`;
    return;
  }
  note.textContent = `共 ${d.n} 部可对比 · 一致 ${d.same} 部`;
  const delta = d.delta;
  const verdict = Math.abs(delta) < 0.4
    ? "和豆瓣基本一致"
    : delta > 0 ? `比豆瓣宽容 ${delta} 分` : `比豆瓣严格 ${Math.abs(delta)} 分`;
  const item = (p) => `
    <div class="gap-item">
      <span class="gap-t" title="${esc(p.title)}">${esc(p.title)}</span>
      <span class="gap-s"><b>${p.my}</b><i>vs</i>${p.douban}</span>
    </div>`;
  body.innerHTML = `
    <div class="gap-hero">
      <span class="gap-num">${delta > 0 ? "+" : ""}${delta}</span>
      <span class="gap-verdict">${verdict}<br>
        <small>我的平均 ${d.my_avg} · 豆瓣平均 ${d.douban_avg}</small></span>
    </div>
    <div class="gap-cols">
      <div>
        <div class="gap-col-t">我觉得更好</div>
        ${d.over.length ? d.over.map(item).join("") : `<div class="rank-empty">—</div>`}
      </div>
      <div>
        <div class="gap-col-t">我觉得更一般</div>
        ${d.under.length ? d.under.map(item).join("") : `<div class="rank-empty">—</div>`}
      </div>
    </div>`;
}

$("#goal-save-btn").addEventListener("click", async () => {
  const v = parseInt($("#goal-input").value);
  if (!v || v < 1) return toast("请输入大于 0 的数字", "err");
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify({ year_goal: v }) });
    toast(`目标已设定：今年看 ${v} 部 🎯`, "ok");
    loadStats();
  } catch (e) {
    toast(e.message, "err");
  }
});

$("#btn-goto-want").addEventListener("click", () => {
  state.mineStatus = "want";
  $$(".sub-tab").forEach((b) => b.classList.toggle("active", b.dataset.status === "want"));
  switchTab("mine");
});

function renderHeatmap(activity) {
  const CELL = 13, GAP = 4, PADL = 30, PADT = 20;
  const COLORS = ["var(--h0)", "var(--h1)", "var(--h2)", "var(--h3)", "var(--h4)"];
  const MONTHS = ["1月","2月","3月","4月","5月","6月","7月","8月","9月","10月","11月","12月"];
  const WDAYS  = ["一","","三","","五","","日"];
  const today = new Date();
  const start = new Date(today);
  start.setHours(0, 0, 0, 0);
  start.setDate(start.getDate() - 364);
  start.setDate(start.getDate() - (start.getDay() + 6) % 7); // 对齐到周一

  let d = new Date(start), rects = "", monthTags = "";
  let curMonth = -1, lastX = -99;
  const keyOf = (dt) => `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
  let w = 0;
  while (d <= today) {
    for (let dow = 0; dow < 7 && d <= today; dow++) {
      const key = keyOf(d);
      const cnt = activity[key] || 0;
      const lvl = cnt === 0 ? 0 : cnt === 1 ? 1 : cnt < 3 ? 2 : cnt < 5 ? 3 : 4;
      const tip = cnt ? `${key} · 看完 ${cnt} 部` : `${key} · 无记录`;
      rects += `<rect x="${PADL + w * (CELL + GAP)}" y="${PADT + dow * (CELL + GAP)}"
        width="${CELL}" height="${CELL}" rx="3.5" fill="${COLORS[lvl]}"><title>${tip}</title></rect>`;
      d.setDate(d.getDate() + 1);
    }
    // 月份标签：当行第一天的月份变化时打标
    if (d <= today && d.getMonth() !== curMonth) {
      curMonth = d.getMonth();
      const x = PADL + w * (CELL + GAP);
      if (x - lastX > 26) {
        monthTags += `<text x="${x}" y="12" fill="#9aa1af" font-size="11">${MONTHS[curMonth]}</text>`;
        lastX = x;
      }
    }
    w++;
  }
  const hTotal = PADT + 7 * (CELL + GAP);
  const wTotal = PADL + w * (CELL + GAP);
  const wdayLabels = WDAYS.map((t, i) =>
    t ? `<text x="${PADL - 8}" y="${PADT + i * (CELL + GAP) + CELL - 2}" fill="#9aa1af" font-size="10" text-anchor="end">${t}</text>` : ""
  ).join("");
  $("#heatmap").innerHTML =
    `<svg width="${wTotal}" height="${hTotal}">${monthTags}${wdayLabels}${rects}</svg>`;

  // 底部汇总：最长连续观看
  const dates = Object.keys(activity).sort();
  let streak = 0, best = 0, prev = null;
  for (const s of dates) {
    const dt = new Date(s + "T00:00:00");
    if (prev && (dt - prev) === 86400000) streak++;
    else streak = 1;
    best = Math.max(best, streak);
    prev = dt;
  }
  const total = dates.reduce((a, k) => a + activity[k], 0);
  $("#heat-foot").innerHTML = dates.length
    ? `近一年共 <b>${total}</b> 次观看 · 覆盖 <b>${dates.length}</b> 天${best > 1 ? ` · 最长连续观看 <b>${best}</b> 天 🔥` : ""}`
    : `看完第一部作品后，这里就会亮起来 ✨`;
}

// ---------------- 设置 ----------------
const SERVER_TYPES = [
  { v: "emby", label: "Emby" },
  { v: "jellyfin", label: "Jellyfin" },
  { v: "plex", label: "Plex" },
];

// 地址 ↔ (主机, 端口) 拆分：地址框里可以带 https:// 和子路径，端口单独一栏
function splitUrl(u) {
  const s = (u || "").trim();
  if (!s) return { host: "", port: "" };
  const sm = s.match(/^(https?:\/\/)/i);
  const scheme = sm ? sm[1] : "";
  const rest = sm ? s.slice(scheme.length) : s;
  const m = rest.match(/^(\[[^\]]+\]|[^/:]+)(?::(\d+))?(\/.*)?$/);
  if (!m) return { host: s, port: "" };
  return { host: scheme + m[1] + (m[3] || ""), port: m[2] || "" };
}

function joinUrl(host, port) {
  const h0 = (host || "").trim();
  if (!h0) return "";
  const sm = h0.match(/^(https?:\/\/)/i);
  const scheme = sm ? sm[1] : "";
  const rest = sm ? h0.slice(scheme.length) : h0;
  const m = rest.match(/^(\[[^\]]+\]|[^/:]+)(?::(\d+))?(\/.*)?$/);
  if (!m) return h0.replace(/\/+$/, "");
  const p = (m[2] || (port || "").trim());
  return `${scheme}${m[1]}${p ? ":" + p : ""}${m[3] || ""}`.replace(/\/+$/, "");
}

const DEFAULT_PORT = { emby: "8096", jellyfin: "8096", plex: "32400" };

function msCardHTML(s) {
  const auth = s.auth_mode || (s.username ? "login" : "key");
  const u = splitUrl(s.url);
  const port = s.port || u.port;
  return `
  <div class="ms-card" data-id="${esc(s.id)}" data-auth="${auth}">
    <div class="ms-head">
      <select class="ms-type">
        ${SERVER_TYPES.map((t) => `<option value="${t.v}" ${s.type === t.v ? "selected" : ""}>${t.label}</option>`).join("")}
      </select>
      <input class="ms-name" placeholder="名称" value="${esc(s.name || "")}">
      <span class="ms-test-result"></span>
      <button class="ms-del" type="button">删除</button>
    </div>
    <label>地址 <span class="ms-url-row">
      <input class="ms-host" placeholder="192.168.1.10 或 https://emby.example.com" value="${esc(u.host)}">
      <span class="ms-port-wrap">端口 <input class="ms-port" inputmode="numeric" placeholder="${DEFAULT_PORT[s.type] || "8096"}" value="${esc(port)}"></span>
    </span></label>
    <p class="hint">只填 IP/域名也行，会自动补 http://；端口留空则按协议自动：IP 补 ${DEFAULT_PORT[s.type] || "8096"}，https 域名走 443 不加端口</p>
    <label>认证方式
      <select class="ms-authmode">
        <option value="key" ${auth === "key" ? "selected" : ""}>API Key（自有服，管理员后台生成）</option>
        <option value="login" ${auth === "login" ? "selected" : ""}>账号密码（朋友的服 / 共享服）</option>
      </select>
    </label>
    <div class="ms-mode-key ${auth === "key" ? "" : "hidden"}">
      <label>API Key / Token <input class="ms-key" type="password" autocomplete="new-password" value="${esc(s.api_key || "")}"></label>
    </div>
    <div class="ms-mode-login ${auth === "login" ? "" : "hidden"}">
      <label>用户名 <input class="ms-user" autocomplete="off" value="${esc(s.username || "")}"></label>
      <label>密码 <input class="ms-pwd" type="password" autocomplete="new-password" value="${s.password ? "******" : ""}"></label>
      <p class="hint">用该服务器给你的普通账号登录，登录后自动换取访问凭证（权限=该账号可见的媒体库）</p>
    </div>
    <label>用户 ID（可留空自动获取）<input class="ms-uid" value="${esc(s.user_id || "")}"></label>
    <div style="margin-top:8px"><button class="mini-btn ms-test" type="button">测试连接</button></div>
  </div>`;
}

function renderMsList(servers) {
  $("#ms-list").innerHTML = (servers || []).map(msCardHTML).join("")
    || `<div class="hint" style="margin-bottom:8px">还没有媒体服务器，点下方按钮添加（支持多个 Emby / Jellyfin / Plex）</div>`;
}

function msCardData(card) {
  const host = card.querySelector(".ms-host").value;
  const port = card.querySelector(".ms-port").value.trim();
  const key = card.querySelector(".ms-key").value.trim();
  const user = card.querySelector(".ms-user").value.trim();
  let mode = card.querySelector(".ms-authmode").value;
  // 容错：选了 API Key 但只填了账号 → 按账号密码走
  if (mode === "key" && !key && user) mode = "login";
  return {
    id: card.dataset.id,
    type: card.querySelector(".ms-type").value,
    name: card.querySelector(".ms-name").value.trim(),
    url: joinUrl(host, port),
    port: port,
    api_key: key,
    username: user,
    password: card.querySelector(".ms-pwd").value,
    auth_mode: mode,
    user_id: card.querySelector(".ms-uid").value.trim(),
  };
}

function collectMsList() {
  return Array.from($$("#ms-list .ms-card")).map(msCardData);
}

async function loadSettings() {
  try {
    const cfg = await api("/api/settings");
    state.cfg = cfg;
    $("#set-mp-url").value = cfg.mp_url || "";
    $("#set-mp-ver").value = cfg.mp_api_version || "auto";
    const det = $("#set-mp-ver").selectedOptions[0];
    if (cfg.mp_api_version_detected) {
      det.textContent = det.textContent.replace(/ \(已识别.*?\)|$/, ` (已识别: ${cfg.mp_api_version_detected})`);
    }
    $("#set-mp-user").value = cfg.mp_username || "";
    $("#set-mp-pass").value = cfg.mp_password ? "******" : "";
    $("#set-mp-token").value = cfg.mp_token ? "******" : "";
    renderMsList(cfg.media_servers || []);
    $("#set-auto-sync").value = cfg.auto_sync === false ? "0" : "1";
    $("#set-sync-min").value = cfg.sync_interval_min || 60;
    $("#sync-last").textContent = cfg.sync_last_at
      ? `上次同步：${fmtTime(cfg.sync_last_at)} · ${cfg.sync_last_result || ""}`
      : "还没有同步过，开启后启动 30 秒内会跑第一次";
    $("#set-115-cookie").value = cfg.pan_115_cookie ? "******" : "";
    $("#set-xiaomi-user").value = cfg.xiaomi_user || "";
    $("#set-xiaomi-pass").value = cfg.xiaomi_pass ? "******" : "";
    $("#set-tts-enabled").value = cfg.tts_enabled ? "1" : "0";
    $("#set-leelaa-url").value = cfg.leelaa_url || "";
    $("#set-leelaa-user").value = cfg.leelaa_user || "";
    $("#set-leelaa-pass").value = cfg.leelaa_pass ? "******" : "";
    $("#set-books-dir").value = cfg.books_dir || "/audiobooks";
    $("#set-books-base").value = cfg.books_base_url || "";
    $("#set-books-mode").value = cfg.books_mode || "local";
    const didSel = $("#set-xiaomi-did");
    if (cfg.xiaomi_did && ![...didSel.options].some((o) => o.value === cfg.xiaomi_did)) {
      didSel.insertAdjacentHTML("beforeend",
        `<option value="${esc(cfg.xiaomi_did)}">已保存的设备</option>`);
    }
    didSel.value = cfg.xiaomi_did || "";
    $("#set-title").value = cfg.site_title || "MediaHub";
    $("#set-goal").value = cfg.year_goal || 0;
    $("#site-title").textContent = cfg.site_title || "MediaHub";
    renderAuthState();
    loadHookInfo();
  } catch (e) { toast(e.message, "err"); }
}

// Webhook 地址与最近事件
async function loadHookInfo() {
  try {
    const d = await api("/api/hook/info");
    $("#hook-url").value = `${location.origin}/api/hook/emby?k=${d.key}`;
    $("#voice-url").value = `${location.origin}/api/hook/mpdl?k=${d.key}`;
    $("#hook-last").textContent = d.last_at
      ? `最近收到：${fmtTime(d.last_at)} · ${d.last_event}`
      : "还没有收到过回调（配置好之后随便看一集就会有记录）";
  } catch { /* 忽略 */ }
}

$("#hook-copy").addEventListener("click", async () => {
  const input = $("#hook-url");
  const val = input.value;
  if (!val) return;
  try {
    await navigator.clipboard.writeText(val);
    toast("已复制，粘贴到 Emby 的 Webhook 地址里", "ok");
  } catch {
    input.removeAttribute("readonly");
    input.select();
    try { document.execCommand("copy"); toast("已复制", "ok"); }
    catch { toast("复制失败，请手动选中复制", "err"); }
    input.setAttribute("readonly", "readonly");
  }
});

// ---------------- 语音下载 / 小爱播报 ----------------
$("#voice-copy").addEventListener("click", async () => {
  const input = $("#voice-url");
  const val = input.value;
  if (!val) return;
  try {
    await navigator.clipboard.writeText(val);
    toast("已复制，粘贴给 migpt-next / 快捷指令用", "ok");
  } catch {
    input.removeAttribute("readonly");
    input.select();
    try { document.execCommand("copy"); toast("已复制", "ok"); }
    catch { toast("复制失败，请手动选中复制", "err"); }
    input.setAttribute("readonly", "readonly");
  }
});

async function saveXiaomiQuiet() {
  // 取设备/试播前先把账号悄悄存一次，否则后端读到的是旧配置
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({
      xiaomi_user: $("#set-xiaomi-user").value.trim(),
      xiaomi_pass: $("#set-xiaomi-pass").value.trim() || undefined,
      xiaomi_did: $("#set-xiaomi-did").value || "",
      tts_enabled: $("#set-tts-enabled").value === "1",
      leelaa_url: $("#set-leelaa-url").value.trim(),
      leelaa_user: $("#set-leelaa-user").value.trim(),
      leelaa_pass: $("#set-leelaa-pass").value.trim() || undefined,
    }),
  });
}

async function saveBooksQuiet() {
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({
      books_dir: $("#set-books-dir").value.trim() || "/audiobooks",
      books_base_url: $("#set-books-base").value.trim(),
      books_mode: $("#set-books-mode").value || "local",
    }),
  });
}

$("#books-test-btn")?.addEventListener("click", async () => {
  const btn = $("#books-test-btn");
  const out = $("#books-test-result");
  btn.disabled = true; out.textContent = "扫描中…";
  try {
    await saveBooksQuiet();
    const d = await api("/api/books/status");
    if (d.ok) {
      out.textContent = `✅ ${d.books} 本 / ${d.chapters} 集 · ${d.root}${d.exists ? "" : "（目录不存在！）"}`;
      out.style.color = "var(--ok)";
    } else {
      out.textContent = `❌ ${d.msg || "扫描失败"}`;
      out.style.color = "var(--err)";
    }
  } catch (e) { out.textContent = `❌ ${e.message}`; out.style.color = "var(--err)"; }
  btn.disabled = false;
});

$("#tts-devices-btn").addEventListener("click", async () => {
  const out = $("#set-xiaomi-did");
  if (!$("#set-xiaomi-user").value.trim()) { toast("先填小米账号", "err"); return; }
  try {
    await saveXiaomiQuiet();
    const d = await api("/api/tts/devices");
    out.innerHTML = `<option value="">自动（第一个设备）</option>` +
      (d.devices || []).map((x) =>
        `<option value="${esc(x.deviceID)}">${esc(x.name || x.alias || x.deviceID)}${x.current ? "（当前）" : ""}</option>`
      ).join("");
    toast(`找到 ${(d.devices || []).length} 台设备，已保存账号`, "ok");
  } catch (e) { toast(e.message, "err"); }
});

$("#tts-test-btn").addEventListener("click", async () => {
  const out = $("#tts-test-result");
  out.textContent = "播报中…";
  try {
    await saveXiaomiQuiet();
    const d = await api("/api/tts/test", { method: "POST", body: JSON.stringify({}) });
    out.textContent = d.ok ? "✅ 已从音箱播报" : "❌ 播报失败";
  } catch (e) { out.textContent = `❌ ${e.message}`; }
});

$("#mpdl-status-btn").addEventListener("click", async () => {
  const out = $("#mpdl-status-result");
  try {
    const d = await api("/api/mpdl/status");
    if (!d.downloading.length) {
      out.textContent = d.tts ? "队列空 · 播报已开启" : "队列空 · 播报未开启";
    } else {
      out.textContent = d.downloading
        .map((x) => `《${x.name}》${Math.round(x.progress)}%`).join(" · ");
    }
  } catch (e) { out.textContent = `❌ ${e.message}`; }
});
const SET_PANES = {
  mp: { title: "MoviePilot", sub: "订阅 · 自动下载 · 整理入库" },
  ms: { title: "媒体服务器", sub: "Emby / Jellyfin / Plex · 观看记录与追剧进度" },
  pan: { title: "115 网盘", sub: "资源检查 · 查片源在不在" },
  voice: { title: "语音下载", sub: "对小爱说「帮我订阅XX」· 小爱音箱播报下载进度" },
  book: { title: "有声书", sub: "Leelaa Reader 书库 · 小爱音箱直接播放" },
  auth: { title: "访问密码", sub: "打开本站先输密码 · 内网自用可不开" },
  site: { title: "站点与目标", sub: "站点名称 · 年度观影目标 · 数据位置" },
};

function showSetPane(key, focusSearch) {
  if (!SET_PANES[key]) key = "mp";
  state.setPane = key;
  $$("#set-nav .set-nav-item").forEach((b) =>
    b.classList.toggle("on", b.dataset.pane === key));
  $$(".set-pane").forEach((p) => p.classList.toggle("on", p.dataset.pane === key));
  $("#set-pane-title").textContent = SET_PANES[key].title;
  $("#set-pane-sub").textContent = SET_PANES[key].sub;
  const box = $(".set-panes");
  if (box) box.scrollTop = 0;
  try { localStorage.setItem("mh_set_pane", key); } catch { /* ignore */ }
}

function filterSetNav(kw) {
  kw = (kw || "").trim().toLowerCase();
  let firstHit = null;
  $$("#set-nav .set-nav-item").forEach((b) => {
    const pane = document.querySelector(`.set-pane[data-pane="${b.dataset.pane}"]`);
    const hay = (b.textContent + " " + (pane ? pane.textContent : "")).toLowerCase();
    const hit = !kw || hay.includes(kw);
    b.classList.toggle("hidden", !hit);
    if (hit && !firstHit) firstHit = b;
  });
  $("#set-nav-empty").classList.toggle("hidden", !!firstHit);
  if (kw && firstHit) {
    const cur = document.querySelector("#set-nav .set-nav-item.on");
    if (!cur || cur.classList.contains("hidden")) showSetPane(firstHit.dataset.pane);
  }
}

$("#set-nav").addEventListener("click", (e) => {
  const btn = e.target.closest(".set-nav-item");
  if (btn) showSetPane(btn.dataset.pane);
});

$("#set-search").addEventListener("input", (e) => filterSetNav(e.target.value));
$("#set-search").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.target.value = ""; filterSetNav(""); }
});

$("#ms-add").addEventListener("click", () => {
  const id = `srv_${Date.now() % 1000000}`;
  $("#ms-list").insertAdjacentHTML("beforeend", msCardHTML({ id, type: "emby", name: "" }));
  // 滚到新卡片
  const cards = $$("#ms-list .ms-card");
  cards[cards.length - 1].scrollIntoView({ behavior: "smooth", block: "center" });
});

$("#ms-list").addEventListener("change", (e) => {
  const card = e.target.closest(".ms-card");
  if (!card) return;
  if (e.target.classList.contains("ms-authmode")) {
    const mode = e.target.value;
    card.dataset.auth = mode;
    card.querySelector(".ms-mode-key").classList.toggle("hidden", mode !== "key");
    card.querySelector(".ms-mode-login").classList.toggle("hidden", mode !== "login");
  }
  if (e.target.classList.contains("ms-type")) {
    // Plex 只能用 Token（无账号密码换 token 接口）
    const isPlex = e.target.value === "plex";
    const portEl = card.querySelector(".ms-port");
    if (portEl) portEl.placeholder = DEFAULT_PORT[e.target.value] || "8096";
    const authSel = card.querySelector(".ms-authmode");
    const loginOpt = authSel.querySelector('option[value="login"]');
    loginOpt.disabled = isPlex;
    loginOpt.textContent = isPlex
      ? "账号密码（Plex 不支持，请用 Token）"
      : "账号密码（朋友的服 / 共享服）";
    if (isPlex && authSel.value === "login") {
      authSel.value = "key";
      authSel.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }
});

$("#ms-list").addEventListener("click", async (e) => {
  const card = e.target.closest(".ms-card");
  if (!card) return;
  if (e.target.classList.contains("ms-del")) {
    card.remove();
    return;
  }
  if (e.target.classList.contains("ms-test")) {
    const out = card.querySelector(".ms-test-result");
    out.className = "ms-test-result";
    out.textContent = "测试中…";
    const res = await api("/api/ms/test", {
      method: "POST",
      body: JSON.stringify(msCardData(card)),
    });
    if (res.ok) {
      out.className = "ms-test-result ok";
      out.textContent = `✅ ${res.user}`;
    } else {
      out.className = "ms-test-result err";
      out.textContent = `❌ ${res.error || "失败"}`;
    }
  }
});

$("#test-115-btn").addEventListener("click", async () => {
  const out = $("#test-115-result");
  out.textContent = "测试中…";
  const res = await api("/api/pan115/test", { method: "POST" });
  out.textContent = res.ok ? `✅ ${res.user}` : `❌ ${res.error || "失败"}`;
});

$("#save-settings").addEventListener("click", async () => {
  const list = collectMsList();
  // 保存前先挡掉「填了地址但没凭证」的卡片，免得静默丢失
  const bad = list.find((s) => s.url && !s.api_key && !s.username);
  if (bad) {
    toast(`服务器「${bad.name || bad.url}」还没填 API Key 或账号密码`, "err");
    return;
  }
  try {
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        mp_url: $("#set-mp-url").value.trim(),
        mp_api_version: $("#set-mp-ver").value,
        mp_username: $("#set-mp-user").value.trim(),
        mp_password: $("#set-mp-pass").value.trim(),
        mp_token: $("#set-mp-token").value.trim(),
        media_servers: list,
        pan_115_cookie: $("#set-115-cookie").value.trim(),
        site_title: $("#set-title").value.trim() || "MediaHub",
        year_goal: parseInt($("#set-goal").value) || 0,
        auto_sync: $("#set-auto-sync").value === "1",
        sync_interval_min: parseInt($("#set-sync-min").value) || 60,
        xiaomi_user: $("#set-xiaomi-user").value.trim(),
        xiaomi_pass: $("#set-xiaomi-pass").value.trim() || undefined,
        xiaomi_did: $("#set-xiaomi-did").value || "",
        tts_enabled: $("#set-tts-enabled").value === "1",
        leelaa_url: $("#set-leelaa-url").value.trim(),
        leelaa_user: $("#set-leelaa-user").value.trim(),
        leelaa_pass: $("#set-leelaa-pass").value.trim() || undefined,
        books_dir: $("#set-books-dir").value.trim() || "/audiobooks",
        books_base_url: $("#set-books-base").value.trim(),
        books_mode: $("#set-books-mode").value || "local",
      }),
    });
    toast("设置已保存 ✅", "ok");
  } catch (e) {
    toast(e.message, "err");
  }
});

// ---------------- 豆瓣评分/简介聚合 ----------------
let doubanSeq = 0;
async function loadDouban(m) {
  const box = $("#m-douban");
  const seq = ++doubanSeq;
  if (!m.title) return;
  try {
    const d = await api(`/api/douban/info?title=${encodeURIComponent(m.title)}` +
      `&year=${encodeURIComponent(m.year || "")}&media_type=${encodeURIComponent(m.media_type || "")}`);
    if (seq !== doubanSeq || !d.ok) return;  // 弹窗已切换或未命中
    if (!m.douban_id && d.douban_id) { state.current.douban_id = d.douban_id; m.douban_id = d.douban_id; }
    if (d.douban_summary && !$("#m-overview").textContent.includes(d.douban_summary.slice(0, 30))) {
      // 豆瓣简介通常更详细，仅在原简介很短时替换
      if ((m.overview || "").length < 60) $("#m-overview").textContent = d.douban_summary;
    }
    const stars = d.douban_rating ? (Math.round(d.douban_rating) / 2).toFixed(1) : "";
    box.innerHTML = `
      <div class="db-head">
        ${d.douban_rating ? `<span class="db-score">豆瓣 <b>${d.douban_rating}</b>${d.votes ? `<small> ${d.votes} 人评过</small>` : ""}</span>` : ""}
        ${stars ? `<span class="db-stars">★ ${stars}</span>` : ""}
        ${d.genres ? `<span class="db-genres">${esc(String(d.genres).split(",").slice(0, 4).join(" / "))}</span>` : ""}
        ${d.runtime ? `<span class="db-runtime">${d.runtime} 分钟</span>` : ""}
        ${d.douban_id ? `<a class="db-link" href="https://movie.douban.com/subject/${d.douban_id}/" target="_blank">豆瓣页面 ↗</a>` : ""}
      </div>
      ${d.directors ? `<div class="db-row"><b>导演</b>${esc(d.directors)}</div>` : ""}
      ${d.actors ? `<div class="db-row"><b>主演</b>${esc(String(d.actors).split(",").slice(0, 4).join(" / "))}</div>` : ""}`;
    box.classList.remove("hidden");
  } catch { /* 静默失败：豆瓣区不显示 */ }
}

// ---------------- 片单 ----------------
let currentListId = null;

async function renderListsView(listId = null) {
  currentListId = listId;
  const box = $("#lists-view");
  box.classList.remove("hidden");
  $("#records-grid").classList.add("hidden");
  $("#records-empty").classList.add("hidden");
  if (!listId) {
    const lists = await api("/api/lists");
    box.innerHTML = `
      <div class="lists-head">
        <h3 class="section-title">我的片单</h3>
        <span style="flex:1"></span>
        <input id="new-list-name" placeholder="新片单名称，如：周末爽片" style="max-width:220px">
        <button class="btn primary" id="create-list">＋ 新建片单</button>
      </div>
      ${lists.length ? `<div class="grid lists-grid">${lists.map((l) => `
        <div class="card list-card" data-id="${l.id}">
          <div class="list-posters">
            ${l.posters.length ? l.posters.map((p) => `<img src="${esc(p)}" loading="lazy">`).join("")
              : `<div class="list-noimg">📋</div>`}
          </div>
          <div class="list-info">
            <div class="t">${esc(l.name)}</div>
            <div class="year">${l.count} 部作品</div>
          </div>
          <button class="list-del" data-id="${l.id}" title="删除片单">🗑</button>
        </div>`).join("")}</div>`
      : `<div class="empty">还没有片单 · 输入名称创建一个吧（如"周末爽片"）</div>`}`;
    return;
  }
  // 片单详情
  const l = await api(`/api/lists/${listId}`);
  box.innerHTML = `
    <div class="lists-head">
      <button class="mini-btn" id="back-lists">‹ 返回片单</button>
      <h3 class="section-title" style="margin:0">${esc(l.name)}</h3>
      <span style="flex:1"></span>
      <span class="hint">${l.items.length} 部作品</span>
    </div>
    ${l.items.length
      ? `<div class="grid">${l.items.map((r) => {
          return cardHTML(r, r.status === "done" || r.status === "want" ? r : null)
            .replace('class="card"', 'class="card listitem"')
            .replace('data-idx="', `data-ukey="${esc(r.ukey)}" data-idx="`);
        }).join("")}</div>`
      : `<div class="empty">片单还是空的 · 打开任意作品详情，点「📋 加入片单」</div>`}`;
}

$("#lists-view").addEventListener("click", async (e) => {
  if (e.target.id === "create-list") {
    const name = $("#new-list-name").value.trim();
    if (!name) return toast("先输入片单名称", "err");
    await api("/api/lists", { method: "POST", body: JSON.stringify({ name }) });
    toast("片单已创建 ✅", "ok");
    renderListsView();
    return;
  }
  if (e.target.id === "back-lists") { renderListsView(); return; }
  const del = e.target.closest(".list-del");
  if (del) {
    if (!confirm("确定删除这个片单吗？（不影响想看/看完记录）")) return;
    await api(`/api/lists/${del.dataset.id}`, { method: "DELETE" });
    renderListsView();
    return;
  }
  const card = e.target.closest(".list-card");
  if (card) { renderListsView(+card.dataset.id); return; }
  const item = e.target.closest(".listitem");
  if (item) {
    const l = await api(`/api/lists/${currentListId}`);
    const r = l.items.find((x) => x.ukey === item.dataset.ukey);
    if (r) openDetail({ ...r }, true);
  }
});

$("#btn-list").addEventListener("click", async () => {
  const m = state.current;
  const pick = $("#list-pick");
  if (!pick.classList.contains("hidden")) { pick.classList.add("hidden"); return; }
  const lists = await api("/api/lists");
  pick.innerHTML = `
    <div class="hint" style="margin-bottom:6px">加入哪个片单？</div>
    ${lists.length ? lists.map((l) => `<button class="mini-btn lp-item" data-id="${l.id}">${esc(l.name)} (${l.count})</button>`).join("")
      : `<div class="hint">还没有片单</div>`}
    <div style="display:flex;gap:6px;margin-top:8px">
      <input id="lp-new-name" placeholder="新片单名称" style="flex:1;min-width:0">
      <button class="mini-btn" id="lp-create">＋ 创建并加入</button>
    </div>`;
  pick.classList.remove("hidden");
  pick.onclick = async (e) => {
    const addItem = async (lid) => {
      await api(`/api/lists/${lid}/items`, {
        method: "POST",
        body: JSON.stringify({
          title: m.title, media_type: m.media_type, year: m.year || "",
          tmdb_id: m.tmdb_id || null, source: m.source || "mp",
          poster: m.poster || "",
          ukey: (state.currentRecord && state.currentRecord.ukey) || "",
        }),
      });
      toast("已加入片单 ✅", "ok");
      pick.classList.add("hidden");
    };
    if (e.target.classList.contains("lp-item")) { await addItem(+e.target.dataset.id); return; }
    if (e.target.id === "lp-create") {
      const name = $("#lp-new-name").value.trim();
      if (!name) return toast("先输入片单名称", "err");
      const res = await api("/api/lists", { method: "POST", body: JSON.stringify({ name }) });
      await addItem(res.id);
    }
  };
});

// ---------------- 首页横滑：继续观看 / 最近入库 ----------------
let homeRowsAt = 0;

function resumeCardHTML(r, i) {
  const left = r.total_min ? Math.max(1, r.total_min - r.pos_min) : 0;
  const sub = [r.sub, left ? `剩 ${left} 分钟` : ""].filter(Boolean).join(" · ");
  const poster = r.poster
    ? `<img src="${esc(r.poster)}" loading="lazy" onerror="this.style.display='none'">`
    : `<div class="no-poster">🎞</div>`;
  const play = r.play_url
    ? `<a class="hc-play" href="${esc(r.play_url)}" target="_blank" rel="noopener" title="直接继续播放">▶</a>`
    : "";
  return `
  <div class="hc hc-resume" data-idx="${i}">
    <div class="hc-poster">${poster}${play}
      <div class="hc-progress"><i style="width:${r.pct}%"></i></div>
      <span class="hc-badge">${r.pct}%</span>
    </div>
    <div class="hc-t">${esc(r.title)}</div>
    <div class="hc-s">${esc(sub || r.srv_name || "")}</div>
  </div>`;
}

function latestCardHTML(r, i) {
  const poster = r.poster
    ? `<img src="${esc(r.poster)}" loading="lazy" onerror="this.style.display='none'">`
    : `<div class="no-poster">🎞</div>`;
  const when = r.days_ago === 0 ? "今天" : r.days_ago ? `${r.days_ago} 天前` : "";
  return `
  <div class="hc hc-latest" data-idx="${i}">
    <div class="hc-poster">${poster}
      ${when ? `<span class="hc-badge">${when}</span>` : ""}
      <span class="hc-type">${esc(r.media_type)}</span>
    </div>
    <div class="hc-t">${esc(r.title)}</div>
    <div class="hc-s">${esc([r.year, r.srv_name].filter(Boolean).join(" · "))}</div>
  </div>`;
}

async function loadHomeRows(force = false) {
  if (!force && Date.now() - homeRowsAt < 60000) return;
  homeRowsAt = Date.now();
  let d;
  try {
    d = await api("/api/home/rows");
  } catch { return; }

  const resume = d.resume || [];
  const latest = d.latest || [];

  const rBox = $("#row-resume"), lBox = $("#row-latest");
  if (resume.length) {
    rBox.classList.remove("hidden");
    $("#row-resume-sub").textContent = `${resume.length} 部没看完`;
    $("#resume-track").innerHTML = resume.map(resumeCardHTML).join("");
    bindRowCards("#resume-track", resume);
  } else {
    rBox.classList.add("hidden");
  }

  if (latest.length) {
    lBox.classList.remove("hidden");
    $("#row-latest-sub").textContent = latest[0].days_ago === 0
      ? "今天有新片" : "媒体库最近添加";
    $("#latest-track").innerHTML = latest.map(latestCardHTML).join("");
    bindRowCards("#latest-track", latest);
  } else {
    lBox.classList.add("hidden");
  }
}

function bindRowCards(sel, rows) {
  $$(sel + " .hc").forEach((card) => {
    card.addEventListener("click", (e) => {
      if (e.target.closest(".hc-play")) return;   // 播放按钮走自己的链接
      const r = rows[+card.dataset.idx];
      if (!r) return;
      openDetail({
        title: r.title, media_type: r.media_type, year: r.year || "",
        poster: r.poster || "", source: "emby",
      }, true);
    });
  });
}

function hideHomeRows() {
  $("#row-resume").classList.add("hidden");
  $("#row-latest").classList.add("hidden");
}

// ---------------- 回忆杀 ----------------
async function loadMemories() {
  try {
    const d = await api("/api/memories");
    const box = $("#memories-banner");
    if (!d.list || !d.list.length) return;
    const m = d.list[0];
    const when = m.mode === "recent" || m.days_ago != null
      ? (m.days_ago > 0 ? `<b>${m.days_ago} 天前</b>` : "<b>今天</b>")
      : `<b>${m.years_ago} 年前的今天</b>`;
    box.innerHTML = `
      <div class="mb-icon">✨</div>
      <div class="mb-text">
        ${when}，你看了《${esc(m.title)}》${m.rating ? ` · 评分 ★${m.rating}` : ""}
        ${m.comment ? `<span class="mb-cmt">“${esc(m.comment)}”</span>` : ""}
        <div class="mb-sub">点击回顾这段记忆 →</div>
      </div>
      <button class="mb-close" id="mb-close">✕</button>`;
    box.classList.remove("hidden");
    box.onclick = (e) => {
      if (e.target.id === "mb-close") { box.classList.add("hidden"); return; }
      openDetail({ title: m.title, media_type: m.media_type, year: m.year,
                   poster: m.poster, source: "emby", rating: m.rating }, true);
    };
  } catch { /* ignore */ }
}

// ---------------- 追剧进度 ----------------
$("#series-progress-btn").addEventListener("click", async () => {
  const box = $("#series-progress");
  if (!box.classList.contains("hidden")) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  box.innerHTML = `<div class="rc-hint">📺 正在获取追剧进度…</div>`;
  try {
    const d = await api("/api/series/progress");
    if (!d.series.length) {
      box.innerHTML = `<div class="rc-hint">还没有在媒体服务器上看过的剧集 · 看几集再来</div>`;
      return;
    }
    // 分列：追更中（MP 订阅中）/ 正在看（没看完）/ 已经看完（库内全看完）
    const classify = (s) => {
      if (s.sub && s.sub.state && s.sub.state.toUpperCase() === "R") return "chasing";
      if (s.lib_total && s.eps >= s.lib_total) return "done";
      return "watching";
    };
    const groups = { chasing: [], watching: [], done: [] };
    d.series.forEach((s) => groups[classify(s)].push(s));
    Object.values(groups).forEach((arr) => arr.sort((a, b) => b.last_date - a.last_date));
    const badge = {
      chasing: `<span class="sp-badge chase">📡 追更中</span>`,
      watching: `<span class="sp-badge watch">👀 在追</span>`,
      done: `<span class="sp-badge fin">✅ 看完</span>`,
    };
    const cardHTML = (s) => {
      const total = (s.sub && s.sub.total_episode) || s.lib_total || 0;
      const pct = total ? Math.min(100, Math.round(s.eps / total * 100)) : null;
      const subSt = s.sub
        ? (s.sub.state && s.sub.state.toUpperCase() === "R" ? " · 订阅中" : " · 订阅已暂停")
        : "";
      const subTotalTxt = s.sub && s.sub.total_episode ? `（订阅共 ${s.sub.total_episode} 集）` : "";
      const libTxt = s.lib_total
        ? ` · 已下载 ${s.lib_total} 集${s.lib_latest_episode ? `（更新至 S${String(s.lib_latest_season || 1).padStart(2, "0")}E${String(s.lib_latest_episode).padStart(2, "0")}）` : ""}${subTotalTxt}`
        : subTotalTxt;
      const gap = total && s.eps < total ? ` · 还差 ${total - s.eps} 集` : "";
      const when = s.days_ago === 0 ? "今天" :
        s.days_ago === 1 ? "昨天" :
        s.days_ago != null ? `${s.days_ago} 天前` : "";
      const nextEp = s.last_season && s.last_episode
        ? `S${String(s.last_season).padStart(2, "0")}E${String(s.last_episode + 1).padStart(2, "0")}` : "";
      const cls = classify(s);
      return `
      <div class="sp-card ${cls}" data-title="${esc(s.title)}" data-year="${esc(s.year || "")}"
           data-poster="${esc(s.poster || "")}">
        <img class="sp-poster sp-open" loading="lazy" src="${esc(s.poster || "")}" alt=""
             onerror="this.style.visibility='hidden'">
        <div class="sp-body">
          <div class="sp-line1">
            <b class="sp-open" title="查看详情">${esc(s.title)}</b>${s.year ? `<span class="sp-year">${s.year}</span>` : ""} ${badge[cls]}
          </div>
          <div class="sp-meta">
            看到 S${s.last_season || 1}E${s.last_episode || "?"} · 已看 ${s.eps} 集${libTxt}${gap}${subSt}${when ? ` · 上次 ${when}` : ""}
          </div>
          ${pct !== null ? `<div class="sp-bar"><div class="sp-fill" style="width:${pct}%"></div></div>` : ""}
        </div>
        ${s.play_url && cls !== "done" ? `<a class="sp-play" href="${esc(s.play_url)}" target="_blank" rel="noopener">▶ 继续${nextEp ? " " + nextEp : ""}</a>` : ""}
      </div>`;
    };
    const section = (key, icon, label, desc) => {
      const arr = groups[key];
      if (!arr.length) return "";
      return `<div class="sp-section">
        <div class="sp-sec-head"><span class="sp-sec-title">${icon} ${label}</span>
          <span class="sp-sec-count">${arr.length} 部</span>
          ${desc ? `<span class="hint">${desc}</span>` : ""}</div>
        ${arr.map(cardHTML).join("")}
      </div>`;
    };
    box.innerHTML = `<div class="panel">
      ${section("chasing", "📡", "追更中", "MP 正在订阅下载更新")}
      ${section("watching", "👀", "正在看", "看了但还没看完")}
      ${section("done", "✅", "已经看完", "")}
    </div>`;
    // 点海报/标题打开详情
    box.querySelectorAll(".sp-card").forEach((card) => {
      card.querySelectorAll(".sp-open").forEach((el) => {
        el.onclick = () => openDetail({
          title: card.dataset.title, media_type: "电视剧", year: card.dataset.year,
          poster: card.dataset.poster, source: "emby",
        }, true);
      });
    });
  } catch (e) {
    box.innerHTML = `<div class="rc-hint">获取失败：${esc(e.message)}</div>`;
  }
});

// ---------------- 年报分享长图 ----------------
const sharePoster = (p) => {
  if (!p) return "";
  if (p.startsWith("http")) {
    return "/api/img/proxy?url=" + encodeURIComponent(p);
  }
  return p;  // /api/ms/img/... 同源可直接用
};

function loadImage(src, timeout = 8000) {
  return new Promise((resolve) => {
    if (!src) return resolve(null);
    const img = new Image();
    const t = setTimeout(() => resolve(null), timeout);
    img.onload = () => { clearTimeout(t); resolve(img); };
    img.onerror = () => { clearTimeout(t); resolve(null); };
    img.src = src;
  });
}

$("#share-report").addEventListener("click", async () => {
  const d = state.reportData;
  if (!d || !d.total) return toast("这一年还没有观影记录，无法生成", "err");
  toast("正在生成长图，请稍候…", "ok");
  const W = 900;
  const cols = 5, cellW = 148, cellH = 222, gap = 18;
  const rows = Math.ceil(Math.min(10, d.top10.length) / cols);
  const top10H = rows * (cellH + 46) + gap;
  const H = 340 + 150 + 90 + top10H + 160;
  const cv = document.createElement("canvas");
  cv.width = W; cv.height = H;
  const ctx = cv.getContext("2d");
  // 背景
  const bg = ctx.createLinearGradient(0, 0, W, H);
  bg.addColorStop(0, "#141d38"); bg.addColorStop(.5, "#1a2440"); bg.addColorStop(1, "#101a2e");
  ctx.fillStyle = bg; ctx.fillRect(0, 0, W, H);
  const glow = ctx.createRadialGradient(120, 40, 10, 120, 40, 380);
  glow.addColorStop(0, "rgba(108,140,255,.35)"); glow.addColorStop(1, "rgba(108,140,255,0)");
  ctx.fillStyle = glow; ctx.fillRect(0, 0, W, 500);
  const glow2 = ctx.createRadialGradient(W - 80, H - 200, 10, W - 80, H - 200, 400);
  glow2.addColorStop(0, "rgba(56,189,248,.28)"); glow2.addColorStop(1, "rgba(56,189,248,0)");
  ctx.fillStyle = glow2; ctx.fillRect(0, H - 600, W, 600);
  ctx.textAlign = "center";
  const yearTxt = d.all_time
    ? (d.span && d.span[0] !== d.span[1] ? `${d.span[0]} - ${d.span[1]}` : "ALL TIME")
    : String(d.year);
  ctx.fillStyle = "#8fa3d6"; ctx.font = "600 26px 'PingFang SC','Microsoft YaHei',sans-serif";
  ctx.fillText("MediaHub · 年度观影报告", W / 2, 70);
  ctx.fillStyle = "#ffffff"; ctx.font = "800 84px 'PingFang SC','Microsoft YaHei',sans-serif";
  ctx.fillText(yearTxt, W / 2, 175);
  ctx.fillStyle = "#c7d2f0"; ctx.font = "300 20px 'PingFang SC','Microsoft YaHei',sans-serif";
  ctx.fillText(d.total ? RP_SLOGANS[d.total % RP_SLOGANS.length](d.total) : "", W / 2, 225);
  // 大数字
  ctx.fillStyle = "#6c8cff"; ctx.font = "800 120px 'PingFang SC','Microsoft YaHei',sans-serif";
  ctx.fillText(String(d.total), W / 2, 360);
  ctx.fillStyle = "#c7d2f0"; ctx.font = "400 22px 'PingFang SC','Microsoft YaHei',sans-serif";
  ctx.fillText("部作品看完", W / 2, 400);
  // 四项指标
  const stats = [
    [d.active_days, "天有观影记录"], [d.best_streak, "最长连续（天）"],
    [d.avg_rating ?? "–", "平均评分"], [d.total_hours ?? "?", "累计小时"],
  ];
  const sw = W / 4;
  stats.forEach(([v, lbl], i) => {
    const x = sw * i + sw / 2;
    ctx.fillStyle = "#ffffff"; ctx.font = "700 40px 'PingFang SC','Microsoft YaHei',sans-serif";
    ctx.fillText(String(v), x, 480);
    ctx.fillStyle = "#8fa3d6"; ctx.font = "400 17px 'PingFang SC','Microsoft YaHei',sans-serif";
    ctx.fillText(lbl, x, 512);
  });
  // 十佳海报墙
  let y0 = 570;
  ctx.fillStyle = "#c7d2f0"; ctx.font = "600 24px 'PingFang SC','Microsoft YaHei',sans-serif";
  ctx.fillText("🏆 年度十佳", W / 2, y0 - 20);
  const imgs = await Promise.all(d.top10.slice(0, 10).map((t) => loadImage(sharePoster(t.poster))));
  for (let i = 0; i < Math.min(10, d.top10.length); i++) {
    const r = i % cols, c = Math.floor(i / cols);
    const x = (W - cols * cellW - (cols - 1) * gap) / 2 + r * (cellW + gap);
    const y = y0 + c * (cellH + 46);
    ctx.save();
    ctx.beginPath(); ctx.roundRect(x, y, cellW, cellH, 12); ctx.clip();
    if (imgs[i]) {
      const s = Math.max(cellW / imgs[i].width, cellH / imgs[i].height);
      ctx.drawImage(imgs[i], x + (cellW - imgs[i].width * s) / 2,
                    y + (cellH - imgs[i].height * s) / 2, imgs[i].width * s, imgs[i].height * s);
    } else {
      ctx.fillStyle = "#223054"; ctx.fillRect(x, y, cellW, cellH);
      ctx.fillStyle = "#8fa3d6"; ctx.font = "48px sans-serif"; ctx.fillText("🎞", x + cellW / 2, y + cellH / 2 + 16);
    }
    ctx.restore();
    // 排名角标
    ctx.fillStyle = "#6c8cff";
    ctx.beginPath(); ctx.roundRect(x + 6, y + 6, 30, 30, 8); ctx.fill();
    ctx.fillStyle = "#fff"; ctx.font = "700 17px sans-serif"; ctx.textAlign = "center";
    ctx.fillText(String(i + 1), x + 21, y + 27);
    // 标题
    ctx.fillStyle = "#e6ecff"; ctx.font = "500 14px 'PingFang SC','Microsoft YaHei',sans-serif";
    const title = d.top10[i].title.length > 11 ? d.top10[i].title.slice(0, 11) + "…" : d.top10[i].title;
    ctx.fillText(title, x + cellW / 2, y + cellH + 24);
    ctx.fillStyle = "#f5c542"; ctx.font = "600 14px sans-serif";
    ctx.fillText("★ " + d.top10[i].rating, x + cellW / 2, y + cellH + 42);
  }
  // 页脚
  ctx.fillStyle = "#8fa3d6"; ctx.font = "400 16px 'PingFang SC','Microsoft YaHei',sans-serif";
  const siteName = $("#site-title").textContent || "MediaHub";
  ctx.fillText(`${siteName} · 生成于 ${new Date().toLocaleDateString("zh-CN")}`, W / 2, H - 36);
  // 展示
  const dataUrl = cv.toDataURL("image/png");
  $("#share-img").src = dataUrl;
  $("#share-dl").href = dataUrl;
  $("#share-dl").download = `观影报告-${yearTxt}.png`;
  $("#share-modal").classList.remove("hidden");
});
$("#share-close").addEventListener("click", () => $("#share-modal").classList.add("hidden"));
$("#share-modal").addEventListener("click", (e) => {
  if (e.target === $("#share-modal")) $("#share-modal").classList.add("hidden");
});

// ---------------- 年度报告 ----------------
const RP_SLOGANS = [
  (n) => `这一年，你把 ${n} 个故事看进了心里`,
  (n) => `${n} 部作品，是你这一年的光影足迹`,
  (n) => `灯一关，就是 ${n} 段新的人生`,
  (n) => `这一年有 ${n} 次，你选择了看电影`,
];

async function loadReport(year) {
  let d;
  try {
    d = await api(`/api/report${year !== undefined && year !== null ? "?year=" + year : ""}`);
  } catch (e) {
    toast(e.message, "err");
    return;
  }
  state.reportYear = d.year;
  state.reportData = d;
  renderReportYears(d);
  // Hero
  if (d.all_time) {
    $("#rp-year").textContent = d.span && d.span[0] !== d.span[1]
      ? `ALL TIME · ${d.span[0]} – ${d.span[1]}` : "ALL TIME · 全部时间";
  } else {
    $("#rp-year").textContent = d.year + " · 年度观影报告";
  }
  $("#rp-total").textContent = d.total;
  $("#rp-slogan").textContent = d.total
    ? RP_SLOGANS[d.total % RP_SLOGANS.length](d.total)
    : "这一年还没有留下观影记录，快去点亮它";
  // 四张卡
  $("#rp-active-days").textContent = d.active_days;
  $("#rp-streak").textContent = d.best_streak;
  $("#rp-avg").textContent = d.avg_rating ?? "–";
  $("#rp-rated-count").textContent = d.rated_count;
  $("#rp-best-month").textContent = d.best_month ? d.best_month.month + "月" : "–";
  $("#rp-best-month-count").textContent = d.best_month ? d.best_month.count : 0;
  // 月度趋势
  const maxM = Math.max(1, ...d.monthly);
  $("#rp-months").innerHTML = d.monthly.map((v, i) => `
    <div class="m">
      <div class="mv">${v || ""}</div>
      <div class="bar ${v ? "" : "empty"}" style="height:${Math.max(3, v / maxM * 100)}%"></div>
      <div class="ml">${i + 1}</div>
    </div>`).join("");
  // 分类占比
  const types = Object.entries(d.by_type || {}).sort((a, b) => b[1] - a[1]);
  $("#rp-types").innerHTML = types.length
    ? types.map(([k, v]) => `
      <div class="t-row">
        <span class="t-name">${esc(k)}</span>
        <div class="t-barwrap"><div class="t-bar" style="width:${v / d.total * 100}%"></div></div>
        <span class="t-count">${v}</span>
      </div>`).join("")
    : `<div class="rp-empty">暂无数据</div>`;
  // 年度十佳
  $("#rp-top10").innerHTML = d.top10.length
    ? d.top10.map((r, i) => `
      <div class="p-item">
        ${r.poster ? `<img src="${esc(r.poster)}" loading="lazy">` : `<div class="p-no">🎞</div>`}
        <span class="p-rank">${i + 1}</span>
        <div class="p-title" title="${esc(r.title)}">${esc(r.title)}</div>
        <div class="p-star">★ ${r.rating}</div>
      </div>`).join("")
    : `<div class="rp-empty">这一年的作品看完并打分后，就会出现在这里</div>`;
  // 评分分布
  const maxR = Math.max(1, ...Object.values(d.rating_dist));
  $("#rp-rdist").innerHTML = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1].map((sc) => {
    const v = d.rating_dist[String(sc)] || 0;
    return `
    <div class="d-row">
      <span class="d-score">${sc} 分</span>
      <div class="d-barwrap"><div class="d-bar" style="width:${v / maxR * 100}%"></div></div>
      <span class="d-count">${v}</span>
    </div>`;
  }).join("");
  // 评论墙
  $("#rp-comments").innerHTML = d.comments.length
    ? d.comments.map((c) => `
      <div class="c-item">
        <div class="c-head">
          <span class="c-title">${esc(c.title)}</span>
          <span class="c-stars">★${c.rating ?? "–"}</span>
        </div>
        <div class="c-text">${esc(c.comment)}</div>
        ${(c.tags || "") ? `<div class="c-tags">${c.tags.split(",").filter(Boolean).map((t) => `<i>${esc(t.trim())}</i>`).join("")}</div>` : ""}
      </div>`).join("")
    : `<div class="rp-empty">今年还没有写过评论，写下第一条吧 ✍️</div>`;
}

function renderReportYears(d) {
  const cur = Math.max(...d.years);
  const min = Math.min(d.min_year ?? cur, cur);
  const opts = [];
  for (let y = cur; y >= min; y--) opts.push(`<option value="${y}">${y} 年</option>`);
  const sel = $("#rp-year-select");
  sel.innerHTML = opts.join("");
  sel.value = d.all_time ? String(cur) : String(d.year);
  const prev = $("#rp-prev"), next = $("#rp-next");
  prev.disabled = d.all_time || d.year <= min;
  next.disabled = d.all_time || d.year >= cur;
  $("#rp-all").classList.toggle("on", !!d.all_time);
}

$("#rp-prev").addEventListener("click", () => {
  if (!state.reportYear) return;
  loadReport(Math.max(Math.min(...[state.reportYear - 1]), 1970));
});
$("#rp-next").addEventListener("click", () => {
  if (!state.reportYear) return;
  loadReport(state.reportYear + 1);
});
$("#rp-year-select").addEventListener("change", (e) => loadReport(+e.target.value));
$("#rp-all").addEventListener("click", () => loadReport(0));
// 键盘左右方向键翻年
document.addEventListener("keydown", (e) => {
  if ($("#report-inside").classList.contains("hidden")) return;
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  if (e.key === "ArrowLeft") $("#rp-prev").click();
  if (e.key === "ArrowRight") $("#rp-next").click();
});

// ---------------- 访问密码 ----------------
const AUTH = { enabled: false, logged_in: true, site_title: "MediaHub", days: 30 };

function showLogin(msg = "") {
  $("#login-title").textContent = AUTH.site_title || "MediaHub";
  $("#login-screen").classList.remove("hidden");
  $("#login-err").textContent = msg;
  document.body.style.overflow = "hidden";
  setTimeout(() => $("#login-pass") && $("#login-pass").focus(), 60);
}

function hideLogin() {
  $("#login-screen").classList.add("hidden");
  document.body.style.overflow = "";
  $("#login-pass").value = "";
  $("#login-err").textContent = "";
}

async function loadAuthStatus() {
  try {
    const d = await fetch("/api/auth/status").then((r) => r.json());
    Object.assign(AUTH, d);
  } catch { /* 拿不到状态按未开启处理 */ }
  return AUTH;
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pw = $("#login-pass").value;
  const btn = $("#login-btn");
  if (!pw) { $("#login-err").textContent = "请输入密码"; return; }
  btn.disabled = true;
  btn.textContent = "验证中…";
  try {
    await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ password: pw, remember: $("#login-remember").checked }),
    });
    AUTH.logged_in = true;
    hideLogin();
    startApp();
  } catch (err) {
    $("#login-err").textContent = err.message;
    const card = $(".login-card");
    card.classList.add("shake");
    setTimeout(() => card.classList.remove("shake"), 320);
    $("#login-pass").select();
  } finally {
    btn.disabled = false;
    btn.textContent = "进入";
  }
});

// 设置页 · 访问密码状态
async function renderAuthState() {
  await loadAuthStatus();
  const el = $("#auth-state");
  if (!el) return;
  el.innerHTML = AUTH.enabled
    ? `<span class="on">● 已开启</span> · 打开本站需输入密码，${AUTH.days} 天内免重复输入`
    : `<span>○ 未设置</span> · 现在任何人打开本站都能看（只在内网自用可以不开）`;
  $("#set-auth-days").value = AUTH.days || 30;
  $("#auth-off").classList.toggle("hidden", !AUTH.enabled);
  $("#auth-logout").classList.toggle("hidden", !AUTH.enabled);
}

$("#auth-save").addEventListener("click", async () => {
  const pw = $("#set-auth-pass").value;
  const days = +$("#set-auth-days").value || 30;
  if (!pw && !AUTH.enabled) { toast("请先设置一个密码", "err"); return; }
  if (pw && pw.length < 4) { toast("密码至少 4 位", "err"); return; }
  try {
    await api("/api/auth/setup", {
      method: "POST",
      body: JSON.stringify({ password: pw, enabled: true, days }),
    });
    $("#set-auth-pass").value = "";
    AUTH.enabled = true;
    AUTH.logged_in = true;
    toast(pw ? "密码已设置，下次打开要输入" : "已保存", "ok");
    renderAuthState();
  } catch (e) { toast(e.message, "err"); }
});

$("#auth-off").addEventListener("click", async () => {
  if (!confirm("关闭后任何人打开本站都不需要密码，确定？")) return;
  try {
    await api("/api/auth/setup", { method: "POST", body: JSON.stringify({ enabled: false }) });
    AUTH.enabled = false;
    toast("已关闭访问密码", "ok");
    renderAuthState();
  } catch (e) { toast(e.message, "err"); }
});

$("#auth-logout").addEventListener("click", async () => {
  if (!confirm("退出后需要重新输入密码，确定？")) return;
  try {
    await api("/api/auth/logout", { method: "POST" });
    location.reload();
  } catch (e) { toast(e.message, "err"); }
});

// ---------------- 启动 ----------------
let started = false;
function startApp() {
  if (started) return;
  started = true;
  renderStars();
  api("/api/settings")
    .then((cfg) => {
      const title = cfg.site_title || "MediaHub";
      $("#site-title").textContent = title;
      document.title = `${title} · 我的影音中心`;
    })
    .catch(() => {});
  loadBrowse();
  loadMemories();
  loadHomeRows();
  // 恢复上次停留的设置分区（仅作用于设置页，不影响首屏）
  try {
    const lastPane = localStorage.getItem("mh_set_pane");
    if (lastPane && SET_PANES[lastPane]) showSetPane(lastPane);
  } catch { /* 忽略隐私模式下的存储异常 */ }
}

// ---------------- 有声书（Leelaa → 小爱音箱） ----------------
const BK = { page: 1, query: "", total: 0, cur: null };

function bkCardHTML(b) {
  const p = Math.round((Number(b.progress) || 0) * 100);
  const sub = [b.author, b.narrator ? `演播 ${b.narrator}` : ""].filter(Boolean).join(" · ");
  return `<article class="bk-card" data-id="${b.id}">
    <div class="bk-cover">${b.cover
      ? `<img loading="lazy" src="${b.cover}" alt="">`
      : `<span class="bk-noimg">🎧</span>`}
      ${p > 0 ? `<span class="bk-prog"><i style="width:${p}%"></i></span>` : ""}
    </div>
    <div class="bk-card-t">${esc(b.title)}</div>
    <div class="bk-card-sub">${esc(sub || (b.chapters ? b.chapters + " 章" : ""))}</div>
  </article>`;
}

async function loadBooks(reset = true) {
  if (reset) { BK.page = 1; BK.query = ($("#bk-q")?.value || "").trim(); }
  const grid = $("#book-grid");
  if (reset) grid.innerHTML = `<div class="bk-skel">加载书库…</div>`;
  try {
    const d = await api(`/api/books?q=${encodeURIComponent(BK.query)}&page=${BK.page}&limit=60`);
    BK.total = d.total || 0;
    const html = (d.items || []).map(bkCardHTML).join("");
    if (reset) grid.innerHTML = html;
    else grid.insertAdjacentHTML("beforeend", html);
    $("#bk-empty").classList.toggle("hidden", (d.items || []).length > 0 || BK.page > 1);
    if (!(d.items || []).length && reset) {
      $("#bk-empty").innerHTML = BK.query
        ? `没搜到「${esc(BK.query)}」，换个词试试。`
        : `书库是空的。去 <b>⚙️ 设置 → 🎧 有声书</b> 检查目录路径，或点一下「扫描检查」。`;
    }
    $("#bk-more").classList.toggle("hidden", grid.children.length >= BK.total);
    $("#bk-more").onclick = () => { BK.page += 1; loadBooks(false); };
  } catch (e) {
    grid.innerHTML = "";
    $("#bk-empty").classList.remove("hidden");
    $("#bk-empty").innerHTML = `读取书库失败：${esc(e.message)}<br>去 <b>⚙️ 设置 → 🎧 有声书</b> 检查连接。`;
  }
  if (reset) loadBookContinue();
}

async function loadBookContinue() {
  const wrap = $("#bk-continue-wrap");
  const box = $("#bk-continue");
  try {
    const d = await api("/api/books/recent");
    const items = d.items || [];
    if (!items.length) { wrap.classList.add("hidden"); return; }
    wrap.classList.remove("hidden");
    box.innerHTML = items.map((h) => `
      <article class="bk-cont" data-book="${h.id}" data-chapter="${h.chapter_id || ""}">
        <div class="bk-cont-cover">${h.cover ? `<img loading="lazy" src="${h.cover}" alt="">` : `<span class="bk-noimg">🎧</span>`}</div>
        <div class="bk-cont-t">${esc(h.title)}</div>
        <div class="bk-cont-sub">${esc(h.chapter_title || "")}</div>
        <button class="bk-play" data-play-book="${h.id}" data-play-chapter="${h.chapter_id || ""}" type="button">▶</button>
      </article>`).join("");
  } catch {
    wrap.classList.add("hidden");
  }
}

async function bkPlay(bookId, chapterId, btn) {
  if (!chapterId) { toast("这本书还没有章节", "err"); return; }
  const old = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  try {
    const d = await api("/api/books/play", {
      method: "POST",
      body: JSON.stringify({ book_id: Number(bookId), chapter_id: Number(chapterId) }),
    });
    toast(`已推送到音箱：${d.title || ""} ${d.chapter || ""}`, "ok");
  } catch (e) {
    toast(e.message, "err");
  }
  if (btn) { btn.disabled = false; btn.textContent = old || "▶"; }
}

async function openBook(id) {
  const m = $("#book-modal");
  $("#bk-title").textContent = "加载中…";
  $("#bk-meta").textContent = "";
  $("#bk-desc").textContent = "";
  $("#bk-cover").removeAttribute("src");
  $("#bk-chapters").innerHTML = `<div class="bk-skel">读取章节…</div>`;
  m.classList.remove("hidden");
  try {
    const d = await api(`/api/books/${id}`);
    BK.cur = d;
    const b = d.book || {};
    $("#bk-title").textContent = b.title || "";
    $("#bk-meta").textContent = [b.author, b.narrator ? `演播 ${b.narrator}` : "",
      (d.chapters || []).length ? `${d.chapters.length} 章` : ""].filter(Boolean).join(" · ");
    $("#bk-desc").textContent = b.desc || "";
    if (b.cover) $("#bk-cover").src = b.cover;
    else $("#bk-cover").removeAttribute("src");
    if (!state.cfg) {
      try { state.cfg = await api("/api/settings"); } catch { /* ignore */ }
    }
    const localMode = (state.cfg?.books_mode || "local") !== "leelaa";
    const openEl = $("#bk-open");
    if (openEl) {
      if (localMode || !state.cfg?.leelaa_url) openEl.classList.add("hidden");
      else { openEl.classList.remove("hidden"); openEl.href = state.cfg.leelaa_url + "/books/" + id; }
    }
    const prog = d.progress || {};
    const resumeCh = (d.chapters || []).find((c) => String(c.id) === String(prog.chapter_id));
    $("#bk-resume-btn").textContent = resumeCh
      ? `▶ 继续听（${resumeCh.title}）` : "▶ 从第一章开始";
    $("#bk-resume-btn").dataset.chapter = resumeCh ? resumeCh.id : ((d.chapters[0] || {}).id || "");
    $("#bk-first-btn").dataset.chapter = ((d.chapters[0] || {}).id || "");
    $("#bk-hint").textContent = resumeCh && prog.last_position
      ? `上次听到 ${resumeCh.title} 第 ${Math.floor(prog.last_position / 60)} 分钟`
      : "";
    $("#bk-chapter-count").textContent = `${(d.chapters || []).length} 章`;
    $("#bk-chapters").innerHTML = (d.chapters || []).map((c) => {
      const on = String(c.id) === String(prog.chapter_id);
      const dur = c.duration ? `${Math.round(c.duration / 60)} 分钟` : "";
      return `<div class="bk-ch-row${on ? " on" : ""}" data-chapter="${c.id}">
        <span class="bk-ch-i">${c.index}</span>
        <span class="bk-ch-t">${esc(c.title)}</span>
        <span class="bk-ch-d">${dur}</span>
        <button class="mini-btn bk-ch-play" type="button">▶</button>
      </div>`;
    }).join("");
  } catch (e) {
    $("#bk-chapters").innerHTML = `<div class="bk-skel">${esc(e.message)}</div>`;
  }
}

document.addEventListener("click", (e) => {
  const card = e.target.closest("#book-grid .bk-card");
  if (card) { openBook(card.dataset.id); return; }
  const p1 = e.target.closest("[data-play-chapter]");
  if (p1 && e.target.closest("#bk-continue")) {
    e.stopPropagation();
    bkPlay(p1.dataset.playBook, p1.dataset.playChapter, p1);
    return;
  }
  const cont = e.target.closest("#bk-continue .bk-cont");
  if (cont && cont.dataset.chapter) { bkPlay(cont.dataset.book, cont.dataset.chapter, null); return; }
  const row = e.target.closest("#bk-chapters .bk-ch-row");
  if (row && BK.cur) { bkPlay(BK.cur.book.id, row.dataset.chapter, row.querySelector(".bk-ch-play")); }
});

$("#book-close").addEventListener("click", () => $("#book-modal").classList.add("hidden"));
$("#book-modal").addEventListener("click", (e) => {
  if (e.target.id === "book-modal") $("#book-modal").classList.add("hidden");
});
$("#bk-resume-btn").addEventListener("click", (e) => bkPlay(BK.cur?.book?.id, e.currentTarget.dataset.chapter, e.currentTarget));
$("#bk-first-btn").addEventListener("click", (e) => bkPlay(BK.cur?.book?.id, e.currentTarget.dataset.chapter, e.currentTarget));
$("#bk-search-btn").addEventListener("click", () => loadBooks(true));
$("#bk-q").addEventListener("keydown", (e) => { if (e.key === "Enter") loadBooks(true); });

// ---------------- PWA：Service Worker ----------------
(function registerSW() {
  if (!("serviceWorker" in navigator)) return;
  const secure = location.protocol === "https:"
    || location.hostname === "localhost" || location.hostname === "127.0.0.1";
  if (!secure) return;      // 局域网 http 下浏览器不允许注册，静默跳过
  navigator.serviceWorker.register("/sw.js").catch(() => {});
})();

(async function init() {
  await loadAuthStatus();
  if (AUTH.enabled && !AUTH.logged_in) {
    showLogin();
    return;
  }
  startApp();
  // 支持 PWA 快捷方式：/?tab=stats
  const t = new URLSearchParams(location.search).get("tab");
  if (["browse", "books", "mine", "stats", "settings"].includes(t)) switchTab(t);
})();

// ---------------- 全局易用性增强 ----------------
// 回到顶部
const backTop = $("#back-top");
window.addEventListener("scroll", () => {
  backTop.classList.toggle("show", window.scrollY > 600);
}, { passive: true });
backTop.addEventListener("click", () =>
  window.scrollTo({ top: 0, behavior: "smooth" }));

// 快捷键：/ 聚焦搜索；Esc 关闭所有弹窗
document.addEventListener("keydown", (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
  if (e.key === "/" && !typing) {
    e.preventDefault();
    switchTab("browse");
    $("#search-input").focus();
    $("#search-input").select();
    return;
  }
  if (e.key === "Escape") {
    ["#modal", "#book-modal", "#share-modal"].forEach((s) => $(s)?.classList.add("hidden"));
  }
});

// ---------------- 主题配色 ----------------
const THEMES = ["night", "green", "pink", "orange", "purple"];
function applyTheme(name) {
  if (!THEMES.includes(name)) name = "night";
  if (name === "night") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", name);
  try { localStorage.setItem("mh-theme", name); } catch { /* ignore */ }
  $$("#theme-pop .swatch").forEach((b) =>
    b.classList.toggle("on", b.dataset.theme === name));
}
(function initTheme() {
  let saved = "night";
  try { saved = localStorage.getItem("mh-theme") || "night"; } catch { /* ignore */ }
  applyTheme(saved);
})();
$("#theme-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  $("#theme-pop").classList.toggle("hidden");
});
$$("#theme-pop .swatch").forEach((b) =>
  b.addEventListener("click", () => { applyTheme(b.dataset.theme); }));
document.addEventListener("click", (e) => {
  if (!e.target.closest(".theme-wrap")) $("#theme-pop").classList.add("hidden");
});
