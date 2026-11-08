/* HORIZON AI - operations assistant panel for the AI Operations Centre (/ai-centre).
   Chat about alerts, services and incidents, answered by Claude from the data this page shows (POST /api/assistant/ask).
   Voice: microphone in (browser speech recognition) and spoken answers (browser voices). Languages: English, 中文, Bahasa Melayu.
   Faces: "Agent" (drawn here, mouth moves while it speaks), "Orb", or "Photo" (a picture you choose; stays in this browser).
   Advisory only: nothing here changes service. window.HAI_DEMO = true -> canned answers, no server (preview pages). */
(function(){
  "use strict";
  if(window.__HAI) return; window.__HAI = true;

  /* ---------------- words in three languages ---------------- */
  var T = {
    en: {sub: "Operations Assistant", on: "online", off: "not set up", ph: "Ask about an alert, service or incident", send: "Send", mic: "Speak your question",
         micNo: "Voice input works in Chrome or Edge", listen: "Listening…", speaking: "Speaking", idle: "Ready", think: "Checking the live data…",
         faces: {agent: "Agent", orb: "Orb", photo: "Photo"}, replay: "Replay", mute: "Mute voice", unmute: "Voice on", stop: "Stop",
         min: "Minimise", big: "Bigger", small: "Smaller", you: "You", hello: "Hello, I'm HORIZON AI. Ask me about any alert, service or incident on this page and I'll explain why it happened and what to do.",
         foot: "Advisory only · answers from this page's data · the Service Controller decides", choose: "Choose photo", photoNote: "Use a picture you have permission to use. It stays in this browser only.",
         noPhoto: "No photo yet", fab: "Ask HORIZON AI", noVoice: "No voice for this language on this computer: showing text only.", idVoice: "No Malay voice on this computer: using an Indonesian voice.",
         cats: {loading: "heavily loaded", traffic: "delayed by traffic", bunching: "bunching", gap: "running with a long gap"},
         why: "Why is {svc} {cat} at {loc}?", now: "Which services need action now?", sum: "Summarise the network in 3 lines.", cp: "Are the checkpoints jammed?",
         err: "Could not get an answer", offMsg: "HORIZON AI is not set up yet: add ANTHROPIC_API_KEY in Render > Environment."},
    zh: {sub: "运营助理", on: "在线", off: "未设置", ph: "询问警报、巴士服务或事故", send: "发送", mic: "说出你的问题",
         micNo: "语音输入请使用 Chrome 或 Edge", listen: "正在聆听…", speaking: "正在说话", idle: "就绪", think: "正在查看实时数据…",
         faces: {agent: "助理", orb: "光环", photo: "照片"}, replay: "重播", mute: "关闭语音", unmute: "开启语音", stop: "停止",
         min: "最小化", big: "放大", small: "缩小", you: "你", hello: "您好，我是 HORIZON AI。请问我本页上的任何警报、巴士服务或事故，我会解释原因和处理方法。",
         foot: "仅供参考 · 根据本页数据回答 · 由服务控制员决定", choose: "选择照片", photoNote: "请使用您有权使用的照片。照片只保存在此浏览器中。",
         noPhoto: "尚未选择照片", fab: "询问 HORIZON AI", noVoice: "此电脑没有中文语音：只显示文字。", idVoice: "",
         cats: {loading: "载客量高", traffic: "因交通延误", bunching: "串车（车距过近）", gap: "班距过大"},
         why: "{svc} 号为什么在 {loc} {cat}？", now: "现在哪些巴士服务需要处理？", sum: "用三句话总结目前的网络状况。", cp: "关卡现在拥堵吗？",
         err: "无法取得回答", offMsg: "HORIZON AI 尚未设置：请在 Render > Environment 加入 ANTHROPIC_API_KEY。"},
    ms: {sub: "Pembantu Operasi", on: "dalam talian", off: "belum disediakan", ph: "Tanya tentang amaran, perkhidmatan atau insiden", send: "Hantar", mic: "Sebut soalan anda",
         micNo: "Input suara berfungsi dalam Chrome atau Edge", listen: "Sedang mendengar…", speaking: "Sedang bercakap", idle: "Sedia", think: "Sedang menyemak data langsung…",
         faces: {agent: "Ejen", orb: "Orb", photo: "Foto"}, replay: "Main semula", mute: "Senyapkan suara", unmute: "Suara dihidupkan", stop: "Henti",
         min: "Kecilkan", big: "Besarkan", small: "Kecilkan saiz", you: "Anda", hello: "Helo, saya HORIZON AI. Tanya saya tentang sebarang amaran, perkhidmatan atau insiden di halaman ini, dan saya akan terangkan sebabnya dan apa yang perlu dibuat.",
         foot: "Nasihat sahaja · jawapan daripada data halaman ini · Pengawal Perkhidmatan yang memutuskan", choose: "Pilih foto", photoNote: "Gunakan gambar yang anda dibenarkan guna. Ia disimpan dalam pelayar ini sahaja.",
         noPhoto: "Belum ada foto", fab: "Tanya HORIZON AI", noVoice: "Tiada suara Bahasa Melayu pada komputer ini: teks sahaja.", idVoice: "Tiada suara Bahasa Melayu pada komputer ini: menggunakan suara Bahasa Indonesia.",
         cats: {loading: "penuh sesak", traffic: "lewat kerana trafik", bunching: "berkelompok", gap: "jurang panjang"},
         why: "Kenapa perkhidmatan {svc} {cat} di {loc}?", now: "Perkhidmatan mana perlu tindakan sekarang?", sum: "Ringkaskan rangkaian dalam 3 ayat.", cp: "Adakah pusat pemeriksaan sesak?",
         err: "Tidak dapat jawapan", offMsg: "HORIZON AI belum disediakan: tambah ANTHROPIC_API_KEY dalam Render > Environment."}
  };
  var LANGS = [["en", "EN"], ["zh", "中文"], ["ms", "BM"]];
  var STT = {en: "en-SG", zh: "zh-CN", ms: "ms-MY"};

  function store(k, v){ try{ if(v === undefined) return localStorage.getItem("hai." + k); localStorage.setItem("hai." + k, v); }catch(e){} return null; }
  var S = {lang: store("lang") || ((navigator.language || "").slice(0, 2) === "zh" ? "zh" : "en"), face: store("face") || "agent", mute: store("mute") === "1",
           big: store("big") === "1", open: store("open") !== "0", history: [], speaking: false, level: 0, configured: null, lastAnswer: "", listening: false};
  if(!T[S.lang]) S.lang = "en";
  if(["agent", "orb", "photo"].indexOf(S.face) < 0) S.face = "agent";
  var t = function(k){ return T[S.lang][k]; };
  var esc = function(s){ return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){ return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]; }); };
  var clock = function(){ return new Date().toLocaleTimeString("en-SG", {hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Singapore"}); };

  /* ---------------- faces ---------------- */
  function agentSVG(){            // an original drawn agent with a headset; mouth and eyes are animated
    return '<svg class="hai-agent" viewBox="0 0 200 200" aria-hidden="true">'
      + '<defs><radialGradient id="haiBg" cx="50%" cy="38%" r="70%"><stop offset="0" stop-color="#16407a"/><stop offset="1" stop-color="#061631"/></radialGradient>'
      + '<linearGradient id="haiSuit" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#14305a"/><stop offset="1" stop-color="#0a1b36"/></linearGradient></defs>'
      + '<rect width="200" height="200" fill="url(#haiBg)"/>'
      + '<path d="M30 200c4-34 26-52 52-58h36c26 6 48 24 52 58z" fill="url(#haiSuit)"/>'
      + '<path d="M82 142l18 22 18-22" fill="none" stroke="#3fd3ff" stroke-width="2.4" stroke-linejoin="round"/>'
      + '<path d="M88 124h24v20c0 6-24 6-24 0z" fill="#d9a988"/>'
      + '<path d="M60 92c0-34 18-54 40-54s40 20 40 54c0 10-2 22-6 30l-6-26-56 0-6 26c-4-8-6-20-6-30z" fill="#1a1f2e"/>'
      + '<ellipse cx="100" cy="96" rx="31" ry="37" fill="#e8bd9b"/>'
      + '<path d="M68 88c4-22 18-32 34-32 14 0 26 8 31 24-12-8-26-12-40-10-10 2-18 8-25 18z" fill="#1a1f2e"/>'
      + '<path d="M80 86q8-4 15 0M105 86q8-4 15 0" fill="none" stroke="#2a2230" stroke-width="2.2" stroke-linecap="round"/>'
      + '<g class="blink"><ellipse cx="88" cy="95" rx="3.4" ry="4" fill="#1c2232"/><ellipse cx="112" cy="95" rx="3.4" ry="4" fill="#1c2232"/>'
      + '<circle cx="89.2" cy="93.6" r="1" fill="#fff"/><circle cx="113.2" cy="93.6" r="1" fill="#fff"/></g>'
      + '<path d="M100 99q-2 9 0 12h3" fill="none" stroke="#c48f70" stroke-width="1.6" stroke-linecap="round"/>'
      + '<ellipse cx="82" cy="110" rx="6" ry="3.4" fill="#f09a8a" opacity=".28"/><ellipse cx="118" cy="110" rx="6" ry="3.4" fill="#f09a8a" opacity=".28"/>'
      + '<g id="haiMouth"><path id="haiLipU" d="M91 118q9 -3 18 0" fill="none" stroke="#b5615a" stroke-width="2.2" stroke-linecap="round"/>'
      + '<ellipse id="haiOpen" cx="100" cy="119.5" rx="7" ry="0.1" fill="#5a1f28"/>'
      + '<path id="haiLipL" d="M91 118q9 4 18 0" fill="none" stroke="#b5615a" stroke-width="2.2" stroke-linecap="round"/></g>'
      + '<path d="M62 96c0-30 16-46 38-46s38 16 38 46" fill="none" stroke="#2c3a56" stroke-width="5" stroke-linecap="round"/>'
      + '<rect x="55" y="88" width="13" height="22" rx="6" fill="#2c3a56"/><rect x="132" y="88" width="13" height="22" rx="6" fill="#2c3a56"/>'
      + '<rect x="57" y="92" width="4" height="14" rx="2" fill="#3fd3ff" opacity=".85"/>'
      + '<path d="M62 109c2 12 10 18 24 17" fill="none" stroke="#2c3a56" stroke-width="3" stroke-linecap="round"/><rect x="84" y="123" width="8" height="5" rx="2.5" fill="#3fd3ff"/>'
      + "</svg>";
  }
  function orbSVG(){
    return '<svg class="hai-orb" viewBox="0 0 200 200" aria-hidden="true"><defs><radialGradient id="haiCore" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#eef9ff"/><stop offset=".35" stop-color="#3fd3ff"/><stop offset="1" stop-color="#1f8fff" stop-opacity="0"/></radialGradient></defs>'
      + '<rect width="200" height="200" fill="#051428"/>'
      + '<circle class="ring r1" cx="100" cy="100" r="74" fill="none" stroke="#3fd3ff" stroke-width="2" stroke-dasharray="6 10" opacity=".7"/>'
      + '<circle class="ring r2" cx="100" cy="100" r="60" fill="none" stroke="#a07bff" stroke-width="2" stroke-dasharray="30 14" opacity=".75"/>'
      + '<circle id="haiCoreC" cx="100" cy="100" r="34" fill="url(#haiCore)"/>'
      + '<circle id="haiWave" cx="100" cy="100" r="44" fill="none" stroke="#3fd3ff" stroke-width="2" opacity="0"/></svg>';
  }
  function photoHTML(){
    var src = store("photo");
    return src ? '<img id="haiImg" alt="" src="' + esc(src) + '">' : '<div class="hai-nophoto">' + esc(t("noPhoto")) + "</div>";
  }
  function faceHTML(){ return S.face === "orb" ? orbSVG() : S.face === "photo" ? photoHTML() : agentSVG(); }

  /* ---------------- panel ---------------- */
  var css = document.createElement("style");
  css.textContent = ".hai-nophoto{display:grid;place-items:center;width:100%;height:100%;color:#8ca6c4;font:600 12px/1.3 Rajdhani,sans-serif;text-align:center;padding:12px}";
  document.head.appendChild(css);
  var el = document.createElement("section");
  el.className = "hai"; el.id = "hai"; el.setAttribute("role", "dialog"); el.setAttribute("aria-label", "HORIZON AI operations assistant");
  var fab = document.createElement("button");
  fab.type = "button"; fab.className = "hai-fab"; fab.id = "haiFab";
  document.body.appendChild(el); document.body.appendChild(fab);

  function frame(){
    el.classList.toggle("big", S.big);
    el.innerHTML = '<header class="hai-h" id="haiDrag"><b>HORIZON AI</b><small id="haiSub"></small><span class="hai-st" id="haiSt"><i></i><span></span></span>'
      + '<select id="haiLang" aria-label="Language">' + LANGS.map(function(l){ return '<option value="' + l[0] + '">' + l[1] + "</option>"; }).join("") + "</select>"
      + '<button type="button" class="hai-btn" id="haiBig"></button><button type="button" class="hai-btn" id="haiMin">–</button></header>'
      + '<div class="hai-stage"><div class="hai-face" id="haiFace"></div><div class="hai-side">'
      + '<div class="hai-say" id="haiSay"><span class="hai-bars" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></span><span></span></div>'
      + '<div class="hai-faces" id="haiFaces" role="group" aria-label="Face"></div>'
      + '<div class="hai-ctl"><button type="button" class="hai-btn" id="haiReplay"></button><button type="button" class="hai-btn" id="haiMute"></button><button type="button" class="hai-btn" id="haiStop"></button></div>'
      + '<div class="hai-note" id="haiNote"></div></div></div>'
      + '<div class="hai-log" id="haiLog" aria-live="polite"></div><div class="hai-sugg" id="haiSugg"></div>'
      + '<form class="hai-in" id="haiForm" autocomplete="off"><input id="haiQ" maxlength="600"><button type="button" class="hai-btn mic" id="haiMic">🎙</button><button type="submit" class="hai-btn send" id="haiSend"></button></form>'
      + '<div class="hai-foot" id="haiFoot"></div>';
    $("haiLang").value = S.lang;
    wire(); words(); drawFace(); renderLog(); suggestions(); status();
  }
  function $(id){ return document.getElementById(id); }
  function words(){
    $("haiSub").textContent = t("sub");
    $("haiQ").placeholder = t("ph");
    $("haiSend").textContent = t("send");
    $("haiMic").title = SR ? t("mic") : t("micNo"); $("haiMic").setAttribute("aria-label", $("haiMic").title); $("haiMic").disabled = !SR;
    $("haiReplay").textContent = "↻ " + t("replay");
    $("haiMute").textContent = S.mute ? "🔇" : "🔊"; $("haiMute").title = S.mute ? t("unmute") : t("mute"); $("haiMute").setAttribute("aria-pressed", S.mute ? "true" : "false");
    $("haiStop").textContent = "■"; $("haiStop").title = t("stop");
    $("haiMin").title = t("min"); $("haiMin").setAttribute("aria-label", t("min"));
    $("haiBig").textContent = S.big ? "⤡" : "⤢"; $("haiBig").title = S.big ? t("small") : t("big");
    $("haiFoot").textContent = t("foot");
    $("haiFaces").innerHTML = ["agent", "orb", "photo"].map(function(f){ return '<button type="button" class="hai-btn" data-face="' + f + '" aria-pressed="' + (S.face === f) + '">' + esc(t("faces")[f]) + "</button>"; }).join("")
      + (S.face === "photo" ? '<label class="hai-btn" for="haiFile" style="cursor:pointer">' + esc(t("choose")) + '</label><input type="file" id="haiFile" accept="image/*" hidden>' : "");
    $("haiNote").textContent = S.face === "photo" ? t("photoNote") : (S.voiceNote || "");
    say(S.speaking);
    fab.innerHTML = '<span class="mini">' + faceHTML() + "</span>" + esc(t("fab"));
    fab.hidden = S.open; el.hidden = !S.open;
  }
  function drawFace(){ $("haiFace").innerHTML = faceHTML(); }
  function say(on){
    var s = $("haiSay"); if(!s) return;
    s.classList.toggle("on", !!on); s.lastChild.textContent = S.listening ? t("listen") : on ? t("speaking") : t("idle");
  }
  function status(){
    var st = $("haiSt"); if(!st) return;
    var ok = window.HAI_DEMO || S.configured !== false;
    st.classList.toggle("off", !ok); st.lastChild.textContent = ok ? t("on") : t("off");
  }

  /* markdown-lite for answers: **bold**, "- " bullets, "1. " steps, paragraphs */
  function md(text){
    var out = [], list = null;
    String(text || "").split(/\n/).forEach(function(line){
      var l = line.trim(), m;
      var inl = function(s){ return esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>"); };
      if((m = l.match(/^[-•*]\s+(.*)$/))){ if(list !== "ul"){ if(list) out.push("</" + list + ">"); out.push("<ul>"); list = "ul"; } out.push("<li>" + inl(m[1]) + "</li>"); return; }
      if((m = l.match(/^\d+[.)]\s+(.*)$/))){ if(list !== "ol"){ if(list) out.push("</" + list + ">"); out.push("<ol>"); list = "ol"; } out.push("<li>" + inl(m[1]) + "</li>"); return; }
      if(list){ out.push("</" + list + ">"); list = null; }
      if(l) out.push("<p>" + inl(l) + "</p>");
    });
    if(list) out.push("</" + list + ">");
    return out.join("");
  }
  function plain(text){ return String(text || "").replace(/\*\*/g, "").replace(/^[-•*]\s+/gm, "").replace(/^(\d+)[.)]\s+/gm, "$1. ").replace(/[#>`_]/g, ""); }

  var LOG = [];
  function addMsg(kind, html, time){ LOG.push({kind: kind, html: html, time: time || clock()}); renderLog(); }
  function renderLog(){
    var g = $("haiLog"); if(!g) return;
    if(!LOG.length) LOG.push({kind: "a", html: md(t("hello")), time: clock(), hello: true});
    g.innerHTML = LOG.map(function(m){ return '<div class="hai-m ' + m.kind + '">' + m.html + (m.kind === "sys" ? "" : "<time>" + esc(m.time) + "</time>") + "</div>"; }).join("");
    g.scrollTop = g.scrollHeight;
  }

  /* ---------------- what the page shows (sent with each question) ---------------- */
  function pageData(){ var s = window.SENTRY2026 && window.SENTRY2026.state; return s && s.data ? s : null; }
  function snapshot(){
    var s = pageData(); if(!s) return {page: "AI Operations Centre", note: "Forecast Centre data not loaded yet"};
    var d = s.data, pick = function(o, keys){ var r = {}; keys.forEach(function(k){ if(o && o[k] !== undefined && o[k] !== null && o[k] !== "") r[k] = o[k]; }); return r; };
    var alerts = (d.alerts || []).slice().sort(function(a, b){ return (b.score || 0) - (a.score || 0); }).slice(0, 30)
      .map(function(a){ return pick(a, ["category", "svc", "dir", "location", "impact", "onset", "cause", "confidence", "status", "delay", "evidence"]); });
    var sev = function(x){ return (x.delay || 0) * 2 + (x.gaps || 0) * 3 + (x.three || 0) * 3 + (x.two || 0) * 2 + (x.heavy_buses || 0); };
    var services = (d.services || []).slice().sort(function(a, b){ return sev(b) - sev(a); }).slice(0, 40)
      .map(function(x){ return pick(x, ["svc", "dir", "operator", "location", "delay", "occupancy", "heavy_buses", "three", "two", "gaps", "headway", "scheduled", "load_counts"]); });
    var recs = (d.recommendations || []).slice(0, 12).map(function(r){ var o = pick(r, ["svc", "action", "reason", "explanation", "approval", "constraints", "improvement"]); o.options = (r.options || []).map(function(x){ return x.name; }); return o; });
    return {page: "AI Operations Centre", mode: d.mode, updated: d.updated ? new Date(d.updated * 1000).toISOString() : null, weather: d.weather && d.weather.text,
      kpis: d.kpis, rules: d.rules, delay_filter: d.delay_filter, selected: s.services || [], category: s.category, horizon: d.horizon,
      sources: (d.sources || []).map(function(x){ return {name: x.name, status: x.status, detail: String(x.detail || "").slice(0, 160)}; }),
      alerts: alerts, recommendations: recs, services: services};
  }
  function suggestions(){
    var g = $("haiSugg"); if(!g) return;
    var s = pageData(), q = [], c = t("cats");
    if(s){
      (s.data.alerts || []).slice().sort(function(a, b){ return (b.score || 0) - (a.score || 0); }).slice(0, 2).forEach(function(a){
        if(a.svc && c[a.category]) q.push(t("why").replace("{svc}", a.svc).replace("{cat}", c[a.category]).replace("{loc}", a.location || "?"));
      });
    } else if(window.HAI_DEMO){ q.push(t("why").replace("{svc}", "156").replace("{cat}", c.traffic).replace("{loc}", "Clementi Ave 2")); }
    q.push(t("now"), t("sum"), t("cp"));
    g.innerHTML = q.map(function(x){ return '<button type="button" class="hai-chip" title="' + esc(x) + '">' + esc(x) + "</button>"; }).join("");
  }

  /* ---------------- asking ---------------- */
  var busy = false;
  function ask(q){
    q = String(q || "").trim(); if(!q || busy) return;
    stopSpeaking();
    busy = true; $("haiQ").value = "";
    addMsg("u", "<p>" + esc(q) + "</p>");
    var hist = S.history.slice(-6);
    LOG.push({kind: "a", html: '<span class="hai-typing" aria-label="' + esc(t("think")) + '"><i></i><i></i><i></i></span> ' + esc(t("think")), time: clock(), wait: true}); renderLog();
    var done = function(ok, text, meta){
      LOG = LOG.filter(function(m){ return !m.wait; });
      busy = false;
      if(!ok){ addMsg("e", "<p><b>" + esc(t("err")) + ":</b> " + esc(text) + "</p>"); return; }
      S.history.push({role: "user", text: q}, {role: "assistant", text: text}); S.history = S.history.slice(-12);
      S.lastAnswer = text;
      addMsg("a", md(text) + (meta ? '<p style="opacity:.65;font-size:11px">' + esc(meta) + "</p>" : ""));
      speak(text);
    };
    if(window.HAI_DEMO){ setTimeout(function(){ done(true, DEMO[S.lang], "DEMO answer (simulated data)"); }, 900); return; }
    fetch("/api/assistant/ask", {method: "POST", headers: {"content-type": "application/json"}, credentials: "same-origin",
      body: JSON.stringify({q: q, lang: S.lang, history: hist, ctx: snapshot()})})
      .then(function(r){ return r.json().catch(function(){ return {ok: false, error: "HTTP " + r.status}; }); })
      .then(function(j){
        if(j && j.configured === false){ S.configured = false; status(); }
        if(j && j.ok) done(true, j.answer, (j.mode === "demo" ? "Simulated data · " : "Live data · ") + (j.model || "") + (j.ms ? " · " + (j.ms / 1000).toFixed(1) + " s" : ""));
        else done(false, (j && j.error) || "unknown error");
      })
      .catch(function(e){ done(false, "network (" + (e && e.message || e) + ")"); });
  }
  var DEMO = {
    en: "Service 156 Dir 1 is about 10 min late near Clementi Ave 2 because of a jam on the same road, in the same direction (simulated data).\nWhy:\n- The jam overlaps the route in the direction of travel, so every 156 bus there is slowed.\n- The gap behind the delayed bus is growing, so the next stops will see a long wait.\nWhat to do:\n1. Check the Clementi Ave 2 camera or ask the bus captain to confirm the jam.\n2. If it lasts, hold the following 156 for 2 to 3 min at the interchange to keep the spacing (Service Controller approval).\n3. Open the Recovery Decision Engine if the delay passes 15 min.\nExpected recovery: about 15 min after the jam clears.",
    zh: "156 号（方向 1）在 Clementi Ave 2 附近延误约 10 分钟，原因是同一道路、同一方向发生交通堵塞（模拟数据）。\n原因：\n- 堵塞路段与路线方向重叠，所有经过的 156 号都会被拖慢。\n- 延误巴士后方的间隔正在拉大，后面的车站会等更久。\n处理方法：\n1. 查看 Clementi Ave 2 的交通摄像头，或请车长确认堵塞情况。\n2. 如果持续，在转换站让下一辆 156 号多停 2 至 3 分钟，保持间隔（需服务控制员批准）。\n3. 若延误超过 15 分钟，打开恢复决策引擎。\n预计恢复：堵塞解除后约 15 分钟。",
    ms: "Perkhidmatan 156 Arah 1 lewat kira-kira 10 minit berhampiran Clementi Ave 2 kerana kesesakan di jalan yang sama, arah yang sama (data simulasi).\nSebab:\n- Kesesakan bertindih dengan laluan dalam arah perjalanan, jadi setiap bas 156 di situ diperlahankan.\n- Jurang di belakang bas yang lewat semakin besar, jadi hentian seterusnya akan menunggu lebih lama.\nApa perlu dibuat:\n1. Semak kamera Clementi Ave 2 atau minta kapten bas mengesahkan kesesakan.\n2. Jika berterusan, tahan bas 156 berikutnya 2 hingga 3 minit di pertukaran untuk menjaga jarak (kelulusan Pengawal Perkhidmatan).\n3. Buka Enjin Keputusan Pemulihan jika kelewatan melebihi 15 minit.\nJangkaan pemulihan: kira-kira 15 minit selepas kesesakan reda."
  };

  /* ---------------- voice out ---------------- */
  var synth = window.speechSynthesis || null, queue = [], anim = 0;
  function pickVoice(){
    if(!synth) return null;
    var vs = synth.getVoices() || [], want = {en: ["en-SG", "en-GB", "en-AU", "en-US", "en"], zh: ["zh-CN", "zh-SG", "cmn", "zh-TW", "zh"], ms: ["ms-MY", "ms"]}[S.lang];
    var find = function(p){ return vs.filter(function(v){ return (v.lang || "").replace("_", "-").toLowerCase().indexOf(p.toLowerCase()) === 0; }); };
    for(var i = 0; i < want.length; i++){ var hit = find(want[i]); if(hit.length){ S.voiceNote = ""; return hit.find(function(v){ return /natural|neural|google|online/i.test(v.name); }) || hit[0]; } }
    if(S.lang === "ms"){ var id = find("id"); if(id.length){ S.voiceNote = t("idVoice"); return id[0]; } }
    S.voiceNote = vs.length ? t("noVoice") : "";
    return null;
  }
  function speak(text){
    if(!synth || S.mute) return;
    stopSpeaking();
    var v = pickVoice(); if($("haiNote") && S.face !== "photo") $("haiNote").textContent = S.voiceNote || "";
    if(!v && synth.getVoices().length) return;
    // Chrome stops long utterances after ~15 s, so speak sentence by sentence
    queue = plain(text).split(/(?<=[.!?。！？])\s+|\n+/).map(function(x){ return x.trim(); }).filter(Boolean);
    next();
  }
  function next(){
    if(!queue.length){ setSpeaking(false); return; }
    var u = new SpeechSynthesisUtterance(queue.shift()), v = pickVoice();
    if(v){ u.voice = v; u.lang = v.lang; } else u.lang = STT[S.lang];
    u.rate = S.lang === "en" ? 1.02 : 1; u.pitch = 1.05;
    u.onstart = function(){ setSpeaking(true); };
    u.onboundary = function(){ S.kick = 1; };
    u.onend = function(){ next(); };
    u.onerror = function(){ next(); };
    synth.speak(u);
  }
  function stopSpeaking(){ queue = []; if(synth){ try{ synth.cancel(); }catch(e){} } setSpeaking(false); }
  function setSpeaking(on){
    S.speaking = on; say(on);
    if(on && !anim) anim = requestAnimationFrame(tick);
  }
  function tick(ts){
    var target = S.speaking ? Math.max(S.kick || 0, .25 + .55 * Math.abs(Math.sin(ts / 95)) * (.6 + .4 * Math.random())) : 0;
    S.kick = (S.kick || 0) * .82;
    S.level += (target - S.level) * .45;
    render(S.level);
    if(S.speaking || S.level > .01) anim = requestAnimationFrame(tick); else { anim = 0; render(0); }
  }
  function render(lv){
    var f = $("haiFace"); if(f) f.style.setProperty("--lv", lv.toFixed(3));
    var o = $("haiOpen"); if(o){ o.setAttribute("ry", (0.1 + lv * 6.2).toFixed(2)); }
    var lu = $("haiLipU"), ll = $("haiLipL");
    if(lu && ll){ lu.setAttribute("d", "M91 " + (118 - lv * 2.5).toFixed(2) + "q9 " + (-3 - lv * 2).toFixed(2) + " 18 0"); ll.setAttribute("d", "M91 " + (118 + lv * 2.5).toFixed(2) + "q9 " + (4 + lv * 5).toFixed(2) + " 18 0"); }
    var c = $("haiCoreC"), w = $("haiWave"); if(c){ c.setAttribute("r", (34 + lv * 10).toFixed(1)); } if(w){ w.setAttribute("r", (44 + lv * 26).toFixed(1)); w.setAttribute("opacity", (lv * .8).toFixed(2)); }
    var bars = document.querySelectorAll("#haiSay .hai-bars i");
    for(var i = 0; i < bars.length; i++) bars[i].style.height = (3 + lv * (6 + 5 * Math.abs(Math.sin(i * 1.7 + performance.now() / 120)))).toFixed(1) + "px";
  }
  if(synth && synth.onvoiceschanged !== undefined) synth.onvoiceschanged = function(){ pickVoice(); };

  /* ---------------- voice in ---------------- */
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition || null, rec = null;
  function listen(){
    if(!SR) return;
    if(S.listening && rec){ try{ rec.stop(); }catch(e){} return; }
    stopSpeaking();
    rec = new SR(); rec.lang = STT[S.lang]; rec.interimResults = true; rec.maxAlternatives = 1;
    var final = "";
    rec.onresult = function(e){ var txt = ""; for(var i = e.resultIndex; i < e.results.length; i++){ txt += e.results[i][0].transcript; if(e.results[i].isFinal) final += e.results[i][0].transcript; } $("haiQ").value = final || txt; };
    rec.onstart = function(){ S.listening = true; $("haiMic").classList.add("on"); say(false); };
    rec.onend = function(){ S.listening = false; var m = $("haiMic"); if(m) m.classList.remove("on"); say(false); if(final.trim()) ask(final); };
    rec.onerror = function(){ S.listening = false; };
    try{ rec.start(); }catch(e){ S.listening = false; }
  }

  /* ---------------- photo: shrink to 360 px and keep in this browser ---------------- */
  function setPhoto(file){
    if(!file || !/^image\//.test(file.type)) return;
    var rd = new FileReader();
    rd.onload = function(){
      var img = new Image();
      img.onload = function(){
        var c = document.createElement("canvas"), n = 360, s = Math.min(img.width, img.height);
        c.width = c.height = n;
        c.getContext("2d").drawImage(img, (img.width - s) / 2, (img.height - s) / 2, s, s, 0, 0, n, n);
        store("photo", c.toDataURL("image/jpeg", .86)); drawFace(); words();
      };
      img.src = rd.result;
    };
    rd.readAsDataURL(file);
  }

  /* ---------------- events, dragging, size ---------------- */
  function wire(){
    $("haiForm").addEventListener("submit", function(e){ e.preventDefault(); ask($("haiQ").value); });
    $("haiMic").addEventListener("click", listen);
    $("haiSugg").addEventListener("click", function(e){ var b = e.target.closest(".hai-chip"); if(b) ask(b.textContent); });
    $("haiLang").addEventListener("change", function(){ S.lang = this.value; store("lang", S.lang); stopSpeaking(); LOG = LOG.filter(function(m){ return !m.hello; }); words(); renderLog(); suggestions(); pickVoice(); if($("haiNote") && S.face !== "photo") $("haiNote").textContent = S.voiceNote || ""; status(); });
    $("haiFaces").addEventListener("click", function(e){ var b = e.target.closest("[data-face]"); if(!b) return; S.face = b.dataset.face; store("face", S.face); drawFace(); words(); });
    $("haiFaces").addEventListener("change", function(e){ if(e.target.id === "haiFile") setPhoto(e.target.files && e.target.files[0]); });
    $("haiReplay").addEventListener("click", function(){ if(S.lastAnswer){ var m = S.mute; S.mute = false; speak(S.lastAnswer); S.mute = m; } });
    $("haiMute").addEventListener("click", function(){ S.mute = !S.mute; store("mute", S.mute ? "1" : "0"); if(S.mute) stopSpeaking(); words(); });
    $("haiStop").addEventListener("click", stopSpeaking);
    $("haiMin").addEventListener("click", function(){ S.open = false; store("open", "0"); stopSpeaking(); words(); fab.focus(); });
    $("haiBig").addEventListener("click", function(){ S.big = !S.big; store("big", S.big ? "1" : "0"); el.classList.toggle("big", S.big); words(); place(); });
    var h = $("haiDrag"), sx, sy, ox, oy, drag = false;
    h.addEventListener("pointerdown", function(e){ if(e.target.closest("select,button") || innerWidth < 640) return; drag = true; h.setPointerCapture(e.pointerId); var r = el.getBoundingClientRect(); sx = e.clientX; sy = e.clientY; ox = r.left; oy = r.top; });
    h.addEventListener("pointermove", function(e){ if(!drag) return; el.style.left = Math.max(4, Math.min(innerWidth - el.offsetWidth - 4, ox + e.clientX - sx)) + "px"; el.style.top = Math.max(4, Math.min(innerHeight - 60, oy + e.clientY - sy)) + "px"; el.style.right = "auto"; });
    h.addEventListener("pointerup", function(){ if(!drag) return; drag = false; store("pos", JSON.stringify({l: parseInt(el.style.left, 10), t: parseInt(el.style.top, 10)})); });
  }
  function place(){
    var p = null; try{ p = JSON.parse(store("pos") || "null"); }catch(e){}
    if(p && innerWidth >= 640 && p.l >= 0 && p.l < innerWidth - 120 && p.t >= 0 && p.t < innerHeight - 80){ el.style.left = Math.min(p.l, innerWidth - el.offsetWidth - 4) + "px"; el.style.top = p.t + "px"; el.style.right = "auto"; }
  }
  fab.addEventListener("click", function(){ S.open = true; store("open", "1"); words(); var q = $("haiQ"); if(q) q.focus(); });
  addEventListener("keydown", function(e){ if(e.key === "Escape" && S.speaking) stopSpeaking(); });

  frame(); place();
  if(!window.HAI_DEMO) fetch("/api/assistant/status", {credentials: "same-origin"}).then(function(r){ return r.json(); }).then(function(j){ S.configured = !!j.configured; status(); if(!j.configured) addMsg("sys", esc(t("offMsg"))); }).catch(function(){});
  var sugWait = setInterval(function(){ if(pageData()){ suggestions(); clearInterval(sugWait); } }, 1500);   // first data from the Forecast Centre
  setTimeout(function(){ clearInterval(sugWait); }, 60000);
  setInterval(suggestions, 30000);
  window.HORIZON_AI = {ask: ask, speak: speak, stop: stopSpeaking, state: S, snapshot: snapshot, setFace: function(f){ S.face = f; store("face", f); drawFace(); words(); }, setLang: function(l){ $("haiLang").value = l; $("haiLang").dispatchEvent(new Event("change")); },
    faceSVG: function(f){ return f === "orb" ? orbSVG() : agentSVG(); }, setPhoto: setPhoto,
    open: function(on){ S.open = on !== false; store("open", S.open ? "1" : "0"); words(); }};
})();
