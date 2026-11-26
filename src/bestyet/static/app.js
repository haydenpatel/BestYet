// Remember the last athlete chosen, in this browser only. The pages work without this.
(function () {
  var chips = document.querySelector("[data-athlete-chips]");
  if (!chips) return;
  var KEY = "bestyet.athlete";
  try {
    var current = chips.querySelector('[aria-current="true"]');
    if (current) {
      localStorage.setItem(KEY, current.dataset.athleteId);
    } else if (!new URLSearchParams(location.search).has("athlete")) {
      var id = localStorage.getItem(KEY);
      var chip = Array.from(chips.querySelectorAll("a")).find(function (a) {
        return a.dataset.athleteId === id;
      });
      // replace, so that Back does not return to the page with nobody chosen
      if (chip) location.replace(chip.href);
    }
  } catch (e) {
    // Storage is blocked: nothing is remembered.
  }
})();
