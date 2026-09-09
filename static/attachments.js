(() => {
  document.querySelectorAll(".attachment-masonry.is-loading").forEach((gallery) => {
    const images = [...gallery.querySelectorAll("img")];
    const settled = images.map((image) => image.complete
      ? Promise.resolve()
      : new Promise((resolve) => {
        image.addEventListener("load", resolve, { once: true });
        image.addEventListener("error", resolve, { once: true });
      }));
    Promise.all(settled).then(() => requestAnimationFrame(() => gallery.classList.remove("is-loading")));
  });

  document.querySelectorAll("[data-attachment-picker]").forEach((picker) => {
    const form = picker.closest("form");
    const label = picker.querySelector("label");
    const selection = picker.querySelector("[data-attachment-selection]");
    const list = picker.querySelector("[data-attachment-list]");
    const clear = picker.querySelector("[data-attachment-clear]");
    const staged = [];
    const fileLimit = Number(picker.dataset.fileLimit);
    const cardLimit = Number(picker.dataset.cardLimit);
    const stored = Number(picker.dataset.storedBytes || 0);
    const usage = picker.querySelector("[data-attachment-usage]");
    const progress = picker.querySelector("[data-attachment-progress]");
    const warning = picker.querySelector("[data-attachment-warning]");
    const submits = [...form.querySelectorAll('button:not([type]), button[type="submit"], input[type="submit"]')]
      .filter((button) => !(button.name === "action" && button.value === "save"));
    const originallyDisabled = new Map(submits.map((button) => [button, button.disabled]));
    const mib = (bytes) => (Math.ceil(bytes / 1048576 * 100) / 100).toFixed(2);
    let invalid = false;
    const updateQuota = () => {
      const queued = staged.reduce((total, item) => total + item.file.size, 0);
      const projected = stored + queued;
      const oversized = staged.filter((item) => item.file.size > fileLimit);
      invalid = oversized.length > 0 || (staged.length > 0 && projected > cardLimit);
      if (usage) {
        const remaining = (Math.floor(Math.max(0, cardLimit - projected) / 1048576 * 100) / 100).toFixed(2);
        usage.textContent = `${mib(stored)} MiB used${staged.length ? ` · ${mib(queued)} MiB queued` : ""} · ${remaining} MiB left`;
      }
      if (progress) progress.value = Math.min(projected, cardLimit);
      if (warning) {
        const reasons = [];
        if (oversized.length) reasons.push(`${oversized.length} file(s) exceed the ${mib(fileLimit)} MiB per-file limit.`);
        if (projected > cardLimit) reasons.push(`Attachments exceed the card limit by ${mib(projected - cardLimit)} MiB.`);
        warning.hidden = reasons.length === 0;
        warning.textContent = reasons.join(" ") + (invalid ? " Remove files before submitting." : "");
      }
      picker.classList.toggle("is-over-limit", invalid);
      submits.forEach((button) => { button.disabled = originallyDisabled.get(button) || invalid; });
    };

    const render = () => {
      updateQuota();
      selection.hidden = staged.length === 0;
      list.replaceChildren(...staged.map((item) => {
        const row = document.createElement("li");
        const name = document.createElement("b"); name.textContent = item.file.name;
        if (item.file.size > fileLimit) {
          row.classList.add("is-oversized");
          const error = document.createElement("small");
          error.textContent = `Over the ${mib(fileLimit)} MiB file limit`;
          name.append(document.createElement("br"), error);
        }
        const size = document.createElement("span"); size.textContent = `${Math.max(1, Math.ceil(item.file.size / 1024))} KB`;
        const remove = document.createElement("button"); remove.type = "button"; remove.className = "attachment-remove";
        remove.textContent = "×"; remove.setAttribute("aria-label", `Remove ${item.file.name}`);
        remove.addEventListener("click", () => {
          item.input.remove(); staged.splice(staged.indexOf(item), 1); render();
        });
        row.append(remove, name, size); return row;
      }));
    };
    const activate = (input) => {
      input.addEventListener("change", () => {
        if (!input.files.length) return;
        const files = [...input.files];
        const next = input.cloneNode();
        next.hidden = false; next.required = false; next.value = "";
        input.remove(); label.append(next); activate(next);
        files.forEach((file) => {
          const queuedInput = next.cloneNode();
          queuedInput.hidden = true; queuedInput.required = false; queuedInput.value = "";
          const transfer = new DataTransfer(); transfer.items.add(file); queuedInput.files = transfer.files;
          label.append(queuedInput); staged.push({ input: queuedInput, file });
        });
        render();
      }, { once: true });
    };
    activate(label.querySelector('input[type="file"]'));
    clear.addEventListener("click", () => {
      staged.splice(0).forEach((item) => item.input.remove());
      render();
    });
    render();
    form?.addEventListener("submit", (event) => {
      if (event.submitter?.name === "action" && event.submitter.value === "save") {
        // Saving text must still work when queued attachments exceed upload limits.
        picker.querySelectorAll('input[type="file"]').forEach((input) => { input.disabled = true; });
        return;
      }
      updateQuota();
      if (invalid) {
        event.preventDefault();
        warning?.scrollIntoView({ block: "center", behavior: "smooth" });
        return;
      }
      if (form.hasAttribute("data-attachment-required") && !staged.length) {
        event.preventDefault();
        const input = label.querySelector('input[type="file"]:not([hidden])');
        input.setCustomValidity("Choose at least one file."); input.reportValidity(); input.setCustomValidity("");
      }
    });
  });
})();
