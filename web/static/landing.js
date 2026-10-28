/* RESOLVE landing page: raw-WebGL "causal ledger" constellation + live data from the local API.
   No external libraries; works offline. */
(function () {
  'use strict';

  /* ---------- 3D scene ---------- */
  var canvas = document.getElementById('scene');
  var hero = canvas && canvas.closest('.hero');
  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');

  function fallback(reason) {
    if (hero) hero.classList.add('no-webgl');
    if (canvas) { canvas.dataset.renderer = 'fallback'; canvas.dataset.reason = reason || ''; }
  }

  // Deterministic PRNG so the constellation is the same on every load.
  var seed = 7;
  function rnd() { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; }

  var COLORS = {
    intent: [0.96, 0.77, 0.42], entity: [0.49, 0.95, 0.81], version: [0.71, 0.61, 1.0],
    effect: [0.42, 0.89, 0.56], ledger: [0.55, 0.61, 1.0], ghost: [0.45, 0.5, 0.62]
  };

  function buildGraph() {
    var nodes = [], edges = [];
    function add(p, c, s) { nodes.push({ p: p, c: c, s: s }); return nodes.length - 1; }
    var CHAINS = 14, RING = 64, ringR = 2.55, ringY = -1.35;
    var ringIdx = [];
    for (var r = 0; r < RING; r++) {
      var a = r / RING * Math.PI * 2;
      ringIdx.push(add([Math.cos(a) * ringR, ringY + Math.sin(a * 3) * 0.04, Math.sin(a) * ringR], COLORS.ledger, 5));
      if (r > 0) edges.push([ringIdx[r - 1], ringIdx[r], 0.55]);
    }
    edges.push([ringIdx[RING - 1], ringIdx[0], 0.55]);
    var layers = [['intent', 0.55, 1.45, 15], ['entity', 1.15, 0.65, 12], ['version', 1.7, -0.05, 11], ['effect', 2.15, -0.7, 12]];
    for (var i = 0; i < CHAINS; i++) {
      var base = i / CHAINS * Math.PI * 2 + rnd() * 0.25;
      var prev = -1;
      for (var l = 0; l < layers.length; l++) {
        var L = layers[l], ang = base + l * 0.32;
        var rad = L[1] + (rnd() - 0.5) * 0.25, y = L[2] + (rnd() - 0.5) * 0.3;
        var idx = add([Math.cos(ang) * rad, y, Math.sin(ang) * rad], COLORS[L[0]], L[3]);
        if (prev >= 0) edges.push([prev, idx, 1]);
        // "two Jordan Lees": an ambiguous twin entity, faint and unbound
        if (L[0] === 'entity' && i % 4 === 0) {
          var twin = add([Math.cos(ang + 0.22) * (rad + 0.18), y + 0.18, Math.sin(ang + 0.22) * (rad + 0.18)], COLORS.ghost, 8);
          edges.push([prev, twin, 0.22]);
        }
        prev = idx;
      }
      edges.push([prev, ringIdx[Math.round((base + 0.96) / (Math.PI * 2) * RING) % RING], 0.6]);
    }
    // dust
    for (var d = 0; d < 160; d++) {
      var th = rnd() * Math.PI * 2, ph = Math.acos(2 * rnd() - 1), rr = 3.2 + rnd() * 3.5;
      add([Math.sin(ph) * Math.cos(th) * rr, Math.cos(ph) * rr * 0.6, Math.sin(ph) * Math.sin(th) * rr], [0.6, 0.66, 0.85], 2 + rnd() * 2.5);
    }
    return { nodes: nodes, edges: edges.filter(function (e) { return e[2] >= 0.5; }), faint: edges.filter(function (e) { return e[2] < 0.5; }) };
  }

  var VS_POINT = 'attribute vec3 aPos;attribute vec3 aCol;attribute float aSize;uniform mat4 uMVP;uniform float uScale;varying vec3 vCol;varying float vFade;' +
    'void main(){vec4 p=uMVP*vec4(aPos,1.0);gl_Position=p;gl_PointSize=clamp(aSize*uScale/p.w,1.0,64.0);vCol=aCol;vFade=clamp(1.35-(p.w-5.0)*0.18,0.25,1.0);}';
  var FS_POINT = 'precision mediump float;varying vec3 vCol;varying float vFade;uniform float uAlpha;' +
    'void main(){vec2 q=gl_PointCoord-0.5;float d=length(q)*2.0;if(d>1.0)discard;float core=smoothstep(0.35,0.0,d);float glow=exp(-d*d*5.0);' +
    'gl_FragColor=vec4(vCol*(glow+core*0.9),(glow*0.85+core)*vFade*uAlpha);}';
  var VS_LINE = 'attribute vec3 aPos;attribute vec3 aCol;uniform mat4 uMVP;varying vec3 vCol;varying float vFade;' +
    'void main(){vec4 p=uMVP*vec4(aPos,1.0);gl_Position=p;vCol=aCol;vFade=clamp(1.3-(p.w-5.0)*0.2,0.15,1.0);}';
  var FS_LINE = 'precision mediump float;varying vec3 vCol;varying float vFade;uniform float uAlpha;void main(){gl_FragColor=vec4(vCol,uAlpha*vFade);}';

  function compile(gl, vs, fs) {
    function sh(type, src) {
      var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    }
    var p = gl.createProgram();
    gl.attachShader(p, sh(gl.VERTEX_SHADER, vs)); gl.attachShader(p, sh(gl.FRAGMENT_SHADER, fs)); gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    return p;
  }

  function mul(a, b) { // column-major 4x4
    var o = new Float32Array(16);
    for (var c = 0; c < 4; c++) for (var r = 0; r < 4; r++) {
      o[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
    }
    return o;
  }
  function perspective(fovy, aspect, n, f) {
    var t = 1 / Math.tan(fovy / 2), o = new Float32Array(16);
    o[0] = t / aspect; o[5] = t; o[10] = (f + n) / (n - f); o[11] = -1; o[14] = 2 * f * n / (n - f); return o;
  }
  function rotY(a) { var c = Math.cos(a), s = Math.sin(a); return new Float32Array([c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0, 0, 0, 0, 1]); }
  function rotX(a) { var c = Math.cos(a), s = Math.sin(a); return new Float32Array([1, 0, 0, 0, 0, c, s, 0, 0, -s, c, 0, 0, 0, 0, 1]); }
  function translate(x, y, z) { return new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1]); }

  function startScene() {
    if (!canvas) return;
    var gl = null;
    try {
      var opts = { antialias: true, alpha: true, premultipliedAlpha: false, preserveDrawingBuffer: false };
      gl = canvas.getContext('webgl', opts) || canvas.getContext('experimental-webgl', opts);
    } catch (e) { gl = null; }
    if (!gl) return fallback('WebGL unavailable');

    var g = buildGraph(), progP, progL;
    try { progP = compile(gl, VS_POINT, FS_POINT); progL = compile(gl, VS_LINE, FS_LINE); }
    catch (e) { return fallback('shader error'); }

    function buf(data) { var b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW); return b; }
    var pts = new Float32Array(g.nodes.length * 7);
    g.nodes.forEach(function (n, i) { pts.set([n.p[0], n.p[1], n.p[2], n.c[0], n.c[1], n.c[2], n.s], i * 7); });
    var nodeBuf = buf(pts);
    function lineData(list) {
      var a = new Float32Array(list.length * 12);
      list.forEach(function (e, i) {
        var A = g.nodes[e[0]], B = g.nodes[e[1]];
        a.set([A.p[0], A.p[1], A.p[2], A.c[0], A.c[1], A.c[2], B.p[0], B.p[1], B.p[2], B.c[0], B.c[1], B.c[2]], i * 12);
      });
      return a;
    }
    var edgeBuf = buf(lineData(g.edges)), faintBuf = buf(lineData(g.faint));
    // pulses travel intent -> effect along chain edges (the causal direction)
    var chainEdges = g.edges.filter(function (e) { return e[2] === 1; });
    var PULSES = 26, pulseData = new Float32Array(PULSES * 7), pulseBuf = gl.createBuffer();
    var pulses = []; for (var i = 0; i < PULSES; i++) pulses.push({ e: Math.floor(rnd() * chainEdges.length), t: rnd(), v: 0.18 + rnd() * 0.25 });

    var loc = {
      P: { pos: gl.getAttribLocation(progP, 'aPos'), col: gl.getAttribLocation(progP, 'aCol'), size: gl.getAttribLocation(progP, 'aSize'), mvp: gl.getUniformLocation(progP, 'uMVP'), scale: gl.getUniformLocation(progP, 'uScale'), alpha: gl.getUniformLocation(progP, 'uAlpha') },
      L: { pos: gl.getAttribLocation(progL, 'aPos'), col: gl.getAttribLocation(progL, 'aCol'), mvp: gl.getUniformLocation(progL, 'uMVP'), alpha: gl.getUniformLocation(progL, 'uAlpha') }
    };

    var mouse = { x: 0, y: 0, tx: 0, ty: 0 }, dpr = 1, W = 1, H = 1;
    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      W = Math.max(1, Math.floor(canvas.clientWidth * dpr)); H = Math.max(1, Math.floor(canvas.clientHeight * dpr));
      if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
      gl.viewport(0, 0, W, H);
    }
    window.addEventListener('pointermove', function (e) {
      mouse.tx = (e.clientX / window.innerWidth - 0.5) * 2; mouse.ty = (e.clientY / window.innerHeight - 0.5) * 2;
    }, { passive: true });

    function drawPoints(buffer, count, mvp, scale, alpha) {
      gl.useProgram(progP);
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.enableVertexAttribArray(loc.P.pos); gl.vertexAttribPointer(loc.P.pos, 3, gl.FLOAT, false, 28, 0);
      gl.enableVertexAttribArray(loc.P.col); gl.vertexAttribPointer(loc.P.col, 3, gl.FLOAT, false, 28, 12);
      gl.enableVertexAttribArray(loc.P.size); gl.vertexAttribPointer(loc.P.size, 1, gl.FLOAT, false, 28, 24);
      gl.uniformMatrix4fv(loc.P.mvp, false, mvp); gl.uniform1f(loc.P.scale, scale); gl.uniform1f(loc.P.alpha, alpha);
      gl.drawArrays(gl.POINTS, 0, count);
    }
    function drawLines(buffer, count, mvp, alpha) {
      gl.useProgram(progL);
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.enableVertexAttribArray(loc.L.pos); gl.vertexAttribPointer(loc.L.pos, 3, gl.FLOAT, false, 24, 0);
      gl.enableVertexAttribArray(loc.L.col); gl.vertexAttribPointer(loc.L.col, 3, gl.FLOAT, false, 24, 12);
      gl.uniformMatrix4fv(loc.L.mvp, false, mvp); gl.uniform1f(loc.L.alpha, alpha);
      gl.drawArrays(gl.LINES, 0, count);
    }

    var t0 = performance.now(), last = t0, raf = 0, lost = false;
    function frame(now) {
      if (lost) return;
      var dt = Math.min(0.05, (now - last) / 1000); last = now;
      var t = (now - t0) / 1000;
      resize();
      mouse.x += (mouse.tx - mouse.x) * 0.04; mouse.y += (mouse.ty - mouse.y) * 0.04;
      var aspect = W / H, wide = aspect > 1.15;
      var model = mul(rotX(0.42 + mouse.y * 0.12), rotY(t * 0.07 + mouse.x * 0.35));
      var view = translate(wide ? 1.9 : 0, wide ? 0.05 : 0.6, wide ? -7.6 : -9.4);
      var mvp = mul(perspective(0.78, aspect, 0.1, 60), mul(view, model));
      gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
      gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
      drawLines(faintBuf, g.faint.length * 2, mvp, 0.3);
      drawLines(edgeBuf, g.edges.length * 2, mvp, 0.75);
      var scale = Math.max(H / 900, 0.7) * 34;
      drawPoints(nodeBuf, g.nodes.length, mvp, scale, 0.95);
      // pulses
      for (var i = 0; i < PULSES; i++) {
        var p = pulses[i]; p.t += p.v * dt;
        if (p.t > 1) { p.t = 0; p.e = Math.floor(rnd() * chainEdges.length); }
        var e = chainEdges[p.e], A = g.nodes[e[0]].p, B = g.nodes[e[1]].p, C = g.nodes[e[1]].c;
        pulseData.set([A[0] + (B[0] - A[0]) * p.t, A[1] + (B[1] - A[1]) * p.t, A[2] + (B[2] - A[2]) * p.t, C[0], C[1], C[2], 7], i * 7);
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, pulseBuf); gl.bufferData(gl.ARRAY_BUFFER, pulseData, gl.DYNAMIC_DRAW);
      drawPoints(pulseBuf, PULSES, mvp, scale, 1.0);
      canvas.dataset.frames = String((+canvas.dataset.frames || 0) + 1);
      if (!(reduceMotion && reduceMotion.matches) && !document.hidden) raf = requestAnimationFrame(frame);
      else raf = 0;
    }
    function kick() { if (!raf && !lost) raf = requestAnimationFrame(frame); }
    canvas.addEventListener('webglcontextlost', function (e) { e.preventDefault(); lost = true; fallback('context lost'); });
    document.addEventListener('visibilitychange', kick);
    window.addEventListener('resize', kick);
    if (reduceMotion && reduceMotion.addEventListener) reduceMotion.addEventListener('change', kick);
    canvas.dataset.renderer = 'webgl';
    canvas.dataset.motion = reduceMotion && reduceMotion.matches ? 'reduced' : 'animated';
    kick();
  }

  try { startScene(); } catch (e) { fallback('scene error'); }

  /* ---------- live data ---------- */
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function getJSON(path) {
    return fetch(path, { headers: { Accept: 'application/json' } }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) throw new Error((d && d.error) || ('HTTP ' + r.status));
        return d;
      });
    });
  }

  var health = document.getElementById('health-chip');
  getJSON('/api/health').then(function (h) {
    health.textContent = (h.status === 'ok' ? 'online' : esc(h.status)) + (h.version ? ' · v' + h.version : '');
    health.className = 'chip ' + (h.status === 'ok' ? 'chip-ok' : 'chip-warn');
  }).catch(function () { health.textContent = 'unavailable'; health.className = 'chip chip-warn'; });

  var POLICY_INFO = {
    A_naive: 'Baseline: first name match, first report, trusts the app response, retries on silence.',
    B_target_guard: 'Baseline: asks which contact and picks the newest report, but no version-bound approval or read-only reconciliation.',
    C_ieec_csl: 'RESOLVE: IEEC binding + Causal State Ledger, driven through the browser.'
  };
  var table = document.getElementById('bench-table'), body = document.getElementById('bench-body'), note = document.getElementById('bench-note');
  getJSON('/api/web/benchmark').then(function (b) {
    var per = b && b.per_policy;
    if (!per || typeof per !== 'object' || !Object.keys(per).length) throw new Error('no per-policy data');
    body.innerHTML = Object.keys(per).map(function (name) {
      var p = per[name] || {}, runs = Number(p.runs), bad = Number(p.unsafe_or_false);
      var hasNums = isFinite(runs) && isFinite(bad) && runs > 0;
      var pct = hasNums ? Math.round(bad / runs * 100) : null;
      var wi = Array.isArray(p.unsafe_or_false_wilson95) ? p.unsafe_or_false_wilson95.map(function (x) { return (x * 100).toFixed(1) + '%'; }).join(' – ') : 'unavailable';
      var verdicts = p.verdict_counts ? Object.keys(p.verdict_counts).map(function (k) { return '<span>' + esc(k.replace(/_/g, ' ')) + ' ' + esc(p.verdict_counts[k]) + '</span>'; }).join('') : '<span>unavailable</span>';
      return '<tr><td class="policy">' + esc(name) + '</td><td>' + esc(POLICY_INFO[name] || '') + '</td>' +
        '<td class="num">' + (isFinite(runs) ? esc(runs) : 'unavailable') + '</td>' +
        '<td class="num">' + (hasNums ? esc(bad) + ' / ' + esc(runs) + ' <span class="muted">(' + pct + '%)</span><div class="bar" aria-hidden="true"><span class="' + (bad === 0 ? 'good' : '') + '" style="width:' + Math.max(pct, 2) + '%"></span></div>' : 'unavailable') + '</td>' +
        '<td>' + esc(wi) + '</td><td><div class="verdicts">' + verdicts + '</div></td></tr>';
    }).join('');
    note.textContent = 'Note from the recorded run: ' + (b.note || 'none recorded') + (b.seeds ? ' Seeds: ' + b.seeds.join(', ') + '.' : '');
    table.dataset.state = 'loaded';
  }).catch(function (e) {
    body.innerHTML = '<tr><td colspan="6"><span class="unavailable">Benchmark data unavailable</span> <span class="muted">(' + esc(e.message) + '). Run <code>python benchmarks/web_bench.py</code> to record a result.</span></td></tr>';
    note.textContent = 'Note: unavailable.';
    table.dataset.state = 'unavailable';
  });
})();
