"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const canvas = $("desktop"), ctx = canvas.getContext("2d", {alpha:false});
  let socket = null, epoch = 0, scene = 0, canControl = false, controlling = false;
  let switching = false, selected = 0, requestId = 0, lastError = "", latestMove = null;
  let drawn = 0, lastMetricAt = performance.now(), displayedFps = 0;
  const keys = new Map();
  const status = message => { $("status").textContent = message; };
  let modeChoice = "auto", fallbackTried = false, fallbackPending = false, userStopped = false, reconnectTimer = null;
  const video = new window.BriocheVideo({draw:paint, failure:fallback, switching:()=>switching});
  const rawToken = location.hash.slice(1);
  if (/^[A-Za-z0-9_-]{43}$/.test(rawToken)) $("token").value = rawToken;
  // Keep credentials out of subsequent navigation/referrers and persistent storage.
  if (location.hash) history.replaceState(null, "", location.pathname);

  function send(value) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return false;
    if (socket.bufferedAmount > 65536) {
      lastError = "网络发送拥塞，请重新连接";
      socket.close();
      return false;
    }
    socket.send(JSON.stringify(value));
    return true;
  }
  function input(event) {
    if (!controlling || switching) return;
    send({type:"input", stream_id:epoch, ...event});
  }
  function release() {
    controlling = false;
    latestMove = null;
    keys.clear();
    canvas.classList.remove("controlling");
    $("control").textContent = "启用控制";
    $("permission").textContent = canControl ? "可控制 · 尚未启用" : "仅查看";
    send({type:"input", event:"release"});
  }
  function overlay(message) {
    $("overlay").textContent = message;
    $("overlay").hidden = false;
  }
  function configure(info) {
    release();
    epoch = info.stream_id;
    video.reset(info, socket);
    selected = info.monitor;
    switching = false;
    scene++;
    canvas.width = info.width;
    canvas.height = info.height;
    ctx.fillStyle = "#171714";
    ctx.fillRect(0,0,canvas.width,canvas.height);
    canControl = !!info.input;
    $("control").disabled = !canControl;
    $("permission").textContent = canControl ? "可控制 · 尚未启用" : "仅查看";
    $("monitors").replaceChildren(...info.monitors.map(m => {
      const option = document.createElement("option");
      option.value = String(m.id);
      option.textContent = `显示器 ${m.id} · ${m.width} × ${m.height}${m.height > m.width ? " · 竖屏" : ""}`;
      return option;
    }));
    $("monitors").value = String(selected);
    $("monitors").disabled = info.monitors.length < 2;
    $("welcome").hidden = true;
    $("session").hidden = false;
    drawn = 0;
    lastMetricAt = performance.now();
    $("metrics").textContent = video.label() + " · 等待画面";
    overlay("正在等待画面…");
    status(canControl ? "已连接。点击“启用控制”后操作，Ctrl + Alt + Shift 释放输入。" : "已连接 · 被控端仅允许查看画面");
  }
  function paint(image) {
    ctx.drawImage(image,0,0,canvas.width,canvas.height);
    $("overlay").hidden = true;
    drawn++;
    const now = performance.now();
    if (now-lastMetricAt >= 1000) {
      displayedFps = drawn*1000/(now-lastMetricAt);
      drawn = 0; lastMetricAt = now;
    }
    $("metrics").textContent = `${canvas.width} × ${canvas.height} · 画布提交 ${displayedFps?displayedFps.toFixed(1):"—"} FPS · ${video.label()}`;
  }
  function fallback(message, channel) {
    if (channel !== socket || userStopped) return;
    if (video.codec === "h264" && modeChoice === "auto" && !fallbackTried) {
      fallbackTried = true; fallbackPending = true;
      status("当前 H.264 解码不可用，正在切换兼容模式…");
      release(); video.close(); channel.close();
    } else {
      lastError = message + "，请选择兼容模式或使用 Brioche 客户端";
      release(); video.close(); channel?.close();
    }
  }
  function connectionControls(disabled) {
    $("connect").disabled = disabled;
    $("quality").disabled = disabled;
    $("codec").disabled = disabled;
    $("fps").disabled = disabled || $("codec").value === "mjpeg";
  }
  async function startConnection(forceCompat=false, fresh=true) {
    if (socket && socket.readyState < WebSocket.CLOSING) return;
    if (fresh) {
      clearTimeout(reconnectTimer); userStopped=false;
      fallbackTried=false; fallbackPending=false; modeChoice=$("codec").value;
    }
    const token=$("token").value.trim();
    if (!/^[A-Za-z0-9_-]{43}$/.test(token)) { status("请使用完整浏览器链接或有效访问码。"); return; }
    if (!window.createImageBitmap || !window.WebSocket) { status("请使用现代浏览器。"); return; }
    connectionControls(true);
    let wire="mjpeg";
    if (!forceCompat && modeChoice !== "mjpeg") {
      const supported=await window.BriocheVideo.supportsH264();
      if (userStopped) { connectionControls(false); return; }
      if (supported) wire="h264";
      else if (modeChoice === "h264") {
        status("此浏览器不支持 H.264 WebCodecs，请选择自动或兼容模式。");
        connectionControls(false); return;
      }
    }
    const fps=wire === "h264" ? Number($("fps").value) : 30;
    lastError=""; scene++;
    const channel=new WebSocket(`wss://${location.host}/ws`);
    socket=channel; channel.binaryType="arraybuffer";
    status(wire === "h264" ? "正在建立 H.264 视频连接…" : "正在建立兼容模式连接（最高 30 FPS）…");
    channel.onopen=()=>channel.send(JSON.stringify({type:"auth",token,codec:wire,
      config:{resolution:$("quality").value,fps,bitrate_mbps:20}}));
    channel.onmessage=event=>{
      if (socket !== channel) return;
      if (event.data instanceof ArrayBuffer) { void video.receive(event.data,channel); return; }
      try {
        const message=JSON.parse(event.data);
        if (message.type === "welcome" || message.type === "stream") configure(message);
        else if (message.type === "video_config") video.configure(message,channel);
        else if (message.type === "monitor_error" && message.request === requestId) {
          switching=false; $("monitors").value=String(selected);
          $("monitors").disabled=$("monitors").options.length<2;
          $("control").disabled=!canControl; status(message.message);
        } else if (message.type === "error") {
          if(message.code === "video_unavailable" && wire === "h264") fallback(message.message,channel);
          else { lastError=message.message; status(lastError); }
        }
      } catch (_) { lastError="连接响应异常，请重新连接"; channel.close(); }
    };
    channel.onerror=()=>{ lastError ||= "无法连接，请确认共享状态和本机 HTTPS 证书。"; };
    channel.onclose=()=>{
      if (socket !== channel) return;
      release(); video.close(); canControl=false; switching=false; scene++;
      $("control").disabled=true; $("monitors").disabled=true;
      if (fallbackPending && !userStopped) {
        fallbackPending=false;
        reconnectTimer=setTimeout(()=>{if(!userStopped) void startConnection(true,false);},100);
        return;
      }
      $("welcome").hidden=false; $("session").hidden=true;
      connectionControls(false); status(lastError || "已断开，可以重新连接。");
    };
  }
  $("connect").addEventListener("click",()=>{void startConnection();});
  $("codec").addEventListener("change",()=>{
    if($("codec").value === "mjpeg") $("fps").value="30";
    $("fps").disabled=$("codec").value === "mjpeg";
  });
  $("disconnect").addEventListener("click", () => { userStopped=true; fallbackPending=false; clearTimeout(reconnectTimer); release(); send({type:"bye"}); video.close(); socket?.close(); });
  $("release").addEventListener("click", release);
  $("control").addEventListener("click", () => {
    if (controlling) { release(); return; }
    if (!canControl || switching) return;
    controlling = true;
    canvas.classList.add("controlling");
    $("control").textContent = "关闭控制";
    $("permission").textContent = "控制已启用";
    canvas.focus({preventScroll:true});
  });
  $("fullscreen").addEventListener("click", async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await $("stage").requestFullscreen();
      if (controlling) canvas.focus({preventScroll:true});
    } catch (_) { status("当前浏览器不支持全屏，请使用浏览器自带的全屏功能。"); }
  });
  $("monitors").addEventListener("change", () => {
    const target = Number($("monitors").value);
    if (target === selected || switching) return;
    release();
    switching = true;
    scene++;
    $("monitors").disabled = true;
    $("control").disabled = true;
    overlay(`正在切换到显示器 ${target}…`);
    send({type:"switch_monitor", monitor:target, request:++requestId});
  });
  function point(event, clamp=false) {
    const box=canvas.getBoundingClientRect(), scale=Math.min(box.width/canvas.width,box.height/canvas.height);
    const w=canvas.width*scale, h=canvas.height*scale;
    let x=(event.clientX-box.left-(box.width-w)/2)/w, y=(event.clientY-box.top-(box.height-h)/2)/h;
    if (!clamp && (x<0||x>1||y<0||y>1)) return null;
    return {event:"move",x:Math.max(0,Math.min(1,x)),y:Math.max(0,Math.min(1,y))};
  }
  const buttons=["left","middle","right"];
  canvas.addEventListener("pointermove", event => { if(controlling&&!switching) latestMove=point(event,true); });
  function pumpMove() {
    if (latestMove) { input(latestMove); latestMove=null; }
    requestAnimationFrame(pumpMove);
  }
  requestAnimationFrame(pumpMove);
  canvas.addEventListener("pointerdown", event => {
    if (!controlling || switching || !buttons[event.button]) return;
    const p=point(event); if(!p) return;
    event.preventDefault();
    canvas.focus({preventScroll:true});
    canvas.setPointerCapture(event.pointerId);
    latestMove=null; input(p); input({event:"button",button:buttons[event.button],down:true});
  });
  canvas.addEventListener("pointerup", event => {
    if(!controlling||!buttons[event.button]) return;
    latestMove=null; input(point(event,true)); input({event:"button",button:buttons[event.button],down:false});
    if(canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    event.preventDefault();
  });
  canvas.addEventListener("pointercancel", release);
  canvas.addEventListener("contextmenu", event => { if(controlling) event.preventDefault(); });
  canvas.addEventListener("wheel", event => {
    if(!controlling) return;
    event.preventDefault();
    input({event:"scroll",dy:-Math.sign(event.deltaY)*Math.max(1,Math.min(10,Math.round(Math.abs(event.deltaY)/100)))});
  }, {passive:false});
  const special={AltLeft:"alt_l",AltRight:"alt_r",ControlLeft:"ctrl_l",ControlRight:"ctrl_r",ShiftLeft:"shift_l",ShiftRight:"shift_r",MetaLeft:"cmd_l",MetaRight:"cmd_r",Backspace:"backspace",CapsLock:"caps_lock",Delete:"delete",ArrowDown:"down",End:"end",Enter:"enter",NumpadEnter:"enter",Escape:"esc",Home:"home",Insert:"insert",ArrowLeft:"left",PageDown:"page_down",PageUp:"page_up",ArrowRight:"right",Space:"space",Tab:"tab",ArrowUp:"up"};
  canvas.addEventListener("keydown", event => {
    if(!controlling) return;
    event.preventDefault();
    if(event.ctrlKey&&event.altKey&&event.shiftKey) { release(); return; }
    if(event.repeat||event.isComposing) return;
    let key=special[event.code];
    if(!key&&/^F([1-9]|1[0-2])$/.test(event.code)) key=event.code.toLowerCase();
    if(!key&&/^Key[A-Z]$/.test(event.code)) key=event.code.slice(3).toLowerCase();
    if(!key&&event.key.length===1&&event.key.charCodeAt(0)>=32) key=event.key;
    if(key) { keys.set(event.code,key); input({event:"key",key,down:true}); }
  });
  canvas.addEventListener("keyup", event => {
    const key=keys.get(event.code); keys.delete(event.code);
    if(key) { event.preventDefault(); input({event:"key",key,down:false}); }
  });
  canvas.addEventListener("blur", release);
  window.addEventListener("blur", release);
  document.addEventListener("visibilitychange", () => { if(document.hidden) release(); });
  window.addEventListener("pagehide", () => { userStopped=true; clearTimeout(reconnectTimer); release(); video.close(); socket?.close(); });
})();
