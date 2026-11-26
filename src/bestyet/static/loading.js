// Feedback for page changes, for when there is no browser UI (an installed app).
// The pages work without it.
// iOS Safari showed no feedback at all when the navigation started with the tap, so the page is
// painted with the choice first, and what comes later (the bar, the dimming) is delayed by CSS set
// at the same moment rather than by a script that may no longer change what is shown.
// Loaded in the head and not deferred: a page that redirects itself (see _switcher.js) never gets
// as far as running deferred scripts.
(function () {
  var SHOW_AFTER = 300; // as the CSS delay: quicker pages get no progress bar
  // Longer than the 15 s Grist timeout, so that its error page normally arrives first.
  var GIVE_UP_AFTER = 20000;
  var root = document.documentElement;
  var chosen, showTimer, giveUpTimer;
  var pending = 0; // changes with each page change begun or dropped

  function announce(text) {
    var status = document.querySelector("[data-loading-status]");
    if (status) status.textContent = text;
  }

  function clear() {
    pending += 1;
    clearTimeout(showTimer);
    clearTimeout(giveUpTimer);
    root.classList.remove("leaving", "loading");
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
    root.classList.add("leaving"); // the CSS delays what this shows
    showTimer = setTimeout(function () {
      root.classList.add("loading");
      announce("Loading");
    }, SHOW_AFTER);
    giveUpTimer = setTimeout(function () {
      reset();
      announce("The page did not load. Try again.");
    }, GIVE_UP_AFTER);
  }

  // Go once a frame with the choice in it has been painted, unless another tap came first.
  function leaveFor(url) {
    var mine = pending;
    function go() {
      if (mine !== pending) return;
      mine = -1;
      location.assign(url);
    }
    requestAnimationFrame(function () {
      requestAnimationFrame(go);
    });
    setTimeout(go, 100); // in case no frames are being drawn
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
      event.preventDefault();
      start(link);
      leaveFor(link.href);
    }
  });

  // The athlete dropdown (see _switcher.js). It is never disabled, so it keeps its value.
  document.addEventListener("pickathlete", function (event) {
    event.preventDefault(); // we go there ourselves
    start(event.target);
    leaveFor(event.detail);
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
