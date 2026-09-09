(() => {
  const dialog = document.querySelector("#image-viewer");
  const links = [...document.querySelectorAll("[data-image-viewer]")];
  if (!dialog || !links.length || typeof dialog.showModal !== "function") return;
  const picture = dialog.querySelector("[data-viewer-image]");
  const video = dialog.querySelector("[data-viewer-video]");
  const audio = dialog.querySelector("[data-viewer-audio]");
  const status = dialog.querySelector("[data-image-status]");
  const name = dialog.querySelector("[data-image-name]");
  const position = dialog.querySelector("[data-image-position]");
  const download = dialog.querySelector("[data-image-download]");
  const previous = dialog.querySelector("[data-image-previous]");
  const next = dialog.querySelector("[data-image-next]");
  let index = 0;
  let opener;
  let current;
  const release = () => {
    current = null;
    picture.removeAttribute("src");
    picture.hidden = true;
    for (const media of [video, audio]) {
      media.pause();
      media.removeAttribute("src");
      media.load();
      media.hidden = true;
    }
  };
  const show = (requested) => {
    release();
    index = (requested + links.length) % links.length;
    const link = links[index];
    const filename = link.dataset.mediaName || link.querySelector("img").alt;
    const type = link.dataset.mediaType || "image";
    current = type.startsWith("video/") ? video : type.startsWith("audio/") ? audio : picture;
    name.textContent = filename;
    position.textContent = `${index + 1} / ${links.length}`;
    status.hidden = false;
    status.textContent = "Loading attachment…";
    const url = new URL(link.href);
    url.searchParams.delete("inline");
    download.href = url.href;
    download.download = filename;
    if (current === picture) picture.alt = filename;
    else {
      current.hidden = false;
      current.setAttribute("aria-label", filename);
    }
    current.src = link.href;
  };
  for (const media of [picture, video, audio]) {
    media.addEventListener(media === picture ? "load" : "loadedmetadata", () => {
      if (media !== current || !media.getAttribute("src")) return;
      media.hidden = false;
      status.hidden = true;
    });
    media.addEventListener("error", () => {
      if (media !== current || !media.getAttribute("src")) return;
      media.hidden = true;
      status.hidden = false;
      status.textContent = "This attachment couldn’t be opened. Your browser may not support its format, or the file may be unavailable. Try downloading it.";
    });
  }
  links.forEach((link, linkIndex) => link.addEventListener("click", (event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    opener = link;
    show(linkIndex);
    dialog.showModal();
    document.documentElement.classList.add("image-viewer-open");
  }));
  previous.hidden = next.hidden = links.length < 2;
  previous.addEventListener("click", () => show(index - 1));
  next.addEventListener("click", () => show(index + 1));
  const close = () => { release(); dialog.close(); };
  dialog.querySelector("[data-image-close]").addEventListener("click", close);
  dialog.addEventListener("click", (event) => { if (event.target === dialog) close(); });
  dialog.addEventListener("cancel", release);
  dialog.addEventListener("keydown", (event) => {
    if (event.target.closest("video, audio, input, textarea, select")) return;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      show(index + (event.key === "ArrowLeft" ? -1 : 1));
    }
  });
  dialog.addEventListener("close", () => {
    document.documentElement.classList.remove("image-viewer-open");
    release();
    opener?.focus({ preventScroll: true });
  });
  window.addEventListener("pagehide", () => { if (dialog.open) dialog.close(); release(); });
})();
