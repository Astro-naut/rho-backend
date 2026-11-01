/**
 * Rho tracker — drop-in for the Sprout PWA.
 *
 * Setup (before other scripts, or at top of this file):
 *   window.RHO_API = "https://YOUR-TUNNEL.example.com";  // Rho API base, no trailing slash
 *
 * Usage per funnel stage (call after the user successfully completes that step):
 *   window.rhoTrack("signup",  "completed");  // after beginRegistration / account created
 *   window.rhoTrack("profile", "completed");  // after investor profile step
 *   window.rhoTrack("kyc",     "completed");  // after PAN + identity steps
 *   window.rhoTrack("bank",    "completed");  // after bank link step
 *   window.rhoTrack("plan",    "completed");  // after SIP / plan confirm
 *   window.rhoTrack("deposit", "completed");  // after first funded trade / deposit confirm
 *
 * Identify (once phone + consent known):
 *   window.rhoIdentify("+9198XXXXXXXX", "hinglish", true, "John Doe");
 *
 * See snippet/INTEGRATION.md for exact Sprout function/button hook points.
 */
(function (global) {
  "use strict";

  var STORAGE_KEY = "rho_uid";
  var API = (global.RHO_API || "").replace(/\/$/, "");

  function uid() {
    try {
      var id = localStorage.getItem(STORAGE_KEY);
      if (id) return id;
      id =
        "u_" +
        Math.random().toString(36).slice(2, 10) +
        Date.now().toString(36).slice(-4);
      localStorage.setItem(STORAGE_KEY, id);
      return id;
    } catch (e) {
      return "u_anon_" + String(Date.now());
    }
  }

  function post(path, body, keepalive) {
    if (!API) {
      if (typeof console !== "undefined") {
        console.warn("[rho] RHO_API not set; skip", path, body);
      }
      return;
    }
    var url = API + path;
    var payload = JSON.stringify(body);
    if (keepalive && typeof navigator !== "undefined" && navigator.sendBeacon) {
      try {
        var blob = new Blob([payload], { type: "application/json" });
        if (navigator.sendBeacon(url, blob)) return;
      } catch (e) {}
    }
    try {
      fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload,
        keepalive: !!keepalive,
        mode: "cors",
      }).catch(function () {});
    } catch (e) {}
  }

  function rhoTrack(stage, event) {
    var userId = uid();
    post(
      "/events",
      {
        user_id: userId,
        stage: String(stage || ""),
        event: String(event || "completed"),
        ts: new Date().toISOString(),
      },
      true
    );
  }

  function rhoIdentify(phone, language, consent, name) {
    var userId = uid();
    post(
      "/users/" + encodeURIComponent(userId) + "/profile",
      {
        phone: phone || null,
        language: language || "hinglish",
        consent: !!consent,
        name: name || null,
      },
      false
    );
    return userId;
  }

  global.rhoTrack = rhoTrack;
  global.rhoIdentify = rhoIdentify;
  global.rhoUserId = uid;
})(typeof window !== "undefined" ? window : globalThis);
