# PWA diffs against https://github.com/astro-naut/Rho-for-Bolna (origin/main @ 2b842bb)

Do NOT apply these to the Sprout remote from this repo. Patched copies live under `pwa/patched/`.

Changes:
- Read `?ref=` on load into localStorage `rho_ref`; send `ref_code` on profile POST and events.
- Share button fetches `GET {RHO_API}/users/{id}/referral-code`, falls back to `SPROUT-NAME`.
- Include code in `referral`/`shared` event.
- Default `RHO_API` = `https://rho-backend.onrender.com`.

```diff
--- pwa/base/sprout-tracker.js	2026-11-01 13:15:47.581641688 +0000
+++ pwa/patched/sprout-tracker.js	2026-11-01 13:15:47.601641575 +0000
@@ -3,6 +3,7 @@
  *
  * Setup (before other scripts, or at top of this file):
  *   window.RHO_API = "https://YOUR-TUNNEL.example.com";  // Rho API base, no trailing slash
+ * Default (if unset): https://rho-backend.onrender.com
  *
  * Usage per funnel stage (call after the user successfully completes that step):
  *   window.rhoTrack("signup",  "completed");  // after beginRegistration / account created
@@ -15,13 +16,19 @@
  * Identify (once phone + consent known):
  *   window.rhoIdentify("+9198XXXXXXXX", "hinglish", true, "John Doe");
  *
+ * Referral:
+ *   ?ref=CODE on load is stored in localStorage 'rho_ref' and sent as ref_code
+ *   on profile POST and events. Share flow should call rhoReferralCode() first.
+ *
  * See snippet/INTEGRATION.md for exact Sprout function/button hook points.
  */
 (function (global) {
   "use strict";
 
   var STORAGE_KEY = "rho_uid";
-  var API = (global.RHO_API || "").replace(/\/$/, "");
+  var REF_KEY = "rho_ref";
+  var DEFAULT_API = "https://rho-backend.onrender.com";
+  var API = (global.RHO_API || DEFAULT_API).replace(/\/$/, "");
 
   function uid() {
     try {
@@ -38,6 +45,28 @@
     }
   }
 
+  function getRef() {
+    try {
+      return localStorage.getItem(REF_KEY) || null;
+    } catch (e) {
+      return null;
+    }
+  }
+
+  function setRef(code) {
+    if (!code) return;
+    try {
+      localStorage.setItem(REF_KEY, String(code));
+    } catch (e) {}
+  }
+
+  // Capture ?ref= on load
+  try {
+    var params = new URLSearchParams(global.location && global.location.search || "");
+    var refQ = params.get("ref");
+    if (refQ) setRef(refQ);
+  } catch (e) {}
+
   function post(path, body, keepalive) {
     if (!API) {
       if (typeof console !== "undefined") {
@@ -64,36 +93,58 @@
     } catch (e) {}
   }
 
-  function rhoTrack(stage, event) {
+  function rhoTrack(stage, event, extra) {
     var userId = uid();
-    post(
-      "/events",
-      {
-        user_id: userId,
-        stage: String(stage || ""),
-        event: String(event || "completed"),
-        ts: new Date().toISOString(),
-      },
-      true
-    );
+    var body = {
+      user_id: userId,
+      stage: String(stage || ""),
+      event: String(event || "completed"),
+      ts: new Date().toISOString(),
+    };
+    var ref = (extra && extra.ref_code) || getRef();
+    if (ref) body.ref_code = ref;
+    if (extra && extra.code && !body.ref_code) body.ref_code = extra.code;
+    post("/events", body, true);
   }
 
   function rhoIdentify(phone, language, consent, name) {
     var userId = uid();
-    post(
-      "/users/" + encodeURIComponent(userId) + "/profile",
-      {
-        phone: phone || null,
-        language: language || "hinglish",
-        consent: !!consent,
-        name: name || null,
-      },
-      false
-    );
+    var body = {
+      phone: phone || null,
+      language: language || "hinglish",
+      consent: !!consent,
+      name: name || null,
+    };
+    var ref = getRef();
+    if (ref) body.ref_code = ref;
+    post("/users/" + encodeURIComponent(userId) + "/profile", body, false);
     return userId;
   }
 
+  function rhoReferralCode() {
+    var userId = uid();
+    if (!API) return Promise.resolve(null);
+    return fetch(API + "/users/" + encodeURIComponent(userId) + "/referral-code", {
+      method: "GET",
+      mode: "cors",
+    })
+      .then(function (r) {
+        if (!r.ok) throw new Error("referral-code " + r.status);
+        return r.json();
+      })
+      .then(function (data) {
+        return (data && (data.referral_code || data.code)) || null;
+      })
+      .catch(function () {
+        return null;
+      });
+  }
+
   global.rhoTrack = rhoTrack;
   global.rhoIdentify = rhoIdentify;
   global.rhoUserId = uid;
+  global.rhoReferralCode = rhoReferralCode;
+  global.rhoGetRef = getRef;
+  global.rhoSetRef = setRef;
+  global.RHO_API = API;
 })(typeof window !== "undefined" ? window : globalThis);
```

```diff
--- pwa/base/index.html	2026-11-01 13:15:47.581641688 +0000
+++ pwa/patched/index.html	2026-11-01 13:15:47.601641575 +0000
@@ -31,7 +31,9 @@
     }
     if (params.get('fresh') === '1') {
       var keepRho = rhoQ ? String(rhoQ).replace(/\/$/, '') : localStorage.getItem('rho_api');
+      var keepRef = null; try { keepRef = localStorage.getItem('rho_ref'); } catch (e) {}
       try { localStorage.clear(); } catch (e) {}
+      if (keepRef) { try { localStorage.setItem('rho_ref', keepRef); } catch (e) {} }
       if (keepRho) {
         try { localStorage.setItem('rho_api', keepRho); } catch (e) {}
       }
@@ -48,9 +50,16 @@
     }
     var stored = null;
     try { stored = localStorage.getItem('rho_api'); } catch (e) {}
-    window.RHO_API = stored || window.RHO_API || 'https://rho-for-bolna.vercel.app';
+    
+    // Rho: persist ?ref= into localStorage 'rho_ref'
+    try {
+      var _refParams = new URLSearchParams(location.search);
+      var _ref = _refParams.get('ref');
+      if (_ref) localStorage.setItem('rho_ref', String(_ref));
+    } catch (e) {}
+    window.RHO_API = stored || window.RHO_API || 'https://rho-backend.onrender.com';
   } catch (e) {
-    window.RHO_API = window.RHO_API || 'https://rho-for-bolna.vercel.app';
+    window.RHO_API = window.RHO_API || 'https://rho-backend.onrender.com';
   }
 })();
 </script>
```

```diff
--- pwa/base/auth-pwa.js	2026-11-01 13:15:47.585641665 +0000
+++ pwa/patched/auth-pwa.js	2026-11-01 13:15:47.601641575 +0000
@@ -12,7 +12,7 @@
 function settingsPage(){return `<section class="content">${header('ACCOUNT','Settings & security','Manage your profile and preferences.') }<div class="panel">${[['Personal details','Name, email and mobile number'],['KYC & identity','Demo verification status'],['Bank & demat','Linked account details'],['Privacy','Data and device preferences'],['Help & support','Sprout care']].map(x=>`<div class="transaction-row"><span class="tx-info"><strong>${x[0]}</strong><small>${x[1]}</small></span><button class="text-link" onclick="toast('Settings are demo only')">Manage →</button></div>`).join('')}</div></section>`}
 function metalOrder(metal,side){dialog={kind:'metal',symbol:metal,side};paint()}
 function calcLoan(){let a=Number(document.getElementById('loanAmt')?.value||0),n=Number(document.getElementById('loanTerm')?.value||1),r=.115/12,emi=a*r*Math.pow(1+r,n)/(Math.pow(1+r,n)-1);let el=document.getElementById('emiValue');if(el)el.textContent='₹'+Math.round(emi||0).toLocaleString('en-IN')}
-function copyReferral(){const code='SPROUT-'+(person||'John').split(/\s+/)[0].toUpperCase();const link='https://astro-naut.github.io/Rho-for-Bolna/?ref='+encodeURIComponent(code);const msg='Hey! I'+String.fromCharCode(39)+'m investing with Sprout, it'+String.fromCharCode(39)+'s super easy to start. Use my invite code '+code+' when you sign up: '+link;try{navigator.clipboard&&navigator.clipboard.writeText(code).catch(()=>{})}catch(e){}try{window.rhoTrack&&window.rhoTrack('referral','shared')}catch(e){}window.open('https://wa.me/?text='+encodeURIComponent(msg),'_blank');toast('Opening WhatsApp to share your invite')}
+function copyReferral(){const fallback='SPROUT-'+((person||'John').split(/\s+/)[0].toUpperCase());const finish=(code)=>{const link='https://astro-naut.github.io/Rho-for-Bolna/?ref='+encodeURIComponent(code);const msg='Hey! I'+String.fromCharCode(39)+'m investing with Sprout, it'+String.fromCharCode(39)+'s super easy to start. Use my invite code '+code+' when you sign up: '+link;try{navigator.clipboard&&navigator.clipboard.writeText(code).catch(()=>{})}catch(e){}try{if(window.rhoTrack)window.rhoTrack('referral','shared',{ref_code:code,code:code})}catch(e){}window.open('https://wa.me/?text='+encodeURIComponent(msg),'_blank');toast('Opening WhatsApp to share your invite')};const p=(window.rhoReferralCode?window.rhoReferralCode():Promise.resolve(null));Promise.resolve(p).then(c=>finish(c||fallback)).catch(()=>finish(fallback))})}catch(e){}try{window.rhoTrack&&window.rhoTrack('referral','shared')}catch(e){}window.open('https://wa.me/?text='+encodeURIComponent(msg),'_blank');toast('Opening WhatsApp to share your invite')}
 function setAuthMode(m){authMode=m;authModal='';paint()}function showRecovery(){authModal='recovery';paint()}function finishRecovery(){let e=document.getElementById('recoverEmail')?.value;if(!e)return toast('Enter your email address');authModal='';paint();toast('If an account exists, recovery instructions are ready in this demo')}
 function authLogin(){let u=document.getElementById('authUser')?.value,p=document.getElementById('authPass')?.value;if(!u||!p)return toast('Enter your username and password');auth=true;localStorage.setItem('sprout-demo-auth','yes');stage='app';localStorage.setItem('sprout-demo-stage','app');paint()}
 function beginRegistration(){let n=document.getElementById('regName')?.value||'John Doe';if(!n)return toast('Enter your name');person=n;localStorage.setItem('sprout-demo-name',person);flow=0;stage='onboarding';localStorage.setItem('sprout-demo-stage',stage);try{if(window.rhoTrack)window.rhoTrack('signup','completed')}catch(e){}paint()}
```
