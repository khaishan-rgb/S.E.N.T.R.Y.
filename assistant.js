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
         faces: {agent: "Officer", orb: "Orb", photo: "Photo"}, replay: "Replay", mute: "Mute voice", unmute: "Voice on", stop: "Stop",
         min: "Minimise", big: "Bigger", small: "Smaller", you: "You", hello: "Hello, I'm HORIZON AI. Ask me about any alert, service or incident on this page and I'll explain why it happened and what to do.",
         foot: "Advisory only · answers from this page and live service checks · the Service Controller decides", choose: "Choose photo", photoNote: "Use a picture you have permission to use. It stays in this browser only.",
         noPhoto: "No photo yet", fab: "Ask HORIZON AI", noVoice: "No voice for this language on this computer: showing text only.", idVoice: "No Malay voice on this computer: using an Indonesian voice.",
         cats: {loading: "heavily loaded", traffic: "delayed by traffic", bunching: "bunching", gap: "running with a long gap"},
         why: "Why is {svc} {cat} at {loc}?", now: "Which services need action now?", sum: "Summarise the network in 3 lines.", cp: "Are the checkpoints jammed?",
         err: "Could not get an answer", offMsg: "HORIZON AI is not set up yet: add ANTHROPIC_API_KEY in Render > Environment."},
    zh: {sub: "运营助理", on: "在线", off: "未设置", ph: "询问警报、巴士服务或事故", send: "发送", mic: "说出你的问题",
         micNo: "语音输入请使用 Chrome 或 Edge", listen: "正在聆听…", speaking: "正在说话", idle: "就绪", think: "正在查看实时数据…",
         faces: {agent: "职员", orb: "光环", photo: "照片"}, replay: "重播", mute: "关闭语音", unmute: "开启语音", stop: "停止",
         min: "最小化", big: "放大", small: "缩小", you: "你", hello: "您好，我是 HORIZON AI。请问我本页上的任何警报、巴士服务或事故，我会解释原因和处理方法。",
         foot: "仅供参考 · 根据本页数据和实时服务检查回答 · 由服务控制员决定", choose: "选择照片", photoNote: "请使用您有权使用的照片。照片只保存在此浏览器中。",
         noPhoto: "尚未选择照片", fab: "询问 HORIZON AI", noVoice: "此电脑没有中文语音：只显示文字。", idVoice: "",
         cats: {loading: "载客量高", traffic: "因交通延误", bunching: "串车（车距过近）", gap: "班距过大"},
         why: "{svc} 号为什么在 {loc} {cat}？", now: "现在哪些巴士服务需要处理？", sum: "用三句话总结目前的网络状况。", cp: "关卡现在拥堵吗？",
         err: "无法取得回答", offMsg: "HORIZON AI 尚未设置：请在 Render > Environment 加入 ANTHROPIC_API_KEY。"},
    ms: {sub: "Pembantu Operasi", on: "dalam talian", off: "belum disediakan", ph: "Tanya tentang amaran, perkhidmatan atau insiden", send: "Hantar", mic: "Sebut soalan anda",
         micNo: "Input suara berfungsi dalam Chrome atau Edge", listen: "Sedang mendengar…", speaking: "Sedang bercakap", idle: "Sedia", think: "Sedang menyemak data langsung…",
         faces: {agent: "Pegawai", orb: "Orb", photo: "Foto"}, replay: "Main semula", mute: "Senyapkan suara", unmute: "Suara dihidupkan", stop: "Henti",
         min: "Kecilkan", big: "Besarkan", small: "Kecilkan saiz", you: "Anda", hello: "Helo, saya HORIZON AI. Tanya saya tentang sebarang amaran, perkhidmatan atau insiden di halaman ini, dan saya akan terangkan sebabnya dan apa yang perlu dibuat.",
         foot: "Nasihat sahaja · jawapan daripada halaman ini dan semakan perkhidmatan langsung · Pengawal Perkhidmatan yang memutuskan", choose: "Pilih foto", photoNote: "Gunakan gambar yang anda dibenarkan guna. Ia disimpan dalam pelayar ini sahaja.",
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
  /* Officer: an original drawn young Chinese man in a white shirt with purple collar and shoulders and an orange
     stripe (the SBS Transit uniform colours). He breathes, blinks, glances, nods and lifts his brows while he talks,
     and his mouth takes the shape of the letters being spoken. k = id suffix for extra copies; mini = face only. */
  function mouthD(o, w, r){        // o = open, w = wide, r = round (0..1) -> mouth, teeth, tongue and lip paths
    var cx = 150, cy = 191.5, hw = 12.6 * (1 + .18 * w - .34 * r), h = .4 + 11 * o, s = 2.4 * (1 - o) * (1 - r) + .5;
    var f = function(v){ return v.toFixed(2); }, L = f(cx - hw) + " " + f(cy - s), R = f(cx + hw) + " " + f(cy - s);
    var top = cy - h * .3, bot = cy + h * .7, th = Math.min(2.8, h * .4);
    return {
      cav: "M" + L + "Q" + cx + " " + f(top - .8) + " " + R + "Q" + cx + " " + f(bot + 1) + " " + L + "Z",
      teeth: "M" + f(cx - hw * .8) + " " + f(top - 1) + "H" + f(cx + hw * .8) + "V" + f(top + th) + "Q" + cx + " " + f(top + th + .8) + " " + f(cx - hw * .8) + " " + f(top + th) + "Z",
      tg: "M" + f(cx - hw * .55) + " " + f(bot + 1) + "Q" + cx + " " + f(bot - h * .45) + " " + f(cx + hw * .55) + " " + f(bot + 1) + "Z",
      up: "M" + L + "C" + f(cx - hw * .55) + " " + f(top - 2.6) + " " + f(cx - 2.4) + " " + f(top - 2.5) + " " + cx + " " + f(top - 1.7) + "C" + f(cx + 2.4) + " " + f(top - 2.5) + " " + f(cx + hw * .55) + " " + f(top - 2.6) + " " + R
        + "Q" + cx + " " + f(top + .2) + " " + L + "Z",
      lo: "M" + L + "Q" + cx + " " + f(bot + .9) + " " + R + "C" + f(cx + hw * .5) + " " + f(bot + 4.4) + " " + f(cx - hw * .5) + " " + f(bot + 4.4) + " " + L + "Z"
    };
  }
  function agentSVG(k, mini){
    k = k || "";
    var id = function(n){ return ' id="hai' + n + k + '"'; }, u = function(n){ return "url(#hai" + n + k + ")"; }, m = mouthD(0, 0, 0);
    var mir = function(d){ return d.replace(/(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)/g, function(_, x, y){ return (300 - +x).toFixed(1).replace(/\.0$/, "") + " " + y; }); };
    var LINE = "#b87c60", HAIR = "#191c24";
    var eye = function(side){              // side -1 = his right (viewer left), +1 = his left (viewer right)
      var lids = '<path d="M115.4 143.4C119.4 136 132.8 134.8 140.6 141" fill="none" stroke="#1a1310" stroke-width="2.3" stroke-linecap="round"/>'
        + '<path d="M118.6 145.4C124 147.6 133 147.2 137.8 144.2" fill="none" stroke="#cf9a7e" stroke-width=".9" opacity=".7"/>'
        + '<path d="M120.6 136.4C126.6 134 135 134.4 139.8 137.8" fill="none" stroke="#c98f73" stroke-width=".8" opacity=".4"/>';
      var white = '<path d="M116.4 143C120 137 132.4 136 139.8 141.4C135.2 145.6 122.4 146.6 116.4 143Z" fill="#fbf8f4"/>';
      var ix = side < 0 ? 128.6 : 171.4;     // light comes from the upper left on both eyes
      var iris = '<g clip-path="' + u(side < 0 ? "EyeL" : "EyeR") + '"><g class="iris"><circle cx="' + ix + '" cy="141.4" r="5.2" fill="' + u("Iris") + '"/><circle cx="' + ix + '" cy="141.4" r="2.5" fill="#0b0807"/>'
        + '<circle cx="' + (ix + 1.9).toFixed(1) + '" cy="139.3" r="1.5" fill="#fff"/><circle cx="' + (ix - 1.8).toFixed(1) + '" cy="143.4" r=".7" fill="#fff" opacity=".7"/></g></g>';
      return side < 0 ? white + iris + lids : mir(white) + iris + mir(lids);
    };
    var ear = '<path d="M102 132C93 126 86.6 134 88.4 146C90 157 96 165 104.4 163Z" fill="#e9b493"/><path d="M99.6 137C94.6 136 92.4 142 94 149C95 154 98 157 101 157" fill="none" stroke="' + LINE + '" stroke-width="1.2" stroke-linecap="round" opacity=".6"/>';
    var shirtL = '<path d="M131 242C116 250 92 256 70 262C44 270 28 290 22 318L16 372C24 374 30 374 36 372C44 320 80 282 136 258Z" fill="' + u("Purple") + '"/>';
    var collarL = '<path d="M128.6 234C121 243 112.6 249.6 105.6 253.6L130.6 283L146.6 260.4C138.6 258 131.6 249 128.6 234Z" fill="' + u("Purple") + '"/>';
    return '<svg class="hai-agent' + (mini ? " mini" : "") + '" viewBox="' + (mini ? "93 64 114 114" : "15 14 270 360") + '" preserveAspectRatio="xMidYMid slice" aria-hidden="true"><defs>'
      + '<radialGradient' + id("Bg") + ' cx="50%" cy="34%" r="78%"><stop offset="0" stop-color="#2d4390"/><stop offset=".45" stop-color="#132457"/><stop offset="1" stop-color="#050c20"/></radialGradient>'
      + '<radialGradient' + id("Glow") + ' cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#63dcff" stop-opacity=".30"/><stop offset="1" stop-color="#63dcff" stop-opacity="0"/></radialGradient>'
      + '<radialGradient' + id("Skin") + ' cx="42%" cy="36%" r="72%"><stop offset="0" stop-color="#fde3cd"/><stop offset=".55" stop-color="#f5cfb2"/><stop offset="1" stop-color="#e3aa89"/></radialGradient>'
      + '<linearGradient' + id("Neck") + ' x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#d79a7a"/><stop offset=".6" stop-color="#ebb998"/><stop offset="1" stop-color="#f0c3a4"/></linearGradient>'
      + '<radialGradient' + id("Iris") + ' cx="50%" cy="40%" r="60%"><stop offset="0" stop-color="#5b3a24"/><stop offset="1" stop-color="#21150d"/></radialGradient>'
      + '<linearGradient' + id("Hair") + ' x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2c3242"/><stop offset=".5" stop-color="' + HAIR + '"/><stop offset="1" stop-color="#0e1016"/></linearGradient>'
      + '<linearGradient' + id("Shirt") + ' x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#c3c7d4"/><stop offset=".28" stop-color="#eff1f6"/><stop offset=".65" stop-color="#f9fafc"/><stop offset="1" stop-color="#bfc3d1"/></linearGradient>'
      + '<linearGradient' + id("Purple") + ' x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5e2c78"/><stop offset="1" stop-color="#3b1852"/></linearGradient>'
      + '<clipPath' + id("EyeL") + '><path d="M116.4 143C120 137 132.4 136 139.8 141.4C135.2 145.6 122.4 146.6 116.4 143Z"/></clipPath>'
      + '<clipPath' + id("EyeR") + '><path d="' + mir("M116.4 143C120 137 132.4 136 139.8 141.4C135.2 145.6 122.4 146.6 116.4 143Z") + '"/></clipPath>'
      + '<clipPath' + id("FaceClip") + '><path d="M100 136C100 96 122 74 150 74C178 74 200 96 200 136C200 166 195 186 185 200C174 215 162 222 150 222C138 222 126 215 115 200C105 186 100 166 100 136Z"/></clipPath>'
      + '<radialGradient' + id("Blush") + ' cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#f19a88" stop-opacity=".32"/><stop offset="1" stop-color="#f19a88" stop-opacity="0"/></radialGradient>'
      + '<clipPath' + id("Cav") + '><path' + id("CavClip") + ' d="' + m.cav + '"/></clipPath>'
      + "</defs>"
      + '<rect width="300" height="400" fill="' + u("Bg") + '"/>'
      + '<circle cx="150" cy="150" r="128" fill="' + u("Glow") + '"/>'
      + '<g class="breathe">'
      // shirt
      + '<g transform="translate(0 -7)">'
      + '<path d="M131 242C116 250 92 256 70 262C44 270 28 290 22 318L14 400H286L278 318C272 290 256 270 230 262C208 256 184 250 169 242C162 252 138 252 131 242Z" fill="' + u("Shirt") + '"/>'
      + '<path d="M66 330C70 356 70 380 68 400M234 330C230 356 230 380 232 400" fill="none" stroke="#aeb3c3" stroke-width="1.2" opacity=".7"/>'
      + shirtL + mir(shirtL)
      + '<path d="M136 258C80 282 44 320 36 372M164 258C220 282 256 320 264 372" fill="none" stroke="#f28a1e" stroke-width="4" stroke-linecap="round"/>'
      + '<path d="M74 266C94 260 114 254 128 246M226 266C206 260 186 254 172 246" fill="none" stroke="#83519e" stroke-width="1.4" opacity=".5"/>'
      + "</g>"
      // neck
      + '<path d="M124 192V240C134 250 166 250 176 240V192Z" fill="' + u("Neck") + '"/>'
      + '<path d="M124 206C136 226 164 226 176 206V196H124Z" fill="#b97558" opacity=".35"/>'
      + '<path d="M134 242L150 255L166 242" fill="#f3f4f8"/>'
      + '<g transform="translate(0 -7)">'
      + collarL + mir(collarL)
      + '<path d="M130.6 283L146.6 260.4M169.4 283L153.4 260.4" fill="none" stroke="#25102f" stroke-width="1" opacity=".5"/>'
      + '<path d="M144.6 260.4H155.4V318H144.6Z" fill="#e7e9f0" stroke="#c6cad7" stroke-width="1"/>'
      + '<circle cx="150" cy="275" r="1.9" fill="#9da3b6"/><circle cx="150" cy="296" r="1.9" fill="#9da3b6"/>'
      + '<rect x="176" y="300" width="40" height="13" rx="2.6" fill="#0c1a36"/><rect x="179" y="303.4" width="6" height="6" rx="1.3" fill="#3fd3ff"/>'
      + '<text x="188.4" y="309.6" font-family="Rajdhani,Arial,sans-serif" font-size="7.4" font-weight="700" fill="#e8f4ff" textLength="24.6" lengthAdjust="spacingAndGlyphs">HORIZON</text>'
      + "</g>"
      // head
      + '<g class="head">'
      + ear + mir(ear)
      + '<path d="M100 136C100 96 122 74 150 74C178 74 200 96 200 136C200 166 195 186 185 200C174 215 162 222 150 222C138 222 126 215 115 200C105 186 100 166 100 136Z" fill="' + u("Skin") + '"/>'
      + '<g clip-path="' + u("FaceClip") + '">'
      + '<path d="M100 168C104 192 120 214 146 224L100 224ZM200 168C196 192 180 214 154 224L200 224Z" fill="#d69474" opacity=".28"/>'
      + '<path d="M101.4 140C97 120 97 100 103 86C106 78 110 72 116 67C118 62 124 58 130 57C136 52 146 51 154 53C162 51 172 53 178 57C186 59 192 64 196 70C202 78 205 90 205 104C205 114 203 122 200.4 128L199 116.4C192.6 108.4 181 104.4 169 104.8C158 105.4 147 110.6 140.6 118.4C138.6 115.4 137.8 112.6 137.8 110C127.6 113.2 120.6 118.6 117.6 124.4C116.6 120.6 116.6 117.6 117.4 114.6C110.4 119.4 104.4 128 101.4 140Z" transform="translate(1.5 4.5)" fill="#b67256" opacity=".34"/>'
      + '<ellipse cx="121" cy="168" rx="13" ry="7.5" fill="' + u("Blush") + '"/><ellipse cx="179" cy="168" rx="13" ry="7.5" fill="' + u("Blush") + '"/>'
      + "</g>"
      // hair: short textured cut, fringe swept across the forehead
      + '<path d="M101.4 140C97 120 97 100 103 86C106 78 110 72 116 67C118 62 124 58 130 57C136 52 146 51 154 53C162 51 172 53 178 57C186 59 192 64 196 70C202 78 205 90 205 104C205 114 203 122 200.4 128L199 116.4C192.6 108.4 181 104.4 169 104.8C158 105.4 147 110.6 140.6 118.4C138.6 115.4 137.8 112.6 137.8 110C127.6 113.2 120.6 118.6 117.6 124.4C116.6 120.6 116.6 117.6 117.4 114.6C110.4 119.4 104.4 128 101.4 140Z" fill="' + u("Hair") + '"/>'
      + '<path d="M197 104C188 94 172 90 156 92M188 98C174 90 156 90 140 96M172 100C154 100 136 106 124 116M154 104C140 108 128 114 120 122M190 72C176 62 154 60 134 66M198 88C184 76 162 72 142 76M112 92C120 80 132 74 146 72" fill="none" stroke="#56627e" stroke-width="1.5" stroke-linecap="round" opacity=".5"/>'
      + '<path d="M134 62C150 56 168 57 182 64" fill="none" stroke="#6f7c9c" stroke-width="3" stroke-linecap="round" opacity=".34"/>'
      // brows
      + '<g class="brows"' + id("Brows") + '><path d="M112.4 128.6C119 123.6 130.6 122 141.4 124.8C142.6 125.2 142.6 128.8 141 129.4C131 127.6 121.4 127.6 113.8 130.2C112.8 130.4 112 129.4 112.4 128.6Z" fill="#241d1b"/>'
      + '<path d="' + mir("M112.4 128.6C119 123.6 130.6 122 141.4 124.8C142.6 125.2 142.6 128.8 141 129.4C131 127.6 121.4 127.6 113.8 130.2C112.8 130.4 112 129.4 112.4 128.6Z") + '" fill="#241d1b"/></g>'
      // eyes
      + '<g class="blink">' + eye(-1) + eye(1) + "</g>"
      // nose
      + '<path d="M146.4 148C145.6 156 144.6 162 143.2 166" fill="none" stroke="#dba487" stroke-width="1.4" stroke-linecap="round" opacity=".55"/>'
      + '<path d="M141.6 170.2C144.4 174.6 155.6 174.6 158.4 170.2" fill="none" stroke="#c4846a" stroke-width="1.9" stroke-linecap="round"/>'
      + '<ellipse cx="145.8" cy="171" rx="2.1" ry="1.1" fill="#a86a52" opacity=".45"/><ellipse cx="154.2" cy="171" rx="2.1" ry="1.1" fill="#a86a52" opacity=".45"/>'
      + '<ellipse cx="150.6" cy="165" rx="2.6" ry="2.2" fill="#fff" opacity=".28"/>'
      // cheeks
      // mouth
      + '<g' + id("Mouth") + ' data-open="0"><path' + id("Open") + ' d="' + m.cav + '" fill="#62262c"/>'
      + '<path' + id("Tongue") + ' d="' + m.tg + '" fill="#c4646a" clip-path="' + u("Cav") + '"/>'
      + '<path' + id("Teeth") + ' d="' + m.teeth + '" fill="#f7f3ee" clip-path="' + u("Cav") + '"/>'
      + '<path' + id("LipU") + ' d="' + m.up + '" fill="#bf7564"/><path' + id("LipL") + ' d="' + m.lo + '" fill="#d6907c"/></g>'
      + "</g>"
      + "</g></svg>";
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
  function faceHTML(mini){ return S.face === "orb" ? orbSVG() : S.face === "photo" ? photoHTML() : mini ? agentSVG("Fab", true) : agentSVG(); }

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
    fab.innerHTML = '<span class="mini">' + faceHTML(true) + "</span>" + esc(t("fab"));
    fab.hidden = S.open; el.hidden = !S.open;
  }
  function drawFace(){ var f = $("haiFace"); f.innerHTML = faceHTML(); f.classList.toggle("tall", S.face === "agent"); talk(S.speaking); }
  function talk(on){ var a = document.querySelector("#haiFace .hai-agent"); if(a) a.classList.toggle("talk", !!on); }
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
      if(!l) return;
      if(!out.length && l.length < 90 && !/[:：]\s*\S/.test(l)) out.push('<p class="hai-head">' + inl(l) + "</p>");                 // "Service 147 - ..." status line
      else if(/^(data confidence|数据可信度|数据置信度|keyakinan data)\s*[:：]/i.test(l)) out.push('<p class="hai-conf">' + inl(l) + "</p>");
      else if((m = l.match(/^([^:：]{2,24})([:：])\s*(.*)$/)) && /^(assessment|key findings|action|评估|主要发现|行动|penilaian|penemuan utama|tindakan)$/i.test(m[1].trim()))
        out.push('<p><span class="hai-lbl">' + esc(m[1].trim()) + m[2] + "</span> " + inl(m[3]) + "</p>");
      else out.push("<p>" + inl(l) + "</p>");
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
  // the service numbers in a question, exact (147 is not 147A); the server checks them against the LTA list
  function named(q){
    var out = [], re = /(^|[^A-Za-z0-9.:\/])([A-Za-z]{0,2}\d{1,3}[A-Za-z]{0,2})(?![A-Za-z0-9.:\/%])/g, m;
    while((m = re.exec(String(q || ""))) && out.length < 6){ var t = m[2].toUpperCase(); if(out.indexOf(t) < 0) out.push(t); }
    return out;
  }
  function snapshot(q){
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
      alerts: alerts, recommendations: recs, services: services,
      coverage: {alerts_total: (d.alerts || []).length, alerts_included: alerts.length, services_total: (d.services || []).length, services_included: services.length,
        what: "Forecast Centre alerts and services with forecast risk only, after the page filters. A service not listed here is not evidence of anything."},
      focus: focusOn(d, named(q), pick)};
  }
  function focusOn(d, svcs, pick){          // every Forecast Centre row for the services named in the question, not only the top ones
    var f = {};
    svcs.forEach(function(v){
      var same = function(x){ return String(x.svc || "").toUpperCase() === v; };
      var a = (d.alerts || []).filter(same).slice(0, 20).map(function(x){ return pick(x, ["category", "dir", "location", "impact", "onset", "cause", "confidence", "status", "delay", "evidence"]); });
      var sv = (d.services || []).filter(same).map(function(x){ return pick(x, ["dir", "operator", "location", "delay", "occupancy", "heavy_buses", "three", "two", "gaps", "headway", "scheduled"]); });
      var r = (d.recommendations || []).filter(same).slice(0, 4).map(function(x){ return pick(x, ["action", "reason", "approval", "constraints"]); });
      if(a.length || sv.length || r.length) f[v] = {alerts: a, services: sv, recommendations: r};
      else f[v] = {alerts: [], services: [], recommendations: [], note: "Not on the Forecast Centre page (it lists services with forecast risk only)."};
    });
    return f;
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
      body: JSON.stringify({q: q, lang: S.lang, history: hist, ctx: snapshot(q)})})
      .then(function(r){ return r.json().catch(function(){ return {ok: false, error: "HTTP " + r.status}; }); })
      .then(function(j){
        if(j && j.configured === false){ S.configured = false; status(); }
        if(j && j.ok) done(true, j.answer, (j.mode === "demo" ? "Simulated data · " : "Live data · ") + (j.model || "") + (j.ms ? " · " + (j.ms / 1000).toFixed(1) + " s" : ""));
        else done(false, (j && j.error) || "unknown error");
      })
      .catch(function(e){ done(false, "network (" + (e && e.message || e) + ")"); });
  }
  var DEMO = {                                     // preview only (window.HAI_DEMO): one sample answer in the HORIZON answer format
    en: "Service 156 - Operating with Abnormalities\nAssessment: Dir 1 is about 10 min late near Clementi Ave 2, inside a same-direction jam (simulated data).\nKey findings:\n- Dir 1: traffic delay +10 min at Clementi Ave 2, still building.\n- 3 stops affected, Blk 431 to Aft Clementi Ave 2.\n- Dir 2: no bunching or long-headway alert on this page.\nAction: Monitor the gap behind the delayed bus. Assess whether holding the next 156 at the interchange is feasible (Service Controller approval).\nData confidence: Partial - demo page, simulated data.",
    zh: "156 号服务 - 运行中，有异常\n评估：方向 1 在 Clementi Ave 2 附近因同向交通堵塞延误约 10 分钟（模拟数据）。\n主要发现：\n- 方向 1：Clementi Ave 2 交通延误 +10 分钟，仍在加剧。\n- 受影响 3 个车站：Blk 431 至 Aft Clementi Ave 2。\n- 方向 2：本页面没有串车或长间隔警报。\n行动：监控延误巴士后方的间隔；评估在转换站让下一辆 156 号多停是否可行（需服务控制员批准）。\n数据可信度：部分 - 演示页面，模拟数据。",
    ms: "Perkhidmatan 156 - Beroperasi dengan Keabnormalan\nPenilaian: Arah 1 lewat kira-kira 10 minit berhampiran Clementi Ave 2 akibat kesesakan sehala (data simulasi).\nPenemuan utama:\n- Arah 1: kelewatan trafik +10 minit di Clementi Ave 2, masih meningkat.\n- 3 hentian terjejas, Blk 431 hingga Aft Clementi Ave 2.\n- Arah 2: tiada amaran bas berkelompok atau sela panjang di halaman ini.\nTindakan: Pantau jurang di belakang bas yang lewat. Nilai sama ada menahan bas 156 berikutnya di pertukaran boleh dilaksanakan (kelulusan Pengawal Perkhidmatan).\nKeyakinan data: Separa - halaman demo, data simulasi."
  };

  /* ---------------- voice out ---------------- */
  var synth = window.speechSynthesis || null, queue = [], anim = 0;
  // male voice names in Chrome, Edge, Windows and Apple (the Officer face is a man; Orb and Photo take the most natural voice)
  var MALE = /\bmale\b|ryan|wayne|guy|davis|tony|jason|christopher|eric|andrew|brian|roger|steffan|william|thomas|liam|george|oliver|james|daniel|aaron|alex|fred|rishi|prabhat|david|mark|yunxi|yunjian|yunyang|yunxia|yunze|kangkang|osman|ardi|danny|reed|rocko|grandpa/i;
  function chooseVoice(lang, face){
    if(!synth) return {v: null, how: "none"};
    var vs = synth.getVoices() || [], want = {en: ["en-SG", "en-GB", "en-AU", "en-US", "en"], zh: ["zh-CN", "zh-SG", "cmn", "zh-TW", "zh"], ms: ["ms-MY", "ms"]}[lang] || ["en"];
    var find = function(p){ return vs.filter(function(v){ return (v.lang || "").replace("_", "-").toLowerCase().indexOf(p.toLowerCase()) === 0; }); };
    var nice = function(v){ return /natural|neural|google|online|premium|enhanced/i.test(v.name); };
    var male = function(v){ return MALE.test(v.name) && !/female/i.test(v.name); };
    var best = function(hit){
      if(face === "agent") return hit.find(function(v){ return male(v) && nice(v); }) || hit.find(male) || hit.find(nice) || hit[0];
      return hit.find(nice) || hit[0];
    };
    var all = [], i;
    for(i = 0; i < want.length; i++) all = all.concat(find(want[i]).filter(function(v){ return all.indexOf(v) < 0; }));
    if(face === "agent" && all.length && !all.some(male)){ for(i = 0; i < want.length; i++){ var h0 = find(want[i]); if(h0.length) return {v: best(h0), how: "ok", male: false}; } }
    for(i = 0; i < want.length; i++){ var hit = find(want[i]); if(hit.length){ var pick = face === "agent" && all.some(male) ? best(all) : best(hit); return {v: pick, how: "ok", male: male(pick)}; } }
    if(lang === "ms"){ var id = find("id"); if(id.length){ var b2 = best(id); return {v: b2, how: "id", male: male(b2)}; } }
    return {v: null, how: vs.length ? "none" : "wait"};
  }
  function pickVoice(){
    var c = chooseVoice(S.lang, S.face);
    S.voiceNote = c.how === "id" ? t("idVoice") : c.how === "none" ? t("noVoice") : "";
    return c.v;
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
    u.rate = S.lang === "en" ? 1.02 : 1; u.pitch = S.face === "agent" ? 1 : 1.05;
    u.onstart = function(){ S.cur = {text: u.text, t0: performance.now(), i0: 0}; setSpeaking(true); };
    u.onboundary = function(e){ S.kick = 1; if(S.cur && e && typeof e.charIndex === "number" && e.name !== "sentence"){ S.cur.i0 = e.charIndex; S.cur.t0 = performance.now(); } };
    u.onend = function(){ next(); };
    u.onerror = function(){ next(); };
    synth.speak(u);
  }
  function stopSpeaking(){ queue = []; if(synth){ try{ synth.cancel(); }catch(e){} } setSpeaking(false); }
  function setSpeaking(on){
    S.speaking = on; say(on); talk(on);
    if(on && !anim) anim = requestAnimationFrame(tick);
  }
  function tick(ts){
    var target = S.speaking ? Math.max(S.kick || 0, .25 + .55 * Math.abs(Math.sin(ts / 95)) * (.6 + .4 * Math.random())) : 0;
    S.kick = (S.kick || 0) * .82;
    S.level += (target - S.level) * .45;
    render(S.level);
    if(S.speaking || S.level > .01) anim = requestAnimationFrame(tick); else { anim = 0; render(0); }
  }
  // mouth shape for the letter being spoken now: [open, wide, round]. The browser does not say which sound is playing,
  // so the position is estimated from the speaking rate and corrected at every word the voice reports.
  function viseme(lv){
    var c = S.cur; if(!S.speaking || !c) return [0, 0, 0];
    var cps = S.lang === "zh" ? 4.4 : 13.5, pos = c.i0 + (performance.now() - c.t0) * cps / 1000, i = Math.floor(pos), f = pos - i;
    if(i >= c.text.length) return [.15 + lv * .55, .25, 0];
    var ch = c.text.charAt(i).toLowerCase();
    if(S.lang === "zh"){
      if(/[\s，。！？、,.!?：:；;（）()]/.test(ch)) return [.04, 0, 0];
      var o = .15 + .75 * Math.sin(Math.PI * f), kind = ch.charCodeAt(0) % 3;
      return kind === 0 ? [o, .25, 0] : kind === 1 ? [o * .65, .8, 0] : [o * .75, 0, .85];
    }
    if(/[aáàâä]/.test(ch)) return [.85, .3, 0];
    if(/[eéèiíy]/.test(ch)) return [.42, .85, 0];
    if(/[oóòuúw]/.test(ch)) return [.6, 0, .9];
    if(/[mbp]/.test(ch)) return [0, .1, 0];
    if(/[fv]/.test(ch)) return [.1, .35, 0];
    if(/[a-z0-9]/.test(ch)) return [.28, .3, 0];
    return [.04, 0, 0];
  }
  function render(lv){
    var f = $("haiFace"); if(f) f.style.setProperty("--lv", lv.toFixed(3));
    var mo = $("haiMouth");
    if(mo){
      var v = viseme(lv), now = performance.now(), dt = Math.min(250, now - (S.mt || now)), a1 = 1 - Math.exp(-dt / 45), a2 = 1 - Math.exp(-dt / 60);
      S.mt = now;                                                   // smoothing by time, so a slow PC shows the same shapes
      S.mo = (S.mo || 0) + (v[0] - (S.mo || 0)) * a1; S.mw = (S.mw || 0) + (v[1] - (S.mw || 0)) * a2; S.mr = (S.mr || 0) + (v[2] - (S.mr || 0)) * a2;
      if(!S.speaking && lv <= .011){ S.mo = S.mw = S.mr = 0; }
      var d = mouthD(S.mo, S.mw, S.mr);
      $("haiOpen").setAttribute("d", d.cav); $("haiCavClip").setAttribute("d", d.cav); $("haiTeeth").setAttribute("d", d.teeth);
      $("haiLipU").setAttribute("d", d.up); $("haiLipL").setAttribute("d", d.lo); mo.setAttribute("data-open", S.mo.toFixed(2));
      if($("haiTongue")) $("haiTongue").setAttribute("d", d.tg);
      if($("haiBrows")) $("haiBrows").setAttribute("transform", "translate(0 " + (-(S.mo || 0) * 1.8).toFixed(2) + ")");
    }
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
    voiceFor: function(lang, face){ return chooseVoice(lang, face || S.face); },
    faceSVG: function(f, k, mini){ return f === "orb" ? orbSVG() : agentSVG(k, mini); }, setPhoto: setPhoto,
    open: function(on){ S.open = on !== false; store("open", S.open ? "1" : "0"); words(); }};
})();
