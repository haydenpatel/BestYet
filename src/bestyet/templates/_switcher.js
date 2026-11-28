// Enhancements for the athlete chips. The pages work without this script.
// It is inlined right after the chips (see _chips.html), so that it runs before they are first
// painted, on a page load and again each time htmx swaps a page in: nothing visibly changes, and
// there is no separate file that could fail to load.
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
        // Once htmx has the page. Replacing, so Back does not return to nobody being chosen.
        var go = function () {
          setTimeout(function () {
            if (!window.htmx) return location.replace(stored.href);
            stored.setAttribute("hx-replace-url", "true");
            stored.click();
          }, 0);
        };
        if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", go);
        else go();
      }
    } catch (e) {
      // Storage is blocked.
    }
  }

  // Chips that would need a third row are replaced by a dropdown.
  var pick = document.createElement("div");
  var select = document.createElement("select");
  pick.className = "pick";
  select.id = "athlete-select"; // htmx gives it back the focus after a swap, if it still has it
  select.setAttribute("aria-label", "Athlete");
  if (!current) {
    var placeholder = new Option("Choose an athlete", "");
    placeholder.disabled = true;
    select.add(placeholder);
  }
  links.forEach(function (a) {
    select.add(new Option(a.textContent, a.getAttribute("href")));
  });
  // The default option is the page's athlete (app.js goes back to it when a choice was not loaded).
  var shown = current ? links.indexOf(current) : 0; // else the placeholder
  select.options[shown].defaultSelected = true;
  select.selectedIndex = shown;
  // After a choice by touch the dropdown gives up the focus: iOS Safari opens its picker again
  // when htmx focuses the new one. Keyboard and other input keep it.
  var byTouch = false;
  select.addEventListener("pointerdown", function (event) {
    byTouch = event.pointerType === "touch";
  });
  select.addEventListener("keydown", function () {
    byTouch = false;
  });
  // Choosing is following the athlete's link, whichever way links are being loaded.
  select.addEventListener("change", function () {
    if (byTouch) select.blur();
    byTouch = false;
    links
      .find(function (a) {
        return a.getAttribute("href") === select.value;
      })
      .click();
  });
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
})();
