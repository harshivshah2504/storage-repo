/* GitHub Drive — Drive-style frontend behaviour */

const state = {
  archives: [],
  filteredArchives: [],
  searchTerm: "",
  selectedArchive: null,
  selectedArchiveContents: null,
  selectedArchivePath: "",
  activeDownloadTaskId: null,
  me: { username: "", token_present: false, repo: "" },
  toastDismissed: false,
  filters: { type: "all", modified: "any", sort: "newest" },
};

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

// ── Network helper ────────────────────────────────────────────────────────────

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
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
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

function show(id, visible) {
  const node = $(id);
  if (node) node.style.display = visible ? "" : "none";
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
  state.me = me;
  const repo = me.repo || "";
  const configured = Boolean(me.token_present && repo);

  $("popoverRepoLine").textContent = configured ? repo : "No GitHub repo configured";
  $("sidebarRepoLine").textContent = configured ? repo : "Not connected";

  if (!configured) {
    // Force the user to set credentials before they can do anything else.
    openModal("credsModal");
  }
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
    closeModal("credsModal");
    await loadMe();
    await loadArchives();
    showFlash(`Connected to ${result.repo}`);
  } catch (error) {
    alert(error.message);
  }
}

async function clearCreds() {
  if (!confirm("Forget the saved GitHub token? You'll need to enter it again.")) return;
  try {
    await fetchJson("/api/me/credentials", { method: "DELETE" });
    closeModal("credsModal");
    await loadMe();
    state.archives = [];
    renderArchives();
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

function basename(relativePath) {
  const parts = String(relativePath || "").split("/").filter(Boolean);
  return parts[parts.length - 1] || relativePath || "";
}

function normalizeArchivePath(path) {
  return String(path || "").replace(/^\/+|\/+$/g, "");
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

function renderArchives() {
  applyFiltersAndSort();
  const grid = $("archivesGrid");
  const empty = $("archivesEmpty");
  if (!state.filteredArchives.length) {
    grid.innerHTML = "";
    grid.style.display = "none";
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
  grid.style.display = "";
  grid.innerHTML = state.filteredArchives.map((archive, index) => {
    const meta = archive.archive || {};
    const title = archiveTitle(archive);
    const count = archiveItemCount(archive);
    const created = formatDate(meta.created_at || archive.created_at || "");
    const kind = dominantKind(archive);
    const kindLabel = KIND_LABEL[kind] || "Files";
    const hasCover = Boolean(meta.cover_asset_name) || kind === "image";
    const thumb = hasCover
      ? `<img loading="lazy" decoding="async" src="/api/archives/${archive.release_id}/cover" alt="">`
      : `<div class="archive-thumb-fallback">${KIND_ICONS[kind] || ARCHIVE_ICON_INLINE}</div>`;
    return `
      <div class="archive-card" data-index="${index}">
        <div class="archive-thumb">
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
  // Wire fallback for broken images without inline JS attribute string escapes.
  grid.querySelectorAll(".archive-thumb img").forEach((img) => {
    img.addEventListener("error", () => {
      const card = img.closest(".archive-card");
      const idx = Number(card?.dataset.index);
      const archive = state.filteredArchives[idx];
      const kind = archive ? dominantKind(archive) : "other";
      img.parentElement.innerHTML = `<div class="archive-thumb-fallback">${KIND_ICONS[kind] || ARCHIVE_ICON_INLINE}</div>`;
    });
  });
  grid.querySelectorAll(".archive-card").forEach((card) => {
    card.addEventListener("click", () => {
      const archive = state.filteredArchives[Number(card.dataset.index)];
      openArchiveDetail(archive);
    });
  });
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
  const path = normalizeArchivePath(state.selectedArchivePath);
  const parts = path ? path.split("/") : [];
  const crumbs = [{ label: "All files", path: "" }];
  let acc = "";
  for (const part of parts) {
    acc = acc ? `${acc}/${part}` : part;
    crumbs.push({ label: part, path: acc });
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
  renderArchiveBreadcrumbs();
  note.textContent = contents.supports_file_delete
    ? "Open folders, preview images, and remove files from this archive."
    : "This archive was bundled for faster upload, so individual file delete is unavailable.";

  const { folders, files } = listArchiveChildren(contents.entries || [], state.selectedArchivePath);
  status.textContent = `${folders.length + files.length} item${folders.length + files.length === 1 ? "" : "s"} here`;

  if (!folders.length && !files.length) {
    grid.innerHTML = `<div class="archive-node-empty">Nothing in this folder.</div>`;
    return;
  }

  const folderCards = folders.map((folder) => `
    <button type="button" class="archive-node folder" data-folder-path="${escapeHtml(folder.path)}">
      <div class="archive-node-icon">${FOLDER_ICON_INLINE}</div>
      <div class="archive-node-body">
        <div class="archive-node-name">${escapeHtml(folder.name)}</div>
        <div class="archive-node-meta">
          <span>${folder.fileCount} file${folder.fileCount === 1 ? "" : "s"}</span>
          ${folder.imageCount ? `<span>${folder.imageCount} image${folder.imageCount === 1 ? "" : "s"}</span>` : ""}
        </div>
      </div>
    </button>
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
        <div class="archive-node-actions">
          <button type="button" class="icon-button" data-delete-path="${escapeHtml(file.relative_path)}" title="Delete file">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zm3.46-7.12 1.41-1.41L12 11.59l1.12-1.12 1.41 1.41L13.41 13l1.12 1.12-1.41 1.41L12 14.41l-1.12 1.12-1.41-1.41L10.59 13l-1.13-1.12zM15.5 4l-1-1h-5l-1 1H5v2h14V4z"/></svg>
          </button>
        </div>
      `
      : "";
    return `
      <div class="archive-node file" data-file-path="${escapeHtml(file.relative_path)}" data-previewable="${previewable ? "1" : "0"}">
        <div class="archive-node-thumb">${thumb}</div>
        <div class="archive-node-body">
          <div class="archive-node-name">${escapeHtml(basename(file.relative_path))}</div>
          <div class="archive-node-meta">
            <span>${escapeHtml(KIND_LABEL[file.kind] || "File")}</span>
            <span>${escapeHtml(formatBytes(file.original_size || 0))}</span>
          </div>
        </div>
        ${deleteButton}
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
  grid.querySelectorAll("[data-file-path]").forEach((card) => {
    card.addEventListener("click", () => {
      if (card.dataset.previewable !== "1") return;
      const entry = (contents.entries || []).find((item) => item.relative_path === card.dataset.filePath);
      if (entry) openImagePreview(entry);
    });
  });
  grid.querySelectorAll("[data-delete-path]").forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      await deleteArchiveEntry(button.dataset.deletePath || "");
    });
  });
  grid.querySelectorAll(".archive-node-thumb img").forEach((img) => {
    img.addEventListener("error", () => {
      const filePath = img.closest("[data-file-path]")?.dataset.filePath || "";
      const entry = (contents.entries || []).find((item) => item.relative_path === filePath);
      const kind = entry?.kind || "other";
      img.parentElement.innerHTML = `<div class="archive-node-icon">${KIND_ICONS[kind] || ARCHIVE_ICON_INLINE}</div>`;
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

async function loadSelectedArchiveContents() {
  const archive = state.selectedArchive;
  if (!archive) return;
  $("archiveBrowserStatus").textContent = "Loading files…";
  $("archiveBrowserGrid").innerHTML = "";
  const contents = await fetchJson(`/api/archives/${archive.release_id}/contents`);
  state.selectedArchiveContents = contents;
  syncSelectedArchiveFromContents(contents);
  renderArchiveContents();
}

function openImagePreview(file) {
  const archive = state.selectedArchive;
  if (!archive) return;
  $("imagePreviewTitle").textContent = basename(file.relative_path);
  $("imagePreviewMeta").textContent = `${KIND_LABEL[file.kind] || "File"} · ${formatBytes(file.original_size || 0)}`;
  $("imagePreviewImage").src = archiveFileUrl(archive.release_id, file.relative_path, false);
  openModal("imagePreviewModal");
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
      closeModal("archiveDetailModal");
      state.selectedArchive = null;
      state.selectedArchiveContents = null;
      await loadArchives();
      return;
    }
    await loadSelectedArchiveContents();
    await loadArchives();
  } catch (error) {
    alert(error.message);
  }
}

async function loadArchives() {
  if (!state.me.token_present || !state.me.repo) {
    state.archives = [];
    renderArchives();
    return;
  }
  try {
    const payload = await fetchJson("/api/archives");
    state.archives = payload.archives || [];
    renderArchives();
  } catch (error) {
    state.archives = [];
    renderArchives();
    console.error("loadArchives", error);
  }
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
  $("archiveDetailTag").value = archive.tag || "";
  const githubLink = $("archiveOpenOnGithub");
  if (archive.html_url) {
    githubLink.href = archive.html_url;
    githubLink.style.display = "";
  } else {
    githubLink.style.display = "none";
  }
  openModal("archiveDetailModal");
  loadSelectedArchiveContents().catch((error) => {
    $("archiveBrowserStatus").textContent = error.message;
    $("archiveBrowserGrid").innerHTML = "";
  });
}

async function startDownload() {
  const archive = state.selectedArchive;
  if (!archive) return;
  const workers = Number($("downloadWorkers").value || 4);
  closeModal("archiveDetailModal");
  showTransferToast();
  try {
    const result = await fetchJson("/api/download", {
      method: "POST",
      body: JSON.stringify({ tag: archive.tag, workers, retries: 3 }),
    });
    state.activeDownloadTaskId = result.task_id;
    await loadTasks();
  } catch (error) {
    alert(error.message);
  }
}

async function deleteSelectedArchive() {
  const archive = state.selectedArchive;
  if (!archive) return;
  if (!confirm(`Delete the entire archive "${archiveTitle(archive)}"?`)) return;
  try {
    await fetchJson(`/api/archives/${archive.release_id}`, { method: "DELETE" });
    closeModal("archiveDetailModal");
    state.selectedArchive = null;
    state.selectedArchiveContents = null;
    await loadArchives();
  } catch (error) {
    alert(error.message);
  }
}

// ── Tasks → transfer toast ────────────────────────────────────────────────────

function shouldShowToast(tasks) {
  if (state.toastDismissed) return false;
  if (!tasks.length) return false;
  // Show if there's anything queued or running, or if there's a fresh completed download with a fetch link.
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
  const recentlyDone = tasks.filter((t) => t.status === "completed" || t.status === "failed").slice(0, 5);
  title.textContent = active.length ? `Transfers (${active.length})` : "Transfers";

  const ordered = [...active, ...recentlyDone];

  body.innerHTML = ordered.map((task) => {
    const payload = task.payload || {};
    const result = task.result || {};
    const total = Number(task.progress_total || 0);
    const done = Number(task.progress_done || 0);
    const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : (task.status === "completed" ? 100 : 0);

    const label = task.type === "upload"
      ? (payload.upload_origin === "browser-transfer"
          ? `${payload.uploaded_file_count || 0} file${(payload.uploaded_file_count || 0) === 1 ? "" : "s"}`
          : (payload.source_path || "Upload"))
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
  if (!state.me.token_present || !state.me.repo) return;
  try {
    const payload = await fetchJson("/api/tasks");
    renderTasks(payload.tasks || []);
    // If a freshly-completed upload comes in, refresh the archives list.
    const tasks = payload.tasks || [];
    const completedUpload = tasks.find((t) => t.type === "upload" && t.status === "completed" && !t._archivesRefreshed);
    if (completedUpload) {
      completedUpload._archivesRefreshed = true;
      loadArchives();
    }
  } catch (_) { /* ignore polling errors */ }
}

// ── Upload ────────────────────────────────────────────────────────────────────

async function uploadSelectedFiles(files) {
  if (!files.length) return;
  if (!state.me.token_present || !state.me.repo) {
    openModal("credsModal");
    return;
  }

  const uploadForm = $("uploadForm");
  const formData = new FormData(uploadForm);
  formData.set("encrypt", uploadForm.querySelector('input[name="encrypt"]').checked ? "true" : "false");
  formData.append("retries", "3");
  formData.append("recursive", "true");

  for (const file of files) {
    formData.append("files", file, file.name);
    formData.append("relative_paths", file.webkitRelativePath || file.name);
  }

  showTransferToast();
  const response = await fetch("/api/upload-files", { method: "POST", body: formData, credentials: "same-origin" });
  if (response.status === 401) { window.location.href = "/login"; return; }
  let payload = {};
  try { payload = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(payload.error || `Upload failed (${response.status})`);
  await loadTasks();
}

// ── Sidebar + topbar interactions ─────────────────────────────────────────────

function setupNewMenu() {
  const button = $("newButton");
  const menu = $("newMenu");
  button.addEventListener("click", (event) => {
    event.stopPropagation();
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
  $("openCredsFromMenu2").addEventListener("click", () => {
    menu.classList.remove("open");
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
    await loadArchives();
    await loadTasks();
  };
  $("refreshButton").addEventListener("click", handler);
  $("refreshArchivesButton").addEventListener("click", handler);
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
  $("credsForm").addEventListener("submit", submitCreds);
  $("clearCredsButton").addEventListener("click", clearCreds);
  $("startDownloadButton").addEventListener("click", startDownload);
  $("archiveDeleteButton").addEventListener("click", deleteSelectedArchive);

  try {
    await loadMe();
    await loadArchives();
    await loadTasks();
  } catch (error) {
    console.error("init error", error);
  }

  setInterval(loadTasks, 2500);
}

window.addEventListener("DOMContentLoaded", init);
