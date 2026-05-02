const state = {
  archives: [],
  selectedTag: "",
  selectedReleaseId: null,
  activeDownloadTaskId: null,
  me: { username: "", token_present: false, repo: "" },
};

const SECTION_IDS = ["uploadCard", "archivesCard", "downloadCard", "tasksCard"];

// ── Network ───────────────────────────────────────────────────────────────────

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...options,
  });
  if (response.status === 401) {
    window.location.href = "/login";
    throw new Error("Login required");
  }
  let payload;
  try {
    payload = await response.json();
  } catch (_) {
    payload = {};
  }
  if (!response.ok) {
    throw new Error(payload.error || `Request failed (${response.status})`);
  }
  return payload;
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatDate(iso) {
  if (!iso) return "";
  try { return new Date(iso).toLocaleString(); } catch (_) { return iso; }
}

function show(id, visible) {
  const node = document.getElementById(id);
  if (node) node.style.display = visible ? "" : "none";
}

function flash(message) {
  const badge = document.getElementById("repoBadge");
  if (!badge) return;
  const previous = badge.innerHTML;
  badge.innerHTML = `<strong>${escapeHtml(message)}</strong>`;
  setTimeout(() => { badge.innerHTML = previous; }, 2500);
}

// ── User / credentials ────────────────────────────────────────────────────────

function applyMe(me) {
  state.me = me;
  const repoBadge = document.getElementById("repoBadge");
  const configured = Boolean(me.token_present && me.repo);

  if (configured) {
    repoBadge.innerHTML = `Repo: <strong>${escapeHtml(me.repo)}</strong>`;
  } else {
    repoBadge.textContent = "GitHub repo not configured";
  }

  // Show app sections only when credentials exist; show creds card otherwise.
  show("credsCard", !configured);
  SECTION_IDS.forEach((id) => show(id, configured));
  document.getElementById("configureCredsButton").textContent = configured ? "Reconfigure" : "Configure";
}

async function loadMe() {
  const me = await fetchJson("/api/me");
  applyMe(me);
  return me;
}

async function submitCreds(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  try {
    const result = await fetchJson("/api/me/credentials", {
      method: "POST",
      body: JSON.stringify({
        token: data.get("token"),
        repo: data.get("repo"),
        create_repo: data.get("create_repo") === "on",
        private_repo: true,
      }),
    });
    form.reset();
    await loadMe();
    await loadArchives();
    flash(`Linked to ${result.repo} as ${result.login}`);
  } catch (error) {
    alert(error.message);
  }
}

async function clearCreds() {
  if (!confirm("Forget the saved GitHub token for your account? You'll need to re-enter it.")) return;
  try {
    await fetchJson("/api/me/credentials", { method: "DELETE" });
    await loadMe();
  } catch (error) {
    alert(error.message);
  }
}

// ── Archives ──────────────────────────────────────────────────────────────────

function renderArchives(archives) {
  state.archives = archives;
  const tbody = document.getElementById("archivesTableBody");
  if (!archives.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="subtle center">No archives yet — upload one above.</td></tr>';
    return;
  }

  tbody.innerHTML = archives
    .map((archive) => {
      const meta = archive.archive || {};
      const title = escapeHtml(meta.source_name || archive.name || archive.tag || "Archive");
      const count = meta.total_items || archive.asset_count || 0;
      const created = formatDate(meta.created_at || archive.created_at || "");
      const selected = archive.tag === state.selectedTag ? "selected" : "";
      return `
        <tr class="archive-row ${selected}" data-tag="${escapeHtml(archive.tag)}" data-release-id="${archive.release_id}">
          <td>${title}</td>
          <td>${count}</td>
          <td>${escapeHtml(created)}</td>
          <td><code>${escapeHtml(archive.tag)}</code></td>
        </tr>
      `;
    })
    .join("");

  document.querySelectorAll(".archive-row").forEach((row) => {
    row.addEventListener("click", () => {
      state.selectedTag = row.dataset.tag;
      state.selectedReleaseId = Number(row.dataset.releaseId);
      document.getElementById("selectedTag").value = state.selectedTag;
      renderArchives(state.archives);
    });
  });
}

async function loadArchives() {
  if (!state.me.token_present || !state.me.repo) return;
  try {
    const payload = await fetchJson("/api/archives");
    renderArchives(payload.archives || []);
  } catch (error) {
    document.getElementById("archivesTableBody").innerHTML =
      `<tr><td colspan="4" class="subtle center">${escapeHtml(error.message)}</td></tr>`;
  }
}

// ── Tasks ─────────────────────────────────────────────────────────────────────

function renderTasks(tasks) {
  const container = document.getElementById("tasksList");
  if (!tasks.length) {
    container.innerHTML = '<div class="task-card"><div class="subtle">No tasks yet.</div></div>';
    return;
  }

  container.innerHTML = tasks
    .map((task) => {
      const payload = task.payload || {};
      const logs = (task.logs || []).slice(-4).reverse();
      const progressTotal = Number(task.progress_total || 0);
      const progressDone = Number(task.progress_done || 0);

      const label = task.type === "upload"
        ? (payload.upload_origin === "browser-transfer"
            ? `${payload.uploaded_file_count || 0} browser file(s)`
            : (payload.source_path || "Upload"))
        : (payload.tag || payload.archive_id || `release ${payload.release_id || ""}`);

      const result = task.result || {};
      const showDownloadLink = task.type === "download" && task.status === "completed";

      return `
        <div class="task-card">
          <div class="task-head">
            <div class="task-title">${escapeHtml(label)}</div>
            <div class="task-status ${escapeHtml(task.status)}">${escapeHtml(task.status)}</div>
          </div>
          ${progressTotal > 0 ? `<div class="task-progress">${progressDone} / ${progressTotal}</div>` : ""}
          ${result.html_url ? `
            <div class="task-links">
              <a href="${escapeHtml(result.html_url)}" target="_blank" rel="noreferrer">Open Release</a>
              ${result.tag ? `<button type="button" class="use-tag-button" data-tag="${escapeHtml(result.tag)}">Use For Download</button>` : ""}
            </div>
          ` : ""}
          ${showDownloadLink ? `
            <div class="task-links">
              <a href="/api/download-file/${escapeHtml(task.id)}">Save ZIP to Computer</a>
            </div>
          ` : ""}
          ${task.error ? `<div class="task-progress task-error">${escapeHtml(task.error)}</div>` : ""}
          <ul class="task-log">
            ${logs.map((log) => `<li>${escapeHtml(log.message)}</li>`).join("")}
          </ul>
        </div>
      `;
    })
    .join("");

  document.querySelectorAll(".use-tag-button").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedTag = button.dataset.tag;
      document.getElementById("selectedTag").value = state.selectedTag;
      renderArchives(state.archives);
    });
  });

  if (state.activeDownloadTaskId) {
    const active = tasks.find((t) => t.id === state.activeDownloadTaskId);
    if (active && active.status === "completed") {
      const area = document.getElementById("downloadReadyArea");
      const link = document.getElementById("downloadFileLink");
      link.href = `/api/download-file/${state.activeDownloadTaskId}`;
      area.style.display = "";
      state.activeDownloadTaskId = null;
    }
  }
}

async function loadTasks() {
  try {
    const payload = await fetchJson("/api/tasks");
    renderTasks(payload.tasks || []);
  } catch (_) { /* ignore polling errors */ }
}

// ── Upload ────────────────────────────────────────────────────────────────────

function setSelectionSummary(files) {
  const summary = document.getElementById("uploadSelectionSummary");
  if (!files.length) { summary.textContent = "No files selected"; return; }
  const totalBytes = files.reduce((sum, file) => sum + (file.size || 0), 0);
  const first = files[0].webkitRelativePath || files[0].name;
  summary.textContent = files.length === 1
    ? `${first} (${formatBytes(totalBytes)})`
    : `${files.length} files selected — ${formatBytes(totalBytes)}`;
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes; let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[i]}`;
}

async function uploadSelectedFiles(files) {
  if (!files.length) return;
  if (!state.me.token_present || !state.me.repo) {
    alert("Please configure your GitHub credentials first.");
    return;
  }
  setSelectionSummary(files);

  const uploadForm = document.getElementById("uploadForm");
  const formData = new FormData(uploadForm);
  formData.set("encrypt", uploadForm.querySelector('input[name="encrypt"]').checked ? "true" : "false");
  formData.append("retries", "3");
  formData.append("recursive", "true");

  for (const file of files) {
    formData.append("files", file, file.name);
    formData.append("relative_paths", file.webkitRelativePath || file.name);
  }

  const response = await fetch("/api/upload-files", { method: "POST", body: formData, credentials: "same-origin" });
  if (response.status === 401) { window.location.href = "/login"; return; }
  let payload = {}; try { payload = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(payload.error || `Upload failed (${response.status})`);
  await loadTasks();
}

// ── Download ──────────────────────────────────────────────────────────────────

async function submitDownload(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const tag = (data.get("tag") || "").trim();
  if (!tag) { alert("Please select an archive from the list above."); return; }

  document.getElementById("downloadReadyArea").style.display = "none";
  try {
    const result = await fetchJson("/api/download", {
      method: "POST",
      body: JSON.stringify({ tag: tag, workers: Number(data.get("workers")), retries: 3 }),
    });
    state.activeDownloadTaskId = result.task_id;
    await loadTasks();
  } catch (error) {
    alert(error.message);
  }
}

// ── File pickers ──────────────────────────────────────────────────────────────

function setupPickers() {
  const filePicker = document.getElementById("filePicker");
  const folderPicker = document.getElementById("folderPicker");
  document.getElementById("pickFilesButton").addEventListener("click", () => filePicker.click());
  document.getElementById("pickFolderButton").addEventListener("click", () => folderPicker.click());

  filePicker.addEventListener("change", async (event) => {
    try { await uploadSelectedFiles(Array.from(event.target.files || [])); }
    catch (error) { alert(error.message); }
    filePicker.value = "";
  });
  folderPicker.addEventListener("change", async (event) => {
    try { await uploadSelectedFiles(Array.from(event.target.files || [])); }
    catch (error) { alert(error.message); }
    folderPicker.value = "";
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  setupPickers();
  document.getElementById("downloadForm").addEventListener("submit", submitDownload);
  document.getElementById("refreshArchivesButton").addEventListener("click", loadArchives);
  document.getElementById("credsForm").addEventListener("submit", submitCreds);
  document.getElementById("clearCredsButton").addEventListener("click", clearCreds);
  document.getElementById("configureCredsButton").addEventListener("click", () => {
    const card = document.getElementById("credsCard");
    card.style.display = card.style.display === "none" ? "" : "none";
  });

  try {
    await loadMe();
    await loadTasks();
    await loadArchives();
  } catch (error) {
    console.error("Init error:", error);
  }

  setInterval(loadTasks, 2500);
}

window.addEventListener("DOMContentLoaded", init);
