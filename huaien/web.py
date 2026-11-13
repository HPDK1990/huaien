"""怀恩 — 手机/局域网 Web 前台 (PWA)

## 这是什么

同一个怀恩, 换个皮: 浏览器打开就能用, 手机上"添加到主屏幕"跟原生 App 一样。

- 后端仍然调 Hermes (`hermes -p huaien chat`), **不另造智能体**
- 会话与桌面版**各是各的** (各自 session_id), 但走同一个 huaien profile → 记忆是通的

## 为什么要有它

用户要"手机上能跟这个管家说话"。真写原生 App 要签名/上架, 不现实。
PWA 是**能落地的手机软件**: 一个网页 + manifest + service worker, 加到主屏有图标。

## 安全 (重要)

这个接口能执行 agent 的能力 (等于把电脑交出去), 所以**必须有口令**:

- 启动时随机生成 16 位 token
- HTML 页面必须带 `?t=<token>` 才能打开
- API 调用必须带 `X-Huaien-Token` 头
- token 只存在进程内存, 不落盘

同网段的人猜不到 token, 但如果他把 URL 转发出去, 对方就能用。
**用户不要把带 token 的链接发给任何人。**

## 跑法

    python -m huaien.web                      # 默认 0.0.0.0:8765
    python -m huaien.web --port 9000
    python -m huaien.web --host 127.0.0.1   # 只本机
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import socket
import sys
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

from huaien.bridge import ChatBridge  # noqa: E402

DEFAULT_PORT = 8765
TOKEN_BYTES = 12   # 12 字节 -> 24 个十六进制字符 (secrets.token_hex)


def local_ip() -> str:
    """本机在局域网里的地址 (连不出网的网卡就不管)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))   # 阿里 DNS, 只用来问路由, 不发数据
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="怀恩">
<title>怀恩</title>
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/icon.svg">
<style>
  :root{
    --bg:#000; --fg:#F2F2F2; --dim:#9A9A9A; --faint:#7A7A7A;
    --accent:#7DF9C7; --accent-dim:#4E8C74; --err:#FF6B6B;
    --pad:22px;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{height:100%;margin:0}
  body{
    background:var(--bg); color:var(--fg);
    font:17px/1.62 "Microsoft YaHei UI","PingFang SC",system-ui,sans-serif;
    display:flex; flex-direction:column; overflow:hidden;
    padding:env(safe-area-inset-top) env(safe-area-inset-right)
            env(safe-area-inset-bottom) env(safe-area-inset-left);
  }
  #log{flex:1;overflow-y:auto;padding:var(--pad) var(--pad) 8px;-webkit-overflow-scrolling:touch}
  #log::-webkit-scrollbar{width:0}
  .u{color:var(--dim);font-size:16px;margin:0 0 14px;white-space:pre-wrap;word-break:break-word}
  .a{color:var(--fg);font-size:19px;margin:0 0 16px;white-space:pre-wrap;word-break:break-word}
  .st{color:var(--faint);font-size:13px;margin:-8px 0 16px;min-height:18px}
  .st.err{color:var(--err)}
  .arts{margin:-8px 0 16px}
  .art{color:var(--dim);font-size:13px;padding:3px 0;cursor:pointer}
  .art:active{color:var(--accent)}
  .art img{max-width:100%;border-radius:6px;margin:4px 0;display:block}
  #bar{border-top:1px solid var(--accent-dim);margin:0 var(--pad)}
  #row{display:flex;align-items:flex-end;gap:10px;padding:10px var(--pad) var(--pad)}
  #in{
    flex:1;background:var(--bg);color:var(--fg);border:0;outline:0;resize:none;
    font:17px/1.5 "Microsoft YaHei UI","PingFang SC",system-ui,sans-serif;
    padding:8px 0;max-height:130px;min-height:34px;
  }
  #send{
    background:var(--bg);border:1px solid var(--accent-dim);color:var(--accent);
    border-radius:999px;padding:7px 15px;font-size:15px;cursor:pointer;flex:none;
  }
  #send:disabled{opacity:.35}
  #mic{
    background:none;border:1px solid var(--accent-dim);color:var(--accent);
    border-radius:999px;padding:7px 12px;font-size:15px;cursor:pointer;flex:none;
  }
  #mic.on{background:var(--accent);color:#000;border-color:var(--accent)}
  #top{
    padding:calc(env(safe-area-inset-top) + 10px) var(--pad) 4px;
    color:var(--faint);font-size:12px;letter-spacing:.05em;
    display:flex;justify-content:space-between;align-items:center;
  }
  #top .dot{color:var(--err)}
  /* ---------- 管家提醒 (规则命中才有) ---------- */
  #alert{display:none;margin:0 var(--pad) 8px;border-left:2px solid var(--err);
         padding:7px 0 7px 11px}
  #alert.show{display:block}
  #alert .t{color:var(--err);font-size:13px;margin:0 0 4px}
  #alert .d{color:var(--dim);font-size:12px;margin:0;white-space:pre-wrap;
            word-break:break-word}
  /* ---------- 秘书台 ---------- */
  #desk{display:none;flex:1;overflow-y:auto;padding:6px var(--pad) 16px;
        -webkit-overflow-scrolling:touch}
  body.desk #log{display:none}
  body.desk #row{display:none}
  body.desk #desk{display:block}
  #tabs{display:flex;border-top:1px solid var(--accent-dim);background:var(--bg);
        padding-bottom:env(safe-area-inset-bottom)}
  #tabs button{flex:1;background:none;border:0;color:var(--faint);padding:10px 0;
        font-size:14px;letter-spacing:.1em;cursor:pointer}
  #tabs button.on{color:var(--accent)}
  .qrow{display:grid;grid-template-columns:1fr 1fr;gap:7px;padding:2px 0 10px}
  .qbtn{background:none;border:1px solid var(--accent-dim);color:var(--accent);
        border-radius:999px;padding:6px 13px;font-size:13px;cursor:pointer;white-space:nowrap}
  .qbtn:active{background:var(--accent-dim)}
  .sect{margin:15px 0 7px;color:var(--faint);font-size:12px;letter-spacing:.08em}
  .sect b{color:var(--fg);font-weight:normal;font-size:13px}
  .card{border-left:2px solid #2e2e2e;padding:6px 0 6px 10px;margin:0 0 8px}
  .card.need{border-left-color:var(--err)}
  .card.run{border-left-color:var(--accent)}
  .card.done{border-left-color:#242424;opacity:.5}
  .card .t{font-size:15px;color:var(--fg);margin:0 0 3px;line-height:1.4}
  .card .m{font-size:12px;color:var(--faint);margin:0}
  .card .m .who{color:var(--accent)}
  .card .m .who.need{color:var(--err)}
  .chips{margin:2px 0 4px}
  .chip{display:inline-block;border:1px solid var(--accent-dim);color:var(--accent);
        border-radius:999px;padding:2px 10px;font-size:12px;margin:0 6px 6px 0}
  .chip.zero{border-color:#2a2a2a;color:var(--faint)}
  #graph{display:flex;gap:9px;overflow-x:auto;padding-bottom:8px}
  .gcol{flex:none;width:118px}
  .glab{color:var(--faint);font-size:11px;margin:0 0 5px}
  .gcard{border:1px solid #2a2a2a;border-radius:6px;padding:6px 8px;margin-bottom:7px;
         font-size:12px;line-height:1.35;color:var(--fg)}
  .gcard .ga{color:var(--accent);font-size:11px;display:block;margin-top:2px}
  .gcard.done{opacity:.4}
  .gcard.run{border-color:var(--accent)}
  .gcard.need{border-color:var(--err)}
  .empty{color:var(--faint);font-size:13px;padding:4px 0 10px}
</style>
</head>
<body>
<div id="top"><span>怀恩 · 在</span><span id="topDot"></span></div>
<div id="alert"></div>
<div id="log"></div>
<div id="desk"></div>
<div id="bar"></div>
<div id="row">
  <button id="mic">语音</button>
  <textarea id="in" rows="1" placeholder="说点什么…" enterkeyhint="send"></textarea>
  <button id="send">发送</button>
</div>
<div id="tabs">
  <button id="tabChat" class="on">说话</button>
  <button id="tabDesk">台</button>
</div>
<script>
const TOKEN = "__TOKEN__";
const log = document.getElementById('log');
const inp = document.getElementById('in');
const btn = document.getElementById('send');
let busy = false;

function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}

function addUser(text){
  const p=document.createElement('p');p.className='u';p.textContent=text;log.appendChild(p);
}
function addStatus(text, isErr){
  let s=log.querySelector('.st:last-child');
  if(!s||s.dataset.done){ s=document.createElement('p');s.className='st';log.appendChild(s); }
  s.textContent=text; s.className='st'+(isErr?' err':'');
  if(!text) s.dataset.done='1';
  else delete s.dataset.done;
  log.scrollTop=log.scrollHeight;
  return s;
}
function addArts(list){
  if(!list||!list.length) return;
  const box=document.createElement('div');box.className='arts';
  list.forEach(a=>{
    const d=document.createElement('div');d.className='art';
    const name=(a.path.split(/[\\/]/).pop())||a.path;
    d.textContent='▸ '+name+(a.exists?'':' (不在)');
    d.onclick=()=>{ if(a.exists) location.href='/api/open?p='+encodeURIComponent(a.path); };
    box.appendChild(d);
    if(a.kind==='image'&&a.exists){
      const im=document.createElement('img');im.src='/api/file?p='+encodeURIComponent(a.path);
      im.onclick=()=>location.href='/api/open?p='+encodeURIComponent(a.path);
      box.appendChild(im);
    }
  });
  log.appendChild(box); log.scrollTop=log.scrollHeight;
}

async function send(){
  if(busy) return;
  const text=inp.value.trim();
  if(!text) return;
  busy=true; btn.disabled=true; inp.value=''; inp.style.height='auto';
  addUser(text);
  const st=addStatus('…');
  const t0=Date.now();
  const tick=setInterval(()=>{
    const s=Date.now()-t0;
    st.textContent = s>8000 ? ('还在想 · '+Math.round(s/1000)+' 秒') : '…';
  },500);
  try{
    const r=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json','X-Huaien-Token':TOKEN},
      body:JSON.stringify({message:text})});
    const j=await r.json();
    clearInterval(tick);
    if(j.error){ addStatus(j.error,true); }
    else{
      st.textContent='';
      const p=document.createElement('p');p.className='a';p.textContent=j.reply||'';log.appendChild(p);
      addArts(j.artifacts);
      log.scrollTop=log.scrollHeight;
    }
  }catch(e){
    clearInterval(tick);
    addStatus('连不上: '+e.message,true);
  }
  busy=false; btn.disabled=false; inp.focus();
}

btn.onclick=send;
inp.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){ e.preventDefault(); send(); }
});
inp.addEventListener('input',()=>{ inp.style.height='auto'; inp.style.height=Math.min(inp.scrollHeight,130)+'px'; });

/* ---------- 秘书台 ---------- */
const desk = document.getElementById('desk');
const tabChat = document.getElementById('tabChat');
const tabDesk = document.getElementById('tabDesk');
let boardData = null;
let doneOpen = false;   // 已完成是否展开

function showDesk(on){
  document.body.classList.toggle('desk', on);
  tabChat.classList.toggle('on', !on);
  tabDesk.classList.toggle('on', on);
  if(on) loadBoard();
}
tabChat.onclick=()=>showDesk(false);
tabDesk.onclick=()=>showDesk(true);

function statusClass(st){
  if(st==='blocked'||st==='review'||st==='triage') return 'need';
  if(st==='running'||st==='ready'||st==='todo'||st==='scheduled') return 'run';
  if(st==='done') return 'done';
  return '';
}
function cardHtml(c){
  const k=statusClass(c.status);
  const who='<span class="who '+(k==='need'?'need':'')+'">'+esc(c.assignee)+'</span>';
  let extra='';
  if(c.error) extra+=' <span class="who need">'+esc(c.error.slice(0,50))+'</span>';
  return '<div class="card '+k+'"><p class="t">'+esc(c.title)+'</p>'
       + '<p class="m">'+who+' · '+esc(c.status)+' · '+esc(c.id)+extra+'</p></div>';
}
function sectHtml(label,n){
  return '<div class="sect">'+label+(n!==''&&n!==null?' <b>'+n+'</b>':'')+'</div>';
}
function renderDesk(){
  const b=boardData; if(!b) return;
  let h='';
  // 快捷指令 —— 不用打字, 点一下直接读看板 (秒回, 不问模型)
  const QS=[['汇报进度','progress'],['待办清单','todo'],['谁在忙','busy'],['现在有什么活','live']];
  h+='<div class="qrow">';
  QS.forEach(function(q){ h+='<button class="qbtn" data-q="'+esc(q[0])+'" data-k="'+q[1]+'">'+esc(q[0])+'</button>'; });
  h+='</div>';
  if(!b.available){ h+='<p class="empty">看板暂时读不到</p>'; desk.innerHTML=h; return; }

  if(b.needs_you.length){
    h+=sectHtml('待你处理',b.needs_you.length);
    b.needs_you.forEach(c=>{ h+=cardHtml(c); });
  }
  if(b.in_flight.length){
    h+=sectHtml('进行中',b.in_flight.length);
    b.in_flight.forEach(c=>{ h+=cardHtml(c); });
  }
  if(!b.needs_you.length && !b.in_flight.length){
    h+='<p class="empty">没有要你处理的，也没有在跑的。</p>';
  }
  // Agent 负载
  h+=sectHtml('Agent 负载','');
  h+='<div class="chips">';
  if(b.agents.length){ b.agents.forEach(a=>{ h+='<span class="chip">'+esc(a.name)+' '+a.count+'</span>'; }); }
  else { h+='<span class="chip zero">没有 agent 在忙</span>'; }
  h+='</div>';
  // 编排图
  const g=b.graph;
  if(g && g.nodes && g.nodes.length){
    h+=sectHtml('编排',g.nodes.length+' 张卡');
    const cols={};
    g.nodes.forEach(n=>{ (cols[n.layer]=cols[n.layer]||[]).push(n); });
    h+='<div id="graph">';
    Object.keys(cols).map(Number).sort((a,b2)=>a-b2).forEach(L=>{
      h+='<div class="gcol"><p class="glab">第'+L+'层</p>';
      cols[L].forEach(n=>{
        h+='<div class="gcard '+statusClass(n.status)+'">'+esc(n.title.slice(0,28))
          +'<span class="ga">'+esc(n.assignee)+'</span></div>';
      });
      h+='</div>';
    });
    h+='</div>';
  }
  if(b.done && b.done.length){
    h+=sectHtml('已完成',b.done_total);
    // 默认只露 3 条, 免得一大屏全是历史
    const shown = doneOpen ? b.done : b.done.slice(0,3);
    shown.forEach(c=>{ h+=cardHtml(c); });
    if(b.done.length>3){
      h+='<button class="qbtn" id="doneMore">'+(doneOpen?'收起':'展开全部 '+b.done.length+' 件')+'</button>';
    }
  }
  desk.innerHTML=h;
}
async function loadBoard(){
  desk.innerHTML='<p class="empty">读看板…</p>';
  try{
    const r=await fetch('/api/board',{headers:{'X-Huaien-Token':TOKEN}});
    if(!r.ok){ desk.innerHTML='<p class="empty">读不到看板 ('+r.status+')</p>'; return; }
    boardData=await r.json();
    renderDesk();
  }catch(e){ desk.innerHTML='<p class="empty">连不上: '+esc(e.message)+'</p>'; }
}
// 快捷指令 = 直读看板, 不经过模型
// ⚠️ 实测: 把"汇报进度"发给模型要 80 秒起步 (它会自己查看板, 还会 spawn 子代理),
//    而 /api/quick 直接读 SQLite 是 1.5 毫秒。用户要的是"不用盯", 慢就是坏。
async function quickAsk(label,key){
  showDesk(false);
  addUser(label);
  const st=addStatus('…');
  try{
    const r=await fetch('/api/quick/'+encodeURIComponent(key),{headers:{'X-Huaien-Token':TOKEN}});
    const j=await r.json();
    st.textContent='';
    const p=document.createElement('p');p.className='a';p.textContent=j.text||j.error||'';log.appendChild(p);
    log.scrollTop=log.scrollHeight;
  }catch(e){
    st.textContent='查不到: '+e.message;
  }
}
// 点快捷指令 = 切回对话并自动发出去 (不用打字)
desk.addEventListener('click',e=>{
  if(e.target.id==='doneMore'){ doneOpen=!doneOpen; renderDesk(); return; }
  const q=e.target.closest('.qbtn');
  if(!q) return;
  const key=q.getAttribute('data-k');
  if(key){ quickAsk(q.getAttribute('data-q'), key); return; }
  showDesk(false);
  inp.value=q.getAttribute('data-q');
  send();
});

inp.focus();

/* ---------- 语音输入: 用户不想打字 ---------- */
const mic = document.getElementById('mic');
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
if(SR){
  const rec = new SR();
  rec.lang = 'zh-CN'; rec.continuous = false; rec.interimResults = true;
  let base = '';
  rec.onresult = function(e){
    let t = '';
    for(let i = e.resultIndex; i < e.results.length; i++) t += e.results[i][0].transcript;
    inp.value = base + t;
    inp.dispatchEvent(new Event('input'));
  };
  rec.onend = rec.onerror = function(){
    mic.classList.remove('on'); mic.textContent = '语音';
  };
  mic.onclick = function(){
    if(rec.state !== 'inactive'){ rec.stop(); return; }
    base = inp.value ? inp.value + ' ' : '';
    try{ rec.start(); mic.classList.add('on'); mic.textContent = '停止'; }
    catch(err){ mic.classList.remove('on'); mic.textContent = '语音'; }
  };
}else{
  mic.style.display = 'none';   // 浏览器不支持就别留个坏按钮
}

/* ---------- 管家提醒: 规则命中才有, 没命中一个字都不说 ---------- */
const alertBox = document.getElementById('alert');
const topDot = document.getElementById('topDot');
let butlerData = null;
function renderAlert(){
  const items = (butlerData && butlerData['待你处理']) || [];
  if(!items.length){
    alertBox.className = ''; alertBox.innerHTML = ''; topDot.textContent = '';
    return;
  }
  topDot.textContent = items.length + ' 件待处理';
  topDot.className = 'dot';
  let h = '<p class="t">管家提醒 · ' + items.length + ' 件事需要你看</p>';
  items.forEach(function(it){
    h += '<p class="d">[' + esc(it.severity) + '] ' + esc(it.rule) + '<br>'
       + esc(it.detail || '').replace(/\n/g, '<br>') + '</p>';
  });
  alertBox.innerHTML = h;
  alertBox.className = 'show';
}
async function loadButler(){
  try{
    const r = await fetch('/api/butler', {headers:{'X-Huaien-Token':TOKEN}});
    if(!r.ok) return;
    butlerData = await r.json();
    renderAlert();
  }catch(e){ /* 读不到就安静, 不打扰 */ }
}
function alertLoading(){
  alertBox.innerHTML = '<p class="t">管家提醒 · 看一下…</p>';
  alertBox.className = 'show';
}
alertLoading();
loadButler();
setInterval(loadButler, 60000);
alertBox.onclick = function(){ showDesk(true); };

// PWA: 注册 service worker (加到主屏后能离线打开外壳)
if('serviceWorker' in navigator){ navigator.serviceWorker.register('/sw.js').catch(()=>{}); }
</script>
</body>
</html>
"""

MANIFEST = {
    "name": "怀恩",
    "short_name": "怀恩",
    "description": "极简私人管家",
    "start_url": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#000000",
    "theme_color": "#000000",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml",
               "purpose": "any maskable"}],
}

ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">'
    '<rect width="192" height="192" fill="#000"/>'
    '<circle cx="96" cy="96" r="9" fill="#7DF9C7"/>'
    '</svg>'
)

SW_JS = """// 怀恩 PWA service worker
// 只缓存外壳, 不缓存 API —— 聊天内容必须实时
const CACHE = 'huaien-shell-v1';
const SHELL = ['/', '/manifest.json', '/icon.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(self.clients.claim());
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.pathname.startsWith('/api/')) return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
"""


class HuaienHandler(BaseHTTPRequestHandler):
    """处理 HTTP 请求。token 校验在这里。

    token / bridge 由 create_server 通过 functools.partial 注入 ——
    比动态造子类干净, 也比设类属性安全 (多实例不会互相污染)。
    """

    server_version = "HuaiEn/0.3"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, token: str = "", bridge: ChatBridge | None = None, **kwargs):
        self.token = token
        self.bridge = bridge
        super().__init__(*args, **kwargs)

    # ---------- 工具 ----------
    def log_message(self, fmt, *args):
        pass    # 别刷屏

    def _ok(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._ok(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                 "application/json; charset=utf-8")

    def _token_ok(self, query_token: str = "") -> bool:
        got = self.headers.get("X-Huaien-Token") or query_token
        return bool(got) and secrets.compare_digest(str(got), self.token)

    def _deny(self):
        self._ok(403, "口令不对。".encode(), "text/plain; charset=utf-8")

    # ---------- 路由 ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        qtok = (q.get("t") or [""])[0]

        if u.path == "/api/health":
            self._json({"ok": True, "token_required": True})
            return

        # ⚠️ /api/open 和 /api/file 能碰任意路径 —— token 是唯一的防线。
        # 所以 token 必须 16 位随机 (12 字节), 且绝不能转发这个链接。
        if u.path == "/api/open":
            if not self._token_ok(qtok):
                self._deny()
                return
            path = (q.get("p") or [""])[0]
            if path and os.path.exists(path):
                with contextlib.suppress(OSError):
                    os.startfile(path)       # noqa: S606
            self._json({"opened": bool(path and os.path.exists(path))})
            return

        if u.path == "/api/file":
            if not self._token_ok(qtok):
                self._deny()
                return
            path = (q.get("p") or [""])[0]
            if not path or not os.path.isfile(path):
                self._ok(404, b"not found", "text/plain; charset=utf-8")
                return
            ext = os.path.splitext(path)[1].lower()
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".gif": "image/gif", ".webp": "image/webp",
                     ".svg": "image/svg+xml", ".pdf": "application/pdf"}.get(ext,
                                                                        "application/octet-stream")
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                self._ok(404, b"not found", "text/plain; charset=utf-8")
                return
            self._ok(200, data, ctype)
            return

        if u.path == "/api/board":
            if not self._token_ok(qtok):
                self._deny()
                return
            from huaien import board as board_mod
            self._json(board_mod.board())
            return

        if u.path == "/api/board/summary":
            if not self._token_ok(qtok):
                self._deny()
                return
            from huaien import board as board_mod
            self._json({"text": board_mod.summary_text()})
            return

        # 管家面板: 规则命中的"需要你处理"的事 —— 用户要的是"不用盯"
        if u.path == "/api/butler":
            if not self._token_ok(qtok):
                self._deny()
                return
            from huaien import butler as butler_mod
            self._json(butler_mod.panel())
            return

        # 快捷指令: 直读看板, 不问模型 —— 要的就是秒回
        if u.path.startswith("/api/quick/"):
            if not self._token_ok(qtok):
                self._deny()
                return
            key = u.path.rsplit("/", 1)[-1]
            from huaien import board as board_mod
            self._json({"text": board_mod.quick_text(key)})
            return

        # 以下都要 token
        if not self._token_ok(qtok):
            self._deny()
            return

        if u.path in ("/", "/index.html"):
            body = PAGE.replace("__TOKEN__", self.token).encode("utf-8")
            self._ok(200, body, "text/html; charset=utf-8")
            return
        if u.path == "/manifest.json":
            self._ok(200, json.dumps(MANIFEST, ensure_ascii=False).encode("utf-8"),
                     "application/manifest+json; charset=utf-8")
            return
        if u.path == "/icon.svg":
            self._ok(200, ICON_SVG.encode("utf-8"), "image/svg+xml")
            return
        if u.path == "/sw.js":
            self._ok(200, SW_JS.encode("utf-8"), "application/javascript; charset=utf-8",
                     {"Service-Worker-Allowed": "/"})
            return
        self._ok(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/ask":
            self._ok(404, b"not found", "text/plain; charset=utf-8")
            return
        if not self._token_ok():
            self._deny()
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, OSError):
            self._json({"error": "请求体坏了"}, 400)
            return
        msg = (payload.get("message") or "").strip()
        if not msg:
            self._json({"error": "空的"}, 400)
            return
        if self.bridge is None:
            self._json({"error": "桥接没起来"}, 500)
            return
        try:
            reply, arts = self.bridge.ask_sync(msg, timeout=900)
            self._json({"reply": reply, "artifacts": [
                {"kind": a.kind, "path": a.path, "exists": a.exists} for a in arts]})
        except Exception as e:
            self._json({"error": str(e)}, 500)


def create_server(host: str = "0.0.0.0", port: int = DEFAULT_PORT,
                  token: str | None = None) -> tuple[ThreadingHTTPServer, str]:
    token = token or secrets.token_hex(TOKEN_BYTES)
    handler = partial(HuaienHandler, token=token, bridge=ChatBridge())
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd, token


def bind_free_port(host: str, start: int, tries: int = 8) -> tuple[ThreadingHTTPServer, str, int]:
    """从 start 开始找一个没被占的端口。

    ⚠️ 实测坑: 8765 长期被 ESP32 项目的 spectrum_server_v17.py 占着,
       怀恩 PWA 硬编码 8765 会直接起不来 (OSError: address already in use)。
       所以不硬编码, 往后找, 并把实际端口写下来给用户看。
    """
    last: Exception | None = None
    for p in range(start, start + tries):
        try:
            httpd, token = create_server(host, p)
            return httpd, token, p
        except OSError as e:
            last = e
            continue
    raise OSError(f"{start}..{start + tries - 1} 都没占住: {last}")


def write_port_file(port: int, token: str) -> str:
    """把实际端口+口令写进文件 —— 用户在手机上要用, 脚本和文档也要读。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "logs", "pwa_endpoint.txt")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ip = local_ip()
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"PORT={port}\nTOKEN={token}\nURL=http://{ip}:{port}/?t={token}\n")
    except OSError:
        pass
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="怀恩 Web 前台 (PWA)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--token", default=None, help="固定口令 (默认随机生成)")
    args = ap.parse_args()

    httpd, token, port = bind_free_port(args.host, args.port)
    write_port_file(port, token)

    # 后台预热管家面板 —— 重算要拉 `hermes kanban list` 子进程 (3-10 秒),
    # 先算好, 用户第一次打开手机端就是秒回。
    from huaien import butler as butler_mod
    threading.Thread(target=butler_mod.warmup, daemon=True).start()

    ip = local_ip()
    base = f"http://{ip}:{port}"
    # flush=True 是必须的: bat 起的窗口是重定向的, 不刷新就看不到地址和口令
    for line in (
        "=" * 60,
        "  怀恩 · Web 前台已启动",
        f"  本机:   http://127.0.0.1:{port}/?t={token}",
        f"  手机:   {base}/?t={token}",
        f"  口令:   {token}",
        "  (手机要和这台电脑在同一个 WiFi)",
        "  在手机浏览器打开上面的地址, 再点浏览器菜单里的",
        "  『添加到主屏幕』, 就跟 App 一样了。",
        "  [!] 这个链接等同于你的电脑权限, 不要转发给别人",
        "=" * 60,
        "  Ctrl+C 退出",
    ):
        print(line, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
