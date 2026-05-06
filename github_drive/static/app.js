/* GitHub Drive — Drive-style frontend behaviour */

const state = {
  archives: [],
  filteredArchives: [],
  archivePage: 0,
  archivePageSize: 24,
  archiveHasMore: false,
  archiveLoading: false,
  searchTerm: "",
  selectedArchive: null,
  selectedArchiveContents: null,
  selectedArchivePath: "",
  activeDownloadTaskId: null,
  me: { username: "", token_present: false, repo: "", repos: [], github_oauth_connected: false },
  toastDismissed: false,
  filters: { type: "all", modified: "any", sort: "newest" },
  viewMode: (typeof localStorage !== "undefined" && localStorage.getItem("gd_view_mode")) || "grid",
  taskPollTimer: null,
  taskPollInFlight: false,
  taskPollHasActive: false,
  taskPollErrorCount: 0,
  refreshedUploadTaskIds: {},
};

const TEXT_PREVIEW_EXTENSIONS = new Set([
  ".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".csv", ".tsv", ".log",
  ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cc", ".cpp", ".h", ".hpp",
  ".go", ".rs", ".rb", ".php", ".sh", ".html", ".css",
]);
const PDF_EXTENSIONS = new Set([".pdf"]);
const TEXT_PREVIEW_MAX_BYTES = 256 * 1024;

const FILTER_OPTIONS = {
  type: [
    { value: "all", label: "All types" },
    { value: "image", label: "Images" },
    { value: "video", label: "Videos" },
    { value: "audio", label: "Audio" },
    { value: "document", label: "Documents" },
    { value: "archive", label: "Archives" },
    { value: "code", label: "Code" },
    { value: "other", label: "Other" },
    { value: "encrypted", label: "Encrypted only" },
  ],
  modified: [
    { value: "any", label: "Any time" },
    { value: "today", label: "Today" },
    { value: "week", label: "Last 7 days" },
    { value: "month", label: "Last 30 days" },
    { value: "year", label: "Last year" },
  ],
  sort: [
    { value: "newest", label: "Newest first" },
    { value: "oldest", label: "Oldest first" },
    { value: "name_asc", label: "Name (A → Z)" },
    { value: "name_desc", label: "Name (Z → A)" },
    { value: "items_desc", label: "Most items" },
    { value: "items_asc", label: "Fewest items" },
  ],
};

const KIND_LABEL = {
  folder: "Folder",
  image: "Images", video: "Video", audio: "Audio",
  document: "Documents", archive: "Archive", code: "Code", other: "Files",
};
const IMAGE_EXTENSIONS = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"]);
const VIDEO_EXTENSIONS = new Set([".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts", ".m2ts", ".wmv", ".flv"]);
const AUDIO_EXTENSIONS = new Set([".mp3", ".flac", ".wav", ".aac", ".ogg", ".opus", ".m4a"]);
const DOC_EXTENSIONS = new Set([".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".md", ".csv", ".rtf", ".odt"]);
const ARCHIVE_EXTENSIONS = new Set([".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z", ".rar"]);
const CODE_EXTENSIONS = new Set([".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cc", ".cpp", ".h", ".hpp",
  ".go", ".rs", ".rb", ".php", ".sh", ".html", ".css", ".json", ".yaml", ".yml", ".toml"]);
const ACTIVE_TASK_STATUSES = new Set(["queued", "running"]);
const TASK_POLL_ACTIVE_MS = 2500;
const TASK_POLL_IDLE_MS = 15000;
const TASK_POLL_HIDDEN_MS = 30000;
const TASK_POLL_ERROR_BASE_MS = 10000;
const TASK_POLL_ERROR_MAX_MS = 60000;

// ── Network helper ────────────────────────────────────────────────────────────

async function fetchJson(url, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (window.__GD_CSRF_TOKEN__) headers["X-CSRF-Token"] = window.__GD_CSRF_TOKEN__;
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
    headers,
  });
  if (response.status === 401) {
    window.location.href = "/login";
    throw new Error("Login required");
  }
  let payload;
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok) {
    const error = new Error(payload.error || `Request failed (${response.status})`);
    error.payload = payload;
    error.status = response.status;
    throw error;
  }
  return payload;
}

// ── DOM utilities ─────────────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);

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
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: "numeric", month: "short", day: "numeric",
    });
  } catch (_) { return iso; }
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[i]}`;
}

function updateStorageUsage() {
  const totalBytes = state.archives.reduce((sum, archive) => sum + Number(archive.total_asset_bytes || 0), 0);
  const limitBytes = Number(window.__GD_STORAGE_LIMIT_BYTES__ || 0);
  const label = $("storageUsageLabel");
  const percentLabel = $("storageUsagePercent");
  const fill = $("storageBarFill");
  if (label) {
    if (state.archiveHasMore) {
      label.textContent = `${formatBytes(totalBytes)} used in loaded archives`;
    } else {
      label.textContent = limitBytes > 0
        ? `${formatBytes(totalBytes)} of ${formatBytes(limitBytes)} used`
        : `${formatBytes(totalBytes)} used`;
    }
  }
  if (percentLabel) {
    percentLabel.textContent = (limitBytes > 0 && !state.archiveHasMore)
      ? `${Math.min(999, Math.round((totalBytes / limitBytes) * 100))}%`
      : "";
  }
  if (fill) {
    const pct = limitBytes > 0
      ? Math.max(0, Math.min(100, (totalBytes / limitBytes) * 100))
      : (totalBytes > 0 ? 100 : 0);
    fill.style.width = `${pct}%`;
    fill.style.opacity = state.archiveHasMore
      ? "0.45"
      : (limitBytes > 0 ? "1" : (totalBytes > 0 ? "0.45" : "0.2"));
  }
}

function show(id, visible) {
  const node = $(id);
  if (node) node.style.display = visible ? "" : "none";
}

function clearTaskPollTimer() {
  if (state.taskPollTimer !== null) {
    clearTimeout(state.taskPollTimer);
    state.taskPollTimer = null;
  }
}

function hasConfiguredRepo() {
  return Boolean(state.me.token_present && state.me.repo);
}

function hasActiveTasks(tasks) {
  return (tasks || []).some((task) => ACTIVE_TASK_STATUSES.has(task.status));
}

function nextTaskPollDelay() {
  if (state.taskPollErrorCount > 0) {
    return Math.min(
      TASK_POLL_ERROR_BASE_MS * (2 ** (state.taskPollErrorCount - 1)),
      TASK_POLL_ERROR_MAX_MS,
    );
  }
  if (document.visibilityState === "hidden") {
    return state.taskPollHasActive ? TASK_POLL_IDLE_MS : TASK_POLL_HIDDEN_MS;
  }
  return state.taskPollHasActive ? TASK_POLL_ACTIVE_MS : TASK_POLL_IDLE_MS;
}

function scheduleTaskPoll(delayOverride = null) {
  clearTaskPollTimer();
  if (!hasConfiguredRepo()) return;
  const delay = Math.max(0, delayOverride == null ? nextTaskPollDelay() : delayOverride);
  state.taskPollTimer = window.setTimeout(async () => {
    state.taskPollTimer = null;
    await loadTasks();
  }, delay);
}

// ── Modal helpers ─────────────────────────────────────────────────────────────

function openModal(id) { $(id).classList.add("open"); }
function closeModal(id) { $(id).classList.remove("open"); }

function setupModalDismiss() {
  document.querySelectorAll(".modal-backdrop").forEach((backdrop) => {
    backdrop.addEventListener("click", (event) => {
      if (event.target === backdrop) backdrop.classList.remove("open");
    });
  });
  document.querySelectorAll("[data-close-modal]").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      const modal = event.currentTarget.closest(".modal-backdrop");
      if (modal) modal.classList.remove("open");
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      document.querySelectorAll(".modal-backdrop.open").forEach((m) => m.classList.remove("open"));
      $("newMenu")?.classList.remove("open");
      $("accountPopover")?.classList.remove("open");
    }
  });
}

// ── User / credentials ────────────────────────────────────────────────────────

function applyMe(me) {
  const wasConfigured = hasConfiguredRepo();
  state.me = me;
  const repo = me.repo || "";
  const configured = Boolean(me.token_present && repo);

  $("popoverRepoLine").textContent = configured ? repo : "No GitHub repo configured";
  $("sidebarRepoLine").textContent = configured ? repo : "Not connected";
  syncCredsModal();

  if (!configured) {
    // Force the user to set credentials before they can do anything else.
    clearTaskPollTimer();
    state.taskPollHasActive = false;
    state.taskPollErrorCount = 0;
    renderTasks([]);
    openModal("credsModal");
  } else if (!wasConfigured || (!state.taskPollTimer && !state.taskPollInFlight)) {
    state.taskPollErrorCount = 0;
    scheduleTaskPoll(0);
  }
  updateStorageUsage();
}

function syncCredsModal() {
  const isOauthUser = Boolean(state.me.github_oauth_connected);
  const tokenField = $("credsTokenField");
  const subtitle = $("credsModalSubtitle");
  const savedRepoField = $("credsSavedRepoField");
  const savedRepoSelect = $("credsSavedRepoSelect");
  const repoLabel = $("credsRepoLabel");
  const repoInput = $("credsRepoInput");
  const clearButton = $("clearCredsButton");
  const repos = Array.isArray(state.me.repos) ? state.me.repos : [];
  const activeRepo = state.me.repo || "";

  if (tokenField) tokenField.style.display = isOauthUser ? "none" : "";
  if (subtitle) {
    subtitle.innerHTML = isOauthUser
      ? "Your GitHub sign-in already provides the token. Choose a saved repository or add another <code>owner/repo</code>."
      : "Each account uses its own Personal Access Token and target repository. Create one at <a href=\"https://github.com/settings/tokens\" target=\"_blank\" rel=\"noreferrer\">github.com/settings/tokens</a> with the <code>repo</code> scope.";
  }
  if (savedRepoField) savedRepoField.style.display = repos.length ? "" : "none";
  if (savedRepoSelect) {
    savedRepoSelect.innerHTML = ['<option value="">Choose a saved repo</option>']
      .concat(repos.map((entry) => {
        const slug = entry.slug || "";
        const selected = slug === activeRepo ? " selected" : "";
        const label = entry.active ? `${slug} (active)` : slug;
        return `<option value="${escapeHtml(slug)}"${selected}>${escapeHtml(label)}</option>`;
      }))
      .join("");
  }
  if (repoLabel) repoLabel.textContent = repos.length ? "Add or switch repository" : "Target repository";
  if (repoInput) {
    repoInput.required = !repos.length;
    repoInput.placeholder = repos.length ? "owner/repo for a new repo or leave blank to use a saved one" : "your-username/github-drive-archives";
    repoInput.value = "";
  }
  if (clearButton) clearButton.textContent = isOauthUser ? "Disconnect" : "Forget";
}

function handleCredentialError(error) {
  if (!error?.payload?.credential_recovery_required) return false;
  state.me.token_present = false;
  $("popoverRepoLine").textContent = "GitHub token needs to be re-entered";
  $("sidebarRepoLine").textContent = "Reconnect required";
  openModal("credsModal");
  alert(error.message);
  return true;
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
  const repo = String(data.get("repo") || "").trim();
  const savedRepo = String(data.get("saved_repo") || "").trim();
  try {
    const result = await fetchJson("/api/me/credentials", {
      method: "POST",
      body: JSON.stringify({
        token: data.get("token"),
        repo,
        saved_repo: savedRepo,
        create_repo: data.get("create_repo") === "on",
        private_repo: true,
      }),
    });
    form.reset();
    closeModal("credsModal");
    await loadMe();
    await loadArchives({ reset: true });
    showFlash(`Connected to ${result.repo}`);
  } catch (error) {
    if (handleCredentialError(error)) return;
    alert(error.message);
  }
}

async function clearCreds() {
  if (!confirm("Disconnect the saved GitHub token and saved repositories for this account?")) return;
  try {
    await fetchJson("/api/me/credentials", { method: "DELETE" });
    closeModal("credsModal");
    await loadMe();
    await loadArchives({ reset: true });
  } catch (error) {
    if (handleCredentialError(error)) return;
    alert(error.message);
  }
}

async function exportAccountData() {
  try {
    const payload = await fetchJson("/api/me/export");
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `github-drive-${state.me.username || "account"}-export.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (error) {
    if (handleCredentialError(error)) return;
    alert(error.message);
  }
}

async function deleteAccount() {
  if (!confirm("Delete your GitHub Drive account from this server? Your GitHub releases are not deleted.")) return;
  if (!confirm("This removes your login, saved token, and server task history. Continue?")) return;
  try {
    await fetchJson("/api/me", { method: "DELETE" });
    window.location.href = "/login";
  } catch (error) {
    alert(error.message);
  }
}

async function reportAbuse() {
  const subject = prompt("Short summary");
  if (!subject) return;
  const details = prompt("Details");
  if (!details) return;
  try {
    await fetchJson("/api/abuse-report", {
      method: "POST",
      body: JSON.stringify({ subject, details }),
    });
    alert("Report submitted.");
  } catch (error) {
    alert(error.message);
  }
}

function showFlash(message) {
  const el = $("popoverRepoLine");
  if (!el) return;
  const previous = el.textContent;
  el.textContent = message;
  setTimeout(() => { el.textContent = previous; }, 2500);
}

// ── Archives ──────────────────────────────────────────────────────────────────

const ARCHIVE_ICON_INLINE = `<svg width="28" height="28" viewBox="0 0 24 24" fill="currentColor"><path d="M3 4h18v2H3V4zm2 4h14v12H5V8zm5 3v2h4v-2h-4z"/></svg>`;
const FOLDER_ICON_INLINE = `<svg width="28" height="28" viewBox="0 0 24 24" fill="currentColor"><path d="M10 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/></svg>`;
const KIND_ICONS = {
  folder: FOLDER_ICON_INLINE,
  image: `<svg width="28" height="28" viewBox="0 0 24 24" fill="currentColor"><path d="M21 19V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2zM8.5 13.5l2.5 3 3.5-4.5L19 18H5l3.5-4.5z"/></svg>`,
  video: `<svg width="28" height="28" viewBox="0 0 24 24" fill="currentColor"><path d="M17 10.5V7c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h12c.55 0 1-.45 1-1v-3.5l4 4v-11l-4 4z"/></svg>`,
  audio: `<svg width="28" height="28" viewBox="0 0 24 24" fill="currentColor"><path d="M12 3v9.55A4 4 0 1 0 14 16V7h4V3h-6z"/></svg>`,
  document: `<svg width="28" height="28" viewBox="0 0 24 24" fill="currentColor"><path d="M14 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V8l-6-6zm4 18H6V4h7v5h5v11z"/></svg>`,
  archive: `<svg width="28" height="28" viewBox="0 0 24 24" fill="currentColor"><path d="M20 6h-3V4c0-1.1-.9-2-2-2H9c-1.1 0-2 .9-2 2v2H4c-1.1 0-2 .9-2 2v11c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zM9 4h6v2H9V4zm4 11h-2v-2h2v2z"/></svg>`,
  code: `<svg width="28" height="28" viewBox="0 0 24 24" fill="currentColor"><path d="M9.4 16.6 4.8 12l4.6-4.6L8 6l-6 6 6 6 1.4-1.4zm5.2 0 4.6-4.6-4.6-4.6L16 6l6 6-6 6-1.4-1.4z"/></svg>`,
  other: ARCHIVE_ICON_INLINE,
};

function dominantKind(archive) {
  const kinds = archiveKinds(archive);
  let best = null;
  let bestCount = 0;
  for (const [name, count] of Object.entries(kinds)) {
    if (count > bestCount) { best = name; bestCount = count; }
  }
  return best || "other";
}

function archiveDisplayKind(archive) {
  const meta = archive.archive || {};
  if (meta.source_type === "directory") return "folder";
  return dominantKind(archive);
}

function archiveTimestamp(archive) {
  const iso = (archive.archive && archive.archive.created_at) || archive.created_at || archive.updated_at;
  if (!iso) return 0;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : 0;
}

function archiveTitle(archive) {
  const meta = archive.archive || {};
  return meta.source_name || archive.name || archive.tag || "Archive";
}

function archiveItemCount(archive) {
  const meta = archive.archive || {};
  return meta.total_items || archive.asset_count || 0;
}

function archiveFileUrl(releaseId, relativePath, thumb = false) {
  const params = new URLSearchParams({ path: relativePath });
  if (thumb) params.set("thumb", "1");
  return `/api/archives/${releaseId}/file?${params.toString()}`;
}

function archiveEntryDownloadUrl(releaseId, relativePath, kind = "file") {
  const params = new URLSearchParams({ path: relativePath, kind });
  return `/api/archives/${releaseId}/download-entry?${params.toString()}`;
}

function basename(relativePath) {
  const parts = String(relativePath || "").split("/").filter(Boolean);
  return parts[parts.length - 1] || relativePath || "";
}

function normalizeArchivePath(path) {
  return String(path || "").replace(/^\/+|\/+$/g, "");
}

function legacySyntheticRootPath(archive = state.selectedArchive, contents = state.selectedArchiveContents) {
  if (!archive || !contents) return "";
  const rootName = normalizeArchivePath(archiveTitle(archive));
  if (!rootName) return "";
  const entries = Array.isArray(contents.entries) ? contents.entries : [];
  if (!entries.length) return "";
  const hasRootFolder = entries.some((entry) => (
    entry.kind === "folder" && normalizeArchivePath(entry.relative_path) === rootName
  ));
  if (!hasRootFolder) return "";
  const allInsideRoot = entries.every((entry) => {
    const relativePath = normalizeArchivePath(entry.relative_path);
    return relativePath === rootName || relativePath.startsWith(`${rootName}/`);
  });
  return allInsideRoot ? rootName : "";
}

function archivePathContext(archive = state.selectedArchive, contents = state.selectedArchiveContents, rawPath = state.selectedArchivePath) {
  const normalizedPath = normalizeArchivePath(rawPath);
  const syntheticRootPath = legacySyntheticRootPath(archive, contents);
  if (!syntheticRootPath) {
    return {
      rootPath: "",
      rawPath: normalizedPath,
      visiblePath: normalizedPath,
      segments: normalizedPath ? normalizedPath.split("/") : [],
    };
  }
  if (normalizedPath === syntheticRootPath) {
    return {
      rootPath: syntheticRootPath,
      rawPath: normalizedPath,
      visiblePath: "",
      segments: [],
    };
  }
  if (normalizedPath.startsWith(`${syntheticRootPath}/`)) {
    const visiblePath = normalizedPath.slice(syntheticRootPath.length + 1);
    return {
      rootPath: syntheticRootPath,
      rawPath: normalizedPath,
      visiblePath,
      segments: visiblePath ? visiblePath.split("/") : [],
    };
  }
  return {
    rootPath: syntheticRootPath,
    rawPath: normalizedPath,
    visiblePath: normalizedPath,
    segments: normalizedPath ? normalizedPath.split("/") : [],
  };
}

function downloadSelectedEntry(relativePath, kind = "file") {
  const archive = state.selectedArchive;
  if (!archive || !archive.release_id || !relativePath) return;
  window.location.href = archiveEntryDownloadUrl(archive.release_id, relativePath, kind);
}

function updatePageChrome() {
  const title = $("pageTitle");
  const subtitle = $("pageSubtitle");
  if (!title || !subtitle) return;
  const archive = state.selectedArchive;
  if (!archive) {
    title.textContent = "My Drive";
    subtitle.textContent = state.archiveHasMore
      ? `${state.archives.length} recent archive${state.archives.length === 1 ? "" : "s"} loaded`
      : `${state.archives.length} archive${state.archives.length === 1 ? "" : "s"}`;
    return;
  }
  const pathContext = archivePathContext();
  const meta = archive.archive || {};
  const count = meta.total_items || archive.asset_count || 0;
  const created = formatDate(meta.created_at || archive.created_at || "");
  const pathLabel = ["Home", archiveTitle(archive), ...pathContext.segments].join(" / ");
  title.textContent = pathLabel;
  subtitle.textContent = `${count} item${count === 1 ? "" : "s"}${created ? ` · ${created}` : ""}`;
}

function mergeArchives(existing, incoming) {
  const merged = new Map();
  for (const archive of existing || []) merged.set(archive.release_id, archive);
  for (const archive of incoming || []) merged.set(archive.release_id, archive);
  return Array.from(merged.values());
}

function syncArchivesPagination() {
  const wrapper = $("archivesPagination");
  const button = $("loadMoreArchivesButton");
  const note = $("archivesPaginationNote");
  if (!wrapper || !button || !note) return;

  const hasLoadedAny = state.archives.length > 0;
  const show = hasLoadedAny && (state.archiveHasMore || state.archiveLoading);
  wrapper.style.display = show ? "" : "none";
  button.disabled = state.archiveLoading || !state.archiveHasMore;
  button.textContent = state.archiveLoading ? "Loading..." : "Load more";

  if (state.archiveHasMore) {
    note.textContent = state.searchTerm.trim()
      ? "Search and filters apply to the loaded archives. Load more to include older results."
      : "Showing recent archives first. Load more to fetch older releases.";
  } else if (state.archiveLoading) {
    note.textContent = "Loading more archives…";
  } else {
    note.textContent = "";
  }
}

async function deleteArchiveRecord(archive) {
  if (!archive?.release_id) return;
  if (!confirm(`Delete "${archiveTitle(archive)}" permanently?`)) return;
  try {
    await fetchJson(`/api/archives/${archive.release_id}`, { method: "DELETE" });
    if (state.selectedArchive && state.selectedArchive.release_id === archive.release_id) {
      state.selectedArchive = null;
      state.selectedArchiveContents = null;
      state.selectedArchivePath = "";
      showArchiveListView();
    }
    await loadArchives();
  } catch (error) {
    alert(error.message);
  }
}

function syncArchiveBrowserToolbar() {
  const archive = state.selectedArchive;
  const contents = state.selectedArchiveContents;
  const pathContext = archivePathContext(archive, contents, state.selectedArchivePath);
  const tag = $("archiveDetailTagText");
  const button = $("downloadCurrentFolderButton");
  if (tag) {
    tag.textContent = archive?.tag ? `Tag: ${archive.tag}` : "";
  }
  syncNewMenuState();
  if (!button) return;
  if (!archive?.release_id || !pathContext.visiblePath) {
    button.style.display = "none";
    return;
  }
  button.style.display = "";
  button.textContent = `Download ${basename(pathContext.visiblePath) || "folder"}`;
}

function isMutableArchiveContext() {
  const archive = state.selectedArchive;
  const contents = state.selectedArchiveContents;
  const archiveMeta = contents?.archive || archive?.archive || {};
  return Boolean(
    archive?.release_id
    && archive?.tag
    && contents
    && contents.supports_file_delete
    && archiveMeta.source_type !== "file"
  );
}

function matchesTypeFilter(archive, value) {
  if (value === "all") return true;
  if (value === "encrypted") return Boolean(archive.archive && archive.archive.encrypted);
  const kinds = archiveKinds(archive);
  return (kinds[value] || 0) > 0;
}

function archiveKinds(archive) {
  const meta = archive.archive || {};
  const kinds = meta.kinds || {};
  if (Object.values(kinds).some((count) => Number(count) > 0)) {
    return kinds;
  }
  const inferred = inferKindFromName(meta.source_name || archive.name || archive.tag || "");
  return {
    image: inferred === "image" ? 1 : 0,
    video: inferred === "video" ? 1 : 0,
    audio: inferred === "audio" ? 1 : 0,
    document: inferred === "document" ? 1 : 0,
    archive: inferred === "archive" ? 1 : 0,
    code: inferred === "code" ? 1 : 0,
    other: inferred === "other" ? 1 : 0,
  };
}

function inferKindFromName(name) {
  const dot = String(name || "").lastIndexOf(".");
  const ext = dot >= 0 ? String(name).slice(dot).toLowerCase() : "";
  if (IMAGE_EXTENSIONS.has(ext)) return "image";
  if (VIDEO_EXTENSIONS.has(ext)) return "video";
  if (AUDIO_EXTENSIONS.has(ext)) return "audio";
  if (DOC_EXTENSIONS.has(ext)) return "document";
  if (ARCHIVE_EXTENSIONS.has(ext)) return "archive";
  if (CODE_EXTENSIONS.has(ext)) return "code";
  return "other";
}

function matchesModifiedFilter(archive, value) {
  if (value === "any") return true;
  const t = archiveTimestamp(archive);
  if (!t) return value === "any";
  const ageMs = Date.now() - t;
  const day = 24 * 60 * 60 * 1000;
  if (value === "today") return ageMs < day;
  if (value === "week") return ageMs < 7 * day;
  if (value === "month") return ageMs < 30 * day;
  if (value === "year") return ageMs < 365 * day;
  return true;
}

function applyFiltersAndSort() {
  const term = state.searchTerm.trim().toLowerCase();
  let result = state.archives.filter((archive) => {
    if (!matchesTypeFilter(archive, state.filters.type)) return false;
    if (!matchesModifiedFilter(archive, state.filters.modified)) return false;
    if (term) {
      const meta = archive.archive || {};
      const haystack = [meta.source_name || "", archive.name || "", archive.tag || ""].join(" ").toLowerCase();
      if (!haystack.includes(term)) return false;
    }
    return true;
  });

  const sort = state.filters.sort;
  result.sort((a, b) => {
    if (sort === "newest") return archiveTimestamp(b) - archiveTimestamp(a);
    if (sort === "oldest") return archiveTimestamp(a) - archiveTimestamp(b);
    if (sort === "name_asc") return archiveTitle(a).localeCompare(archiveTitle(b));
    if (sort === "name_desc") return archiveTitle(b).localeCompare(archiveTitle(a));
    if (sort === "items_desc") return archiveItemCount(b) - archiveItemCount(a);
    if (sort === "items_asc") return archiveItemCount(a) - archiveItemCount(b);
    return 0;
  });
  state.filteredArchives = result;
}

function showArchiveListView() {
  show("filterBar", true);
  show("archivesContainer", true);
  show("archiveView", false);
  updatePageChrome();
}

function showArchiveBrowserView() {
  show("filterBar", false);
  show("archivesContainer", false);
  show("archiveView", true);
  updatePageChrome();
}

function renderArchives() {
  applyFiltersAndSort();
  const grid = $("archivesGrid");
  const empty = $("archivesEmpty");
  // List view always renders alongside the grid; toggle handles visibility.
  renderArchivesList(state.filteredArchives);
  syncArchivesPagination();
  if (!state.filteredArchives.length) {
    grid.innerHTML = "";
    show("archivesGrid", false);
    show("archivesList", false);
    empty.style.display = "";
    if (state.archives.length) {
      empty.querySelector("h2").textContent = "No matches";
      empty.querySelector("p").innerHTML = "Try clearing the filters or search to see all archives.";
    } else {
      empty.querySelector("h2").textContent = "No archives yet";
      empty.querySelector("p").innerHTML = "Click <strong>New</strong> in the sidebar to upload your first file or folder.";
    }
    return;
  }
  empty.style.display = "none";
  applyViewMode(state.viewMode);
  grid.innerHTML = state.filteredArchives.map((archive, index) => {
    const meta = archive.archive || {};
    const title = archiveTitle(archive);
    const count = archiveItemCount(archive);
    const created = formatDate(meta.created_at || archive.created_at || "");
    const kind = archiveDisplayKind(archive);
    const kindLabel = KIND_LABEL[kind] || "Files";
    // Server-side thumbnail generation has been removed (it loaded full assets
    // into RAM and was OOM-killing the 512 MB instance). Always render the
    // kind icon — cheap, no extra request, and uniform across archive types.
    const thumb = `<div class="archive-thumb-fallback">${KIND_ICONS[kind] || ARCHIVE_ICON_INLINE}</div>`;
    return `
      <div class="archive-card" data-index="${index}">
        <div class="archive-thumb">
          <div class="archive-card-actions">
            <button type="button" class="icon-button archive-card-action" data-delete-archive-index="${index}" title="Delete archive" aria-label="Delete archive">
              ${ICON_DELETE}
            </button>
          </div>
          ${thumb}
          <span class="archive-kind-pill">${escapeHtml(kindLabel)}${meta.encrypted ? " · encrypted" : ""}</span>
        </div>
        <div class="archive-body">
          <div class="archive-name">${escapeHtml(title)}</div>
          <div class="archive-meta">
            <span>${count} item${count === 1 ? "" : "s"}</span>
            <span>${escapeHtml(created)}</span>
          </div>
          <div class="archive-tag">${escapeHtml(archive.tag)}</div>
        </div>
      </div>
    `;
  }).join("");
  grid.querySelectorAll(".archive-card").forEach((card) => {
    card.addEventListener("click", () => {
      const archive = state.filteredArchives[Number(card.dataset.index)];
      openArchiveDetail(archive);
    });
    card.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      const archive = state.filteredArchives[Number(card.dataset.index)];
      if (archive) showArchiveContextMenu(event.clientX, event.clientY, archive);
    });
  });
  grid.querySelectorAll("[data-delete-archive-index]").forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      const archive = state.filteredArchives[Number(button.dataset.deleteArchiveIndex)];
      await deleteArchiveRecord(archive);
    });
  });
  syncArchivesPagination();
}

// ── Filter chips ──────────────────────────────────────────────────────────────

function renderChipMenus() {
  document.querySelectorAll("[data-chip-menu]").forEach((menu) => {
    const key = menu.dataset.chipMenu;
    const options = FILTER_OPTIONS[key] || [];
    menu.innerHTML = options.map((opt) => `
      <button type="button" data-value="${escapeHtml(opt.value)}" class="${state.filters[key] === opt.value ? 'selected' : ''}">
        <span>${escapeHtml(opt.label)}</span>
        <svg class="check" width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41L9 16.17z"/></svg>
      </button>
    `).join("");
  });
}

function chipLabelFor(key) {
  const value = state.filters[key];
  const opt = (FILTER_OPTIONS[key] || []).find((o) => o.value === value);
  if (!opt) return key;
  if (key === "sort") return `Sort: ${opt.label.replace(/\\s*first$/, "")}`;
  if (key === "type" && value === "all") return "Type";
  if (key === "modified" && value === "any") return "Modified";
  return opt.label;
}

function syncChipButtons() {
  document.querySelectorAll(".filter-chip-button").forEach((btn) => {
    const key = btn.dataset.chip;
    const labelEl = btn.querySelector("[data-chip-label]");
    if (labelEl) labelEl.textContent = chipLabelFor(key);
    const isDefault = state.filters[key] === FILTER_OPTIONS[key][0].value;
    btn.classList.toggle("active", !isDefault);
  });
}

function setupFilterChips() {
  renderChipMenus();
  syncChipButtons();
  document.querySelectorAll(".filter-chip-button").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      const key = btn.dataset.chip;
      const menu = btn.parentElement.querySelector("[data-chip-menu]");
      // Close other menus first.
      document.querySelectorAll(".filter-chip-menu.open").forEach((m) => { if (m !== menu) m.classList.remove("open"); });
      menu.classList.toggle("open");
    });
  });
  document.querySelectorAll(".filter-chip-menu").forEach((menu) => {
    menu.addEventListener("click", (event) => {
      const target = event.target.closest("button[data-value]");
      if (!target) return;
      const key = menu.dataset.chipMenu;
      state.filters[key] = target.dataset.value;
      renderChipMenus();
      syncChipButtons();
      menu.classList.remove("open");
      renderArchives();
    });
  });
  document.addEventListener("click", (event) => {
    document.querySelectorAll(".filter-chip-menu.open").forEach((menu) => {
      if (!menu.parentElement.contains(event.target)) menu.classList.remove("open");
    });
  });
}

function listArchiveChildren(entries, currentPath) {
  const prefix = currentPath ? `${currentPath}/` : "";
  const folders = new Map();
  const files = [];

  for (const entry of entries || []) {
    const relativePath = String(entry.relative_path || "");
    if (prefix && !relativePath.startsWith(prefix)) continue;
    const remainder = prefix ? relativePath.slice(prefix.length) : relativePath;
    if (!remainder) continue;
    if (entry.kind === "folder") {
      const folderParts = remainder.split("/").filter(Boolean);
      if (!folderParts.length) continue;
      const folderName = folderParts[0];
      const folderPath = currentPath ? `${currentPath}/${folderName}` : folderName;
      const folder = folders.get(folderPath) || { name: folderName, path: folderPath, fileCount: 0, imageCount: 0 };
      folders.set(folderPath, folder);
      continue;
    }
    const parts = remainder.split("/").filter(Boolean);
    if (parts.length > 1) {
      const folderName = parts[0];
      const folderPath = currentPath ? `${currentPath}/${folderName}` : folderName;
      const folder = folders.get(folderPath) || { name: folderName, path: folderPath, fileCount: 0, imageCount: 0 };
      folder.fileCount += 1;
      if (entry.kind === "image") folder.imageCount += 1;
      folders.set(folderPath, folder);
      continue;
    }
    files.push(entry);
  }

  return {
    folders: Array.from(folders.values()).sort((a, b) => a.name.localeCompare(b.name)),
    files: files.sort((a, b) => basename(a.relative_path).localeCompare(basename(b.relative_path))),
  };
}

function renderArchiveBreadcrumbs() {
  const breadcrumbs = $("archiveBreadcrumbs");
  const archive = state.selectedArchive;
  const pathContext = archivePathContext();
  const crumbs = [{
    label: archive ? archiveTitle(archive) : "Archive",
    path: pathContext.rootPath || "",
  }];
  let acc = "";
  for (const part of pathContext.segments) {
    acc = acc ? `${acc}/${part}` : part;
    const fullPath = pathContext.rootPath ? `${pathContext.rootPath}/${acc}` : acc;
    crumbs.push({ label: part, path: fullPath });
  }
  breadcrumbs.innerHTML = crumbs.map((crumb, index) => {
    const isCurrent = index === crumbs.length - 1;
    return `
      <button type="button" class="${isCurrent ? "current" : ""}" data-path="${escapeHtml(crumb.path)}"${isCurrent ? " aria-current=\"page\"" : ""}>
        ${escapeHtml(crumb.label)}
      </button>
      ${isCurrent ? "" : '<span class="sep">/</span>'}
    `;
  }).join("");
  breadcrumbs.querySelectorAll("button[data-path]").forEach((button) => {
    if (button.classList.contains("current")) return;
    button.addEventListener("click", () => {
      state.selectedArchivePath = button.dataset.path || "";
      renderArchiveContents();
    });
  });
}

function renderArchiveContents() {
  const contents = state.selectedArchiveContents;
  const grid = $("archiveBrowserGrid");
  const status = $("archiveBrowserStatus");
  const note = $("archiveBrowserNote");

  if (!contents) {
    $("archiveBreadcrumbs").innerHTML = "";
    note.textContent = "";
    status.textContent = "Loading files…";
    grid.innerHTML = "";
    return;
  }

  state.selectedArchivePath = normalizeArchivePath(state.selectedArchivePath);
  updatePageChrome();
  renderArchiveBreadcrumbs();
  syncArchiveBrowserToolbar();
  note.textContent = contents.supports_file_delete ? "" : "Bundled archive";

  const { folders, files } = listArchiveChildren(contents.entries || [], state.selectedArchivePath);
  status.textContent = `${folders.length + files.length} item${folders.length + files.length === 1 ? "" : "s"} here`;

  if (!folders.length && !files.length) {
    grid.innerHTML = `<div class="archive-node-empty">Nothing in this folder.</div>`;
    return;
  }

  const folderCards = folders.map((folder) => `
    <div class="archive-card archive-browser-card folder" data-folder-path="${escapeHtml(folder.path)}">
      <div class="archive-thumb">
        <div class="archive-thumb-fallback">${FOLDER_ICON_INLINE}</div>
        <span class="archive-kind-pill">Folder</span>
      </div>
      <div class="archive-body">
        <div class="archive-name">${escapeHtml(folder.name)}</div>
        <div class="archive-meta">
          <span>${folder.fileCount} file${folder.fileCount === 1 ? "" : "s"}</span>
          ${folder.imageCount ? `<span>${folder.imageCount} image${folder.imageCount === 1 ? "" : "s"}</span>` : ""}
        </div>
      </div>
      <div class="archive-browser-card-actions">
        <div class="archive-node-actions">
          <button type="button" class="icon-button" data-download-path="${escapeHtml(folder.path)}" data-download-kind="folder" title="Download folder">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M5 20h14v-2H5v2zm7-18-5.5 5.5h3.5V16h4V7.5H17.5L12 2z"/></svg>
          </button>
        </div>
      </div>
    </div>
  `).join("");

  const archive = state.selectedArchive;
  const releaseId = archive ? archive.release_id : null;
  const fileCards = files.map((file) => {
    const previewable = Boolean(file.previewable && releaseId);
    const icon = KIND_ICONS[file.kind] || ARCHIVE_ICON_INLINE;
    const thumb = previewable
      ? `<img loading="lazy" decoding="async" src="${archiveFileUrl(releaseId, file.relative_path, true)}" alt="">`
      : `<div class="archive-node-icon">${icon}</div>`;
    const deleteButton = contents.supports_file_delete
      ? `
        <button type="button" class="icon-button" data-delete-path="${escapeHtml(file.relative_path)}" title="Delete file">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zm3.46-7.12 1.41-1.41L12 11.59l1.12-1.12 1.41 1.41L13.41 13l1.12 1.12-1.41 1.41L12 14.41l-1.12 1.12-1.41-1.41L10.59 13l-1.13-1.12zM15.5 4l-1-1h-5l-1 1H5v2h14V4z"/></svg>
        </button>
      `
      : "";
    return `
      <div class="archive-card archive-browser-card file" data-file-path="${escapeHtml(file.relative_path)}" data-previewable="${previewable ? "1" : "0"}">
        <div class="archive-thumb">${thumb}<span class="archive-kind-pill">${escapeHtml(KIND_LABEL[file.kind] || "File")}</span></div>
        <div class="archive-body">
          <div class="archive-name">${escapeHtml(basename(file.relative_path))}</div>
          <div class="archive-meta">
            <span>${escapeHtml(KIND_LABEL[file.kind] || "File")}</span>
            <span>${escapeHtml(formatBytes(file.original_size || 0))}</span>
          </div>
        </div>
        <div class="archive-browser-card-actions">
          <div class="archive-node-actions">
            <button type="button" class="icon-button" data-download-path="${escapeHtml(file.relative_path)}" data-download-kind="file" title="Download file">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M5 20h14v-2H5v2zm7-18-5.5 5.5h3.5V16h4V7.5H17.5L12 2z"/></svg>
            </button>
            ${deleteButton}
          </div>
        </div>
      </div>
    `;
  }).join("");

  grid.innerHTML = `${folderCards}${fileCards}`;

  grid.querySelectorAll("[data-folder-path]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedArchivePath = button.dataset.folderPath || "";
      renderArchiveContents();
    });
  });
  grid.querySelectorAll("[data-download-path]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      downloadSelectedEntry(button.dataset.downloadPath || "", button.dataset.downloadKind || "file");
    });
  });
  grid.querySelectorAll("[data-file-path]").forEach((card) => {
    card.addEventListener("click", () => {
      const entry = (contents.entries || []).find((item) => item.relative_path === card.dataset.filePath);
      if (entry) openPreview(entry);
    });
    card.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      const entry = (contents.entries || []).find((item) => item.relative_path === card.dataset.filePath);
      if (entry) showEntryContextMenu(event.clientX, event.clientY, entry);
    });
  });
  grid.querySelectorAll("[data-folder-path]").forEach((card) => {
    card.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      const path = card.dataset.folderPath || "";
      const entry = { relative_path: path, kind: "folder" };
      showEntryContextMenu(event.clientX, event.clientY, entry);
    });
  });
  grid.querySelectorAll("[data-delete-path]").forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      await deleteArchiveEntry(button.dataset.deletePath || "");
    });
  });
  grid.querySelectorAll(".archive-thumb img").forEach((img) => {
    img.addEventListener("error", () => {
      const filePath = img.closest("[data-file-path]")?.dataset.filePath || "";
      const entry = (contents.entries || []).find((item) => item.relative_path === filePath);
      const kind = entry?.kind || "other";
      img.parentElement.innerHTML = `<div class="archive-thumb-fallback">${KIND_ICONS[kind] || ARCHIVE_ICON_INLINE}</div><span class="archive-kind-pill">${escapeHtml(KIND_LABEL[kind] || "File")}</span>`;
    });
  });
}

function syncSelectedArchiveFromContents(contents) {
  if (!contents) return;
  const updated = {
    release_id: contents.release_id,
    tag: contents.tag,
    name: contents.name,
    html_url: contents.html_url,
    created_at: contents.created_at,
    updated_at: contents.updated_at,
    archive: contents.archive,
    asset_count: (contents.entries || []).length,
  };
  state.selectedArchive = updated;
  const idx = state.archives.findIndex((archive) => archive.release_id === contents.release_id);
  if (idx >= 0) state.archives[idx] = { ...state.archives[idx], ...updated };
}

function isSingleFileArchive(contents) {
  const entries = contents && Array.isArray(contents.entries) ? contents.entries : [];
  if (entries.length !== 1) return false;
  const entry = entries[0];
  if (!entry || !entry.relative_path) return false;
  if (entry.kind === "folder") return false;
  const archiveMeta = contents.archive || {};
  if (archiveMeta.source_type === "file") return true;
  return normalizeArchivePath(entry.relative_path) === basename(entry.relative_path);
}

async function loadSelectedArchiveContents() {
  const archive = state.selectedArchive;
  if (!archive) return;
  $("archiveBrowserStatus").textContent = "Loading files…";
  $("archiveBrowserGrid").innerHTML = "";
  const contents = await fetchJson(`/api/archives/${archive.release_id}/contents`);
  state.selectedArchiveContents = contents;
  if (!normalizeArchivePath(state.selectedArchivePath)) {
    const syntheticRootPath = legacySyntheticRootPath(archive, contents);
    if (syntheticRootPath) {
      state.selectedArchivePath = syntheticRootPath;
    }
  }
  syncSelectedArchiveFromContents(contents);
  renderArchiveContents();
}

function previewKindFor(file) {
  const ext = ("." + (file.relative_path || "").split(".").pop() || "").toLowerCase();
  if (file.kind === "folder") return "folder";
  if (IMAGE_EXTENSIONS.has(ext)) return "image";
  if (VIDEO_EXTENSIONS.has(ext)) return "video";
  if (AUDIO_EXTENSIONS.has(ext)) return "audio";
  if (PDF_EXTENSIONS.has(ext)) return "pdf";
  if (TEXT_PREVIEW_EXTENSIONS.has(ext)) return "text";
  return "other";
}

async function openPreview(file) {
  const archive = state.selectedArchive;
  if (!archive || !file || file.kind === "folder") return;
  const url = archiveFileUrl(archive.release_id, file.relative_path, false);
  const downloadUrl = archiveEntryDownloadUrl(archive.release_id, file.relative_path, "file");
  const name = basename(file.relative_path);
  const kind = previewKindFor(file);
  const sizeLabel = formatBytes(file.original_size || 0);

  $("previewName").textContent = name;
  $("previewMeta").textContent = `${KIND_LABEL[file.kind] || "File"} · ${sizeLabel}`;
  const dl = $("previewDownloadButton");
  dl.href = downloadUrl;
  dl.setAttribute("download", name);

  const body = $("previewBody");
  body.innerHTML = `<div class="preview-spinner" aria-hidden="true"></div>`;
  $("previewBackdrop").classList.add("open");

  if (kind === "image") {
    body.innerHTML = `<img alt="${escapeHtml(name)}" src="${escapeHtml(url)}">`;
  } else if (kind === "video") {
    body.innerHTML = `<video controls preload="metadata" src="${escapeHtml(url)}"></video>`;
  } else if (kind === "audio") {
    body.innerHTML = `<audio controls preload="metadata" src="${escapeHtml(url)}"></audio>`;
  } else if (kind === "pdf") {
    body.innerHTML = `<iframe title="${escapeHtml(name)}" src="${escapeHtml(url)}"></iframe>`;
  } else if (kind === "text") {
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const buf = await response.arrayBuffer();
      const slice = buf.slice(0, TEXT_PREVIEW_MAX_BYTES);
      const decoder = new TextDecoder("utf-8", { fatal: false });
      let text = decoder.decode(slice);
      if (buf.byteLength > TEXT_PREVIEW_MAX_BYTES) {
        text += `\n\n--- truncated (${formatBytes(buf.byteLength - TEXT_PREVIEW_MAX_BYTES)} more) ---`;
      }
      body.innerHTML = `<pre>${escapeHtml(text)}</pre>`;
    } catch (error) {
      body.innerHTML = `<div class="preview-fallback">Could not load preview: ${escapeHtml(error.message)}<br><br><a class="btn btn-primary" href="${escapeHtml(downloadUrl)}" download>Download</a></div>`;
    }
  } else {
    body.innerHTML = `<div class="preview-fallback">Preview not available for this file type.<br><br><a class="btn btn-primary" href="${escapeHtml(downloadUrl)}" download>Download</a></div>`;
  }
}

function closePreview() {
  const backdrop = $("previewBackdrop");
  backdrop.classList.remove("open");
  // Stop video/audio playback by emptying the body.
  $("previewBody").innerHTML = "";
}

async function deleteArchiveEntry(relativePath) {
  const archive = state.selectedArchive;
  if (!archive || !relativePath) return;
  if (!confirm(`Delete ${basename(relativePath)} from this archive?`)) return;
  try {
    const result = await fetchJson(`/api/archives/${archive.release_id}/files`, {
      method: "DELETE",
      body: JSON.stringify({ relative_path: relativePath }),
    });
    if (result.archive_deleted) {
      state.selectedArchive = null;
      state.selectedArchiveContents = null;
      state.selectedArchivePath = "";
      showArchiveListView();
      await loadArchives({ reset: true });
      return;
    }
    await loadSelectedArchiveContents();
    await loadArchives({ reset: true });
  } catch (error) {
    alert(error.message);
  }
}

async function loadArchives(options = {}) {
  const reset = options.reset !== false;
  if (!state.me.token_present || !state.me.repo) {
    state.archives = [];
    state.archivePage = 0;
    state.archiveHasMore = false;
    state.archiveLoading = false;
    showArchiveListView();
    renderArchives();
    updateStorageUsage();
    return;
  }
  if (state.archiveLoading) return;
  state.archiveLoading = true;
  syncArchivesPagination();
  try {
    const targetPage = reset ? 1 : Math.max(1, state.archivePage + 1);
    const payload = await fetchJson(`/api/archives?page=${targetPage}&per_page=${state.archivePageSize}`);
    const archives = payload.archives || [];
    state.archives = reset ? archives : mergeArchives(state.archives, archives);
    state.archivePage = Number(payload.page || targetPage);
    state.archivePageSize = Number(payload.per_page || state.archivePageSize || 24);
    state.archiveHasMore = Boolean(payload.has_more);
    renderArchives();
    updateStorageUsage();
    updatePageChrome();
  } catch (error) {
    if (reset) {
      state.archives = [];
      state.archivePage = 0;
      state.archiveHasMore = false;
    }
    renderArchives();
    updateStorageUsage();
    handleCredentialError(error);
    console.error("loadArchives", error);
  } finally {
    state.archiveLoading = false;
    syncArchivesPagination();
  }
}

async function loadMoreArchives() {
  if (!state.archiveHasMore || state.archiveLoading) return;
  await loadArchives({ reset: false });
}

// ── Archive detail / download modal ───────────────────────────────────────────

function openArchiveDetail(archive) {
  state.selectedArchive = archive;
  state.selectedArchiveContents = null;
  state.selectedArchivePath = "";
  const meta = archive.archive || {};
  const title = meta.source_name || archive.name || archive.tag || "Archive";
  const count = meta.total_items || archive.asset_count || 0;
  const created = formatDate(meta.created_at || archive.created_at || "");
  $("archiveDetailTitle").textContent = title;
  $("archiveDetailMeta").textContent = `${count} item${count === 1 ? "" : "s"} · ${created}`;
  $("archiveDetailTagText").textContent = archive.tag ? `Tag: ${archive.tag}` : "";
  $("archiveBrowserStatus").textContent = "Loading files…";
  $("archiveBrowserGrid").innerHTML = "";
  $("archiveBrowserNote").textContent = "";
  syncArchiveBrowserToolbar();
  updatePageChrome();
  const githubLink = $("archiveOpenOnGithub");
  if (archive.html_url) {
    githubLink.href = archive.html_url;
    githubLink.style.display = "";
  } else {
    githubLink.style.display = "none";
  }
  loadSelectedArchiveContents()
    .then(() => {
      const contents = state.selectedArchiveContents;
      if (isSingleFileArchive(contents)) {
        showArchiveListView();
        const [entry] = contents.entries || [];
        if (entry) openPreview(entry);
        return;
      }
      showArchiveBrowserView();
    })
    .catch((error) => {
      showArchiveBrowserView();
      $("archiveBrowserStatus").textContent = error.message;
      $("archiveBrowserGrid").innerHTML = "";
    });
}

async function startDownload() {
  const archive = state.selectedArchive;
  if (!archive) return;
  const workers = Number($("downloadWorkers").value || 2);
  showTransferToast();
  try {
    const result = await fetchJson("/api/download", {
      method: "POST",
      body: JSON.stringify({ tag: archive.tag, workers, retries: 3 }),
    });
    state.activeDownloadTaskId = result.task_id;
    await loadTasks();
  } catch (error) {
    if (handleCredentialError(error)) return;
    alert(error.message);
  }
}

async function deleteSelectedArchive() {
  const archive = state.selectedArchive;
  if (!archive) return;
  if (!confirm(`Delete the entire archive "${archiveTitle(archive)}"?`)) return;
  try {
    await fetchJson(`/api/archives/${archive.release_id}`, { method: "DELETE" });
    state.selectedArchive = null;
    state.selectedArchiveContents = null;
    state.selectedArchivePath = "";
    showArchiveListView();
    await loadArchives();
  } catch (error) {
    if (handleCredentialError(error)) return;
    alert(error.message);
  }
}

// ── Tasks → transfer toast ────────────────────────────────────────────────────

function shouldShowToast(tasks) {
  if (state.toastDismissed) return false;
  if (!tasks.length) return false;
  const hasActive = tasks.some((t) => t.status === "queued" || t.status === "running");
  const hasFreshDownload = tasks.some((t) => t.type === "download" && t.status === "completed" && t.id === state.activeDownloadTaskId);
  return hasActive || hasFreshDownload;
}

function showTransferToast() {
  state.toastDismissed = false;
  $("transferToast").classList.add("open");
}

function hideTransferToast() {
  $("transferToast").classList.remove("open");
}

function renderTasks(tasks) {
  const body = $("transferToastBody");
  const title = $("transferToastTitle");

  if (shouldShowToast(tasks)) {
    showTransferToast();
  } else if (state.toastDismissed) {
    hideTransferToast();
  }

  const active = tasks.filter((t) => t.status === "queued" || t.status === "running");
  const currentDownload = tasks.find((t) => t.type === "download" && t.status === "completed" && t.id === state.activeDownloadTaskId);
  const ordered = active.length ? active : (currentDownload ? [currentDownload] : []);
  title.textContent = ordered.length ? `Transfers (${ordered.length})` : "Transfers";

  body.innerHTML = ordered.map((task) => {
    const payload = task.payload || {};
    const result = task.result || {};
    const total = Number(task.progress_total || 0);
    const done = Number(task.progress_done || 0);
    const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : (task.status === "completed" ? 100 : 0);

    const label = task.type === "upload"
      ? uploadTaskLabel(payload)
      : (result.title || payload.tag || `release ${payload.release_id || ""}`);
    const icon = task.type === "upload"
      ? `<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M9 16h6v-6h4l-7-7-7 7h4v6zm-4 2h14v2H5v-2z"/></svg>`
      : `<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>`;

    const downloadLink = (task.type === "download" && task.status === "completed")
      ? `<a class="download-link" href="/api/download-file/${escapeHtml(task.id)}">Save ZIP to computer</a>`
      : "";
    const releaseLink = result.html_url
      ? `<a class="release-link" href="${escapeHtml(result.html_url)}" target="_blank" rel="noreferrer">View release</a>`
      : "";

    return `
      <div class="transfer-row">
        <div class="name">${icon}<span>${escapeHtml(label)}</span><span class="status ${escapeHtml(task.status)}">${escapeHtml(task.status)}</span></div>
        ${total > 0 ? `<div class="progress-bar"><div class="progress-bar-fill" style="width:${pct}%"></div></div>` : ""}
        <div class="meta">
          <span>${total > 0 ? `${done} of ${total}` : ""}</span>
          <span>${escapeHtml(formatDate(new Date(task.created_at * 1000).toISOString()))}</span>
        </div>
        ${downloadLink}${releaseLink}
        ${task.error ? `<div class="error">${escapeHtml(task.error)}</div>` : ""}
      </div>
    `;
  }).join("");
}

async function loadTasks() {
  if (!hasConfiguredRepo()) {
    clearTaskPollTimer();
    return;
  }
  if (state.taskPollInFlight) return;
  state.taskPollInFlight = true;
  try {
    const payload = await fetchJson("/api/tasks");
    const tasks = payload.tasks || [];
    renderTasks(tasks);
    state.taskPollHasActive = hasActiveTasks(tasks);
    state.taskPollErrorCount = 0;
    // If a freshly-completed upload comes in, refresh the archives list.
    const completedUpload = tasks.find((t) => (
      t.type === "upload"
      && t.status === "completed"
      && !state.refreshedUploadTaskIds[t.id]
    ));
    if (completedUpload) {
      state.refreshedUploadTaskIds[completedUpload.id] = true;
      loadArchives({ reset: true });
    }
  } catch (_) {
    state.taskPollErrorCount += 1;
  } finally {
    state.taskPollInFlight = false;
    scheduleTaskPoll();
  }
}

// ── Upload ────────────────────────────────────────────────────────────────────

function uploadTaskLabel(payload) {
  if (payload.upload_origin !== "browser-transfer") return payload.source_path || "Upload";
  const displayName = payload.browser_display_name || payload.source_name_override || "";
  const count = Number(payload.uploaded_file_count || 0);
  if (displayName && count <= 1) return displayName;
  const countLabel = `${count} file${count === 1 ? "" : "s"}`;
  return displayName ? `${displayName} - ${countLabel}` : countLabel;
}

function normalizeUploadRelativePath(path) {
  return String(path || "")
    .replace(/\\/g, "/")
    .replace(/\/+/g, "/")
    .replace(/^\/+|\/+$/g, "");
}

function uploadRelativePathForFile(file) {
  return normalizeUploadRelativePath(file.webkitRelativePath || file.name || "upload");
}

function groupFilesForUpload(files) {
  const groups = new Map();
  let looseFileIndex = 0;
  for (const file of files) {
    const relativePath = uploadRelativePathForFile(file);
    const parts = relativePath.split("/").filter(Boolean);
    const folderRoot = parts.length > 1 ? parts[0] : "";
    const entry = { file, relativePath: relativePath || file.name || "upload" };
    if (!folderRoot) {
      groups.set(`file:${looseFileIndex}`, {
        key: `file:${looseFileIndex}`,
        type: "file",
        name: basename(entry.relativePath) || file.name || "File",
        entries: [entry],
      });
      looseFileIndex += 1;
      continue;
    }

    const key = `folder:${folderRoot}`;
    if (!groups.has(key)) {
      groups.set(key, {
        key,
        type: "folder",
        name: folderRoot,
        entries: [],
      });
    }
    groups.get(key).entries.push(entry);
  }
  return Array.from(groups.values());
}

function makeUploadFormData(uploadForm, group) {
  const formData = new FormData(uploadForm);
  formData.set("encrypt", uploadForm.querySelector('input[name="encrypt"]').checked ? "true" : "false");
  formData.append("retries", "3");
  formData.append("recursive", "true");
  if (group.appendTag) formData.append("append_tag", group.appendTag);
  if (group.appendRelativePath) formData.append("append_relative_path", group.appendRelativePath);
  if (group.sourceNameOverride) formData.append("source_name_override", group.sourceNameOverride);
  if (group.sourceTypeOverride) formData.append("source_type_override", group.sourceTypeOverride);

  for (const entry of group.entries) {
    formData.append("files", entry.file, entry.file.name);
    formData.append("relative_paths", entry.relativePath);
  }
  return formData;
}

async function uploadFileGroup(group, uploadForm) {
  const formData = makeUploadFormData(uploadForm, group);
  const headers = {};
  if (window.__GD_CSRF_TOKEN__) headers["X-CSRF-Token"] = window.__GD_CSRF_TOKEN__;
  const response = await fetch("/api/upload-files", { method: "POST", body: formData, headers, credentials: "same-origin" });
  if (response.status === 401) {
    window.location.href = "/login";
    throw new Error("Login required");
  }
  let payload = {};
  try { payload = await response.json(); } catch (_) {}
  if (!response.ok) {
    const error = new Error(payload.error || `${group.name} upload failed (${response.status})`);
    error.payload = payload;
    error.status = response.status;
    throw error;
  }
  // Refresh the transfer list right away so the new task appears without
  // waiting for the next poll tick (it was previously sitting on "queued"
  // for up to 1.5s before the UI even saw it had moved to "running").
  loadTasks().catch(() => {});
  return payload;
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function waitForUploadTask(taskId) {
  while (true) {
    let task;
    try {
      task = await fetchJson(`/api/tasks/${taskId}`);
    } catch (error) {
      if (error.status === 404) {
        await sleep(1500);
        continue;
      }
      throw error;
    }
    try {
      await loadTasks();
    } catch (_) {
      // Best-effort UI refresh while the dedicated wait loop continues.
    }
    if (task.status === "completed") {
      return task;
    }
    if (task.status === "failed") {
      throw new Error(task.error || "Upload failed");
    }
    await sleep(1500);
  }
}

async function uploadFileGroups(groups, uploadForm) {
  const results = new Array(groups.length);
  // Submit one archive at a time and wait for completion before starting the next.
  // This keeps independent uploads independent while respecting the free-tier task/memory limits.
  for (let index = 0; index < groups.length; index += 1) {
    try {
      const queued = await uploadFileGroup(groups[index], uploadForm);
      const task = await waitForUploadTask(queued.task_id);
      results[index] = { status: "fulfilled", value: task };
    } catch (error) {
      results[index] = { status: "rejected", reason: error };
      if ([401, 403, 409, 429].includes(Number(error.status || 0))) break;
      if ((error.message || "").includes("active transfer")) break;
    }
  }
  return results;
}

async function uploadSelectedFiles(files) {
  if (!files.length) return;
  if (!state.me.token_present || !state.me.repo) {
    openModal("credsModal");
    return;
  }

  const uploadForm = $("uploadForm");
  const groups = groupFilesForUpload(files).map((group) => {
    if (!isMutableArchiveContext()) return group;
    const archive = state.selectedArchive;
    const currentPath = normalizeArchivePath(state.selectedArchivePath);
    const archiveName = archiveTitle(archive);
    return {
      ...group,
      appendTag: archive.tag,
      appendRelativePath: currentPath,
      sourceNameOverride: archiveName,
      sourceTypeOverride: "directory",
    };
  });

  showTransferToast();
  const mutableContext = isMutableArchiveContext();
  try {
    const results = await uploadFileGroups(groups, uploadForm);
    await loadTasks();
    const failures = results.filter((result) => result.status === "rejected");
    if (failures.length) {
      const recoveryFailure = failures.find((failure) => failure.reason?.payload?.credential_recovery_required);
      if (recoveryFailure) {
        handleCredentialError(recoveryFailure.reason);
        return;
      }
      const uniqueMessages = [];
      for (const failure of failures) {
        const message = failure.reason?.message || "Upload failed";
        if (!uniqueMessages.includes(message)) uniqueMessages.push(message);
      }
      throw new Error(uniqueMessages.join("\n"));
    }
    if (mutableContext && state.selectedArchive) {
      await loadSelectedArchiveContents();
      await loadArchives({ reset: true });
    }
  } catch (error) {
    throw error;
  }
}

async function createEmptyFolder() {
  const rawName = prompt("Folder name");
  const folderName = normalizeUploadRelativePath(rawName || "");
  if (!folderName) return;
  try {
    if (isMutableArchiveContext()) {
      const currentPath = normalizeArchivePath(state.selectedArchivePath);
      const fullPath = currentPath ? `${currentPath}/${folderName}` : folderName;
      await fetchJson(`/api/archives/${state.selectedArchive.release_id}/folders`, {
        method: "POST",
        body: JSON.stringify({ path: fullPath }),
      });
      await loadSelectedArchiveContents();
      state.selectedArchivePath = fullPath;
      renderArchiveContents();
      return;
    }

    const created = await fetchJson("/api/archives/folders", {
      method: "POST",
      body: JSON.stringify({ name: folderName }),
    });
    await loadArchives({ reset: true });
    state.selectedArchive = created;
    state.selectedArchivePath = "";
    await loadSelectedArchiveContents();
    showArchiveBrowserView();
  } catch (error) {
    if (handleCredentialError(error)) return;
    alert(error.message);
  }
}

function syncNewMenuState() {
  const button = $("newEmptyFolderButton");
  if (!button) return;
  button.style.display = state.selectedArchive && !isMutableArchiveContext() ? "none" : "";
}

// ── Sidebar + topbar interactions ─────────────────────────────────────────────

function setupNewMenu() {
  const button = $("newButton");
  const menu = $("newMenu");
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    syncNewMenuState();
    menu.classList.toggle("open");
  });
  document.addEventListener("click", (event) => {
    if (!menu.contains(event.target) && event.target !== button) {
      menu.classList.remove("open");
    }
  });
  $("newFileUploadButton").addEventListener("click", () => {
    menu.classList.remove("open");
    $("filePicker").click();
  });
  $("newFolderUploadButton").addEventListener("click", () => {
    menu.classList.remove("open");
    $("folderPicker").click();
  });
  $("newEmptyFolderButton").addEventListener("click", async () => {
    menu.classList.remove("open");
    await createEmptyFolder();
  });
  $("openCredsFromMenu2").addEventListener("click", () => {
    menu.classList.remove("open");
    syncCredsModal();
    openModal("credsModal");
  });
}

function setupAccountMenu() {
  const avatar = $("userAvatar");
  const popover = $("accountPopover");
  avatar.addEventListener("click", (event) => {
    event.stopPropagation();
    popover.classList.toggle("open");
  });
  document.addEventListener("click", (event) => {
    if (!popover.contains(event.target) && event.target !== avatar) {
      popover.classList.remove("open");
    }
  });
  $("openCredsFromMenuButton").addEventListener("click", () => {
    popover.classList.remove("open");
    syncCredsModal();
    openModal("credsModal");
  });
}

function setupSearch() {
  $("searchInput").addEventListener("input", (event) => {
    state.searchTerm = event.target.value || "";
    renderArchives();
  });
}

function setupFilePickers() {
  const filePicker = $("filePicker");
  const folderPicker = $("folderPicker");

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

function setupTransferToast() {
  $("transferToastClose").addEventListener("click", () => {
    state.toastDismissed = true;
    hideTransferToast();
  });
}

function setupRefresh() {
  const handler = async () => {
    await loadArchives({ reset: true });
    await loadTasks();
  };
  $("refreshButton").addEventListener("click", handler);
  $("refreshArchivesButton").addEventListener("click", handler);
  $("archiveRefreshButton").addEventListener("click", async () => {
    if (state.selectedArchive) {
      await loadSelectedArchiveContents();
    }
    await loadArchives({ reset: true });
    await loadTasks();
  });
}

// ── View toggle (grid / list) ────────────────────────────────────────────────

function setupViewToggle() {
  applyViewMode(state.viewMode);
  $("viewToggle").addEventListener("click", (event) => {
    const btn = event.target.closest("button[data-view]");
    if (!btn) return;
    const mode = btn.dataset.view;
    if (mode === state.viewMode) return;
    state.viewMode = mode;
    try { localStorage.setItem("gd_view_mode", mode); } catch (_) { /* ignore */ }
    applyViewMode(mode);
    renderArchives();
  });
}

function setupArchivesPagination() {
  $("loadMoreArchivesButton").addEventListener("click", async () => {
    await loadMoreArchives();
  });
}

function applyViewMode(mode) {
  $("viewToggle").querySelectorAll("button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === mode);
  });
  show("archivesGrid", mode === "grid");
  show("archivesList", mode === "list");
}

function renderArchivesList(archives) {
  const body = $("archivesListBody");
  if (!archives.length) { body.innerHTML = ""; return; }
  body.innerHTML = archives.map((archive, index) => {
    const meta = archive.archive || {};
    const title = archiveTitle(archive);
    const count = archiveItemCount(archive);
    const created = formatDate(meta.created_at || archive.created_at || "");
    const kind = dominantKind(archive);
    const kindLabel = KIND_LABEL[kind] || "Files";
    return `
      <div class="list-row" data-index="${index}">
        <div class="row-icon">${KIND_ICONS[kind] || ARCHIVE_ICON_INLINE}</div>
        <div>${escapeHtml(title)}</div>
        <div>${count} item${count === 1 ? "" : "s"}</div>
        <div>${escapeHtml(created)}</div>
        <div><code>${escapeHtml(archive.tag)}</code></div>
        <div class="archive-list-actions">
          <button type="button" class="icon-button archive-card-action" data-delete-archive-index="${index}" title="Delete archive" aria-label="Delete archive">
            ${ICON_DELETE}
          </button>
        </div>
      </div>
    `;
  }).join("");
  body.querySelectorAll(".list-row").forEach((row) => {
    row.addEventListener("click", () => {
      const archive = state.filteredArchives[Number(row.dataset.index)];
      if (archive) openArchiveDetail(archive);
    });
    row.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      const archive = state.filteredArchives[Number(row.dataset.index)];
      if (archive) showArchiveContextMenu(event.clientX, event.clientY, archive);
    });
  });
  body.querySelectorAll("[data-delete-archive-index]").forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      const archive = state.filteredArchives[Number(button.dataset.deleteArchiveIndex)];
      await deleteArchiveRecord(archive);
    });
  });
}

// ── Drag-and-drop upload ─────────────────────────────────────────────────────

function setupDragAndDrop() {
  const overlay = $("dropOverlay");
  let depth = 0;
  const isDragWithFiles = (event) => Array.from(event.dataTransfer?.types || []).includes("Files");

  window.addEventListener("dragenter", (event) => {
    if (!isDragWithFiles(event)) return;
    event.preventDefault();
    depth += 1;
    overlay.classList.add("active");
  });
  window.addEventListener("dragover", (event) => {
    if (!isDragWithFiles(event)) return;
    event.preventDefault();
  });
  window.addEventListener("dragleave", (event) => {
    if (!isDragWithFiles(event)) return;
    depth = Math.max(0, depth - 1);
    if (depth === 0) overlay.classList.remove("active");
  });
  window.addEventListener("drop", async (event) => {
    if (!isDragWithFiles(event)) return;
    event.preventDefault();
    depth = 0;
    overlay.classList.remove("active");
    const files = await collectDroppedFiles(event.dataTransfer);
    if (files.length === 0) return;
    try {
      await uploadSelectedFiles(files);
    } catch (error) {
      alert(error.message);
    }
  });
}

async function collectDroppedFiles(dataTransfer) {
  // Prefer the modern items[] API so we can recurse into dropped folders.
  const items = dataTransfer && dataTransfer.items ? Array.from(dataTransfer.items) : [];
  const collected = [];
  if (items.length && items[0].webkitGetAsEntry) {
    const entries = items.map((item) => item.webkitGetAsEntry()).filter(Boolean);
    await Promise.all(entries.map((entry) => walkEntry(entry, "", collected)));
    return collected;
  }
  // Fallback: flat list of files.
  return Array.from(dataTransfer.files || []);
}

function walkEntry(entry, pathPrefix, collected) {
  return new Promise((resolve) => {
    if (entry.isFile) {
      entry.file((file) => {
        // Mimic webkitRelativePath so uploadSelectedFiles produces the right relative_paths.
        const relative = pathPrefix ? `${pathPrefix}${file.name}` : file.name;
        try { Object.defineProperty(file, "webkitRelativePath", { value: relative }); }
        catch (_) { /* ignore on older browsers */ }
        collected.push(file);
        resolve();
      }, () => resolve());
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const all = [];
      const readBatch = () => {
        reader.readEntries(async (entries) => {
          if (!entries.length) {
            await Promise.all(all.map((child) => walkEntry(child, `${pathPrefix}${entry.name}/`, collected)));
            resolve();
          } else {
            all.push(...entries);
            readBatch();
          }
        }, () => resolve());
      };
      readBatch();
    } else {
      resolve();
    }
  });
}

// ── Right-click context menu ─────────────────────────────────────────────────

function setupContextMenu() {
  document.addEventListener("click", () => closeContextMenu());
  document.addEventListener("scroll", () => closeContextMenu(), true);
  window.addEventListener("resize", () => closeContextMenu());
}

function closeContextMenu() { $("contextMenu").classList.remove("open"); }

function buildContextMenu(items, x, y) {
  const menu = $("contextMenu");
  menu.innerHTML = items.map((item, i) => {
    if (item.divider) return `<div class="divider" data-i="${i}"></div>`;
    return `<button type="button" class="${item.destructive ? "destructive" : ""}" data-i="${i}">
      ${item.icon || ""}<span>${escapeHtml(item.label)}</span>
    </button>`;
  }).join("");
  menu.querySelectorAll("button[data-i]").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      closeContextMenu();
      const item = items[Number(btn.dataset.i)];
      if (item && item.onClick) item.onClick();
    });
  });
  // Position, keeping inside viewport.
  menu.classList.add("open");
  const rect = menu.getBoundingClientRect();
  const left = Math.min(x, window.innerWidth - rect.width - 8);
  const top = Math.min(y, window.innerHeight - rect.height - 8);
  menu.style.left = `${Math.max(8, left)}px`;
  menu.style.top = `${Math.max(8, top)}px`;
}

const ICON_OPEN = `<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M19 19H5V5h7V3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14c1.1 0 2-.9 2-2v-7h-2v7zM14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7z"/></svg>`;
const ICON_DOWNLOAD = `<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>`;
const ICON_DELETE = `<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>`;
const ICON_LINK = `<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M3.9 12a3.1 3.1 0 0 1 3.1-3.1h4V7H7a5 5 0 0 0 0 10h4v-1.9H7A3.1 3.1 0 0 1 3.9 12zM8 13h8v-2H8v2zm9-6h-4v1.9h4a3.1 3.1 0 0 1 0 6.2h-4V17h4a5 5 0 0 0 0-10z"/></svg>`;

function showArchiveContextMenu(x, y, archive) {
  const items = [
    { label: "Open", icon: ICON_OPEN, onClick: () => openArchiveDetail(archive) },
    { label: "Download archive", icon: ICON_DOWNLOAD, onClick: () => {
        state.selectedArchive = archive;
        startDownload();
      } },
    { label: "View on GitHub", icon: ICON_LINK, onClick: () => {
        if (archive.html_url) window.open(archive.html_url, "_blank");
      } },
    { divider: true },
    { label: "Delete archive", icon: ICON_DELETE, destructive: true, onClick: async () => deleteArchiveRecord(archive) },
  ];
  buildContextMenu(items, x, y);
}

function showEntryContextMenu(x, y, entry) {
  const archive = state.selectedArchive;
  if (!archive) return;
  const isFolder = entry.kind === "folder";
  const items = [
    !isFolder && { label: "Preview", icon: ICON_OPEN, onClick: () => openPreview(entry) },
    { label: isFolder ? "Download as ZIP" : "Download", icon: ICON_DOWNLOAD, onClick: () => {
        downloadSelectedEntry(entry.relative_path, isFolder ? "folder" : "file");
      } },
    !isFolder && { divider: true },
    !isFolder && { label: "Delete file", icon: ICON_DELETE, destructive: true, onClick: () => deleteArchiveEntry(entry.relative_path) },
  ].filter(Boolean);
  buildContextMenu(items, x, y);
}

// ── Preview backdrop close handlers ──────────────────────────────────────────

function setupPreviewBackdrop() {
  const backdrop = $("previewBackdrop");
  $("previewCloseButton").addEventListener("click", closePreview);
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) closePreview();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && backdrop.classList.contains("open")) closePreview();
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  setupModalDismiss();
  setupNewMenu();
  setupAccountMenu();
  setupFilterChips();
  setupSearch();
  setupFilePickers();
  setupTransferToast();
  setupRefresh();
  setupViewToggle();
  setupArchivesPagination();
  setupDragAndDrop();
  setupContextMenu();
  setupPreviewBackdrop();
  $("credsForm").addEventListener("submit", submitCreds);
  $("clearCredsButton").addEventListener("click", clearCreds);
  $("exportAccountButton").addEventListener("click", exportAccountData);
  $("reportAbuseButton").addEventListener("click", reportAbuse);
  $("deleteAccountButton").addEventListener("click", deleteAccount);
  $("startDownloadButton").addEventListener("click", startDownload);
  $("downloadCurrentFolderButton").addEventListener("click", () => {
    const path = normalizeArchivePath(state.selectedArchivePath);
    if (path) downloadSelectedEntry(path, "folder");
  });
  $("archiveDeleteButton").addEventListener("click", deleteSelectedArchive);
  $("backToArchivesButton").addEventListener("click", () => {
    state.selectedArchive = null;
    state.selectedArchiveContents = null;
    state.selectedArchivePath = "";
    showArchiveListView();
  });

  try {
    showArchiveListView();
    await loadMe();
    await loadArchives({ reset: true });
    await loadTasks();
  } catch (error) {
    console.error("init error", error);
  }

  document.addEventListener("visibilitychange", () => {
    if (!hasConfiguredRepo()) return;
    scheduleTaskPoll(document.visibilityState === "visible" ? 0 : nextTaskPollDelay());
  });
}

window.addEventListener("DOMContentLoaded", init);
