// Apply the saved preference before styles paint (dark unless the user chose otherwise); storage may be unavailable.
(() => {
  let preference = "dark";
  try { preference = localStorage.getItem("spot2deez-theme") || "dark"; } catch {}
  if (!["system", "light", "dark"].includes(preference)) preference = "dark";
  document.documentElement.dataset.theme = preference;
})();
