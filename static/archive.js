(() => {
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) location.reload();
  });
})();
