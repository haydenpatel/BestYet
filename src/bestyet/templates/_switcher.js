// Enhancements for the athlete chips. The pages work without this script.
// It is inlined right after the chips (see _chips.html), so that it runs before they are first
// painted: nothing visibly swaps, and there is no separate file that could fail to load.
(function () {
  var switcher = document.currentScript.previousElementSibling;
  var chips = switcher.querySelector("[data-athlete-chips]");
  var links = Array.from(chips.querySelectorAll("a"));
  var current = chips.querySelector('[aria-current="true"]');

  // Remember the last athlete chosen, in this browser only.
  var KEY = "bestyet.athlete";
  function remember() {
    try {
      if (current) localStorage.setItem(KEY, current.dataset.athleteId);
    } catch (e) {
      // Storage is blocked: nothing is remembered.
    }
  }
  remember();
  if (!current && !new URLSearchParams(location.search).has("athlete")) {
    try {
      var id = localStorage.getItem(KEY);
      var stored = links.find(function (a) {
        return a.dataset.athleteId === id;
      });
      if (stored) {
        // Keep "nobody chosen" from showing on the way; show it after all if that stalls.
        var main = switcher.parentElement;
        main.style.visibility = "hidden";
        setTimeout(function () {
          main.style.visibility = "";
        }, 3000);
        stored.dispatchEvent(new Event("loadingstart", { bubbles: true })); // for loading.js
        // replace, so that Back does not return to the page with nobody chosen
        location.replace(stored.href);
      }
    } catch (e) {
      // Storage is blocked.
    }
  }

  // Chips that would need a third row are replaced by a dropdown.
  var pick = document.createElement("div");
  var select = document.createElement("select");
  pick.className = "pick";
  select.setAttribute("aria-label", "Athlete");
  if (!current) {
    var placeholder = new Option("Choose an athlete", "");
    placeholder.disabled = true;
    select.add(placeholder);
  }
  links.forEach(function (a) {
    select.add(new Option(a.textContent, a.getAttribute("href")));
  });
  function showCurrent() {
    select.value = current ? current.getAttribute("href") : "";
    if (!current) select.selectedIndex = 0; // the disabled placeholder
  }
  select.addEventListener("change", function () {
    // loading.js takes over the navigation if it is there.
    var options = { bubbles: true, cancelable: true, detail: select.value };
    if (pick.dispatchEvent(new CustomEvent("pickathlete", options))) {
      location.assign(select.value);
    }
  });
  showCurrent();
  // A page change that did not happen (see loading.js).
  document.addEventListener("loadingreset", showCurrent);
  pick.append(select);
  switcher.append(pick);

  // The chips stay laid out while collapsed, so their rows can still be counted.
  function fit() {
    var rows = new Set(links.map(function (a) {
      return a.offsetTop;
    }));
    switcher.classList.toggle("collapsed", rows.size > 2);
    pick.hidden = rows.size <= 2;
    switcher.classList.add("ready");
  }
  fit();
  // Fires on resize, rotation and text size changes.
  new ResizeObserver(fit).observe(chips);

  // Back and forward can restore the page as it was left, with another athlete in the dropdown.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    showCurrent();
    remember();
  });
})();
