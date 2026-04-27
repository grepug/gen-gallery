const ACTIVE_STATUSES = new Set(["queued", "running", "retry_waiting"]);
const JOB_PAGE_SIZE = 200;
const PREVIEW_MIN_ZOOM = 1;
const PREVIEW_MAX_ZOOM = 5.5;
const PREVIEW_WHEEL_ZOOM_SENSITIVITY = 0.0022;
const PREVIEW_PINCH_ZOOM_SENSITIVITY = 0.0036;
const PREVIEW_SAFARI_PINCH_DPI_WEIGHT = 0.4;
const PREVIEW_SAFARI_PINCH_SENSITIVITY_MULTIPLIER = 1.12;
const PREVIEW_DEFAULT_DPI_WEIGHT = 0.12;
const PREVIEW_SAFARI_SCROLL_DAMPING = 0.9;
const PREVIEW_SAFARI_SCROLL_BREAKPOINT = 28;
const PREVIEW_SAFARI_SCROLL_TAIL_RATIO = 0.45;

const state = {
  jobs: [],
  filter: "all",
  selectedId: null,
  modalOpen: false,
  immersiveMode: false,
  immersiveChromeVisible: true,
  immersiveChromeTimer: null,
  promptExpanded: false,
  imageViewMode: "fit",
  previewZoom: 1,
  previewPanX: 0,
  previewPanY: 0,
  previewDragging: null,
  previewTransformRaf: null,
  previewWheelTimer: null,
  pollingTimer: null,
  pollingMode: "idle",
};

const els = {
  galleryGrid: document.getElementById("gallery-grid"),
  galleryEmpty: document.getElementById("gallery-empty"),
  viewerModal: document.getElementById("viewer-modal"),
  viewerShell: document.querySelector(".viewer-shell"),
  viewerBackdrop: document.getElementById("viewer-backdrop"),
  viewerFullscreenButton: document.getElementById("viewer-fullscreen-button"),
  viewerCloseButton: document.getElementById("viewer-close-button"),
  detailEmpty: document.getElementById("detail-empty"),
  detailContent: document.getElementById("detail-content"),
  detailStrip: document.getElementById("detail-strip"),
  viewModeGroup: document.getElementById("view-mode-group"),
  detailPreviewFrame: document.querySelector(".detail-preview-frame"),
  detailPreview: document.getElementById("detail-preview"),
  detailPlaceholder: document.getElementById("detail-placeholder"),
  detailTitle: document.getElementById("detail-title"),
  detailStatus: document.getElementById("detail-status"),
  detailPromptPreview: document.getElementById("detail-prompt-preview"),
  detailPromptFull: document.getElementById("detail-prompt-full"),
  detailPromptScroll: document.getElementById("detail-prompt-scroll"),
  detailPromptToggle: document.getElementById("detail-prompt-toggle"),
  detailReference: document.getElementById("detail-reference"),
  detailReferenceImage: document.getElementById("detail-reference-image"),
  metaGrid: document.getElementById("meta-grid"),
  retryButton: document.getElementById("retry-button"),
  deleteButton: document.getElementById("delete-button"),
  globalMessage: document.getElementById("global-message"),
  filterGroup: document.getElementById("filter-group"),
  pollingIndicator: document.getElementById("polling-indicator"),
  statusSummary: document.getElementById("status-summary"),
  refreshButton: document.getElementById("refresh-button"),
};

const IS_SAFARI =
  typeof navigator !== "undefined" &&
  /Safari/i.test(navigator.userAgent) &&
  /Apple/i.test(navigator.vendor || "") &&
  !/CriOS|Chrome|Chromium|EdgiOS|Edg|Firefox|FxiOS|OPR|OPT/i.test(
    navigator.userAgent,
  );

function formatTimestamp(value) {
  if (!value) return "—";
  const date = new Date(value);
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

function promptSnippet(prompt) {
  if (!prompt) return "No prompt";
  return prompt.length > 110 ? `${prompt.slice(0, 107)}...` : prompt;
}

function statusClass(status) {
  return `status-${status}`;
}

function outputFile(job) {
  if (job.output_files && job.output_files.length > 0)
    return job.output_files[0];
  return null;
}

function referenceFile(job) {
  if (job.input_files && job.input_files.length > 0) return job.input_files[0];
  return null;
}

function filteredJobs() {
  if (state.filter === "all") return state.jobs;
  if (state.filter === "active") {
    return state.jobs.filter((job) => ACTIVE_STATUSES.has(job.status));
  }
  return state.jobs.filter((job) => job.status === state.filter);
}

function currentSelectedIndex() {
  return filteredJobs().findIndex((job) => job.id === state.selectedId);
}

function ensureSelection() {
  const jobs = filteredJobs();
  if (!jobs.length) {
    state.selectedId = null;
    state.modalOpen = false;
    return;
  }
  if (!jobs.some((job) => job.id === state.selectedId)) {
    state.selectedId = jobs[0].id;
  }
}

function resetPreviewViewport() {
  state.previewZoom = 1;
  state.previewPanX = 0;
  state.previewPanY = 0;
  state.previewDragging = null;
}

function setImmersiveChromeVisible(visible) {
  state.immersiveChromeVisible = visible;
  els.viewerModal.classList.toggle(
    "immersive-ui-hidden",
    state.immersiveMode && !visible,
  );
}

function scheduleImmersiveChromeHide() {
  window.clearTimeout(state.immersiveChromeTimer);
  if (!state.immersiveMode) return;
  state.immersiveChromeTimer = window.setTimeout(() => {
    setImmersiveChromeVisible(false);
  }, 1600);
}

function pokeImmersiveChrome() {
  if (!state.immersiveMode) return;
  setImmersiveChromeVisible(true);
  scheduleImmersiveChromeHide();
}

function openModal(jobId) {
  state.selectedId = jobId;
  state.modalOpen = true;
  state.immersiveMode = false;
  state.immersiveChromeVisible = true;
  state.promptExpanded = false;
  state.imageViewMode = "fit";
  resetPreviewViewport();
  render();
}

function closeModal() {
  state.modalOpen = false;
  state.immersiveMode = false;
  window.clearTimeout(state.immersiveChromeTimer);
  render();
}

function syncImmersiveState() {
  if (!document.fullscreenElement) {
    state.immersiveMode = false;
    if (state.modalOpen) render();
  }
}

async function enterImmersiveMode() {
  state.immersiveMode = true;
  setImmersiveChromeVisible(true);
  render();
  scheduleImmersiveChromeHide();
  const target = els.viewerModal;
  try {
    if (!document.fullscreenElement && target.requestFullscreen) {
      await target.requestFullscreen();
    }
  } catch {
    // CSS fallback already active via state.immersiveMode
  }
}

async function exitImmersiveMode() {
  const hadFullscreen = Boolean(document.fullscreenElement);
  state.immersiveMode = false;
  window.clearTimeout(state.immersiveChromeTimer);
  setImmersiveChromeVisible(true);
  render();
  try {
    if (hadFullscreen && document.exitFullscreen) {
      await document.exitFullscreen();
    }
  } catch {
    // ignore
  }
}

async function toggleImmersiveMode() {
  if (state.immersiveMode || document.fullscreenElement) {
    await exitImmersiveMode();
  } else {
    await enterImmersiveMode();
  }
}

function renderSummary() {
  const counts = {
    active: state.jobs.filter((job) => ACTIVE_STATUSES.has(job.status)).length,
    failed: state.jobs.filter((job) => job.status === "failed").length,
    succeeded: state.jobs.filter((job) => job.status === "succeeded").length,
  };

  els.statusSummary.innerHTML = "";
  [
    ["Active", counts.active],
    ["Succeeded", counts.succeeded],
    ["Failed", counts.failed],
  ].forEach(([label, value]) => {
    const pill = document.createElement("span");
    pill.className = "summary-pill";
    pill.textContent = `${label} ${value}`;
    els.statusSummary.appendChild(pill);
  });
}

function renderGallery() {
  const jobs = filteredJobs();
  els.galleryGrid.innerHTML = "";
  els.galleryEmpty.classList.toggle("hidden", jobs.length > 0);

  jobs.forEach((job) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = `gallery-card ${job.id === state.selectedId ? "is-selected" : ""}`;
    card.addEventListener("click", () => openModal(job.id));

    const file = outputFile(job);
    if (file) {
      const image = document.createElement("img");
      image.src = file.url;
      image.alt = promptSnippet(job.prompt);
      card.appendChild(image);
    } else {
      const placeholder = document.createElement("div");
      placeholder.className = "card-placeholder";
      placeholder.textContent =
        job.status === "failed" ? "No generated image" : "Waiting for image";
      card.appendChild(placeholder);
    }

    const topline = document.createElement("div");
    topline.className = "card-topline";
    topline.innerHTML = `<p class="card-title">${formatTimestamp(job.created_at)}</p>`;

    const status = document.createElement("span");
    status.className = `status-pill ${statusClass(job.status)}`;
    status.textContent = job.status.replace("_", " ");
    topline.appendChild(status);

    const prompt = document.createElement("p");
    prompt.className = "card-prompt";
    prompt.textContent = promptSnippet(job.prompt);

    const bottomline = document.createElement("div");
    bottomline.className = "card-bottomline";
    bottomline.innerHTML = `<span class="card-title">${job.assigned_key_name || "No key yet"}</span><span class="card-title">Attempt ${job.attempt_count}</span>`;

    card.append(topline, prompt, bottomline);
    els.galleryGrid.appendChild(card);
  });
}

function renderDetailStrip(selectedJob, jobs) {
  els.detailStrip.innerHTML = "";
  jobs.forEach((job) => {
    const thumb = document.createElement("button");
    thumb.type = "button";
    thumb.dataset.jobId = job.id;
    thumb.className = `detail-thumb ${job.id === selectedJob.id ? "is-selected" : ""}`;
    thumb.addEventListener("click", () => {
      state.selectedId = job.id;
      state.promptExpanded = false;
      resetPreviewViewport();
      render();
    });

    const file = outputFile(job);
    if (file) {
      const image = document.createElement("img");
      image.src = file.url;
      image.alt = promptSnippet(job.prompt);
      thumb.appendChild(image);
    } else {
      const fallback = document.createElement("div");
      fallback.className = "thumb-fallback";
      fallback.textContent = job.status === "failed" ? "Failed" : "Pending";
      thumb.appendChild(fallback);
    }

    els.detailStrip.appendChild(thumb);
  });
}

function renderMeta(selectedJob) {
  const items = [
    ["Job ID", selectedJob.id],
    ["Key", selectedJob.assigned_key_name || "—"],
    [
      "Attempts",
      `${selectedJob.attempt_count} / ${selectedJob.max_retries + 1}`,
    ],
    ["Created", formatTimestamp(selectedJob.created_at)],
    ["Updated", formatTimestamp(selectedJob.updated_at)],
    ["Finished", formatTimestamp(selectedJob.finished_at)],
  ];
  if (selectedJob.last_error) {
    items.push(["Last error", selectedJob.last_error]);
  }

  els.metaGrid.innerHTML = "";
  items.forEach(([label, value]) => {
    const wrapper = document.createElement("div");
    wrapper.className = "meta-item";
    wrapper.innerHTML = `<dt>${label}</dt><dd>${value}</dd>`;
    els.metaGrid.appendChild(wrapper);
  });
}

function renderPrompt(selectedJob) {
  const prompt = selectedJob.prompt || "No prompt";
  const longPrompt = prompt.length > 280;
  els.detailPromptPreview.textContent = longPrompt
    ? promptSnippet(prompt)
    : prompt;
  els.detailPromptScroll.textContent = prompt;
  els.detailPromptToggle.classList.toggle("hidden", !longPrompt);
  els.detailPromptToggle.textContent = state.promptExpanded
    ? "Hide full"
    : "Show full";
  els.detailPromptFull.classList.toggle("hidden", !state.promptExpanded);
}

function renderReference(selectedJob) {
  const file = referenceFile(selectedJob);
  const hasReference = Boolean(file);
  els.detailReference.classList.toggle("hidden", !hasReference);
  if (!file) return;
  els.detailReferenceImage.src = file.url;
}

function renderImageViewMode(hasOutputImage) {
  const mode = hasOutputImage ? state.imageViewMode : "fit";
  els.detailPreview.classList.toggle("is-fill", mode === "fill");
  els.detailPreviewFrame.classList.toggle("has-image", hasOutputImage);
  [...els.viewModeGroup.querySelectorAll("[data-view-mode]")].forEach(
    (button) => {
      const active = button.dataset.viewMode === mode;
      button.classList.toggle("is-active", active);
      button.disabled = !hasOutputImage;
    },
  );
}

function clampPreviewPan() {
  if (
    state.previewZoom <= 1 ||
    els.detailPreview.classList.contains("hidden")
  ) {
    state.previewPanX = 0;
    state.previewPanY = 0;
    return;
  }
  const maxX = Math.max(
    0,
    (els.detailPreview.clientWidth * state.previewZoom -
      els.detailPreview.clientWidth) /
      2,
  );
  const maxY = Math.max(
    0,
    (els.detailPreview.clientHeight * state.previewZoom -
      els.detailPreview.clientHeight) /
      2,
  );
  state.previewPanX = Math.max(-maxX, Math.min(maxX, state.previewPanX));
  state.previewPanY = Math.max(-maxY, Math.min(maxY, state.previewPanY));
}

function zoomPreview(nextZoom, anchorClientX = null, anchorClientY = null) {
  const hasImage = !els.detailPreview.classList.contains("hidden");
  if (!hasImage) return;

  const clampedZoom = Math.max(
    PREVIEW_MIN_ZOOM,
    Math.min(PREVIEW_MAX_ZOOM, Number(nextZoom.toFixed(3))),
  );
  if (clampedZoom <= PREVIEW_MIN_ZOOM) {
    resetPreviewViewport();
    schedulePreviewTransform();
    return;
  }

  const frameRect = els.detailPreviewFrame.getBoundingClientRect();
  const centerX = frameRect.left + frameRect.width / 2;
  const centerY = frameRect.top + frameRect.height / 2;
  const anchorOffsetX = (anchorClientX ?? centerX) - centerX;
  const anchorOffsetY = (anchorClientY ?? centerY) - centerY;
  const localX = (anchorOffsetX - state.previewPanX) / state.previewZoom;
  const localY = (anchorOffsetY - state.previewPanY) / state.previewZoom;

  state.previewZoom = clampedZoom;
  state.previewPanX = anchorOffsetX - clampedZoom * localX;
  state.previewPanY = anchorOffsetY - clampedZoom * localY;
  schedulePreviewTransform();
}

function tunePreviewPanDelta(delta, event) {
  if (!IS_SAFARI || event.deltaMode !== WheelEvent.DOM_DELTA_PIXEL)
    return delta;
  const abs = Math.abs(delta);
  if (abs === 0) return 0;
  const compressed =
    abs <= PREVIEW_SAFARI_SCROLL_BREAKPOINT
      ? abs
      : PREVIEW_SAFARI_SCROLL_BREAKPOINT +
        (abs - PREVIEW_SAFARI_SCROLL_BREAKPOINT) *
          PREVIEW_SAFARI_SCROLL_TAIL_RATIO;
  return Math.sign(delta) * compressed * PREVIEW_SAFARI_SCROLL_DAMPING;
}

function syncPreviewTransform() {
  state.previewTransformRaf = null;
  clampPreviewPan();
  const hasImage = !els.detailPreview.classList.contains("hidden");
  els.detailPreview.style.transform = hasImage
    ? `translate(${state.previewPanX}px, ${state.previewPanY}px) scale(${state.previewZoom})`
    : "";
  const zoomed = hasImage && state.previewZoom > 1.001;
  els.detailPreview.classList.toggle("is-zoomed", zoomed);
  els.detailPreviewFrame.classList.toggle("is-zoomed", zoomed);
  els.detailPreviewFrame.classList.toggle(
    "is-dragging",
    Boolean(state.previewDragging),
  );
}

function schedulePreviewTransform() {
  if (state.previewTransformRaf !== null) return;
  state.previewTransformRaf = window.requestAnimationFrame(() => {
    syncPreviewTransform();
  });
}

function renderDetail() {
  const jobs = filteredJobs();
  const selectedJob = jobs.find((job) => job.id === state.selectedId);
  const isOpen = Boolean(selectedJob && state.modalOpen);

  document.body.classList.toggle("modal-open", isOpen);
  document.body.classList.toggle(
    "modal-immersive",
    isOpen && state.immersiveMode,
  );
  els.viewerModal.classList.toggle("hidden", !isOpen);
  els.viewerModal.setAttribute("aria-hidden", isOpen ? "false" : "true");
  els.viewerModal.classList.toggle(
    "is-immersive",
    isOpen && state.immersiveMode,
  );
  els.viewerModal.classList.toggle(
    "immersive-ui-hidden",
    isOpen && state.immersiveMode && !state.immersiveChromeVisible,
  );
  els.viewerFullscreenButton.textContent = state.immersiveMode
    ? "Exit full"
    : "Fullscreen";
  els.detailEmpty.classList.toggle("hidden", isOpen);
  els.detailContent.classList.toggle("hidden", !isOpen);
  if (!selectedJob || !state.modalOpen) return;

  renderDetailStrip(selectedJob, jobs);
  requestAnimationFrame(() => {
    const selectedThumb = els.detailStrip.querySelector(
      `[data-job-id="${selectedJob.id}"]`,
    );
    selectedThumb?.scrollIntoView({ block: "nearest", inline: "nearest" });
  });

  const file = outputFile(selectedJob);
  if (file) {
    els.detailPreview.src = file.url;
    els.detailPreview.alt = selectedJob.prompt;
    els.detailPreview.classList.remove("hidden");
    els.detailPlaceholder.classList.add("hidden");
  } else {
    els.detailPreview.removeAttribute("src");
    els.detailPreview.classList.add("hidden");
    els.detailPlaceholder.classList.remove("hidden");
    els.detailPlaceholder.textContent =
      selectedJob.status === "failed"
        ? "This job did not produce a generated image."
        : "This job has not produced a generated image yet.";
  }
  renderImageViewMode(Boolean(file));
  syncPreviewTransform();

  els.detailTitle.textContent = formatTimestamp(selectedJob.created_at);
  els.detailStatus.className = `status-pill ${statusClass(selectedJob.status)}`;
  els.detailStatus.textContent = selectedJob.status.replace("_", " ");
  renderPrompt(selectedJob);
  renderReference(selectedJob);
  renderMeta(selectedJob);

  const canRetry = selectedJob.status === "failed";
  const canDelete = selectedJob.status !== "running";

  els.retryButton.classList.toggle("hidden", !canRetry);
  els.deleteButton.disabled = !canDelete;
  els.deleteButton.textContent = canDelete ? "Delete" : "Delete unavailable";

  els.retryButton.onclick = async () => {
    try {
      await mutateJob(`/jobs/${selectedJob.id}/retry`, { method: "POST" });
      state.promptExpanded = false;
      resetPreviewViewport();
      setMessage("Job requeued.");
    } catch (error) {
      setMessage(error.message);
    }
  };

  els.deleteButton.onclick = async () => {
    const confirmed = window.confirm("Delete this job and its files?");
    if (!confirmed) return;
    try {
      await mutateJob(`/jobs/${selectedJob.id}`, { method: "DELETE" });
      if (!filteredJobs().length) {
        state.modalOpen = false;
      }
      setMessage("Job deleted.");
    } catch (error) {
      setMessage(error.message);
    }
  };
}

function setMessage(message) {
  els.globalMessage.textContent = message;
  els.globalMessage.classList.remove("hidden");
  window.clearTimeout(setMessage.timer);
  setMessage.timer = window.setTimeout(() => {
    els.globalMessage.classList.add("hidden");
  }, 3000);
}

async function mutateJob(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || "Request failed");
  }
  await fetchJobs({ preserveSelection: true });
}

function updatePollingIndicator() {
  els.pollingIndicator.textContent =
    state.pollingMode === "active" ? "Live polling" : "Low-frequency refresh";
}

function schedulePolling() {
  window.clearTimeout(state.pollingTimer);
  const hasActive = state.jobs.some((job) => ACTIVE_STATUSES.has(job.status));
  state.pollingMode = hasActive ? "active" : "idle";
  updatePollingIndicator();
  const delay = hasActive ? 2500 : 30000;
  state.pollingTimer = window.setTimeout(() => {
    fetchJobs({ preserveSelection: true }).catch((error) =>
      setMessage(error.message),
    );
  }, delay);
}

function render() {
  ensureSelection();
  renderSummary();
  renderGallery();
  renderDetail();
  schedulePolling();
}

async function fetchJobs({ preserveSelection = false } = {}) {
  const response = await fetch(`/jobs?limit=${JOB_PAGE_SIZE}&offset=0`);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || "Failed to load jobs");
  }
  const payload = await response.json();
  state.jobs = payload.items;

  if (!preserveSelection && state.jobs.length && !state.selectedId) {
    state.selectedId = state.jobs[0].id;
  }

  ensureSelection();
  render();
}

function navigate(delta) {
  if (!state.modalOpen) return;
  const jobs = filteredJobs();
  if (!jobs.length) return;
  const index = currentSelectedIndex();
  const nextIndex =
    index < 0 ? 0 : Math.min(Math.max(index + delta, 0), jobs.length - 1);
  state.selectedId = jobs[nextIndex].id;
  state.promptExpanded = false;
  resetPreviewViewport();
  render();
}

function onKeydown(event) {
  const target = event.target;
  if (target && ["INPUT", "TEXTAREA"].includes(target.tagName)) {
    return;
  }
  if (event.key === "Escape" && state.modalOpen) {
    event.preventDefault();
    if (state.immersiveMode || document.fullscreenElement) {
      exitImmersiveMode();
    } else {
      closeModal();
    }
  } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
    event.preventDefault();
    navigate(-1);
  } else if (event.key === "ArrowRight" || event.key === "ArrowDown") {
    event.preventDefault();
    navigate(1);
  }
}

function bindEvents() {
  els.filterGroup.addEventListener("click", (event) => {
    const button = event.target.closest("[data-filter]");
    if (!button) return;
    state.filter = button.dataset.filter;
    [...els.filterGroup.querySelectorAll(".filter-chip")].forEach((chip) => {
      chip.classList.toggle("is-active", chip === button);
    });
    render();
  });

  els.refreshButton.addEventListener("click", () => {
    fetchJobs({ preserveSelection: true }).catch((error) =>
      setMessage(error.message),
    );
  });

  els.viewModeGroup.addEventListener("click", (event) => {
    const button = event.target.closest("[data-view-mode]");
    if (!button || button.disabled) return;
    state.imageViewMode = button.dataset.viewMode;
    resetPreviewViewport();
    render();
  });
  els.detailPromptToggle.addEventListener("click", () => {
    state.promptExpanded = !state.promptExpanded;
    render();
  });
  els.viewerBackdrop.addEventListener("click", closeModal);
  els.viewerFullscreenButton.addEventListener("click", () => {
    toggleImmersiveMode();
  });
  els.viewerCloseButton.addEventListener("click", closeModal);

  els.detailPreviewFrame.addEventListener(
    "wheel",
    (event) => {
      if (els.detailPreview.classList.contains("hidden")) return;
      event.preventDefault();
      els.detailPreviewFrame.classList.add("is-wheeling");
      window.clearTimeout(state.previewWheelTimer);
      state.previewWheelTimer = window.setTimeout(() => {
        els.detailPreviewFrame.classList.remove("is-wheeling");
      }, 120);
      const isPinchGesture = event.ctrlKey || event.metaKey;
      const isZoomGesture =
        state.previewZoom <= 1.001 || isPinchGesture || event.altKey;

      if (isZoomGesture) {
        const baseSensitivity =
          (isPinchGesture
            ? PREVIEW_PINCH_ZOOM_SENSITIVITY
            : PREVIEW_WHEEL_ZOOM_SENSITIVITY) *
          (IS_SAFARI && isPinchGesture
            ? PREVIEW_SAFARI_PINCH_SENSITIVITY_MULTIPLIER
            : 1);
        const dpiWeight =
          IS_SAFARI && isPinchGesture
            ? PREVIEW_SAFARI_PINCH_DPI_WEIGHT
            : PREVIEW_DEFAULT_DPI_WEIGHT;
        const dpiBoost =
          1 + Math.min(window.devicePixelRatio || 1, 3) * dpiWeight;
        const factor = Math.exp(-event.deltaY * baseSensitivity * dpiBoost);
        zoomPreview(state.previewZoom * factor, event.clientX, event.clientY);
        return;
      }

      const panDeltaY = tunePreviewPanDelta(event.deltaY, event);
      const panDeltaX = tunePreviewPanDelta(event.deltaX, event);
      state.previewPanY -= panDeltaY;
      state.previewPanX -= panDeltaX;
      if (event.shiftKey && Math.abs(panDeltaY) > Math.abs(panDeltaX)) {
        state.previewPanX -= panDeltaY;
      }
      schedulePreviewTransform();
    },
    { passive: false },
  );

  els.detailPreviewFrame.addEventListener("dblclick", (event) => {
    if (els.detailPreview.classList.contains("hidden")) return;
    if (state.previewZoom > 1.001) {
      resetPreviewViewport();
      syncPreviewTransform();
    } else {
      zoomPreview(2.2, event.clientX, event.clientY);
    }
  });

  els.detailPreviewFrame.addEventListener("mousedown", (event) => {
    if (
      state.previewZoom <= 1 ||
      els.detailPreview.classList.contains("hidden")
    )
      return;
    event.preventDefault();
    state.previewDragging = {
      startX: event.clientX,
      startY: event.clientY,
      originX: state.previewPanX,
      originY: state.previewPanY,
    };
    schedulePreviewTransform();
  });

  window.addEventListener("mousemove", (event) => {
    if (state.immersiveMode) pokeImmersiveChrome();
    if (!state.previewDragging) return;
    state.previewPanX =
      state.previewDragging.originX +
      (event.clientX - state.previewDragging.startX);
    state.previewPanY =
      state.previewDragging.originY +
      (event.clientY - state.previewDragging.startY);
    schedulePreviewTransform();
  });

  window.addEventListener("mouseup", () => {
    if (!state.previewDragging) return;
    state.previewDragging = null;
    schedulePreviewTransform();
  });
  els.viewerModal.addEventListener("mousemove", () => {
    pokeImmersiveChrome();
  });
  els.detailStrip.addEventListener("mouseenter", () => {
    pokeImmersiveChrome();
  });
  els.viewModeGroup.addEventListener("mouseenter", () => {
    pokeImmersiveChrome();
  });
  document.addEventListener("fullscreenchange", syncImmersiveState);
  window.addEventListener("keydown", onKeydown);
}

async function boot() {
  bindEvents();
  try {
    await fetchJobs();
  } catch (error) {
    setMessage(error.message);
  }
}

boot();
