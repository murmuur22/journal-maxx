(() => {
  const scrollKey = `journalmax:scroll:${location.pathname}`;
  const tabKey = "journalmax:control-tab";
  const tabs = [...document.querySelectorAll('.control-tabs [role="tab"]')];
  const panels = [...document.querySelectorAll('.control-tab-panel[role="tabpanel"]')];

  function showTab(id, updateUrl = true) {
    if (!panels.some((panel) => panel.id === id)) id = "overview";
    panels.forEach((panel) => { panel.hidden = panel.id !== id; });
    tabs.forEach((tab) => {
      const selected = tab.getAttribute("aria-controls") === id;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });
    try { sessionStorage.setItem(tabKey, id); } catch (_) {}
    if (updateUrl) history.replaceState(null, "", `#${id}`);
  }

  let initialTab = location.hash.slice(1);
  try { initialTab ||= sessionStorage.getItem(tabKey) || "overview"; } catch (_) {}
  showTab(initialTab, Boolean(location.hash));

  tabs.forEach((tab) => tab.addEventListener("click", (event) => {
    event.preventDefault();
    showTab(tab.getAttribute("aria-controls"));
  }));

  window.addEventListener("hashchange", () => showTab(location.hash.slice(1), false));

  const activityBoard = document.querySelector("[data-live-activity-url]");
  if (activityBoard) {
    const activityList = activityBoard.querySelector("[data-live-activity-list]");
    const liveState = activityBoard.querySelector("[data-live-state]");
    const countLabel = activityBoard.querySelector("[data-live-count-label]");
    let activitySignature = [...activityList.querySelectorAll("[data-event-id]")].map((row) => row.dataset.eventId).join(",");
    let activityRequestRunning = false;

    const addLinkedText = (parent, tag, className, label, url = "") => {
      const element = document.createElement(url ? "a" : tag);
      if (className) element.className = className;
      if (url) element.href = url;
      element.textContent = label;
      parent.append(element);
    };
    const renderActivity = (items) => {
      const nextSignature = items.map((item) => item.id).join(",");
      if (nextSignature === activitySignature) return;
      const previousNewest = activitySignature.split(",")[0];
      const previousNewestIndex = items.findIndex((item) => String(item.id) === previousNewest);
      activitySignature = nextSignature;
      activityList.replaceChildren();
      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "activity-empty"; empty.textContent = "NO ACTIVITY RECORDED YET";
        activityList.append(empty); return;
      }
      items.forEach((item, index) => {
        const row = document.createElement("article");
        row.className = `activity-item${previousNewest && (previousNewestIndex < 0 || index < previousNewestIndex) ? " is-new" : ""}`;
        row.dataset.eventId = item.id;
        const time = document.createElement("time"); time.dateTime = item.created_at;
        const clock = document.createElement("b"); clock.textContent = item.time;
        const date = document.createElement("span"); date.textContent = item.date;
        time.append(clock, date);
        const copy = document.createElement("div"); copy.className = "activity-copy";
        const sentence = document.createElement("p");
        addLinkedText(sentence, "strong", item.actor_url ? "activity-actor" : "", item.actor_label, item.actor_url);
        sentence.append(document.createTextNode(` ${item.action_label}`));
        if (item.target_label) {
          sentence.append(document.createTextNode(" "));
          addLinkedText(sentence, "b", item.target_url ? "activity-target" : "", item.target_label, item.target_url);
        }
        sentence.append(document.createTextNode("."));
        const action = document.createElement("code"); action.textContent = item.action;
        copy.append(sentence, action);
        const arrow = document.createElement("i"); arrow.ariaHidden = "true"; arrow.textContent = "↗";
        row.append(time, copy, arrow); activityList.append(row);
      });
    };
    const refreshActivity = async () => {
      if (activityRequestRunning || document.hidden) return;
      activityRequestRunning = true;
      try {
        const response = await fetch(activityBoard.dataset.liveActivityUrl, { headers: { Accept: "application/json" }, cache: "no-store" });
        if (!response.ok || response.redirected) throw new Error("Activity feed unavailable");
        const payload = await response.json();
        renderActivity(payload.items);
        Object.entries(payload.counts || {}).forEach(([key, value]) => {
          const counter = document.querySelector(`[data-live-count="${key}"]`);
          if (counter) counter.textContent = value;
        });
        countLabel.textContent = String(payload.items.length).padStart(2, "0");
        liveState.classList.remove("is-offline");
      } catch (_) {
        liveState.classList.add("is-offline");
      } finally {
        activityRequestRunning = false;
      }
    };
    setInterval(refreshActivity, 2500);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshActivity(); });
  }

  const emotionSpace = document.querySelector("[data-emotion-space]");
  const emotionPositionKey = "journalmax:emotion-field-positions-v2";
  if (emotionSpace) {
    const nodes = [...emotionSpace.querySelectorAll("[data-emotion-node]")];
    let positions = {};
    try { positions = JSON.parse(localStorage.getItem(emotionPositionKey) || "{}"); } catch (_) {}
    nodes.forEach((node, index) => {
      const saved = positions[node.dataset.emotionNode];
      const angle = index * 2.399963;
      const radius = Math.min(34, 8 + Math.sqrt(index) * 9);
      const x = saved?.x ?? 50 + Math.cos(angle) * radius;
      const y = saved?.y ?? 50 + Math.sin(angle) * radius * .92;
      node.style.left = `${x}%`; node.style.top = `${y}%`;

      node.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        const startX = event.clientX; const startY = event.clientY;
        const originalX = parseFloat(node.style.left); const originalY = parseFloat(node.style.top);
        let dragged = false;
        node.setPointerCapture(event.pointerId);
        node.classList.add("is-grabbed");
        const move = (moveEvent) => {
          const bounds = emotionSpace.getBoundingClientRect();
          const dx = moveEvent.clientX - startX; const dy = moveEvent.clientY - startY;
          if (Math.abs(dx) + Math.abs(dy) > 5) dragged = true;
          if (!dragged) return;
          moveEvent.preventDefault();
          node.style.left = `${Math.max(7, Math.min(93, originalX + dx / bounds.width * 100))}%`;
          node.style.top = `${Math.max(12, Math.min(88, originalY + dy / bounds.height * 100))}%`;
        };
        const finish = () => {
          node.removeEventListener("pointermove", move);
          node.classList.remove("is-grabbed");
          if (dragged) {
            positions[node.dataset.emotionNode] = { x: parseFloat(node.style.left), y: parseFloat(node.style.top) };
            try { localStorage.setItem(emotionPositionKey, JSON.stringify(positions)); } catch (_) {}
            node.addEventListener("click", (clickEvent) => { clickEvent.preventDefault(); clickEvent.stopImmediatePropagation(); }, { capture: true, once: true });
          }
        };
        node.addEventListener("pointermove", move);
        node.addEventListener("pointerup", finish, { once: true });
        node.addEventListener("pointercancel", finish, { once: true });
      });
    });
  }

  const auditConsole = document.querySelector(".audit-console");
  const auditWidthKey = "journalmax:audit-column-widths";
  const saveAuditWidths = () => {
    if (!auditConsole) return;
    const widths = {};
    auditConsole.querySelectorAll(".audit-resizer").forEach((handle) => {
      widths[handle.dataset.auditColumn] = parseFloat(getComputedStyle(auditConsole).getPropertyValue(`--audit-${handle.dataset.auditColumn}`));
    });
    try { localStorage.setItem(auditWidthKey, JSON.stringify(widths)); } catch (_) {}
  };
  const setAuditWidth = (handle, width) => {
    const minimum = handle.dataset.auditColumn === "ln" ? 34 : 80;
    const value = Math.max(minimum, Math.round(width));
    auditConsole.style.setProperty(`--audit-${handle.dataset.auditColumn}`, `${value}px`);
    handle.setAttribute("aria-valuenow", String(value));
  };

  if (auditConsole) {
    try {
      const saved = JSON.parse(localStorage.getItem(auditWidthKey) || "{}");
      auditConsole.querySelectorAll(".audit-resizer").forEach((handle) => {
        setAuditWidth(handle, Number(saved[handle.dataset.auditColumn]) || Number(handle.dataset.defaultWidth));
      });
    } catch (_) {}

    auditConsole.querySelectorAll(".audit-resizer").forEach((handle) => {
      handle.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        const startX = event.clientX;
        const startWidth = handle.parentElement.getBoundingClientRect().width;
        handle.setPointerCapture(event.pointerId);
        document.body.classList.add("is-resizing-audit");
        const move = (moveEvent) => setAuditWidth(handle, startWidth + moveEvent.clientX - startX);
        const finish = () => {
          handle.removeEventListener("pointermove", move);
          document.body.classList.remove("is-resizing-audit");
          saveAuditWidths();
        };
        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", finish, { once: true });
        handle.addEventListener("pointercancel", finish, { once: true });
      });
      handle.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        setAuditWidth(handle, handle.parentElement.getBoundingClientRect().width + (event.key === "ArrowRight" ? 10 : -10));
        saveAuditWidths();
      });
      handle.addEventListener("dblclick", () => {
        setAuditWidth(handle, Number(handle.dataset.defaultWidth));
        saveAuditWidths();
      });
    });
  }

  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-modal-open]");
    if (opener) {
      document.getElementById(opener.dataset.modalOpen)?.showModal();
      return;
    }
    const closer = event.target.closest("[data-modal-close]");
    if (closer) closer.closest("dialog")?.close();
  });

  const deepLink = new URLSearchParams(location.search);
  const deepLinkedModal = deepLink.get("account")
    ? document.getElementById(`account-edit-${deepLink.get("account")}`)
    : deepLink.get("entry")
      ? document.getElementById(`contents-${deepLink.get("entry")}`)
      : null;
  if (deepLinkedModal?.showModal) deepLinkedModal.showModal();

  document.addEventListener("click", (event) => {
    const heading = event.target.closest(".account-sort");
    if (!heading) return;
    const registry = heading.closest("[data-account-registry]");
    const direction = heading.dataset.direction === "ascending" ? "descending" : "ascending";
    registry.querySelectorAll(".account-sort").forEach((item) => {
      item.dataset.direction = "";
      item.querySelector("i").textContent = "↕";
    });
    heading.dataset.direction = direction;
    heading.querySelector("i").textContent = direction === "ascending" ? "↑" : "↓";
    const key = heading.dataset.accountSort;
    const type = heading.dataset.sortType;
    const rows = [...registry.querySelectorAll(".account-row")];
    rows.sort((left, right) => {
      const a = left.dataset[key]; const b = right.dataset[key];
      const comparison = type === "number" ? Number(a) - Number(b) : a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
      return direction === "ascending" ? comparison : -comparison;
    });
    rows.forEach((row) => registry.append(row));
  });

  document.addEventListener("click", (event) => {
    const generator = event.target.closest("[data-generate-totp]");
    if (!generator) return;
    const bytes = crypto.getRandomValues(new Uint8Array(20));
    const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
    let value = 0; let bits = 0; let secret = "";
    bytes.forEach((byte) => {
      value = (value << 8) | byte; bits += 8;
      while (bits >= 5) { secret += alphabet[(value >>> (bits - 5)) & 31]; bits -= 5; }
    });
    const input = generator.closest("form").querySelector('[name="new_totp_secret"]');
    input.value = secret; input.focus(); input.select();
  });

  document.addEventListener("click", (event) => {
    const heading = event.target.closest(".sort-heading");
    if (!heading) return;
    const table = heading.closest("[data-sortable-table]");
    const header = heading.closest("th");
    const headers = [...table.querySelectorAll("thead th")];
    const column = headers.indexOf(header);
    const direction = header.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
    headers.forEach((item) => item.hasAttribute("aria-sort") && item.setAttribute("aria-sort", "none"));
    header.setAttribute("aria-sort", direction);
    headers.forEach((item) => {
      const marker = item.querySelector(".sort-heading i");
      if (marker) marker.textContent = item === header ? (direction === "ascending" ? "↑" : "↓") : "↕";
    });

    const type = heading.dataset.sortType;
    const rows = [...table.tBodies[0].rows];
    rows.sort((left, right) => {
      const a = left.cells[column].dataset.sortValue || left.cells[column].textContent.trim();
      const b = right.cells[column].dataset.sortValue || right.cells[column].textContent.trim();
      const comparison = type === "number" ? Number(a) - Number(b) : a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
      return direction === "ascending" ? comparison : -comparison;
    });
    rows.forEach((row) => table.tBodies[0].append(row));
  });

  document.addEventListener("click", (event) => {
    if (event.target instanceof HTMLDialogElement) {
      const bounds = event.target.getBoundingClientRect();
      const inside = event.clientX >= bounds.left && event.clientX <= bounds.right && event.clientY >= bounds.top && event.clientY <= bounds.bottom;
      if (!inside) event.target.close();
    }
  });

  const updateWorkspace = document.querySelector("[data-update-workspace]");
  const updateNav = document.querySelector("[data-update-nav]");
  if (updateWorkspace) {
    const connection = updateWorkspace.querySelector("[data-update-connection]");
    const installed = updateWorkspace.querySelector("[data-update-installed]");
    const latest = updateWorkspace.querySelector("[data-update-latest]");
    const availability = updateWorkspace.querySelector("[data-update-availability]");
    const phase = updateWorkspace.querySelector("[data-update-phase]");
    const message = updateWorkspace.querySelector("[data-update-message]");
    const updatedAt = updateWorkspace.querySelector("[data-update-time]");
    const checkButton = updateWorkspace.querySelector("[data-update-check]");
    const installButton = updateWorkspace.querySelector("[data-update-open]");
    const releaseLink = updateWorkspace.querySelector("[data-update-release-link]");
    const confirmModal = document.getElementById("update-confirm");
    const applyForm = confirmModal?.querySelector("[data-update-apply-form]");
    let latestRelease = null;
    let requestRunning = false;

    const csrf = () => updateWorkspace.querySelector('[name="csrfmiddlewaretoken"]')?.value || "";
    const activePhases = new Set(["queued", "downloading", "backing_up", "starting"]);
    const newerThan = (candidate, current) => {
      const left = candidate.split(".").map(Number); const right = current.split(".").map(Number);
      return left.some((part, index) => part !== right[index] && part > right[index] && left.slice(0, index).every((value, prior) => value === right[prior]));
    };
    const renderUpdate = (payload) => {
      const connected = payload.connected === true;
      updateWorkspace.classList.toggle("is-connected", connected);
      updateWorkspace.classList.toggle("is-offline", !connected);
      connection.textContent = connected ? "HOST UPDATER CONNECTED" : "UPDATER NOT CONNECTED";
      checkButton.disabled = !connected || requestRunning || activePhases.has(payload.job?.phase);
      if (payload.installed_version) installed.textContent = `v${payload.installed_version}`;
      if (payload.latest) latestRelease = payload.latest;
      if (latestRelease) {
        latest.textContent = `v${latestRelease.version}`;
        const currentVersion = payload.installed_version || installed.textContent.replace(/^v/, "");
        const available = typeof payload.update_available === "boolean" ? payload.update_available : newerThan(latestRelease.version, currentVersion);
        updateNav?.classList.toggle("has-update", available);
        availability.textContent = available ? "NEW RELEASE AVAILABLE" : "CURRENT RELEASE";
        installButton.disabled = !connected || !available || activePhases.has(payload.job?.phase);
        installButton.dataset.version = latestRelease.version;
      } else {
        installButton.disabled = true;
      }
      if (payload.release_url) releaseLink.href = payload.release_url;
      phase.textContent = (payload.job?.phase || (connected ? "idle" : "offline")).replaceAll("_", " ").toUpperCase();
      message.textContent = payload.job?.message || payload.error || "Updater status unavailable.";
      updatedAt.textContent = payload.job?.updated_at ? new Date(payload.job.updated_at).toLocaleString() : "";
    };
    const updaterFetch = async (url, options = {}) => {
      const response = await fetch(url, { cache: "no-store", credentials: "same-origin", headers: { Accept: "application/json", ...options.headers }, ...options });
      let payload;
      try { payload = await response.json(); } catch (_) { payload = { connected: false, error: "The updater response could not be read." }; }
      if (!response.ok && response.status !== 503) throw new Error(payload.error || `Update request failed (${response.status})`);
      return payload;
    };
    const refreshUpdate = async () => {
      if (requestRunning || document.hidden) return;
      try { renderUpdate(await updaterFetch(updateWorkspace.dataset.statusUrl)); }
      catch (error) { renderUpdate({ connected: false, error: error.message }); }
    };
    checkButton.addEventListener("click", async () => {
      requestRunning = true; checkButton.disabled = true; message.textContent = "CONTACTING THE STABLE RELEASE CHANNEL…";
      try {
        const body = new FormData(); body.set("csrfmiddlewaretoken", csrf());
        renderUpdate(await updaterFetch(updateWorkspace.dataset.checkUrl, { method: "POST", body }));
      } catch (error) { renderUpdate({ connected: false, error: error.message }); }
      finally { requestRunning = false; }
    });
    installButton.addEventListener("click", () => {
      const version = installButton.dataset.version;
      confirmModal.querySelector("[data-update-modal-version]").textContent = `JOURNALMAX v${version}`;
      confirmModal.querySelector("[data-update-version]").value = version;
      confirmModal.querySelector("[data-update-feedback]").textContent = "";
      confirmModal.showModal();
    });
    applyForm?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submitter = event.submitter;
      const feedback = applyForm.querySelector("[data-update-feedback]");
      submitter.disabled = true; feedback.textContent = "AUTHORIZING RELEASE…";
      try {
        const payload = await updaterFetch(updateWorkspace.dataset.applyUrl, { method: "POST", body: new FormData(applyForm) });
        renderUpdate(payload); confirmModal.close(); applyForm.reset();
      } catch (error) { feedback.textContent = error.message; }
      finally { submitter.disabled = false; }
    });
    setInterval(refreshUpdate, 4000);
  }

  if (updateNav && !updateWorkspace) {
    const csrf = document.querySelector('[name="csrfmiddlewaretoken"]')?.value;
    if (csrf) {
      const body = new FormData(); body.set("csrfmiddlewaretoken", csrf);
      fetch(updateNav.dataset.checkUrl, { method: "POST", body, credentials: "same-origin", headers: { Accept: "application/json" } })
        .then((response) => response.ok ? response.json() : null)
        .then((payload) => updateNav.classList.toggle("has-update", Boolean(payload?.update_available)))
        .catch(() => {});
    }
  }

  try {
    const savedPosition = sessionStorage.getItem(scrollKey);
    if (savedPosition !== null) {
      sessionStorage.removeItem(scrollKey);
      requestAnimationFrame(() => scrollTo(0, Number(savedPosition)));
    }
  } catch (_) {
    // The interface still works when browser storage is unavailable.
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;

    const feedbackWorkspace = form.closest("[data-feedback-admin-workspace]");
    if (feedbackWorkspace) {
      event.preventDefault();
      const submitter = event.submitter;
      if (submitter) submitter.disabled = true;
      feedbackWorkspace.setAttribute("aria-busy", "true");
      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new FormData(form, submitter),
          credentials: "same-origin",
          headers: { "X-Requested-With": "XMLHttpRequest" },
        });
        if (!response.ok) throw new Error(`Request failed (${response.status})`);
        const page = new DOMParser().parseFromString(await response.text(), "text/html");
        const replacement = page.querySelector("[data-feedback-admin-workspace]");
        if (!replacement) throw new Error("The updated feedback queue was not returned.");
        feedbackWorkspace.replaceWith(replacement);
        const nextTab = page.querySelector('.control-tabs [aria-controls="feedback"]');
        const currentTab = document.querySelector('.control-tabs [aria-controls="feedback"]');
        if (nextTab && currentTab) currentTab.innerHTML = nextTab.innerHTML;
      } catch (error) {
        feedbackWorkspace.removeAttribute("aria-busy");
        if (submitter) submitter.disabled = false;
        window.alert(`${error.message} Please try again.`);
      }
      return;
    }

    const builder = form.closest("[data-form-builder]");
    if (!builder) {
      try { sessionStorage.setItem(scrollKey, String(scrollY)); } catch (_) {}
      return;
    }

    event.preventDefault();
    const position = scrollY;
    const submitter = event.submitter;
    if (submitter) submitter.disabled = true;
    builder.setAttribute("aria-busy", "true");

    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form, submitter),
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      if (!response.ok) throw new Error(`Request failed (${response.status})`);

      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const replacement = page.querySelector("[data-form-builder]");
      if (!replacement) throw new Error("The updated form builder was not returned.");

      const feedback = replacement.querySelector(".form-builder-feedback");
      page.querySelectorAll("main > .message").forEach((message) => feedback.append(message.cloneNode(true)));
      builder.replaceWith(replacement);
      requestAnimationFrame(() => scrollTo(0, position));
    } catch (error) {
      const feedback = builder.querySelector(".form-builder-feedback");
      feedback.replaceChildren();
      const message = document.createElement("div");
      message.className = "message error";
      message.textContent = `${error.message} Please try again.`;
      feedback.append(message);
      builder.removeAttribute("aria-busy");
      if (submitter) submitter.disabled = false;
    }
  });
})();
