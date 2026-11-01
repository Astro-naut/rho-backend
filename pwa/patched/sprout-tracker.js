/**
 * Rho tracker — drop-in for the Sprout PWA.
 *
 * Setup (before other scripts, or at top of this file):
 *   window.RHO_API = "https://YOUR-TUNNEL.example.com";  // Rho API base, no trailing slash
 * Default (if unset): https://rho-backend.onrender.com
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
 * Referral:
 *   ?ref=CODE on load is stored in localStorage 'rho_ref' and sent as ref_code
 *   on profile POST and events. Share flow should call rhoReferralCode() first.
 *
 * See snippet/INTEGRATION.md for exact Sprout function/button hook points.
 */
(function (global) {
  "use strict";

  var STORAGE_KEY = "rho_uid";
  var REF_KEY = "rho_ref";
  var DEFAULT_API = "https://rho-backend.onrender.com";
  var API = (global.RHO_API || DEFAULT_API).replace(/\/$/, "");

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

  function getRef() {
    try {
      return localStorage.getItem(REF_KEY) || null;
    } catch (e) {
      return null;
    }
  }

  function setRef(code) {
    if (!code) return;
    try {
      localStorage.setItem(REF_KEY, String(code));
    } catch (e) {}
  }

  // Capture ?ref= on load
  try {
    var params = new URLSearchParams(global.location && global.location.search || "");
    var refQ = params.get("ref");
    if (refQ) setRef(refQ);
  } catch (e) {}

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

  function rhoTrack(stage, event, extra) {
    var userId = uid();
    var body = {
      user_id: userId,
      stage: String(stage || ""),
      event: String(event || "completed"),
      ts: new Date().toISOString(),
    };
    var ref = (extra && extra.ref_code) || getRef();
    if (ref) body.ref_code = ref;
    if (extra && extra.code && !body.ref_code) body.ref_code = extra.code;
    post("/events", body, true);
  }

  function rhoIdentify(phone, language, consent, name) {
    var userId = uid();
    var body = {
      phone: phone || null,
      language: language || "hinglish",
      consent: !!consent,
      name: name || null,
    };
    var ref = getRef();
    if (ref) body.ref_code = ref;
    post("/users/" + encodeURIComponent(userId) + "/profile", body, false);
    return userId;
  }

  function rhoReferralCode() {
    var userId = uid();
    if (!API) return Promise.resolve(null);
    return fetch(API + "/users/" + encodeURIComponent(userId) + "/referral-code", {
      method: "GET",
      mode: "cors",
    })
      .then(function (r) {
        if (!r.ok) throw new Error("referral-code " + r.status);
        return r.json();
      })
      .then(function (data) {
        return (data && (data.referral_code || data.code)) || null;
      })
      .catch(function () {
        return null;
      });
  }

  global.rhoTrack = rhoTrack;
  global.rhoIdentify = rhoIdentify;
  global.rhoUserId = uid;
  global.rhoReferralCode = rhoReferralCode;
  global.rhoGetRef = getRef;
  global.rhoSetRef = setRef;
  global.RHO_API = API;
})(typeof window !== "undefined" ? window : globalThis);
