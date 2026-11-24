/* AIRA - what makes the console an installable app, and usable on a phone.
 *
 * Loaded with `defer` on every page, after the page's own script. It never calls the page's
 * functions or changes its data; it only:
 *
 *   1. registers the service worker (sw.js) so the console can be installed;
 *   2. keeps the phone's status-bar colour in step with the light/dark theme;
 *   3. shows an "Install" button in the top bar when the browser offers installation;
 *   4. on the console at phone widths, turns the hidden incident list into its own screen, with a
 *      back button from an incident to the list (Android's back button works too).
 *
 * Every step is wrapped so a failure here can never stop the page itself from working.
 */
(() => {
  "use strict";

  const safe = fn => { try { fn(); } catch (e) { console.warn("[aira-pwa]", e); } };

  // ------------------------------------------------------------ 1. service worker
  safe(() => {
    const secure = location.protocol === "https:" ||
      location.hostname === "localhost" || location.hostname === "127.0.0.1";
    if (!("serviceWorker" in navigator) || !secure) return;
    navigator.serviceWorker.register("/sw.js", { scope: "/" })
      .catch(e => console.warn("[aira-pwa] service worker not registered:", e));
  });

  // ------------------------------------------------------------ 2. status-bar colour follows the theme
  safe(() => {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) return;
    const sync = () => {
      const c = getComputedStyle(document.documentElement).getPropertyValue("--nav").trim();
      if (c) meta.setAttribute("content", c);
    };
    sync();
    new MutationObserver(sync).observe(document.documentElement,
      { attributes: true, attributeFilter: ["data-theme"] });
  });

  // ------------------------------------------------------------ 3. install button
  safe(() => {
    const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
    if (standalone) { document.documentElement.classList.add("pwa-standalone"); return; }
    const right = document.querySelector(".nav-right");
    if (!right) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "icon-btn pwa-install";
    btn.textContent = "Install";
    btn.title = "Install AIRA as an app";
    btn.hidden = true;
    const theme = document.getElementById("theme");
    right.insertBefore(btn, theme && theme.parentNode === right ? theme : null);

    let offer = null;
    window.addEventListener("beforeinstallprompt", e => {
      e.preventDefault();
      offer = e;
      btn.hidden = false;
    });
    btn.addEventListener("click", async () => {
      if (!offer) return;
      offer.prompt();
      try { await offer.userChoice; } catch {}
      offer = null;
      btn.hidden = true;
    });
    window.addEventListener("appinstalled", () => { offer = null; btn.hidden = true; });
  });

  // ------------------------------------------------------------ 4. console: list <-> detail on phones
  safe(() => {
    const main = document.querySelector("main");
    const aside = main && main.querySelector(":scope > aside");
    const article = main && main.querySelector(":scope > article");
    const side = main && main.querySelector(":scope > .side");
    const list = document.getElementById("list");
    if (!main || !aside || !article || !side || !list) return;   // not the console page

    const body = document.body;
    const narrow = matchMedia("(max-width: 900px)");

    // The bar above an incident's detail. It sits beside #detail, never inside it, so the console
    // rebuilding #detail every three seconds does not touch it.
    const bar = document.createElement("div");
    bar.className = "m-bar";
    bar.innerHTML = '<button type="button" class="m-back">&lsaquo; All incidents</button>' +
                    '<button type="button" class="m-wait" hidden></button>';
    article.insertBefore(bar, article.firstChild);
    const back = bar.querySelector(".m-back");
    const wait = bar.querySelector(".m-wait");

    let view = "list";
    const setView = v => {
      view = v;
      body.classList.toggle("m-list", v === "list");
      body.classList.toggle("m-detail", v === "detail");
    };

    const apply = () => {
      if (narrow.matches) {
        body.classList.add("m-on");
        // The right rail is hidden at this width; its content goes under the detail instead. It is
        // moved, not copied, so the console keeps writing to the same #side element.
        if (side.parentNode !== article) article.appendChild(side);
        setView(view);
      } else {
        body.classList.remove("m-on", "m-list", "m-detail");
        if (side.parentNode !== main) main.appendChild(side);
      }
    };

    const openDetail = () => {
      if (!narrow.matches) return;
      if (view !== "detail") {
        setView("detail");
        try { history.pushState({ airaDetail: true }, ""); } catch {}
      }
      article.scrollTop = 0;
    };

    const toList = () => {
      if (history.state && history.state.airaDetail) history.back();   // popstate does the rest
      else setView("list");
    };

    // The tap is recognised in the capture phase, before the console's own handler runs, because
    // that handler repaints the list and the tapped row is no longer in the page afterwards. The
    // switch itself waits until the handler has selected the incident and repainted.
    document.addEventListener("click", e => {
      if (narrow.matches && e.target.closest && e.target.closest("#list .row")) {
        setTimeout(openDetail, 0);
      }
    }, true);
    back.addEventListener("click", toList);
    wait.addEventListener("click", toList);
    window.addEventListener("popstate", e => {
      if (!narrow.matches) return;
      setView(e.state && e.state.airaDetail ? "detail" : "list");
    });

    // How many incidents are waiting for a decision, read from the list the console renders, so
    // it is visible from inside any incident on a phone.
    const countWaiting = () => {
      const n = list.querySelectorAll(".row-wait").length;
      wait.hidden = n === 0;
      wait.textContent = n === 1 ? "1 awaiting approval" : `${n} awaiting approval`;
    };
    new MutationObserver(countWaiting).observe(list, { childList: true });
    countWaiting();

    if (narrow.addEventListener) narrow.addEventListener("change", apply);
    else if (narrow.addListener) narrow.addListener(apply);
    apply();
  });
})();
