// Sync Doctor: small progressive enhancements. The forms all work without JS
// except the vehicle dropdown, which is filled from the selected operator's fleet.
(function () {
  const fleetEl = document.getElementById("fleet-data");
  const operator = document.getElementById("operator");
  const vehicle = document.getElementById("vehicle");

  function fillVehicles(selected) {
    const fleet = JSON.parse(fleetEl.textContent)[operator.value] || [];
    vehicle.innerHTML = "";
    const opts = [{ id: "", label: "Choose a vehicle…" }, { id: "all", label: "All my vehicles" }, ...fleet];
    for (const v of opts) {
      const o = new Option(v.label, v.id, false, v.id === selected);
      vehicle.add(o);
    }
  }

  if (fleetEl && operator && vehicle) {
    fillVehicles(vehicle.dataset.selected);
    operator.addEventListener("change", () => fillVehicles(""));

    // Demo presets: fill operator, vehicle and date.
    document.querySelectorAll(".scenario").forEach((btn) => {
      btn.addEventListener("click", () => {
        operator.value = btn.dataset.operator;
        fillVehicles(btn.dataset.vehicle);
        document.getElementById("start").value = btn.dataset.date;
        document.getElementById("end").value = "";
        document.getElementById("diagnose-form").scrollIntoView({ behavior: "smooth" });
      });
    });
  }

  // "No" on the follow-up reveals an optional note box before sending.
  document.querySelectorAll("[data-reveal]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.getElementById(btn.dataset.reveal).hidden = false;
      btn.hidden = true;
    });
  });
})();
