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

    const render = () => {
      selection.hidden = staged.length === 0;
      list.replaceChildren(...staged.map((item) => {
        const row = document.createElement("li");
        const name = document.createElement("b"); name.textContent = item.file.name;
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
    form?.addEventListener("submit", (event) => {
      if (form.hasAttribute("data-attachment-required") && !staged.length) {
        event.preventDefault();
        const input = label.querySelector('input[type="file"]:not([hidden])');
        input.setCustomValidity("Choose at least one file."); input.reportValidity(); input.setCustomValidity("");
      }
    });
  });
})();
