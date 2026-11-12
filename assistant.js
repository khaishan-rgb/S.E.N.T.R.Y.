/* HORIZON AI - operations assistant panel for the AI Operations Centre (/ai-centre).
   Chat about alerts, services and incidents, answered by Claude from the data this page shows (POST /api/assistant/ask).
   Voice: microphone in (browser speech recognition) and spoken answers (browser voices). Languages: English, 中文, Bahasa Melayu.
   Faces: "Officer" (the default: a 3D model in the SBS Transit uniform, drawn in WebGL so he breathes, turns, blinks and talks;
   the animated portrait stands in while the model loads),
   "AI" (a holographic head drawn in WebGL), "Orb", or "Photo" (a picture you choose; stays in this browser).
   Advisory only: nothing here changes service. window.HAI_DEMO = true -> canned answers, no server (preview pages). */
(function(){
  "use strict";
  if(window.__HAI) return; window.__HAI = true;

  /* ---------------- words in three languages ---------------- */
  var T = {
    en: {sub: "AI operations assistant", thinking: "Thinking", faceBtn: "Change face", on: "online", off: "not set up", ph: "Ask about an alert, service or incident", send: "Send", mic: "Speak your question",
         micNo: "Voice input works in Chrome or Edge", listen: "Listening", speaking: "Speaking", idle: "Ready", think: "Checking the live data…",
         faces: {holo: "AI", agent: "Officer", orb: "Orb", photo: "Photo"}, replay: "Replay", mute: "Mute voice", unmute: "Voice on", stop: "Stop",
         min: "Minimise", big: "Bigger", small: "Smaller", you: "You", hello: "Hello, I'm HORIZON AI. Ask me about any alert, service or incident on this page and I'll explain why it happened and what to do.",
         foot: "Advisory only · answers from this page and live service checks · the Service Controller decides", choose: "Choose photo", photoNote: "Use a picture you have permission to use. It stays in this browser only.",
         noPhoto: "No photo yet", fab: "Ask HORIZON AI", noVoice: "No voice for this language on this computer: showing text only.", idVoice: "No Malay voice on this computer: using an Indonesian voice.",
         cats: {loading: "heavily loaded", traffic: "delayed by traffic", bunching: "bunching", gap: "running with a long gap"},
         why: "Why is {svc} {cat} at {loc}?", now: "Which services need action now?", sum: "Summarise the network in 3 lines.", cp: "Are the checkpoints jammed?",
         err: "Could not get an answer", offMsg: "HORIZON AI is not set up yet: add ANTHROPIC_API_KEY in Render > Environment."},
    zh: {sub: "AI 运营助理", thinking: "正在思考", faceBtn: "更换形象", on: "在线", off: "未设置", ph: "询问警报、巴士服务或事故", send: "发送", mic: "说出你的问题",
         micNo: "语音输入请使用 Chrome 或 Edge", listen: "正在聆听", speaking: "正在说话", idle: "就绪", think: "正在查看实时数据…",
         faces: {holo: "AI", agent: "职员", orb: "光环", photo: "照片"}, replay: "重播", mute: "关闭语音", unmute: "开启语音", stop: "停止",
         min: "最小化", big: "放大", small: "缩小", you: "你", hello: "您好，我是 HORIZON AI。请问我本页上的任何警报、巴士服务或事故，我会解释原因和处理方法。",
         foot: "仅供参考 · 根据本页数据和实时服务检查回答 · 由服务控制员决定", choose: "选择照片", photoNote: "请使用您有权使用的照片。照片只保存在此浏览器中。",
         noPhoto: "尚未选择照片", fab: "询问 HORIZON AI", noVoice: "此电脑没有中文语音：只显示文字。", idVoice: "",
         cats: {loading: "载客量高", traffic: "因交通延误", bunching: "串车（车距过近）", gap: "班距过大"},
         why: "{svc} 号为什么在 {loc} {cat}？", now: "现在哪些巴士服务需要处理？", sum: "用三句话总结目前的网络状况。", cp: "关卡现在拥堵吗？",
         err: "无法取得回答", offMsg: "HORIZON AI 尚未设置：请在 Render > Environment 加入 ANTHROPIC_API_KEY。"},
    ms: {sub: "Pembantu operasi AI", thinking: "Sedang berfikir", faceBtn: "Tukar wajah", on: "dalam talian", off: "belum disediakan", ph: "Tanya tentang amaran, perkhidmatan atau insiden", send: "Hantar", mic: "Sebut soalan anda",
         micNo: "Input suara berfungsi dalam Chrome atau Edge", listen: "Sedang mendengar", speaking: "Sedang bercakap", idle: "Sedia", think: "Sedang menyemak data langsung…",
         faces: {holo: "AI", agent: "Pegawai", orb: "Orb", photo: "Foto"}, replay: "Main semula", mute: "Senyapkan suara", unmute: "Suara dihidupkan", stop: "Henti",
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
  if(store("faceV") !== "3"){ S.face = "agent"; store("face", "agent"); store("faceV", "3"); }    // the Officer portrait is new: show it once to every browser
  if(["holo", "agent", "orb", "photo"].indexOf(S.face) < 0) S.face = "agent";
  var t = function(k){ return T[S.lang][k]; };
  var esc = function(s){ return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){ return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]; }); };
  var clock = function(){ return new Date().toLocaleTimeString("en-SG", {hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Singapore"}); };

  /* ---------------- faces ---------------- */
  /* Officer: the portrait assistant-officer.webp (served as /brand/assistant-officer.webp; a still of the 3D model, same camera), brought to life in WebGL.
     The picture is warped, not redrawn: he breathes, sways a little and blinks, and when he talks his jaw drops, the lips part
     (teeth and mouth inside are painted in) and they widen or round with the letters. Face points below are in the picture's
     own pixels (320 x 427, head-and-shoulders crop). If the picture or WebGL is missing, the plain picture is shown. */
  var OFFICER_V = "2026-11-12c";   // change this whenever assistant-officer.glb or .webp is replaced: browsers keep /brand/ files for a day
  var OFFICER_IMG = "/brand/assistant-officer.webp?v=" + OFFICER_V;
  var OFF_PTS = {W: 320, H: 427, eyeL: [128.1, 138.2, 15.1, 3.7], eyeR: [187.5, 138.4, 14.3, 4.0],  // centre x, y, half width, half height
    mouth: [157.3, 198.0, 25.1, 3.4], chin: 234.4, brow: 123.2, pivot: [160.4, 265.2]};          // centre x, lip line y at centre, half width, smile lift at corners
  var OFF_VS = "attribute vec2 aP;uniform vec4 uFit;varying vec2 vUv;void main(){vUv=vec2(uFit.z+aP.x*.5*uFit.x,uFit.w-aP.y*.5*uFit.y);gl_Position=vec4(aP,0.,1.);}";   // uFit: part of the picture shown (cover fit)
  var OFF_FS = "precision mediump float;varying vec2 vUv;uniform sampler2D uT;uniform vec4 uM;uniform vec4 uA;uniform vec2 uB;"   // uM open, wide, round, level; uA time, head angle, nod, breathe; uB blink, unused
    + "uniform vec4 uE1,uE2,uMo;uniform vec3 uK;"            // eyes; mouth; chin, brow, pivot y
    + "const vec2 SZ=vec2(" + OFF_PTS.W.toFixed(1) + "," + OFF_PTS.H.toFixed(1) + ");"
    + "float g(float v,float s){return exp(-v*v/(2.*s*s));}"
    + "float lineY(float x){float d=(x-uMo.x)/uMo.z;return uMo.y-uMo.w*d*d;}"
    + "vec2 blink(vec2 p,vec4 E){vec2 e=(p-E.xy)/E.zw;if(abs(e.x)>=1.)return p;"
    + "float yt=-sqrt(1.-e.x*e.x),yb=.9*sqrt(1.-e.x*e.x),ye=mix(yt,yb,uB.x),top=yt-1.25;"
    + "if(e.y<top||e.y>ye)return p;float k=(e.y-top)/max(ye-top,.001);return vec2(p.x,E.y+E.w*(top+k*(yt-top)));}"
    + "void main(){vec2 p=vUv*SZ;"
    // breathing: a slow rise of the chest and shoulders, head carried with it
    + "p.y+=uA.w*.9*smoothstep(" + OFF_PTS.H + ".,150.,p.y);"
    // head: small turn and nod about the neck; fades out over the collar so the body stays put
    + "float wh=1.-smoothstep(uK.z-28.,uK.z+8.,p.y);vec2 pv=vec2(uMo.x,uK.z);vec2 q=p-pv;float a=uA.y*wh;"
    + "p=pv+mat2(cos(a),sin(a),-sin(a),cos(a))*q;p.y-=uA.z*wh;"
    // brows lift a little with the voice
    + "p.y+=uM.w*.7*g(p.y-uK.y,6.)*g(abs(p.x-uMo.x)-27.,22.);"
    + "p=blink(p,uE1);p=blink(p,uE2);"
    // mouth: wider or rounder lips, then the jaw drops below the lip line and the upper lip lifts
    + "float hw=uMo.z*(1.+.12*uM.y-.22*uM.z);float dl=p.y-lineY(p.x);"
    + "float mm=g(dl,9.)*g(p.x-uMo.x,30.);p.x=uMo.x+(p.x-uMo.x)/(1.+(.1*uM.y-.17*uM.z)*mm);"
    + "float dx=(p.x-uMo.x)/hw;float ln=pow(max(0.,1.-dx*dx),1.15);float lw=g(p.x-uMo.x,34.);"
    + "dl=p.y-lineY(p.x);float D=uM.x*8.6*mix(ln,lw,smoothstep(0.,14.,dl))*(1.-smoothstep(uK.x,uK.x+16.,p.y));float U=uM.x*1.7*ln;"
    + "vec3 col;float inside=0.;float t=0.;"
    + "if(ln>0.&&dl>-U&&dl<D){inside=1.;t=(dl+U)/max(D+U,.001);}"
    + "if(inside<.5){if(dl<=-U)p.y+=U*exp(-pow((-dl-U)/7.,2.));else p.y-=D;}"
    + "col=texture2D(uT,clamp(p/SZ,0.,1.)).rgb;"
    + "if(inside>.5){float h=D+U;vec3 c=mix(vec3(.09,.025,.035),vec3(.3,.1,.11),smoothstep(.15,.95,abs(dx))*.8+.2*smoothstep(.7,1.,t));"
    + "float th=min(.4,2.5/max(h,.1));c=mix(c,vec3(.92,.89,.85)*(.7+.3*smoothstep(th,0.,t)),smoothstep(th,th*.7,t)*smoothstep(.72,.3,abs(dx))*smoothstep(1.2,2.6,h));"
    + "c=mix(c,vec3(.42,.16,.18),smoothstep(.72,1.,t)*smoothstep(.55,.1,abs(dx))*.8);"
    + "float edge=smoothstep(0.,1.2,(dl+U))*smoothstep(0.,1.4,D-dl);col=mix(col,c,edge*smoothstep(0.,.3,ln));}"
    + "gl_FragColor=vec4(col,1.);}";
  function officerMount(box, get){
    var img = new Image(), cv = document.createElement("canvas"), gl = null, dead = false, raf = 0;
    img.alt = ""; img.className = "hai-officer-img"; img.src = OFFICER_IMG; img.decoding = "async";
    box.appendChild(img);                                         // shown until the moving face is ready, and if WebGL is missing
    try{ gl = cv.getContext("webgl", {alpha: false, antialias: false}); }catch(e){}
    if(!gl) return {stop: function(){ dead = true; }};
    cv.className = "hai-officer";
    var still = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
    function start(){
      if(dead) return;
      var vs = gl.createShader(gl.VERTEX_SHADER), fs = gl.createShader(gl.FRAGMENT_SHADER), p = gl.createProgram();
      gl.shaderSource(vs, OFF_VS); gl.compileShader(vs); gl.shaderSource(fs, OFF_FS); gl.compileShader(fs);
      if(!gl.getShaderParameter(fs, gl.COMPILE_STATUS)){ if(window.console) console.warn("HORIZON AI officer:", gl.getShaderInfoLog(fs)); return; }
      gl.attachShader(p, vs); gl.attachShader(p, fs); gl.linkProgram(p); gl.useProgram(p);
      var b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
      var l = gl.getAttribLocation(p, "aP"); gl.enableVertexAttribArray(l); gl.vertexAttribPointer(l, 2, gl.FLOAT, false, 0, 0);
      var tx = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, tx);
      try{ gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, img); }catch(e){ return; }
      [gl.TEXTURE_WRAP_S, gl.TEXTURE_WRAP_T].forEach(function(k){ gl.texParameteri(gl.TEXTURE_2D, k, gl.CLAMP_TO_EDGE); });
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      var U = function(n){ return gl.getUniformLocation(p, n); }, P = OFF_PTS;
      gl.uniform4f(U("uE1"), P.eyeL[0], P.eyeL[1], P.eyeL[2], P.eyeL[3]); gl.uniform4f(U("uE2"), P.eyeR[0], P.eyeR[1], P.eyeR[2], P.eyeR[3]);
      gl.uniform4f(U("uMo"), P.mouth[0], P.mouth[1], P.mouth[2], P.mouth[3]); gl.uniform3f(U("uK"), P.chin, P.brow, P.pivot[1]);
      var uM = U("uM"), uA = U("uA"), uB = U("uB"), uFit = U("uFit"), t0 = performance.now(), blinkAt = 1.8, nod = 0;
      box.appendChild(cv); img.style.visibility = "hidden";
      function draw(now){
        raf = 0; if(dead) return;
        if(!cv.isConnected){ stop(); return; }
        raf = requestAnimationFrame(draw);
        if(!cv.offsetParent || document.hidden) return;
        var dpr = Math.min(2, window.devicePixelRatio || 1), w = Math.round(cv.clientWidth * dpr), h = Math.round(cv.clientHeight * dpr);
        if(!w || !h) return;
        if(cv.width !== w || cv.height !== h){
          cv.width = w; cv.height = h;
          var ia = P.W / P.H, ca = w / h, sx = ca > ia ? 1 : ca / ia, sy = ca > ia ? ia / ca : 1;   // cover: crop the long side, keep the face (upper part) in view
          gl.uniform4f(uFit, sx, sy, .5, Math.min(1 - sy / 2, Math.max(sy / 2, .4)));
        }
        var s = get() || {}, t = (now - t0) / 1000, mv = still ? 0 : 1, talk = s.mode === 1 ? 1 : 0;
        if(t > blinkAt + .2) blinkAt = t + 2.4 + 3.6 * Math.random() - (Math.random() < .15 ? 2 : 0);   // now and then a double blink
        var bl = mv * Math.max(0, 1 - Math.abs((t - blinkAt - .09) / .09));
        nod += ((talk ? .9 * Math.max(0, Math.sin(t * 4.1)) * (s.lv || 0) : 0) - nod) * .15;
        var ang = mv * (.010 * Math.sin(t * .37) + .005 * Math.sin(t * .91) + talk * .006 * Math.sin(t * 1.7) + (s.mode === 2 ? .008 * Math.sin(t * 1.2) : 0));
        gl.viewport(0, 0, w, h);
        gl.uniform4f(uM, s.o || 0, s.w || 0, s.r || 0, s.lv || 0);
        gl.uniform4f(uA, t, ang, mv * nod, mv * (Math.sin(t * 2 * Math.PI / 4.6) * .5 + .5));
        gl.uniform2f(uB, bl, 0);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      }
      raf = requestAnimationFrame(draw);
    }
    function stop(){ dead = true; if(raf) cancelAnimationFrame(raf); var x = gl.getExtension("WEBGL_lose_context"); if(x) x.loseContext(); }
    if(img.complete && img.naturalWidth) start(); else { img.onload = start; img.onerror = function(){ img.replaceWith(Object.assign(document.createElement("div"), {className: "hai-nophoto", textContent: "assistant-officer.webp missing"})); }; }
    return {stop: stop};
  }
  /* Officer in 3D: assistant-officer.glb (a textured 3D model of the Officer, served as /brand/assistant-officer.glb), drawn
     here in WebGL with no library. The model has no skeleton or face rig, so the movement is done here: he breathes, sways,
     turns and nods his head, blinks, and when he talks his jaw drops and his lips part, widen or round with the letters.
     The face points below are in the model's own metres (Y up, Z towards the viewer). The animated portrait shows while the
     model loads (about 4 MB, then cached), and stays if the model or WebGL cannot be used. */
  var OFFICER_GLB = "/brand/assistant-officer.glb?v=" + OFFICER_V, O3GLB = null;
  var O3 = {mouth: [-0.0128, 1.0504, 0.0170, 0.0023],        // centre x, lip line y at the centre, half width, how much higher the corners are
    chin: 1.0254, neck: [-0.0107, 1.0002, -0.0211], brow: 1.101,   // chin y; neck centre (head turns about it); brow y
    eyeL: [-0.0328, 1.0911, 0.0103, 0.0025], eyeR: [0.0078, 1.091, 0.0098, 0.0027], lid: [0.76, 0.57, 0.48],   // eye centre x, y, half width, half height; eyelid colour
    look: [-0.011, 1.038, 0.0], frameH: 0.155};                  // camera aims here; half the height shown (m)
  var O3_VS = "attribute vec4 aP;attribute vec3 aN;attribute vec2 aU;uniform vec4 uQ;uniform mat4 uVP;uniform vec4 uM,uH,uMo,uK;uniform float uBr;"
    + "varying vec2 vU;varying vec3 vN,vW,vM;varying float vG,vL;"
    + "float g(float v,float s){return exp(-v*v/(2.*s*s));}"
    + "mat3 rY(float a){float c=cos(a),s=sin(a);return mat3(c,0.,-s,0.,1.,0.,s,0.,c);}mat3 rX(float a){float c=cos(a),s=sin(a);return mat3(1.,0.,0.,0.,c,s,0.,-s,c);}"
    + "void main(){vec3 p=aP.xyz*uQ.w+uQ.xyz,n=aN;vM=p;float flex=1.-clamp(aP.w/32767.,0.,1.);"
    + "float front=smoothstep(uK.w+.02,uK.w+.05,p.z);float near=front*step(abs(p.x-uMo.x),.07)*flex;"
    + "float hw=uMo.z*(1.+.12*uM.y-.22*uM.z);float dx=(p.x-uMo.x)/hw;float ln=pow(max(0.,1.-dx*dx),1.15);float lw=g(p.x-uMo.x,.028);"
    + "float dd=(p.x-uMo.x)/uMo.z;float dl=uMo.y+uMo.w*min(dd*dd,1.)-p.y;float below=step(0.,dl);"
    // jaw drops below the lip line (lips first, the chin with them), upper lip lifts a little, lips widen or round
    + "float D=uM.x*.0072*mix(ln,lw,smoothstep(0.,.012,dl))*smoothstep(uK.x-.02,uK.x-.004,p.y)*below*near;p.y-=D;p.z-=D*.25;"
    + "p.y+=(1.-below)*near*uM.x*.0013*ln*g(dl,.004);"
    + "float mm=near*g(dl,.006)*g(p.x-uMo.x,.022);p.x=uMo.x+(p.x-uMo.x)*(1.+(.08*uM.y-.14*uM.z)*mm);p.z+=uM.z*.002*mm;"
    + "p.y+=uM.w*.0007*g(p.y-uBr,.004)*near;"                                   // brows lift with the voice
    + "vG=below;vL=ln*near;"
    // breathing, then the head turns and nods about the neck (not the raised hand), then the whole body sways
    + "p.y+=uH.z*.0012*smoothstep(.7,.98,p.y);p.z+=uH.z*.0015*g(p.y-.88,.06)*step(uK.w,p.z);"
    + "vec3 pv=vec3(uK.y,uK.z,uK.w);float wh=smoothstep(.985,1.03,p.y)*(1.-smoothstep(.13,.16,length(vec2(p.x-pv.x,p.z-pv.z))));"
    + "mat3 R=rY(uH.x*wh)*rX(uH.y*wh);p=pv+R*(p-pv);n=R*n;"
    + "mat3 B=rY(uH.w);p=vec3(pv.x,0.,pv.z)+B*(p-vec3(pv.x,0.,pv.z));n=B*n;"
    + "vW=p;vN=n;vU=aU;gl_Position=uVP*vec4(p,1.);}";
  var O3_FS = "precision highp float;uniform sampler2D uT;uniform vec4 uM,uE1,uE2,uMo;uniform vec3 uLid,uCam,uRim;uniform float uB;"
    + "varying vec2 vU;varying vec3 vN,vW,vM;varying float vG,vL;"
    + "vec3 lid(vec3 c,vec4 E){vec2 e=(vM.xy-E.xy)/E.zw;if(uB<=0.||abs(e.x)>=1.||vM.z<.02)return c;float h=sqrt(1.-e.x*e.x),yt=h*1.05,ed=mix(yt,-.85*h,uB);"
    + "if(e.y>yt+.6||e.y<ed-.05)return c;vec3 l=mix(uLid,uLid*.3,smoothstep(.22,0.,e.y-ed)*.85);"
    + "return mix(c,l,smoothstep(yt+.6,yt,e.y)*smoothstep(ed-.05,ed+.03,e.y)*smoothstep(1.,.8,abs(e.x)));}"
    + "void main(){vec3 c=texture2D(uT,vU,-.6).rgb;c=lid(c,uE1);c=lid(c,uE2);"
    + "vec3 n=normalize(vN),v=normalize(uCam-vW);if(dot(n,v)<0.)n=-n;"
    + "float key=max(dot(n,normalize(vec3(-.45,.55,.7))),0.),fill=max(dot(n,normalize(vec3(.7,.1,.5))),0.),rim=pow(1.-max(dot(n,v),0.),3.);"
    + "vec3 col=c*(.5+.42*key+.14*fill)+uRim*rim*(.22+.3*uM.w);"
    // inside of the mouth: the triangles across the lip line stretch when the jaw drops
    + "float dd=(vM.x-uMo.x)/uMo.z;float zone=step(abs(uMo.y+uMo.w*min(dd*dd,1.)-vM.y),.003)*step(.02,vM.z);"
    + "if(zone>.5&&vL>.04&&vG>.004&&vG<.996&&uM.x>.03){float t=vG;vec3 m=vec3(.1,.03,.04);m=mix(m,vec3(.88,.85,.8),smoothstep(.36,.26,t)*smoothstep(.2,.5,vL));"
    + "m=mix(m,vec3(.4,.15,.17),smoothstep(.72,.95,t)*smoothstep(.3,.7,vL)*.8);col=mix(col,m,smoothstep(.03,.15,uM.x)*smoothstep(.04,.2,vL));}"
    + "gl_FragColor=vec4(col,1.);}";
  function o3Load(){              // fetch and parse the model once per page
    if(O3GLB) return O3GLB;
    O3GLB = fetch(OFFICER_GLB, {credentials: "same-origin"}).then(function(r){ if(!r.ok) throw new Error("HTTP " + r.status); return r.arrayBuffer(); }).then(function(ab){
      var dv = new DataView(ab), jl = dv.getUint32(12, true), J = JSON.parse(new TextDecoder().decode(new Uint8Array(ab, 20, jl))), bin = 20 + jl + 8;
      var bv = function(i){ var b = J.bufferViews[i]; return {data: new Uint8Array(ab, bin + (b.byteOffset || 0), b.byteLength), stride: b.byteStride || 0}; };
      var pr = J.meshes[0].primitives[0], node = J.nodes.filter(function(n){ return n.mesh === 0; })[0] || {};
      var att = function(k){ var a = J.accessors[pr.attributes[k]], v = bv(a.bufferView); return {data: v.data, stride: v.stride, type: a.componentType, norm: !!a.normalized, size: {VEC2: 2, VEC3: 3}[a.type], off: a.byteOffset || 0}; };
      var ia = J.accessors[pr.indices], ib = bv(ia.bufferView), img = J.images[J.textures[J.materials[pr.material || 0].pbrMetallicRoughness.baseColorTexture.index].source];
      var t = node.translation || [0, 0, 0], s = (node.scale || [1, 1, 1])[0];
      var hz = (J.meshes[0].extras || {}).horizon || {};
      return new Promise(function(ok, bad){
        var im = new Image(), url = URL.createObjectURL(new Blob([bv(img.bufferView).data], {type: img.mimeType}));
        im.onload = function(){ URL.revokeObjectURL(url); var pos = att("POSITION"); if(hz.positionW && pos.stride >= 8) pos.size = 4;
          ok({P: pos, N: att("NORMAL"), U: att("TEXCOORD_0"), I: {data: ib.data, type: ia.componentType, count: ia.count}, img: im, q: [t[0], t[1], t[2], s]}); };
        im.onerror = function(){ bad(new Error("texture")); };
        im.src = url;
      });
    });
    O3GLB.catch(function(){ O3GLB = null; });
    return O3GLB;
  }
  function officer3dMount(box, get){
    var flat = officerMount(box, get), cv = document.createElement("canvas"), gl = null, dead = false, raf = 0;   // the animated portrait first
    try{ gl = cv.getContext("webgl", {alpha: false, antialias: true}); }catch(e){}
    if(!gl || !gl.getExtension("OES_element_index_uint")) return flat;
    cv.className = "hai-officer hai-o3";
    var still = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
    var TINT = [[.25, .83, 1], [.25, .83, 1], [.63, .48, 1], [.18, .91, .63]], rim = [.25, .83, 1];
    function sh(type, src){ var o = gl.createShader(type); gl.shaderSource(o, src); gl.compileShader(o); if(!gl.getShaderParameter(o, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(o)); return o; }
    function start(M){
      if(dead) return;
      var p = gl.createProgram();
      try{ gl.attachShader(p, sh(gl.VERTEX_SHADER, O3_VS)); gl.attachShader(p, sh(gl.FRAGMENT_SHADER, O3_FS)); gl.linkProgram(p); if(!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p)); }
      catch(e){ if(window.console) console.warn("HORIZON AI officer 3D:", e.message); return; }
      gl.useProgram(p);
      [["aP", M.P], ["aN", M.N], ["aU", M.U]].forEach(function(a){
        var l = gl.getAttribLocation(p, a[0]), b = gl.createBuffer(); if(l < 0) return;
        gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, a[1].data, gl.STATIC_DRAW);
        gl.enableVertexAttribArray(l); gl.vertexAttribPointer(l, a[1].size, a[1].type, a[1].norm, a[1].stride, a[1].off);
      });
      var ib = gl.createBuffer(); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, M.I.data, gl.STATIC_DRAW);
      var tx = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, tx); gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, M.img); gl.generateMipmap(gl.TEXTURE_2D);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      var U = function(n){ return gl.getUniformLocation(p, n); }, uM = U("uM"), uH = U("uH"), uB = U("uB"), uVP = U("uVP"), uRim = U("uRim"), uCam = U("uCam");
      gl.uniform4f(U("uQ"), M.q[0], M.q[1], M.q[2], M.q[3]);
      gl.uniform4f(U("uMo"), O3.mouth[0], O3.mouth[1], O3.mouth[2], O3.mouth[3]); gl.uniform4f(U("uK"), O3.chin, O3.neck[0], O3.neck[1], O3.neck[2]);
      gl.uniform4f(U("uE1"), O3.eyeL[0], O3.eyeL[1], O3.eyeL[2], O3.eyeL[3]); gl.uniform4f(U("uE2"), O3.eyeR[0], O3.eyeR[1], O3.eyeR[2], O3.eyeR[3]);
      gl.uniform3f(U("uLid"), O3.lid[0], O3.lid[1], O3.lid[2]); gl.uniform1f(U("uBr"), O3.brow);
      var n = M.I.count, t0 = performance.now(), last = 0, blinkAt = 1.6, nod = 0, shown = false;
      box.appendChild(cv);
      function draw(now){
        raf = 0; if(dead) return;
        if(!cv.isConnected){ stop(); return; }
        raf = requestAnimationFrame(draw);
        if(!cv.offsetParent || document.hidden) return;
        var dpr = Math.min(2, window.devicePixelRatio || 1), w = Math.round(cv.clientWidth * dpr), h = Math.round(cv.clientHeight * dpr);
        if(!w || !h) return;
        if(cv.width !== w || cv.height !== h){ cv.width = w; cv.height = h; }
        var s = get() || {}, t = (now - t0) / 1000, dt = Math.min(.1, t - last), mv = still ? 0 : 1, talk = s.mode === 1 ? 1 : 0, i;
        last = t;
        var tc = TINT[s.mode || 0] || TINT[0], k = 1 - Math.exp(-dt * 5);
        for(i = 0; i < 3; i++) rim[i] += (tc[i] - rim[i]) * k;
        if(t > blinkAt + .2) blinkAt = t + 2.4 + 3.6 * Math.random() - (Math.random() < .15 ? 2 : 0);
        var bl = mv * Math.max(0, 1 - Math.abs((t - blinkAt - .09) / .09));
        nod += ((talk ? Math.max(0, Math.sin(t * 4.1)) * (s.lv || 0) : 0) - nod) * .15;
        var yaw = mv * (.05 * Math.sin(t * .31) + .02 * Math.sin(t * .83) + talk * .025 * Math.sin(t * 1.7) + (s.mode === 2 ? .06 * Math.sin(t * 1.1) : 0));
        var pitch = mv * (.012 * Math.sin(t * .47) + .035 * nod) + (s.mode === 3 ? -.03 : 0);
        // camera: head and shoulders, a little above eye level, perspective
        var asp = w / h, fy = 1 / Math.tan(.19), L = O3.look, wide = asp > 1, ly = L[1] + (wide ? .022 : 0), dist = O3.frameH * (wide ? .78 : 1) * fy * (asp < .7 ? .7 / asp : 1);   // wide stage: closer, on the face
        var ex = L[0], ey = ly + .03, ez = L[2] + dist, f = fy, nr = .1, fr = 10, A = (fr + nr) / (nr - fr), Bp = 2 * fr * nr / (nr - fr);
        var dy = ly - ey, dz = L[2] - ez, ln = Math.hypot(dy, dz), cy = -dz / ln, sy = dy / ln;   // small downward look
        // view = rotX(pitch toward target) * translate(-eye); proj = perspective
        var V = [1, 0, 0, 0, 0, cy, -sy, 0, 0, sy, cy, 0, -ex, -(cy * ey + sy * ez), -(-sy * ey + cy * ez), 1];
        var Pm = [f / asp, 0, 0, 0, 0, f, 0, 0, 0, 0, A, -1, 0, 0, Bp, 0], VP = new Float32Array(16), r, c2, j;
        for(c2 = 0; c2 < 4; c2++) for(r = 0; r < 4; r++){ var sum = 0; for(j = 0; j < 4; j++) sum += Pm[j * 4 + r] * V[c2 * 4 + j]; VP[c2 * 4 + r] = sum; }
        gl.viewport(0, 0, w, h); gl.clearColor(.03, .08, .16, 1); gl.enable(gl.DEPTH_TEST); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
        gl.uniformMatrix4fv(uVP, false, VP); gl.uniform3f(uCam, ex, ey, ez);
        gl.uniform4f(uM, s.o || 0, s.w || 0, s.r || 0, s.lv || 0);
        gl.uniform4f(uH, yaw, pitch, mv * (Math.sin(t * 2 * Math.PI / 4.6) * .5 + .5), mv * .03 * Math.sin(t * .23));
        gl.uniform1f(uB, bl); gl.uniform3f(uRim, rim[0], rim[1], rim[2]);
        gl.drawElements(gl.TRIANGLES, n, M.I.type, 0);
        if(!shown){ shown = true; flat.stop(); [].forEach.call(box.querySelectorAll(".hai-officer-img,.hai-nophoto,.hai-officer:not(.hai-o3)"), function(x){ x.remove(); }); }
      }
      raf = requestAnimationFrame(draw);
    }
    function stop(){ dead = true; flat.stop(); if(raf) cancelAnimationFrame(raf); var x = gl.getExtension("WEBGL_lose_context"); if(x) x.loseContext(); }
    o3Load().then(start, function(e){ if(window.console) console.warn("HORIZON AI officer 3D: model not loaded (" + e.message + "), showing the portrait"); });
    return {stop: stop};
  }
  /* AI: a holographic head drawn in WebGL (no library, no picture file). The head is sculpted here from maths, lit, and
     scanned by a light band; the jaw and lips follow the same mouth shapes as the Officer. get() -> {o, w, r, lv, mode}:
     mouth open / wide / round, voice level, and mode 0 idle, 1 speaking, 2 thinking, 3 listening. */
  var HOLO_GEO = null;
  function holoGeo(){
    if(HOLO_GEO) return HOLO_GEO;
    var g = function(v, s){ return Math.exp(-v * v / (2 * s * s)); }, cl = function(v){ return v < 0 ? 0 : v > 1 ? 1 : v; }, sm = function(v){ v = cl(v); return v * v * (3 - 2 * v); };
    var YM = -0.53, EX = .255, P = [], N = [], I = [], X = [];
    function head(a, f){
      var sa = Math.sin(a), y = Math.cos(a), dx = sa * Math.sin(f), dz = sa * Math.cos(f);
      var sq = y < 0 ? Math.pow(1 - Math.pow(-y, 2.7), 1 / 2.7) / (sa || 1) : 1;      // squarer jaw than an egg
      if(y > 0) y *= .87;                                                 // lower crown
      var x = dx * sq * .66 * (y < 0 ? 1 - .1 * sm(-y) : 1), z = dz * sq * (dz > 0 ? .76 : .9);
      var m = sm(dz / .5), ax = Math.abs(x), e = 0, ny = cl((.17 - y) / .47), h = 0;
      e += .02 * g(y - .2, .06) * g(ax - .25, .2) + .03 * g(y - .45, .2) * g(x, .4);                       // brow ridge
      e -= .075 * g(ax - EX, .105) * g(y - .06, .065);                      // eye sockets
      e += .05 * g(ax - EX, .075) * g(y - .04, .042);                     // eyeballs and lids
      if(y < .24 && y > -.5) h = (.028 + .125 * Math.pow(ny, 1.4)) * (y < -.3 ? g(y + .3, .034) : 1) * (y > .17 ? g(y - .17, .04) : 1);
      e += h * g(x, .034 + .032 * ny);                                    // nose ridge and tip
      e += .04 * g(ax - .075, .034) * g(y + .305, .034);                  // nostril wings
      e += .028 * g(ax - .4, .13) * g(y + .12, .14);                      // cheekbones
      e += .034 * g(x, .22) * g(y + .52, .12);                            // muzzle
      e += .042 * g(x, .15) * g(y + .487, .028);                          // upper lip
      e += .05 * g(x, .13) * g(y + .585, .034);                           // lower lip
      e -= .045 * g(x, .19) * g(y - YM, .014);                              // lip line
      e -= .022 * g(x, .16) * g(y + .68, .04);                            // dip above the chin
      e += .075 * g(x, .2) * g(y + .86, .1) + .03 * g(x, .5) * g(y + .62, .2);   // chin and lower face come forward
      z += e * m;
      var side = sm((Math.abs(dx) - .72) / .2), ear = .04 * g(y + .06, .13) * g(dz + .08, .1) * side;
      x += (x < 0 ? -1 : 1) * ear;
      return [x, y, z, m];
    }
    function key(K, y){            // straight-line keys [y, value] from top to bottom, eased
      for(var i = 1; i < K.length; i++) if(y >= K[i][0]){ var u = sm((K[i - 1][0] - y) / (K[i - 1][0] - K[i][0])); return K[i - 1][1] + (K[i][1] - K[i - 1][1]) * u; }
      return K[K.length - 1][1];
    }
    var RX = [[-.55, .33], [-.95, .34], [-1.12, .4], [-1.3, .78], [-1.5, 1.24], [-1.75, 1.48], [-2.4, 1.54]],
        RZ = [[-.55, .34], [-.95, .35], [-1.12, .37], [-1.3, .44], [-1.5, .52], [-1.75, .58], [-2.4, .62]];
    function bust(y, f){ return [key(RX, y) * Math.sin(f), y, key(RZ, y) * Math.cos(f) - .1 - .06 * cl((-1.2 - y) / .6), 0]; }
    function mesh(rows, cols, at, part, cz){
      var base = P.length / 3, pts = [], i, j;
      for(i = 0; i < rows; i++) for(j = 0; j <= cols; j++) pts.push(at(i, j % cols));
      var q = function(i, j){ return pts[Math.max(0, Math.min(rows - 1, i)) * (cols + 1) + ((j % cols) + cols) % cols]; };
      for(i = 0; i < rows; i++) for(j = 0; j <= cols; j++){
        var p = pts[i * (cols + 1) + j], a = q(i, j + 1), b = q(i, j - 1), c = q(i + 1, j), d = q(i - 1, j);
        var ux = a[0] - b[0], uy = a[1] - b[1], uz = a[2] - b[2], vx = c[0] - d[0], vy = c[1] - d[1], vz = c[2] - d[2];
        var nx = uy * vz - uz * vy, nyy = uz * vx - ux * vz, nz = ux * vy - uy * vx, l = Math.hypot(nx, nyy, nz) || 1;
        if(l < 1e-9){ nx = 0; nyy = p[1] > 0 ? 1 : -1; nz = 0; l = 1; }
        if(nx * p[0] + nz * (p[2] - cz) + nyy * (part ? 0 : p[1]) < 0) l = -l;
        P.push(p[0], p[1], p[2]); N.push(nx / l, nyy / l, nz / l); X.push(p[3], i / (rows - 1), j / cols, part);
      }
      for(i = 0; i < rows - 1; i++) for(j = 0; j < cols; j++){ var k = base + i * (cols + 1) + j; I.push(k, k + cols + 1, k + 1, k + 1, k + cols + 1, k + cols + 2); }
    }
    var R = 150, C = 168, lat = [], i, best = 0;
    for(i = 0; i < R; i++) lat.push(Math.PI * i / (R - 1));
    for(i = 0; i < R; i++) if(Math.abs(Math.cos(lat[i]) - YM) < Math.abs(Math.cos(lat[best]) - YM)) best = i;
    lat[best] = Math.acos(YM);                                             // one ring of points sits exactly on the lip line, so the mouth can open there
    mesh(R, C, function(i, j){ var t = j / C; return head(lat[i], 2 * Math.PI * t - .62 * Math.sin(2 * Math.PI * t)); }, 0, 0);
    mesh(44, 72, function(i, j){ return bust(-.55 - 1.85 * i / 43, 2 * Math.PI * j / 72); }, 1, -.1);
    var D = [], n = 150;                                                   // dust: slow points around the head
    for(i = 0; i < n; i++){ var s = Math.sin(i * 12.9898) * 43758.5453; s -= Math.floor(s); var s2 = Math.sin(i * 78.233) * 12543.123; s2 -= Math.floor(s2); D.push(i / n * 6.2832 * 7, -1.9 + 3.2 * s, .95 + .75 * s2, s); }
    HOLO_GEO = {P: new Float32Array(P), N: new Float32Array(N), X: new Float32Array(X), I: new Uint16Array(I), D: new Float32Array(D), YM: YM};
    return HOLO_GEO;
  }
  var HOLO_VS = "attribute vec3 aP,aN;attribute vec4 aX;uniform mat3 uR;uniform mediump vec4 uM;uniform vec3 uV;uniform vec2 uG;varying vec3 vN,vE,vP;varying vec4 vX;varying float vGap;"
    + "float g(float v,float s){return exp(-v*v/(2.*s*s));}"
    + "void main(){vec3 p=aP;float YM=" + (-0.53).toFixed(2) + ";float gap=0.;"
    + "if(aX.w<.5){float dy=YM-p.y;float hw=.2*(1.+.12*uM.y-.3*uM.z);float lens=max(0.,1.-pow(abs(p.x)/hw,2.));"
    + "float below=step(.004,dy);gap=below;float J=mix(lens,1.,smoothstep(0.,.2,dy))*below*aX.x*smoothstep(-1.,-.9,p.y);"
    + "p.y-=uM.x*.115*J;p.z-=uM.x*.03*J*smoothstep(.0,.3,dy);"
    + "p.y+=uM.x*.014*lens*(1.-below)*g(dy,.05);"
    + "float lip=g(abs(p.x)-.17,.07)*g(dy,.07)*aX.x;p.x+=sign(p.x)*(uM.y*.03-uM.z*.05)*lip;p.z+=uM.z*.03*g(p.x,.14)*g(dy,.08)*aX.x;}"
    + "p.x+=uG.x*step(abs(aP.y-uG.y),.07)*.05;"
    + "vec3 pv=vec3(0.,-.75,0.);vec3 q=uR*(p-pv)+pv;q.y+=uV.z;vN=uR*aN;vE=vec3(0.,0.,6.)-q;vP=aP;vX=aX;vGap=gap;"
    + "float w=6.-q.z;gl_Position=vec4(q.x*uV.x,q.y*uV.y,-q.z/3.*w,w);}";
  var HOLO_FS = "varying vec3 vN,vE,vP;varying vec4 vX;varying float vGap;uniform vec3 uC,uC2;uniform mediump vec4 uM;uniform vec4 uS;uniform vec4 uL;"   // uS = time, voice level, blink, reduced motion; uL = gaze x, gaze y, thinking, lines per unit
    + "void main(){vec3 n=normalize(vN),v=normalize(vE);float nv=max(dot(n,v),0.);"
    + "vec3 L=normalize(vec3(-.5,.55,.67));float key=max(dot(n,L),0.),fill=max(dot(n,normalize(vec3(.85,.0,.3))),0.),sp=pow(max(dot(n,normalize(L+v)),0.),22.);"
    + "float fr=pow(1.-nv,2.4);float t=uS.x;"
    // the surface is drawn as stacked slices of light, like a 3D scan: bright where the key light falls
    + "float u=vP.y*uL.w;float aa=AAW(u);float ln=abs(fract(u)-.5);float line=1.-smoothstep(.11,.11+aa,ln);line=mix(line,.3,clamp(aa*1.6-.25,0.,1.));"
    + "float node=line*(1.-smoothstep(.0,.1,abs(fract(vX.z*40.+vX.y*7.)-.5)));"
    + "float lit=.07+1.2*pow(key,1.6)+.2*fill;float far=smoothstep(-.9,.5,vE.z-6.+1.);"
    + "vec3 col=uC*(.025+.11*pow(key,1.5))+vec3(.002,.008,.02)+uC*lit*line*.95+uC2*fill*(line*.4+.05)+vec3(.9,.98,1.)*(sp*(line*.7+.12)+node*(.05+.3*key));"
    + "float sy=mod(t*.33,1.)*4.4-2.8;float sweep=smoothstep(.2,0.,abs(vP.y-sy));col+=mix(uC,vec3(1.),.55)*sweep*(.22+.5*line)*(1.+uL.z);"
    + "col+=mix(uC,vec3(.85,.97,1.),.3)*fr*(.75+.9*uS.y);"
    + "float a=.5+.5*clamp(line*lit+fr,0.,1.);"
    + "if(vX.w<.5){"
    // eyes: lids, lit iris ring and pupil, drawn on the surface
    + "vec2 e=vec2((abs(vP.x)-.255)/.112,(vP.y-.043)/.05);float lid=(1.-e.x*e.x)*(1.-uS.z);float on=step(abs(e.x),1.)*step(.2,vX.x);float inEye=on*smoothstep(0.,.22,lid-abs(e.y));"
    + "vec2 ic=vec2(abs(vP.x)-.255-sign(vP.x)*uL.x,vP.y-.045-uL.y);float r=length(ic);"
    + "vec3 eye=uC*.1+vec3(.01,.03,.05);eye+=mix(uC,vec3(1.),.2)*(smoothstep(.056,.046,r)*smoothstep(.016,.034,r)*(.75+.5*uS.y)+smoothstep(.056,.05,r)*smoothstep(.044,.05,r)*.7);"
    + "eye+=vec3(1.)*smoothstep(.011,.0,length(ic-vec2(-.018,.018)))*.8;"
    + "col=mix(col,eye,inEye);a=mix(a,1.,inEye);col+=mix(uC,vec3(1.),.3)*smoothstep(.22,.0,abs(lid-abs(e.y)))*on*(1.-inEye)*.55;"
    // lips a little brighter; the ring of triangles across the lip line stretches when the jaw drops: shade it as the inside of the mouth
    + "float hw=.2*(1.+.12*uM.y-.3*uM.z);float lp=exp(-pow((vP.y+.535)/.06,2.))*smoothstep(1.,.5,abs(vP.x)/hw)*step(.2,vX.x);col+=uC2*lp*line*.3;"
    + "float inM=step(.002,vGap)*step(vGap,.998)*smoothstep(0.,.25,1.-pow(abs(vP.x)/hw,2.))*smoothstep(.04,.2,uM.x);"
    + "vec3 mouth=uC*.02+uC*.6*smoothstep(.34,.1,vGap)*smoothstep(.02,.1,vGap)*smoothstep(.2,.45,uM.x)*step(abs(vP.x),hw*.72);col=mix(col,mouth,inM);a=mix(a,1.,inM);}"
    + "if(vX.w>.5){float f=smoothstep(-2.3,-1.25,vP.y);a*=f;col*=f;}"
    + "a*=.94+.06*sin(t*31.)*sin(t*7.3)*(1.-uS.w);gl_FragColor=vec4(col*a,a);}";
  var HOLO_DVS = "attribute vec4 aD;uniform vec4 uS;uniform vec3 uV;varying float vA;void main(){float t=uS.x*(1.-uS.w);float a=aD.x+t*(.06+.1*aD.w);float y=mod(aD.y+t*.05*(.4+aD.w)+1.9,3.2)-1.9;"
    + "vec3 q=vec3(sin(a)*aD.z,y+uV.z,cos(a)*aD.z);float w=6.-q.z;gl_Position=vec4(q.x*uV.x,q.y*uV.y,-q.z/3.*w,w);gl_PointSize=(1.2+2.6*aD.w)*uS.y;vA=(.25+.6*aD.w)*smoothstep(-1.9,-1.3,y)*smoothstep(1.3,.9,y);}";
  var HOLO_DFS = "precision mediump float;varying float vA;uniform vec3 uC;void main(){float d=length(gl_PointCoord-.5);float a=smoothstep(.5,.0,d)*vA;gl_FragColor=vec4(mix(uC,vec3(1.),.4)*a,a);}";
  function holoMount(box, get){
    var cv = document.createElement("canvas"), gl = null;
    cv.className = "hai-holo";
    try{ gl = cv.getContext("webgl", {alpha: true, antialias: true, premultipliedAlpha: true}); }catch(e){}
    if(!gl) return null;
    function prog(vs, fs){
      var p = gl.createProgram();
      [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]].forEach(function(s){ var o = gl.createShader(s[0]); gl.shaderSource(o, s[1]); gl.compileShader(o); if(!gl.getShaderParameter(o, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(o)); gl.attachShader(p, o); });
      gl.linkProgram(p); if(!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
      return p;
    }
    var G = holoGeo(), p1, p2;
    var dx = gl.getExtension("OES_standard_derivatives"), head = (dx ? "#extension GL_OES_standard_derivatives : enable\n#define AAW(x) fwidth(x)\n" : "#define AAW(x) .1\n") + "precision mediump float;";
    try{ p1 = prog(HOLO_VS, head + HOLO_FS); p2 = prog(HOLO_DVS, HOLO_DFS); }catch(e){ if(window.console) console.warn("HORIZON AI face:", e.message); return null; }
    function buf(data, kind){ var b = gl.createBuffer(); gl.bindBuffer(kind || gl.ARRAY_BUFFER, b); gl.bufferData(kind || gl.ARRAY_BUFFER, data, gl.STATIC_DRAW); return b; }
    var bP = buf(G.P), bN = buf(G.N), bX = buf(G.X), bI = buf(G.I, gl.ELEMENT_ARRAY_BUFFER), bD = buf(G.D);
    var U = function(p, n){ return gl.getUniformLocation(p, n); }, A = function(p, n, b, k){ var l = gl.getAttribLocation(p, n); if(l < 0) return; gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.enableVertexAttribArray(l); gl.vertexAttribPointer(l, k, gl.FLOAT, false, 0, 0); };
    var still = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
    var col = [.25, .83, 1], t0 = performance.now(), last = 0, blinkAt = 2.5, glAt = 6, raf = 0, dead = false;
    var TINT = [[.25, .83, 1], [.25, .83, 1], [.63, .48, 1], [.18, .91, .63]];        // idle and speaking cyan, thinking violet, listening green
    function draw(now){
      raf = 0;
      if(dead) return;
      if(!cv.isConnected){ stop(); return; }
      raf = requestAnimationFrame(draw);
      if(!cv.offsetParent || document.hidden) return;                    // panel minimised or tab hidden: draw nothing
      var s = get() || {}, t = (now - t0) / 1000, dt = Math.min(.1, t - last); last = t;
      var w = Math.round(cv.clientWidth * Math.min(2, window.devicePixelRatio || 1)), h = Math.round(cv.clientHeight * Math.min(2, window.devicePixelRatio || 1));
      if(!w || !h) return;
      if(cv.width !== w || cv.height !== h){ cv.width = w; cv.height = h; }
      var mode = s.mode || 0, tc = TINT[mode] || TINT[0], k = 1 - Math.exp(-dt * 5), i;
      for(i = 0; i < 3; i++) col[i] += (tc[i] - col[i]) * k;
      var mv = still ? 0 : 1, talk = mode === 1 ? 1 : 0;
      var yaw = mv * (.17 * Math.sin(t * .31) + .05 * Math.sin(t * .83)) + (mode === 2 ? .12 * Math.sin(t * 1.3) : 0), pitch = mv * (.035 * Math.sin(t * .47) + talk * .03 * Math.sin(t * 2.3)) + (mode === 3 ? -.05 : 0), roll = mv * .02 * Math.sin(t * .39);
      var cy = Math.cos(yaw), sy = Math.sin(yaw), cx = Math.cos(pitch), sx = Math.sin(pitch), cz = Math.cos(roll), sz = Math.sin(roll);
      var R = [cy * cz + sy * sx * sz, cx * sz, -sy * cz + cy * sx * sz, -cy * sz + sy * sx * cz, cx * cz, sy * sz + cy * sx * cz, sy * cx, -sx, cy * cx];   // column-major
      if(t > blinkAt) blinkAt = t + .16 + (t > blinkAt + .15 ? 2.2 + 3.5 * Math.random() : 0);
      var bl = still ? 0 : Math.max(0, 1 - Math.abs((blinkAt - t - .08) / .08)); if(blinkAt - t > .16) bl = 0;
      var glitch = 0; if(!still && t > glAt){ glitch = (Math.random() - .5) * 1.4; if(t > glAt + .09) glAt = t + 5 + 9 * Math.random(); }
      var asp = w / h, F = 4.05;
      gl.viewport(0, 0, w, h); gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.enable(gl.DEPTH_TEST); gl.enable(gl.CULL_FACE); gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.useProgram(p1); A(p1, "aP", bP, 3); A(p1, "aN", bN, 3); A(p1, "aX", bX, 4);
      gl.uniformMatrix3fv(U(p1, "uR"), false, R); gl.uniform4f(U(p1, "uM"), s.o || 0, s.w || 0, s.r || 0, 0);
      gl.uniform3f(U(p1, "uV"), F / asp, F, .36); gl.uniform2f(U(p1, "uG"), glitch, Math.sin(t * 91) * 1.2 - .3);
      gl.uniform3f(U(p1, "uC"), col[0], col[1], col[2]); gl.uniform3f(U(p1, "uC2"), .63, .48, 1);
      gl.uniform4f(U(p1, "uS"), still ? 0 : t, s.lv || 0, bl, still ? 1 : 0);
      gl.uniform4f(U(p1, "uL"), mv * (-yaw * .05 + .006 * Math.sin(t * .6)), mv * .004 * Math.sin(t * .9), mode === 2 ? 1 : 0, Math.max(14, Math.min(60, h / 2.96 / 5.2)));
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, bI); gl.drawElements(gl.TRIANGLES, G.I.length, gl.UNSIGNED_SHORT, 0);
      gl.useProgram(p2); A(p2, "aD", bD, 4); gl.depthMask(false);
      gl.uniform4f(U(p2, "uS"), t, h / 300, 0, still ? 1 : 0); gl.uniform3f(U(p2, "uV"), F / asp, F, .36); gl.uniform3f(U(p2, "uC"), col[0], col[1], col[2]);
      gl.drawArrays(gl.POINTS, 0, G.D.length / 4); gl.depthMask(true);
    }
    function stop(){ dead = true; if(raf) cancelAnimationFrame(raf); var x = gl.getExtension("WEBGL_lose_context"); if(x) x.loseContext(); }
    box.appendChild(cv);
    raf = requestAnimationFrame(draw);
    return {stop: stop, canvas: cv};
  }
  function holoMini(){             // small still picture of the AI face for the "Ask HORIZON AI" button
    var l = "", y;
    for(y = 5; y < 44; y += 2.6) l += "M0 " + y.toFixed(1) + "H44";
    return '<svg class="hai-holomini" viewBox="0 0 44 44" aria-hidden="true"><defs><clipPath id="haiHm"><path d="M22 5.5c-6.6 0-10.4 4.8-10.4 11.6 0 6.9 4 12.7 10.4 12.7s10.4-5.8 10.4-12.7C32.4 10.3 28.6 5.500 22 5.500zM7 44c0-6.600 6.200-9.800 15-9.800S37 37.400 37 44z"/></clipPath></defs>'
      + '<rect width="44" height="44" fill="#051428"/><path d="' + l + '" clip-path="url(#haiHm)" stroke="#3fd3ff" stroke-width="1.15" fill="none"/>'
      + '<circle cx="18" cy="17.4" r="1.5" fill="#eafcff"/><circle cx="26" cy="17.4" r="1.5" fill="#eafcff"/></svg>';
  }
  function orbSVG(){
    return '<svg class="hai-orb" viewBox="0 0 200 200" aria-hidden="true"><defs><radialGradient id="haiCore" cx="50%" cy="50%" r="50%"><stop offset="0" stop-color="#eef9ff"/><stop offset=".35" stop-color="#3fd3ff"/><stop offset="1" stop-color="#1f8fff" stop-opacity="0"/></radialGradient></defs>'
      + '<rect width="200" height="200" fill="none"/>'
      + '<circle class="ring r1" cx="100" cy="100" r="74" fill="none" stroke="#3fd3ff" stroke-width="2" stroke-dasharray="6 10" opacity=".7"/>'
      + '<circle class="ring r2" cx="100" cy="100" r="60" fill="none" stroke="#a07bff" stroke-width="2" stroke-dasharray="30 14" opacity=".75"/>'
      + '<circle id="haiCoreC" cx="100" cy="100" r="34" fill="url(#haiCore)"/>'
      + '<circle id="haiWave" cx="100" cy="100" r="44" fill="none" stroke="#3fd3ff" stroke-width="2" opacity="0"/></svg>';
  }
  function photoHTML(){
    var src = store("photo");
    return src ? '<img id="haiImg" alt="" src="' + esc(src) + '">' : '<div class="hai-nophoto">' + esc(t("noPhoto")) + "</div>";
  }
  function faceHTML(mini){ return S.face === "orb" ? orbSVG() : S.face === "photo" ? photoHTML() : S.face === "holo" ? (mini ? holoMini() : "") : mini ? '<span class="hai-offmini" style="background-image:url(' + OFFICER_IMG + ')"></span>' : ""; }

  /* ---------------- icons ---------------- */
  var ICON = (function(){
    var i = function(d, extra){ return '<svg class="hai-i" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"' + (extra || "") + ">" + d + "</svg>"; };
    return {
      spark: i('<path d="M12 2.5c.6 4.6 2.9 6.9 7.5 7.5-4.6.6-6.9 2.9-7.5 7.5-.6-4.6-2.9-6.9-7.5-7.5 4.6-.6 6.9-2.9 7.5-7.5Z" fill="currentColor" stroke="none"/><path d="M19 15.5c.25 1.9 1.1 2.75 3 3-1.9.25-2.75 1.1-3 3-.25-1.9-1.1-2.75-3-3 1.9-.25 2.75-1.1 3-3Z" fill="currentColor" stroke="none" opacity=".7"/>'),
      min: i('<path d="M6 12h12"/>'), grow: i('<path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/>'), shrink: i('<path d="M20 10h-6V4M4 14h6v6M14 10l7-7M10 14l-7 7"/>'),
      replay: i('<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1"/><path d="M3.5 4v4.5H8"/>'),
      vol: i('<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/><path d="M15.5 9a4.2 4.2 0 0 1 0 6M18.3 6.5a8 8 0 0 1 0 11"/>'),
      mute: i('<path d="M4 9.5h3.5L12 5.5v13l-4.5-4H4z"/><path d="M16 9.5l5 5M21 9.5l-5 5"/>'),
      stop: i('<rect x="6.5" y="6.5" width="11" height="11" rx="2" fill="currentColor" stroke="none"/>'),
      face: i('<circle cx="12" cy="9" r="3.6"/><path d="M5.5 19.5c1.2-3.3 3.6-5 6.5-5s5.3 1.7 6.5 5"/>'),
      mic: i('<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/>'),
      send: i('<path d="M4 12 20 4l-4.5 16-3.8-6.7L4 12Z" fill="currentColor" stroke="none"/><path d="m11.7 13.3 4-4" stroke="#03203a"/>'),
      bulb: i('<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.6 10.8c.7.6 1.1 1.3 1.1 2.2h5c0-.9.4-1.6 1.1-2.2A6 6 0 0 0 12 3Z"/>')
    };
  })();

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
    el.innerHTML = '<header class="hai-h" id="haiDrag"><span class="hai-logo">' + ICON.spark + '</span>'
      + '<span class="hai-title"><b>HORIZON AI</b><small id="haiSub"></small></span><span class="hai-st" id="haiSt"><i></i><span></span></span>'
      + '<button type="button" class="hai-ib" id="haiBig"></button><button type="button" class="hai-ib" id="haiMin"></button></header>'
      + '<div class="hai-stage" id="haiStage" data-mode="idle"><div class="hai-face" id="haiFace"></div>'
      + '<span class="hai-hud" aria-hidden="true"><i></i><i></i><i></i><i></i></span>'
      + '<div class="hai-say" id="haiSay" role="status"><span class="hai-bars" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></span><span></span></div>'
      + '<select id="haiLang" aria-label="Language">' + LANGS.map(function(l){ return '<option value="' + l[0] + '">' + l[1] + "</option>"; }).join("") + "</select>"
      + '<span class="hai-mark" aria-hidden="true">HORIZON</span>'
      + '<div class="hai-ctl"><button type="button" class="hai-ib" id="haiReplay"></button><button type="button" class="hai-ib" id="haiMute"></button>'
      + '<button type="button" class="hai-ib" id="haiStop"></button><button type="button" class="hai-ib" id="haiFaceBtn" aria-expanded="false" aria-controls="haiFaces"></button></div>'
      + '<div class="hai-faces" id="haiFaces" role="group" hidden></div>'
      + '<div class="hai-note" id="haiNote"></div><span class="hai-horizon" aria-hidden="true"></span></div>'
      + '<div class="hai-log" id="haiLog" aria-live="polite"></div>'
      + '<div class="hai-sugg" id="haiSugg"></div>'
      + '<form class="hai-in" id="haiForm" autocomplete="off"><div class="hai-field"><input id="haiQ" maxlength="600">'
      + '<button type="button" class="hai-ib mic" id="haiMic"></button></div><button type="submit" class="hai-send" id="haiSend"></button></form>'
      + '<div class="hai-foot" id="haiFoot"></div>';
    $("haiLang").value = S.lang;
    wire(); words(); drawFace(); renderLog(); suggestions(); status();
  }
  function $(id){ return document.getElementById(id); }
  function words(){
    var ib = function(id, icon, label, pressed){ var x = $(id); x.innerHTML = icon; x.title = label; x.setAttribute("aria-label", label); if(pressed != null) x.setAttribute("aria-pressed", pressed ? "true" : "false"); };
    $("haiSub").textContent = t("sub");
    $("haiQ").placeholder = t("ph"); $("haiQ").setAttribute("aria-label", t("ph"));
    ib("haiSend", ICON.send, t("send"));
    ib("haiMic", ICON.mic, SR ? t("mic") : t("micNo")); $("haiMic").disabled = !SR;
    ib("haiReplay", ICON.replay, t("replay"));
    ib("haiMute", S.mute ? ICON.mute : ICON.vol, S.mute ? t("unmute") : t("mute"), S.mute);
    ib("haiStop", ICON.stop, t("stop"));
    ib("haiFaceBtn", ICON.face, t("faceBtn"));
    ib("haiMin", ICON.min, t("min"));
    ib("haiBig", S.big ? ICON.shrink : ICON.grow, S.big ? t("small") : t("big"));
    $("haiFoot").textContent = t("foot");
    $("haiFaces").innerHTML = ["agent", "holo", "orb", "photo"].map(function(f){ return '<button type="button" class="hai-opt" data-face="' + f + '" aria-pressed="' + (S.face === f) + '">' + esc(t("faces")[f]) + "</button>"; }).join("")
      + (S.face === "photo" ? '<label class="hai-opt" for="haiFile">' + esc(t("choose")) + '</label><input type="file" id="haiFile" accept="image/*" hidden><small>' + esc(t("photoNote")) + "</small>" : "");
    $("haiNote").textContent = S.voiceNote || "";
    say();
    fab.innerHTML = '<span class="mini">' + faceHTML(true) + "</span>" + esc(t("fab"));
    fab.hidden = S.open; el.hidden = !S.open;
  }
  var holo = null;
  function drawFace(){
    var f = $("haiFace");
    if(holo){ holo.stop(); holo = null; }
    f.innerHTML = faceHTML(); f.classList.toggle("tall", S.face === "agent" || S.face === "holo"); f.classList.toggle("holo", S.face === "holo");
    var live = function(){ return {o: S.mo || 0, w: S.mw || 0, r: S.mr || 0, lv: S.level || 0, mode: S.listening ? 3 : busy ? 2 : S.speaking ? 1 : 0}; };
    if(S.face === "agent") holo = officer3dMount(f, live);                                   // 3D model; the animated portrait until it loads or if it cannot
    if(S.face === "holo"){
      holo = holoMount(f, live);
      if(!holo){ f.innerHTML = orbSVG(); f.classList.remove("tall", "holo"); }             // no WebGL on this computer: show the Orb
    }
  }
  function say(){
    var s = $("haiSay"), st = $("haiStage"); if(!s) return;
    var mode = S.listening ? "listen" : S.speaking ? "speaking" : busy ? "thinking" : "idle";
    s.classList.toggle("on", mode !== "idle"); s.lastChild.textContent = t(mode);
    if(st) st.dataset.mode = mode;
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
    g.innerHTML = LOG.map(function(m, i){
      if(m.kind === "sys") return '<div class="hai-m sys">' + m.html + "</div>";
      return '<div class="hai-row ' + m.kind + (i === LOG.length - 1 && !m.hello ? " new" : "") + '"><div class="hai-who"><b>' + (m.kind === "u" ? esc(t("you")) : "HORIZON AI") + "</b><time>" + esc(m.time) + "</time></div>"
        + '<div class="hai-m ' + m.kind + '">' + m.html + "</div></div>";
    }).join("");
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
    g.innerHTML = '<span class="hai-bulb">' + ICON.bulb + "</span>" + q.map(function(x){ return '<button type="button" class="hai-chip" title="' + esc(x) + '">' + esc(x) + "</button>"; }).join("");
  }

  /* ---------------- asking ---------------- */
  var busy = false;
  function ask(q){
    q = String(q || "").trim(); if(!q || busy) return;
    stopSpeaking();
    busy = true; $("haiQ").value = ""; say();
    addMsg("u", "<p>" + esc(q) + "</p>");
    var hist = S.history.slice(-6);
    LOG.push({kind: "a", html: '<span class="hai-typing" aria-label="' + esc(t("think")) + '"><i></i><i></i><i></i></span> ' + esc(t("think")), time: clock(), wait: true}); renderLog();
    var done = function(ok, text, meta){
      LOG = LOG.filter(function(m){ return !m.wait; });
      busy = false; say();
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
    var v = pickVoice(); if($("haiNote")) $("haiNote").textContent = S.voiceNote || "";
    if(!v && synth.getVoices().length) return;
    // Chrome stops long utterances after ~15 s, so speak sentence by sentence
    queue = plain(text).split(/(?<=[.!?。！？])\s+|\n+/).map(function(x){ return x.trim(); }).filter(Boolean);
    next();
  }
  function next(){
    if(!queue.length){ setSpeaking(false); return; }
    var u = new SpeechSynthesisUtterance(queue.shift()), v = pickVoice();
    if(v){ u.voice = v; u.lang = v.lang; } else u.lang = STT[S.lang];
    u.rate = S.lang === "en" ? 1.02 : 1; u.pitch = S.face === "agent" ? 1 : S.face === "holo" ? .96 : 1.05;
    u.onstart = function(){ S.cur = {text: u.text, t0: performance.now(), i0: 0}; setSpeaking(true); };
    u.onboundary = function(e){ S.kick = 1; if(S.cur && e && typeof e.charIndex === "number" && e.name !== "sentence"){ S.cur.i0 = e.charIndex; S.cur.t0 = performance.now(); } };
    u.onend = function(){ next(); };
    u.onerror = function(){ next(); };
    synth.speak(u);
  }
  function stopSpeaking(){ queue = []; if(synth){ try{ synth.cancel(); }catch(e){} } setSpeaking(false); }
  function setSpeaking(on){
    S.speaking = on; say();
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
    var f = $("haiStage"); if(f) f.style.setProperty("--lv", lv.toFixed(3));
    var v = viseme(lv), now = performance.now(), dt = Math.min(250, now - (S.mt || now)), a1 = 1 - Math.exp(-dt / 45), a2 = 1 - Math.exp(-dt / 60);
    S.mt = now;                                                     // smoothing by time, so a slow PC shows the same shapes
    S.mo = (S.mo || 0) + (v[0] - (S.mo || 0)) * a1; S.mw = (S.mw || 0) + (v[1] - (S.mw || 0)) * a2; S.mr = (S.mr || 0) + (v[2] - (S.mr || 0)) * a2;
    if(!S.speaking && lv <= .011){ S.mo = S.mw = S.mr = 0; }        // the Officer and AI faces read S.mo / S.mw / S.mr on their own frames
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
    rec.onstart = function(){ S.listening = true; $("haiMic").classList.add("on"); say(); };
    rec.onend = function(){ S.listening = false; var m = $("haiMic"); if(m) m.classList.remove("on"); say(); if(final.trim()) ask(final); };
    rec.onerror = function(){ S.listening = false; say(); };
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
    $("haiLang").addEventListener("change", function(){ S.lang = this.value; store("lang", S.lang); stopSpeaking(); LOG = LOG.filter(function(m){ return !m.hello; }); words(); renderLog(); suggestions(); pickVoice(); if($("haiNote")) $("haiNote").textContent = S.voiceNote || ""; status(); });
    var menu = function(open){ $("haiFaces").hidden = !open; $("haiFaceBtn").setAttribute("aria-expanded", open ? "true" : "false"); };
    $("haiFaceBtn").addEventListener("click", function(e){ e.stopPropagation(); menu($("haiFaces").hidden); });
    el.addEventListener("click", function(e){ if(!e.target.closest("#haiFaces,#haiFaceBtn")) menu(false); });
    $("haiFaces").addEventListener("click", function(e){ var b = e.target.closest("[data-face]"); if(!b) return; S.face = b.dataset.face; store("face", S.face); drawFace(); words(); menu(S.face === "photo"); });
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
  addEventListener("keydown", function(e){ if(e.key !== "Escape") return; var m = $("haiFaces"); if(m && !m.hidden){ m.hidden = true; $("haiFaceBtn").setAttribute("aria-expanded", "false"); $("haiFaceBtn").focus(); } else if(S.speaking) stopSpeaking(); });

  frame(); place();
  if(!window.HAI_DEMO) fetch("/api/assistant/status", {credentials: "same-origin"}).then(function(r){ return r.json(); }).then(function(j){ S.configured = !!j.configured; status(); if(!j.configured) addMsg("sys", esc(t("offMsg"))); }).catch(function(){});
  var sugWait = setInterval(function(){ if(pageData()){ suggestions(); clearInterval(sugWait); } }, 1500);   // first data from the Forecast Centre
  setTimeout(function(){ clearInterval(sugWait); }, 60000);
  setInterval(suggestions, 30000);
  window.HORIZON_AI = {ask: ask, speak: speak, stop: stopSpeaking, state: S, snapshot: snapshot, setFace: function(f){ S.face = f; store("face", f); drawFace(); words(); }, setLang: function(l){ $("haiLang").value = l; $("haiLang").dispatchEvent(new Event("change")); },
    voiceFor: function(lang, face){ return chooseVoice(lang, face || S.face); },
    faceSVG: function(f){ return f === "orb" ? orbSVG() : f === "holo" ? holoMini() : ""; }, setPhoto: setPhoto,
    open: function(on){ S.open = on !== false; store("open", S.open ? "1" : "0"); words(); }};
})();
