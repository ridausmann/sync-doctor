// Sync Doctor: small progressive enhancements, no libraries.
// Forms post normally. JS only builds the vehicle picker, fills demo presets,
// shows loading state, and handles expand/sort interactions.
(function () {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---------------------------------------------------------------- icons
  const ICON = {
    car: '<path d="M19 17h2v-4.5L18.5 7h-13L3 12.5V17h2"/><circle cx="7.5" cy="17" r="2"/><circle cx="16.5" cy="17" r="2"/><path d="M9.5 17h5M3 12.5h18"/>',
    cars: '<path d="M3 11h18M5 11l2-5h10l2 5M4 11v6h2M18 17h2v-6"/><circle cx="8" cy="17" r="1.6"/><circle cx="16" cy="17" r="1.6"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
  };
  const svg = (name) =>
    `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name]}</svg>`;
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function flash(el) {
    if (!el) return;
    el.classList.remove("is-flashed");
    void el.offsetWidth; // restart the animation
    el.classList.add("is-flashed");
    setTimeout(() => el.classList.remove("is-flashed"), 1000);
  }

  // ---------------------------------------------------------------- home: vehicle picker
  const fleetEl = $("#fleet-data");
  const operator = $("#operator");
  const list = $("#vehicle-list");

  function renderVehicles(selected) {
    const fleet = JSON.parse(fleetEl.textContent)[operator.value] || [];
    const options = [{ id: "all", name: "All my vehicles", meta: `${fleet.length} car${fleet.length === 1 ? "" : "s"} · grouped results`, all: true }]
      .concat(fleet.map((v) => {
        // label format from the server: "Name · PLATE (ID)"
        const [name, rest = ""] = v.label.split(" · ");
        return { id: v.id, name, meta: rest.replace(/\s*\((.*)\)$/, " · $1") };
      }));
    list.innerHTML = options.map((o) => `
      <label class="vehicle-option${o.all ? " is-all" : ""}" data-id="${esc(o.id)}">
        <input type="radio" name="vehicle_id" value="${esc(o.id)}"${o.id === selected ? " checked" : ""}>
        <span class="vehicle-card">
          <span class="vehicle-avatar">${svg(o.all ? "cars" : "car")}</span>
          <span class="vehicle-text"><span class="vehicle-name">${esc(o.name)}</span><span class="vehicle-meta">${esc(o.meta)}</span></span>
          <span class="vehicle-check">${svg("check")}</span>
        </span>
      </label>`).join("");
  }

  function selectVehicle(id) {
    const input = $(`input[value="${CSS.escape(id)}"]`, list);
    if (!input) return;
    input.checked = true;
    const option = input.closest(".vehicle-option");
    // Scroll only the list, not the page.
    list.scrollTo({ top: option.offsetTop - list.offsetTop - 8, behavior: reduceMotion ? "auto" : "smooth" });
    flash(option);
  }

  if (fleetEl && operator && list) {
    renderVehicles(list.dataset.selected);
    operator.addEventListener("change", () => renderVehicles(""));

    $$(".scenario").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (operator.value !== btn.dataset.operator) {
          operator.value = btn.dataset.operator;
          flash(operator);
        }
        renderVehicles("");
        selectVehicle(btn.dataset.vehicle);
        const start = $("#start"), end = $("#end");
        start.value = btn.dataset.date;
        end.value = "";
        flash(start);
        $$(".scenario.is-active").forEach((b) => b.classList.remove("is-active"));
        btn.classList.add("is-active");

        const form = $("#diagnose-form");
        flash(form);
        // On narrow screens the form is above the list, so bring it back into view.
        if (form.getBoundingClientRect().top < 0 || window.innerWidth < 960) {
          form.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
        }
      });
    });
  }

  // ---------------------------------------------------------------- loading state
  const form = $("#diagnose-form");
  const button = $("#diagnose-btn");
  if (form && button) {
    const label = $(".btn-label", button);
    form.addEventListener("submit", () => {
      button.classList.add("is-loading");
      button.setAttribute("aria-busy", "true");
      label.textContent = "Running checks…";
      setTimeout(() => (button.disabled = true), 0); // after the submit is on its way
    });
    // Restore the button if the user comes back with the browser's back button.
    window.addEventListener("pageshow", () => {
      button.classList.remove("is-loading");
      button.removeAttribute("aria-busy");
      button.disabled = false;
      label.textContent = "Diagnose";
    });
  }

  // ---------------------------------------------------------------- follow-up "No"
  $$("[data-reveal]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const box = document.getElementById(btn.dataset.reveal);
      box.hidden = false;
      btn.setAttribute("aria-expanded", "true");
      btn.hidden = true;
      $("textarea", box)?.focus();
    });
  });

  // ---------------------------------------------------------------- results: open the anchored run
  // After a follow-up the server redirects to #RUN-xxxx. In the fleet view that run sits
  // inside a collapsed row, so open it and scroll to it.
  if (location.hash) {
    const target = document.getElementById(location.hash.slice(1));
    const row = target?.closest("details.fleet-row");
    if (row) {
      row.open = true;
      requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
    }
  }

  // ---------------------------------------------------------------- timeline: animate when seen
  // The staggered check animation is paused until the timeline scrolls into view.
  if ("IntersectionObserver" in window && !reduceMotion) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) { e.target.classList.remove("tl-pending"); io.unobserve(e.target); }
      });
    }, { threshold: 0.15 });
    $$(".timeline").forEach((tl) => {
      if (tl.getBoundingClientRect().top > window.innerHeight) {
        tl.classList.add("tl-pending");
        io.observe(tl);
      }
    });
  }

  // ---------------------------------------------------------------- fleet: sorting
  const sort = $("#fleet-sort");
  if (sort) {
    const groups = $("#fleet-groups");
    const flat = $("#fleet-flat");
    const rows = $$(".fleet-row", groups);
    const home = new Map(rows.map((r) => [r, r.parentElement])); // original group container
    const original = rows.slice();

    sort.addEventListener("change", () => {
      if (sort.value === "severity") {
        original.forEach((r) => home.get(r).appendChild(r));
        flat.hidden = true;
        groups.hidden = false;
        return;
      }
      const key = sort.value === "name" ? "name" : "vid";
      original.slice()
        .sort((a, b) => a.dataset[key].localeCompare(b.dataset[key], undefined, { numeric: true }))
        .forEach((r) => flat.appendChild(r));
      groups.hidden = true;
      flat.hidden = false;
    });
  }

  // ---------------------------------------------------------------- tickets: expandable rows
  $$(".ticket-row").forEach((row) => {
    const toggle = $(".row-toggle", row);
    const detail = document.getElementById(row.dataset.toggle);
    const set = (open) => {
      detail.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      row.classList.toggle("is-open", open);
    };
    toggle.addEventListener("click", (e) => { e.stopPropagation(); set(detail.hidden); });
    row.addEventListener("click", (e) => {
      if (window.getSelection()?.toString()) return; // don't toggle while selecting text
      set(detail.hidden);
    });
  });
})();
