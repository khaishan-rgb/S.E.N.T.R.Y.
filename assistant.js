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
         foot: "Advisory only · answers from this page's data · the Service Controller decides", choose: "Choose photo", photoNote: "Use a picture you have permission to use. It stays in this browser only.",
         noPhoto: "No photo yet", fab: "Ask HORIZON AI", noVoice: "No voice for this language on this computer: showing text only.", idVoice: "No Malay voice on this computer: using an Indonesian voice.",
         cats: {loading: "heavily loaded", traffic: "delayed by traffic", bunching: "bunching", gap: "running with a long gap"},
         why: "Why is {svc} {cat} at {loc}?", now: "Which services need action now?", sum: "Summarise the network in 3 lines.", cp: "Are the checkpoints jammed?",
         err: "Could not get an answer", offMsg: "HORIZON AI is not set up yet: add ANTHROPIC_API_KEY in Render > Environment."},
    zh: {sub: "运营助理", on: "在线", off: "未设置", ph: "询问警报、巴士服务或事故", send: "发送", mic: "说出你的问题",
         micNo: "语音输入请使用 Chrome 或 Edge", listen: "正在聆听…", speaking: "正在说话", idle: "就绪", think: "正在查看实时数据…",
         faces: {agent: "职员", orb: "光环", photo: "照片"}, replay: "重播", mute: "关闭语音", unmute: "开启语音", stop: "停止",
         min: "最小化", big: "放大", small: "缩小", you: "你", hello: "您好，我是 HORIZON AI。请问我本页上的任何警报、巴士服务或事故，我会解释原因和处理方法。",
         foot: "仅供参考 · 根据本页数据回答 · 由服务控制员决定", choose: "选择照片", photoNote: "请使用您有权使用的照片。照片只保存在此浏览器中。",
         noPhoto: "尚未选择照片", fab: "询问 HORIZON AI", noVoice: "此电脑没有中文语音：只显示文字。", idVoice: "",
         cats: {loading: "载客量高", traffic: "因交通延误", bunching: "串车（车距过近）", gap: "班距过大"},
         why: "{svc} 号为什么在 {loc} {cat}？", now: "现在哪些巴士服务需要处理？", sum: "用三句话总结目前的网络状况。", cp: "关卡现在拥堵吗？",
         err: "无法取得回答", offMsg: "HORIZON AI 尚未设置：请在 Render > Environment 加入 ANTHROPIC_API_KEY。"},
    ms: {sub: "Pembantu Operasi", on: "dalam talian", off: "belum disediakan", ph: "Tanya tentang amaran, perkhidmatan atau insiden", send: "Hantar", mic: "Sebut soalan anda",
         micNo: "Input suara berfungsi dalam Chrome atau Edge", listen: "Sedang mendengar…", speaking: "Sedang bercakap", idle: "Sedia", think: "Sedang menyemak data langsung…",
         faces: {agent: "Pegawai", orb: "Orb", photo: "Foto"}, replay: "Main semula", mute: "Senyapkan suara", unmute: "Suara dihidupkan", stop: "Henti",
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
  /* Agent: an original drawn male operations officer in a white shirt with purple collar and shoulders and an orange
     stripe (the SBS Transit uniform colours). He breathes, blinks, glances, raises his hand while he talks, and his
     mouth takes the shape of the letters being spoken. k = id suffix for extra copies; mini = head only. */
  function mouthD(o, w, r){        // o = open, w = wide, r = round (0..1) -> lip and mouth paths
    var cx = 120, cy = 137.2, hw = 11.4 * (1 + .2 * w - .36 * r), h = .5 + 8.6 * o, s = 1.5 * (1 - o) * (1 - r);
    var L = (cx - hw).toFixed(2) + " " + (cy - s).toFixed(2), R = (cx + hw).toFixed(2) + " " + (cy - s).toFixed(2), f = function(v){ return v.toFixed(2); };
    var top = cy - h * .32, bot = cy + h * .68;
    return {
      cav: "M" + L + "Q" + cx + " " + f(top - .6) + " " + R + "Q" + cx + " " + f(bot + .8) + " " + L + "Z",
      teeth: "M" + f(cx - hw * .72) + " " + f(top - .4) + "H" + f(cx + hw * .72) + "V" + f(top + Math.min(2.6, h * .42)) + "Q" + cx + " " + f(top + Math.min(3.2, h * .5)) + " " + f(cx - hw * .72) + " " + f(top + Math.min(2.6, h * .42)) + "Z",
      up: "M" + L + "C" + f(cx - hw * .62) + " " + f(top - 3.6) + " " + f(cx - 2.2) + " " + f(top - 3.4) + " " + cx + " " + f(top - 2.5) + "C" + f(cx + 2.2) + " " + f(top - 3.4) + " " + f(cx + hw * .62) + " " + f(top - 3.6) + " " + R
        + "Q" + cx + " " + f(top - .2) + " " + L + "Z",
      lo: "M" + L + "Q" + cx + " " + f(bot + .9) + " " + R + "C" + f(cx + hw * .55) + " " + f(bot + 4.6) + " " + f(cx - hw * .55) + " " + f(bot + 4.6) + " " + L + "Z"
    };
  }
  function agentSVG(k, mini){
    k = k || "";
    var id = function(n){ return ' id="hai' + n + k + '"'; }, u = function(n){ return "url(#hai" + n + k + ")"; }, m = mouthD(0, 0, 0);
    var SK = "#e9b893", SKD = "#c98f6c", LINE = "#a8714f";
    return '<svg class="hai-agent' + (mini ? " mini" : "") + '" viewBox="' + (mini ? "76 40 88 88" : "16 20 208 277") + '" preserveAspectRatio="xMidYMid slice" aria-hidden="true"><defs>'
      + '<radialGradient' + id("Bg") + ' cx="50%" cy="30%" r="80%"><stop offset="0" stop-color="#3b3f97"/><stop offset=".42" stop-color="#16205a"/><stop offset="1" stop-color="#050b1f"/></radialGradient>'
      + '<radialGradient' + id("Glow") + ' cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#3fd3ff" stop-opacity=".32"/><stop offset="1" stop-color="#3fd3ff" stop-opacity="0"/></radialGradient>'
      + '<radialGradient' + id("Skin") + ' cx="44%" cy="38%" r="70%"><stop offset="0" stop-color="#f7d6b8"/><stop offset=".62" stop-color="' + SK + '"/><stop offset="1" stop-color="' + SKD + '"/></radialGradient>'
      + '<linearGradient' + id("Neck") + ' x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#b87b5a"/><stop offset=".55" stop-color="#d9a17e"/><stop offset="1" stop-color="#e3ad89"/></linearGradient>'
      + '<linearGradient' + id("Arm") + ' x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#cf966f"/><stop offset=".5" stop-color="#ebbb96"/><stop offset="1" stop-color="#cf966f"/></linearGradient>'
      + '<linearGradient' + id("Hair") + ' x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2f3445"/><stop offset="1" stop-color="#11131b"/></linearGradient>'
      + '<linearGradient' + id("Shirt") + ' x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#b9bdcb"/><stop offset=".3" stop-color="#eef0f5"/><stop offset=".62" stop-color="#f8f9fc"/><stop offset="1" stop-color="#b5b9c8"/></linearGradient>'
      + '<linearGradient' + id("Purple") + ' x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5a2a72"/><stop offset="1" stop-color="#3a1850"/></linearGradient>'
      + '<clipPath' + id("EyeL") + '><path d="M96.6 98.4C100 94.6 109 94.1 113.4 97.9C109.2 101.2 100.8 101.5 96.6 98.4Z"/></clipPath>'
      + '<clipPath' + id("EyeR") + '><path d="M143.4 98.4C140 94.6 131 94.1 126.6 97.9C130.8 101.2 139.2 101.5 143.4 98.4Z"/></clipPath>'
      + '<clipPath' + id("Cav") + '><path' + id("CavClip") + ' d="' + m.cav + '"/></clipPath>'
      + "</defs>"
      + '<rect width="240" height="320" fill="' + u("Bg") + '"/>'
      + '<circle cx="120" cy="104" r="96" fill="' + u("Glow") + '"/>'
      + '<g class="breathe">'
      // arms (skin below the sleeves)
      + '<path d="M26 258C22 280 20 300 20 320H52C52 300 54 282 58 262Z" fill="' + u("Arm") + '"/>'
      + '<path d="M214 258C218 280 220 300 220 320H188C188 300 186 282 182 262Z" fill="' + u("Arm") + '"/>'
      // shirt with sleeves
      + '<path d="M104 170C92 176 74 181 58 186C40 192 30 206 26 226L22 263C34 267 48 268 58 265L60 252C61 276 62 300 62 320H178C178 300 179 276 180 252L182 265C192 268 206 267 218 263L214 226C210 206 200 192 182 186C166 181 148 176 136 170C130 177 110 177 104 170Z" fill="' + u("Shirt") + '"/>'
      + '<path d="M60 252C59 240 58 228 56 216M180 252C181 240 182 228 184 216" fill="none" stroke="#a3a8ba" stroke-width="1.1" opacity=".8"/>'
      + '<path d="M22 263C34 267 48 268 58 265M182 265C192 268 206 267 218 263" fill="none" stroke="#8f95aa" stroke-width="1.3"/>'
      + '<path d="M70 280C72 296 72 308 70 320M170 280C168 296 168 308 170 320" fill="none" stroke="#b4b9c8" stroke-width="1" opacity=".7"/>'
      // purple raglan shoulders, orange stripe from collar to sleeve
      + '<path d="M104 170C92 176 74 181 58 186C40 192 30 206 26 226L22 263C28 265 33 266 38 266C44 236 64 204 108 184Z" fill="' + u("Purple") + '"/>'
      + '<path d="M136 170C148 176 166 181 182 186C200 192 210 206 214 226L218 263C212 265 207 266 202 266C196 236 176 204 132 184Z" fill="' + u("Purple") + '"/>'
      + '<path d="M108 184C64 204 44 236 38 266M132 184C176 204 196 236 202 266" fill="none" stroke="#f28a1e" stroke-width="3.4" stroke-linecap="round"/>'
      + '<path d="M60 190C76 186 92 180 102 174" fill="none" stroke="#7b4a96" stroke-width="1.2" opacity=".55"/><path d="M180 190C164 186 148 180 138 174" fill="none" stroke="#7b4a96" stroke-width="1.2" opacity=".55"/>'
      // neck
      + '<path d="M105 132V172C110 181 130 181 135 172V132Z" fill="' + u("Neck") + '"/>'
      + '<path d="M105 148C112 158 128 158 135 148V136H105Z" fill="#9e6446" opacity=".32"/>'
      + '<path d="M135 140V170M184 187C201 193 211 207 214.6 228" fill="none" stroke="#7fe0ff" stroke-width="1.2" opacity=".3"/>'
      // collar and placket
      + '<path d="M103 166C97 172 91 177 86 181L104 202L118 185C111 184 106 176 103 166Z" fill="' + u("Purple") + '"/>'
      + '<path d="M137 166C143 172 149 177 154 181L136 202L122 185C129 184 134 176 137 166Z" fill="' + u("Purple") + '"/>'
      + '<path d="M104 202L118 185M136 202L122 185" fill="none" stroke="#24102f" stroke-width=".9" opacity=".6"/>'
      + '<path d="M115.5 185H124.5V228H115.5Z" fill="#e6e8ef" stroke="#c5c9d6" stroke-width=".8"/>'
      + '<circle cx="120" cy="196" r="1.5" fill="#9aa0b4"/><circle cx="120" cy="212" r="1.5" fill="#9aa0b4"/>'
      // name badge
      + '<rect x="131" y="222" width="30" height="10" rx="2" fill="#0c1a36"/><rect x="133" y="224.6" width="4.6" height="4.6" rx="1" fill="#3fd3ff"/>'
      + '<text x="140" y="229.4" font-family="Rajdhani,Arial,sans-serif" font-size="5.6" font-weight="700" fill="#e8f4ff" textLength="19" lengthAdjust="spacingAndGlyphs">HORIZON</text>'
      // head
      + '<g class="head">'
      + '<path d="M85 101C77 96 72 104 74 113C76 122 82 126 88 123Z" fill="' + SKD + '"/><path d="M84 105C80 104 78 110 80 115" fill="none" stroke="' + LINE + '" stroke-width="1" opacity=".7"/>'
      + '<path d="M155 101C163 96 168 104 166 113C164 122 158 126 152 123Z" fill="' + SKD + '"/><path d="M156 105C160 104 162 110 160 115" fill="none" stroke="' + LINE + '" stroke-width="1" opacity=".7"/>'
      + '<path d="M84 94C84 66 99 50 120 50C141 50 156 66 156 94C156 116 152 131 143 142C135 151 127 155.5 120 155.5C113 155.5 105 151 97 142C88 131 84 116 84 94Z" fill="' + u("Skin") + '"/>'
      + '<path d="M90 126C96 142 108 152 120 152.5C132 152 144 142 150 126C146 146 134 156 120 156C106 156 94 146 90 126Z" fill="#b98063" opacity=".35"/>'
      + '<path d="M84.6 96C85 116 90 132 99 143C92 128 88.6 112 89.6 96Z" fill="#a8694a" opacity=".22"/><path d="M155.4 96C155 116 150 132 141 143C148 128 151.4 112 150.4 96Z" fill="#a8694a" opacity=".22"/>'
      + '<path d="M101 106C104 108 109 108.6 112 107.6M139 106C136 108 131 108.6 128 107.6" fill="none" stroke="#b87a5a" stroke-width=".8" opacity=".2"/>'
      + '<ellipse cx="108" cy="76" rx="13" ry="5" fill="#fff" opacity=".1"/>'
      // hair: short cut, side part, swept to one side
      + '<path d="M86 100C82 84 83 66 92 54C100 44 112 39 124 39.5C140 40 152 47 157 60C161 72 160 88 156 100L153.6 99C153 90 152 82 149.4 76.6C146 70 139 65.6 129 64.6C118 63.8 107 65 99.6 69C94 72.6 91 79 90 86.6L88.6 100Z" fill="' + u("Hair") + '"/>'
      + '<path d="M91 84C93 70 101 60 113 55C127 50 145 52 153 61C157.6 66.6 158.6 73 157.6 81C152.6 71 143.6 64.6 130 63.6C117 62.8 105 65.4 98 70.6C94.6 73.4 92.4 78 91 84Z" fill="#283044"/>'
      + '<path d="M104 47C116 42.6 133 43 145 49.6C150 52.4 154 56.6 156 62C150 55 140 50.6 128 49.6C118 48.8 110 49.6 104 52Z" fill="#4a5470" opacity=".55"/>'
      + '<path d="M100 51C108 47 122 46 134 48.6M97 58C108 52.6 124 51.4 140 54.6M104 63C116 58.6 132 58.4 148 62.6M94 70C102 64 112 61 122 60.4" fill="none" stroke="#6a7592" stroke-width="1.1" stroke-linecap="round" opacity=".45"/>'
      + '<path d="M101.6 47.6C99.4 52 98.4 57 98.8 62" fill="none" stroke="#0a0c12" stroke-width="1.1" opacity=".7"/>'
      + '<path d="M86.6 96L88.6 108L90.2 107.4L89.6 95ZM153.4 96L151.4 108L149.8 107.4L150.4 95Z" fill="#1b1e29" opacity=".85"/>'
      // brows
      + '<g class="brows"><path d="M95.5 88.6C100.5 84.2 108 83.4 114 85.6L113.6 89.2C107.8 87.6 101.6 88.4 96.6 91.2Z" fill="#1d1a20"/>'
      + '<path d="M144.5 88.6C139.5 84.2 132 83.4 126 85.6L126.4 89.2C132.2 87.6 138.4 88.4 143.4 91.2Z" fill="#1d1a20"/></g>'
      // eyes
      + '<g class="blink">'
      + '<path d="M96.6 98.4C100 94.6 109 94.1 113.4 97.9C109.2 101.2 100.8 101.5 96.6 98.4Z" fill="#f3efea"/>'
      + '<g clip-path="' + u("EyeL") + '"><g class="iris"><circle cx="105.2" cy="97.8" r="3.9" fill="#4a2f1d"/><circle cx="105.2" cy="97.8" r="2.1" fill="#120c09"/><circle cx="106.6" cy="96.5" r=".95" fill="#fff"/></g></g>'
      + '<path d="M96 98.6C99.6 93.8 109.2 93.2 114 97.6" fill="none" stroke="#1e1612" stroke-width="1.6" stroke-linecap="round"/>'
      + '<path d="M98.6 100.4C102.4 101.6 108.6 101.4 112 99.6" fill="none" stroke="' + LINE + '" stroke-width=".7" opacity=".7"/>'
      + '<path d="M143.4 98.4C140 94.6 131 94.1 126.6 97.9C130.8 101.2 139.2 101.5 143.4 98.4Z" fill="#f3efea"/>'
      + '<g clip-path="' + u("EyeR") + '"><g class="iris"><circle cx="134.8" cy="97.8" r="3.9" fill="#4a2f1d"/><circle cx="134.8" cy="97.8" r="2.1" fill="#120c09"/><circle cx="136.2" cy="96.5" r=".95" fill="#fff"/></g></g>'
      + '<path d="M144 98.6C140.4 93.8 130.8 93.2 126 97.6" fill="none" stroke="#1e1612" stroke-width="1.6" stroke-linecap="round"/>'
      + '<path d="M141.4 100.4C137.6 101.6 131.4 101.4 128 99.6" fill="none" stroke="' + LINE + '" stroke-width=".7" opacity=".7"/>'
      + "</g>"
      + '<path d="M97 92.6C101 91 108 90.8 112 92.2M143 92.6C139 91 132 90.8 128 92.2" fill="none" stroke="' + LINE + '" stroke-width=".7" opacity=".45"/>'
      // nose
      + '<path d="M118 100C117 108 115.4 115 113.6 120.6" fill="none" stroke="' + LINE + '" stroke-width="1.1" opacity=".45" stroke-linecap="round"/>'
      + '<path d="M112.6 121.6C114.6 125.6 125.4 125.6 127.4 121.6" fill="none" stroke="' + LINE + '" stroke-width="1.3" stroke-linecap="round"/>'
      + '<ellipse cx="116" cy="123.2" rx="1.9" ry="1" fill="#8f5a40" opacity=".55"/><ellipse cx="124" cy="123.2" rx="1.9" ry="1" fill="#8f5a40" opacity=".55"/>'
      + '<ellipse cx="121" cy="117" rx="2.2" ry="3" fill="#fff" opacity=".14"/>'
      + '<ellipse cx="100" cy="117" rx="7" ry="4" fill="#e28d77" opacity=".14"/><ellipse cx="140" cy="117" rx="7" ry="4" fill="#e28d77" opacity=".14"/>'
      // mouth
      + '<g' + id("Mouth") + ' data-open="0"><path' + id("Open") + ' d="' + m.cav + '" fill="#4a1a22"/>'
      + '<path' + id("Teeth") + ' d="' + m.teeth + '" fill="#f4efe8" clip-path="' + u("Cav") + '"/>'
      + '<path' + id("LipU") + ' d="' + m.up + '" fill="#b06a5a"/><path' + id("LipL") + ' d="' + m.lo + '" fill="#c07d6b"/>'
      + '<path d="M115 143.6C118 145 122 145 125 143.6" fill="none" stroke="#fff" stroke-width=".8" opacity=".22"/></g>'
      + '<path d="M113 130.2C116 131 124 131 127 130.2" fill="none" stroke="' + LINE + '" stroke-width=".7" opacity=".35"/>'
      + "</g>"
      // gesturing forearm and hand (rotates up from the elbow while he talks)
      + '<g class="fore">'
      + '<path d="M34 306C46 286 62 264 77 245L95 255C83 273 69 295 59 314Z" fill="' + u("Arm") + '"/><path d="M59 314C69 295 83 273 95 255" fill="none" stroke="#b97e5c" stroke-width="1" opacity=".5"/>'
      + '<g transform="rotate(-14 94 246)">'
      + '<path d="M80 246C82 236 84 226 87 218C88 214 92 214 92.6 218L92 230L95 214C96 209.6 100.4 210 100.6 214.4L99 232L102.6 217C103.8 213 108 213.8 107.6 218L104.6 236L107.6 225C108.8 221.6 112.6 222.6 112.2 226L108.4 246C107 254 100 260 92 258L84 254C80 252 78 250 80 246Z" fill="#ebbb96" stroke="' + LINE + '" stroke-width=".8"/>'
      + '<path d="M80 246C76 240 72 234 71 229C70 225 74 223 77 226L84 236" fill="#e2ad87" stroke="' + LINE + '" stroke-width=".8"/>'
      + '<path d="M92 230L91.6 238M99 232L98.2 240M104.6 236L103 242" fill="none" stroke="' + LINE + '" stroke-width=".7" opacity=".6"/>'
      + "</g></g>"
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
