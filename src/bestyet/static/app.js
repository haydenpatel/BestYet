// Pages are loaded by htmx (set up in base.html). This adds the feedback needed where there is no
// browser UI (an installed app) and the handling htmx leaves to the page. Without scripting, or
// without htmx, links load whole pages and none of this applies.
(function () {
  var SHOW_AFTER = 300; // quicker pages get no progress bar
  var root = document.documentElement;
  var request; // the XMLHttpRequest being waited for
  var chosen, timer;

  function announce(text) {
    document.querySelector("[data-loading-status]").textContent = text;
  }

  // A message over the foot of the page, taken away again by an empty text. Its Reload link retries
  // by loading `path` as a whole page, for use without browser UI.
  function warn(text, path) {
    var notice = document.querySelector("[data-load-failed]");
    notice.querySelector("p").textContent = text;
    if (path) notice.querySelector("a").href = path;
    notice.hidden = !text;
  }

  // No longer waiting: the bar, the dimming and the highlight go.
  function done() {
    clearTimeout(timer);
    root.classList.remove("loading");
    announce("");
    if (chosen) {
      chosen.classList.remove("chosen");
      chosen.parentElement.classList.remove("choosing");
    }
    chosen = request = null;
  }

  // For a page that was not replaced: the dropdown goes back to the athlete it shows.
  function resetDropdown() {
    document.querySelectorAll(".pick option").forEach(function (option) {
      if (option.defaultSelected) option.selected = true;
    });
  }

  function failed(event) {
    resetDropdown();
    // _switcher.js hides it on the way to the remembered athlete
    document.querySelector("main").style.visibility = "";
    warn(
      "Could not load the page. Check the connection and try again.",
      event.detail.pathInfo.finalRequestPath
    );
  }

  // What shows a choice: the link, or the dropdown standing in for the chips.
  function controlFor(element) {
    var switcher = element.closest(".switcher.collapsed");
    return switcher ? switcher.querySelector(".pick") : element;
  }

  // Capturing, to come before htmx's own listeners.
  document.addEventListener(
    "click",
    function (event) {
      var control = event.target.closest("a[href], .pick");
      if (!control) return;
      if (event.shiftKey || event.altKey) {
        event.stopImmediatePropagation(); // for the browser: htmx leaves only Ctrl and Cmd to it
      } else if (root.classList.contains("loading") && controlFor(control) !== chosen) {
        event.preventDefault(); // dimmed: also for the keyboard
        event.stopImmediatePropagation();
      }
    },
    true
  );

  // On an athlete's page the home tab means that page: going by way of "/" and its redirect to the
  // remembered athlete would leave the same page in the history twice.
  document.addEventListener("htmx:configRequest", function (event) {
    if (
      event.detail.elt.id === "tab-home" &&
      event.detail.path === location.pathname &&
      document.querySelector('#page .chips [aria-current="true"]')
    ) {
      event.detail.path = location.pathname + location.search;
    }
  });

  document.addEventListener("htmx:beforeRequest", function (event) {
    done();
    warn("");
    request = event.detail.xhr;
    // Loading the page already shown only refreshes it.
    if (event.detail.pathInfo.finalRequestPath === location.pathname + location.search) return;
    chosen = controlFor(event.detail.elt);
    chosen.classList.add("chosen");
    chosen.parentElement.classList.add("choosing");
    timer = setTimeout(function () {
      root.classList.add("loading");
      announce("Loading");
    }, SHOW_AFTER);
  });

  document.addEventListener("htmx:afterRequest", function (event) {
    if (event.detail.xhr !== request) return;
    done();
    if (event.detail.xhr.status) announce(document.title); // a new page, for screen readers
  });

  // No answer at all, or none within htmx's timeout.
  document.addEventListener("htmx:sendError", failed);
  document.addEventListener("htmx:timeout", failed);

  // Error pages are swapped in like any other. A response that is not one of the app's pages has
  // nothing to swap in, so the browser loads it.
  document.addEventListener("htmx:beforeSwap", function (event) {
    if (event.detail.serverResponse.indexOf('id="page"') === -1) {
      event.detail.shouldSwap = false;
      location.assign(event.detail.pathInfo.finalRequestPath);
    }
  });

  // No new history entry for loading the page already shown, nor for the first athlete chosen:
  // going Back to "nobody chosen" would only go on to the remembered athlete again.
  document.addEventListener("htmx:beforeHistoryUpdate", function (event) {
    var path = event.detail.history.path;
    var first = !location.search && path.indexOf(location.pathname + "?athlete=") === 0;
    if (first || path === location.pathname + location.search) {
      event.detail.history.type = "replace";
    }
  });

  // Back from another site can restore the page as it was left.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    done();
    warn("");
    resetDropdown();
  });
})();
