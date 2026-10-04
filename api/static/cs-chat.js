"use strict";(()=>{var z="cschat_session_v1",Z={load_history:"Loading conversation\u2026",classify_ticket:"Understanding your message\u2026",apply_escalation:"Checking conversation history\u2026",fetch_order_context:"Looking up your order\u2026",fetch_knowledge_context:"Checking our help docs\u2026",generate_response:"Writing a reply\u2026",decide_auto_send:"Reviewing confidence\u2026",save_results:"Saving\u2026"};function U(o){return Z[o]||"Working\u2026"}function ee(){let o="";return n=>{o+=n;let i=[],v;for(;(v=o.indexOf(`

`))!==-1;){let L=o.slice(0,v);o=o.slice(v+2);let u="message",c=[];for(let l of L.split(`
`))l.startsWith("event:")?u=l.slice(6).trim():l.startsWith("data:")&&c.push(l.slice(5).trim());if(c.length!==0)try{i.push({event:u,data:JSON.parse(c.join(`
`))})}catch(l){}}return i}}function te(){let o=new Map;return{getItem:n=>o.has(n)?o.get(n):null,setItem:(n,i)=>void o.set(n,i),removeItem:n=>void o.delete(n)}}async function ne(o){var R,H,B,j,K;let n=(H=(R=o.mount)==null?void 0:R.ownerDocument)!=null?H:document,i=(B=o.storage)!=null?B:oe(),v=(j=o.fetchImpl)!=null?j:((e,t)=>fetch(e,t)),L=o.apiBase.replace(/\/$/,"");async function u(e,t){return v(`${L}${e}`,{...t,headers:{"Content-Type":"application/json","X-Widget-Key":o.apiKey,...(t==null?void 0:t.headers)||{}}})}let c;try{let e=await u(`/chat/config?key=${encodeURIComponent(o.apiKey)}`);if(!e.ok||(c=await e.json(),!c.enabled))return null}catch(e){return null}let l=/^#[0-9a-fA-F]{6}$/.test(c.color)?c.color:"#2E8C82",d=(i==null?void 0:i.getItem(z))||null;async function W(){if(d)try{let e=await u(`/chat/sessions/${d}`);if(e.ok)return d;e.status===404&&(d=null,i==null||i.removeItem(z))}catch(e){return null}try{let e=await u("/chat/sessions",{method:"POST",body:JSON.stringify({email:o.email||null,name:o.name||null,order_number:o.orderNumber||null})});if(!e.ok)return null;let t=await e.json();return d=t.session_id,i==null||i.setItem(z,d),A(t.history||[]),d}catch(e){return null}}async function D(){if(d)try{let e=await u(`/chat/sessions/${d}`);if(e.ok){let t=await e.json();A(t.history||[])}}catch(e){}}let _=n.createElement("div");_.setAttribute("data-cs-chat","");let T=_.attachShadow({mode:"open"});((K=o.mount)!=null?K:n.body).appendChild(_);let O=n.createElement("style");O.textContent=`
    :host { all: initial; }
    * { box-sizing: border-box; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
    .launcher {
      position: fixed; right: 20px; bottom: 20px; z-index: 2147483000;
      width: 56px; height: 56px; border-radius: 50%; border: none; cursor: pointer;
      background: ${l}; color: #fff; display: flex; align-items: center; justify-content: center;
      box-shadow: 0 6px 20px rgba(0,0,0,.25); transition: transform .15s ease;
    }
    .launcher:hover { transform: scale(1.06); }
    .launcher:focus-visible { outline: 3px solid #111; outline-offset: 2px; }
    .panel {
      position: fixed; right: 20px; bottom: 86px; z-index: 2147483001;
      width: 380px; max-width: calc(100vw - 32px); height: 560px; max-height: calc(100vh - 120px);
      background: #fff; color: #1a1f2b; border-radius: 16px; overflow: hidden;
      box-shadow: 0 12px 40px rgba(0,0,0,.28); display: none; flex-direction: column;
      border: 1px solid rgba(0,0,0,.08);
    }
    .panel.open { display: flex; }
    .header {
      background: ${l}; color: #fff; padding: 14px 16px; display: flex; align-items: center; gap: 10px;
    }
    .header img { width: 28px; height: 28px; border-radius: 6px; object-fit: cover; background: #fff; }
    .header .title { font-weight: 600; font-size: 15px; flex: 1; }
    .close {
      background: transparent; border: none; color: #fff; cursor: pointer; font-size: 20px;
      line-height: 1; padding: 4px 6px; border-radius: 6px;
    }
    .close:hover { background: rgba(255,255,255,.18); }
    .close:focus-visible { outline: 2px solid #fff; outline-offset: 1px; }
    .messages { flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 8px; background: #f7f8fa; }
    .msg { max-width: 82%; padding: 9px 12px; border-radius: 14px; font-size: 14px; line-height: 1.45; white-space: pre-wrap; word-break: break-word; }
    .msg.customer { align-self: flex-end; background: ${l}; color: #fff; border-bottom-right-radius: 4px; }
    .msg.assistant, .msg.agent { align-self: flex-start; background: #fff; color: #1a1f2b; border: 1px solid rgba(0,0,0,.07); border-bottom-left-radius: 4px; }
    .msg.system { align-self: center; background: transparent; color: #5b6675; font-size: 12.5px; text-align: center; max-width: 92%; border: none; }
    .msg.error { align-self: center; background: #fdecec; color: #9b2c2c; font-size: 13px; }
    .meta { display: flex; gap: 6px; align-items: center; margin-top: 6px; flex-wrap: wrap; }
    .chip { font-size: 10.5px; padding: 2px 7px; border-radius: 999px; background: #eef1f5; color: #5b6675; border: 1px solid rgba(0,0,0,.06); }
    .chip.human { background: #fff6e5; color: #8a6116; border-color: #f0dfb5; }
    .handoff {
      margin-top: 8px; font-size: 13px; padding: 7px 12px; border-radius: 10px; cursor: pointer;
      background: #fff; color: ${l}; border: 1px solid ${l}; font-weight: 600;
    }
    .handoff:hover { background: ${l}14; }
    .handoff:focus-visible { outline: 2px solid ${l}; outline-offset: 2px; }
    .typing { align-self: flex-start; display: flex; gap: 5px; align-items: center; padding: 10px 12px; background: #fff; border: 1px solid rgba(0,0,0,.07); border-radius: 14px; font-size: 13px; color: #5b6675; }
    .typing .dots { display: inline-flex; gap: 3px; }
    .typing .dots i { width: 5px; height: 5px; border-radius: 50%; background: #9aa6b5; animation: blink 1.2s infinite; }
    .typing .dots i:nth-child(2) { animation-delay: .2s; }
    .typing .dots i:nth-child(3) { animation-delay: .4s; }
    @keyframes blink { 0%, 80%, 100% { opacity: .25; } 40% { opacity: 1; } }
    .composer { display: flex; gap: 8px; padding: 10px; border-top: 1px solid rgba(0,0,0,.08); background: #fff; }
    .composer input {
      flex: 1; border: 1px solid rgba(0,0,0,.14); border-radius: 10px; padding: 10px 12px; font-size: 14px;
      outline: none; background: #fff; color: #1a1f2b;
    }
    .composer input:focus { border-color: ${l}; box-shadow: 0 0 0 3px ${l}33; }
    .composer button {
      border: none; background: ${l}; color: #fff; border-radius: 10px; padding: 0 16px; cursor: pointer; font-weight: 600; font-size: 14px;
    }
    .composer button:disabled { opacity: .5; cursor: default; }
    .composer button:focus-visible { outline: 2px solid #111; outline-offset: 2px; }
    @media (max-width: 480px) {
      .panel { right: 0; bottom: 0; left: 0; top: 0; width: 100vw; max-width: 100vw; height: 100dvh; max-height: 100dvh; border-radius: 0; }
      .launcher { right: 14px; bottom: 14px; }
    }
    .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  `,T.appendChild(O);let h=n.createElement("button");h.className="launcher",h.setAttribute("aria-label",`Open ${c.title}`),h.setAttribute("aria-expanded","false"),h.innerHTML='<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>';let g=n.createElement("div");g.className="panel",g.setAttribute("role","dialog"),g.setAttribute("aria-modal","true"),g.setAttribute("aria-label",c.title);let C=n.createElement("div");if(C.className="header",c.logo_url){let e=n.createElement("img");e.src=c.logo_url,e.alt="",C.appendChild(e)}let $=n.createElement("span");$.className="title",$.textContent=c.title,C.appendChild($);let E=n.createElement("button");E.className="close",E.setAttribute("aria-label","Close chat"),E.textContent="\xD7",C.appendChild(E);let p=n.createElement("div");p.className="messages",p.setAttribute("role","log"),p.setAttribute("aria-live","polite"),p.setAttribute("aria-label","Chat messages");let w=n.createElement("form");w.className="composer",w.setAttribute("aria-label","Send a message");let m=n.createElement("input");m.type="text",m.placeholder="Type a message\u2026",m.setAttribute("aria-label","Message"),m.maxLength=4e3;let S=n.createElement("button");S.type="submit",S.textContent="Send",w.appendChild(m),w.appendChild(S),g.appendChild(C),g.appendChild(p),g.appendChild(w),T.appendChild(g),T.appendChild(h);let k=!1,I=!1;function P(e){k=e,g.classList.toggle("open",k),h.setAttribute("aria-expanded",String(k)),k?(W(),m.focus()):h.focus()}h.addEventListener("click",()=>P(!k)),E.addEventListener("click",()=>P(!1)),n.addEventListener("keydown",e=>{e.key==="Escape"&&k&&P(!1)});function b(){p.scrollTop=p.scrollHeight}function f(e,t){let a=n.createElement("div");return a.className=`msg ${e}`,a.textContent=t,p.appendChild(a),b(),{el:a}}function A(e){p.innerHTML="",f("system",c.welcome_message);for(let t of e)t.role==="customer"?f("customer",t.content):t.role==="agent"?f("agent",t.content):t.role==="assistant"&&f("assistant",t.content);b()}function J(e){let t=n.createElement("div");t.className="typing";let a=n.createElement("span");a.textContent=e;let s=n.createElement("span");return s.className="dots",s.innerHTML="<i></i><i></i><i></i>",t.appendChild(s),t.appendChild(a),p.appendChild(t),b(),{update:r=>{a.textContent=r,b()},remove:()=>t.remove()}}function F(e,t){return new Promise(a=>{let s=t.length;if(s<=60){e.textContent=t,b(),a();return}let r=Math.max(2,Math.ceil(s/60)),x=0,N=()=>{x=Math.min(s,x+r),e.textContent=t.slice(0,x),b(),x<s?requestAnimationFrame(N):a()};requestAnimationFrame(N)})}function G(e,t){let a=n.createElement("div");if(a.className="meta",t.show_confidence&&typeof t.confidence=="number"){let s=n.createElement("span");s.className="chip",s.textContent=`confidence ${Math.round(t.confidence*100)}%`,a.appendChild(s)}if(t.needs_human){let s=n.createElement("span");s.className="chip human",s.textContent="a human will follow up",a.appendChild(s);let r=n.createElement("button");r.className="handoff",r.type="button",r.textContent="Talk to a human",r.addEventListener("click",async()=>{r.disabled=!0,r.textContent="Connecting\u2026";try{(await u(`/chat/sessions/${d}/handoff`,{method:"POST",body:JSON.stringify({reason:"customer_requested"})})).ok?r.textContent="We've alerted our team \u2713":(r.disabled=!1,r.textContent="Talk to a human")}catch(x){r.disabled=!1,r.textContent="Talk to a human"}}),a.appendChild(r)}a.childElementCount>0&&(e.appendChild(a),b())}async function V(e){var s;if(I)return;let t=await W();if(!t){f("error","We can't reach support right now. Please try again in a moment.");return}I=!0,S.disabled=!0,f("customer",e);let a=J(U("classify_ticket"));try{let r=await u(`/chat/sessions/${t}/messages`,{method:"POST",body:JSON.stringify({body:e})});if(!r.ok||!r.body)throw new Error(`HTTP ${r.status}`);let x=r.body.getReader(),N=new TextDecoder,X=ee(),M=!1;for(;;){let{done:Y,value:Q}=await x.read();if(Y)break;for(let y of X(N.decode(Q,{stream:!0})))if(y.event==="stage")a.update(String(y.data.label||U(y.data.stage)));else if(y.event==="message"){a.remove(),M=!0;let q=f("assistant","");await F(q.el,String((s=y.data.content)!=null?s:"")),G(q.el,y.data)}else y.event==="error"&&(a.remove(),f("error",String(y.data.message||"Something went wrong. Please try again.")),M=!0)}M||(a.remove(),f("error","No reply came through. Please try again."))}catch(r){a.remove(),f("error","We lost connection to support. Please try again.")}finally{I=!1,S.disabled=!1,m.focus(),b()}}return w.addEventListener("submit",e=>{e.preventDefault();let t=m.value.trim();!t||I||(m.value="",V(t))}),A([]),D(),{destroy(){_.remove()}}}function oe(){try{let o=globalThis.localStorage,n="__cschat_probe__";return o.setItem(n,"1"),o.removeItem(n),o}catch(o){return te()}}function ae(){let o=document.currentScript;if(!o)return;let n=o.dataset.key;if(!n){console.warn("[cs-chat] missing data-key \u2014 widget not started");return}let i=o.dataset.api||"";if(!i)try{i=new URL(o.src,window.location.href).origin}catch(v){i=window.location.origin}ne({apiKey:n,apiBase:i,email:o.dataset.email,name:o.dataset.name,orderNumber:o.dataset.order})}typeof document!="undefined"&&ae();})();
