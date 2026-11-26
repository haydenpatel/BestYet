// Feedback for page changes, for when there is no browser UI (an installed app).
// The pages work without it.
// Loaded in the head and not deferred: a page that redirects itself (see _switcher.js) never gets
// as far as running deferred scripts.
(function () {
  var SHOW_AFTER = 300; // quicker pages get no progress bar
  // Longer than the 15 s Grist timeout, so that its error page normally arrives first.
  var GIVE_UP_AFTER = 20000;
  var root = document.documentElement;
  var chosen, showTimer, giveUpTimer;

  function announce(text) {
    var status = document.querySelector("[data-loading-status]");
    if (status) status.textContent = text;
  }

  function clear() {
    clearTimeout(showTimer);
    clearTimeout(giveUpTimer);
    root.classList.remove("loading");
    announce("");
    if (chosen) {
      chosen.classList.remove("chosen");
      chosen.parentElement.classList.remove("choosing");
      chosen = null;
    }
  }

  // Back to the page as it is. _switcher.js listens, to put its dropdown back too.
  function reset() {
    clear();
    document.dispatchEvent(new Event("loadingreset"));
  }

  function start(control) {
    clear();
    chosen = control;
    chosen.classList.add("chosen");
    chosen.parentElement.classList.add("choosing");
    showTimer = setTimeout(function () {
      root.classList.add("loading");
      announce("Loading");
    }, SHOW_AFTER);
    giveUpTimer = setTimeout(function () {
      reset();
      announce("The page did not load. Try again.");
    }, GIVE_UP_AFTER);
  }

  function here(url) {
    return url.split("#")[0] === location.href.split("#")[0];
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest("a[href]");
    // Leave alone anything that does not replace this page, such as opening a new tab.
    if (!link || event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if ((link.target && link.target !== "_self") || link.hasAttribute("download")) return;
    if (link === chosen) return;
    if (root.classList.contains("loading")) {
      event.preventDefault(); // dimmed: also for the keyboard
    } else if (here(link.href)) {
      reset(); // reloading this page instead of any change still pending
    } else if (link.origin === location.origin) {
      start(link);
    }
  });

  // The athlete dropdown. It is never disabled, so it keeps its value.
  document.addEventListener("change", function (event) {
    var pick = event.target.closest(".pick");
    if (pick) start(pick);
  });

  // Back and forward can restore the page as it was left, still loading.
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) reset();
  });

  // _switcher.js is on its way to the remembered athlete.
  document.addEventListener("loadingstart", function (event) {
    start(event.target);
  });
})();
