'use strict';
const S=JSON.parse(document.querySelector('#state').textContent),app=document.querySelector('#app');
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const url=(path,params={})=>path+'?'+new URLSearchParams({...params,reader_token:S.token});
async function api(path,params={}){const r=await fetch(url(path,params));if(!r.ok)throw Error(r.status===403?'Session expired. Reopen this title from Library.':'Request failed. Try again.');const data=await r.json();if(data.error)throw Error(data.error);return data}
async function post(path,data){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...data,reader_token:S.token}),keepalive:true});if(!r.ok)throw Error('Could not save. Reopen from Library if your session expired.');const result=await r.json();if(result.error)throw Error(result.error);return result}
let noticeTimer;function notify(message,sticky=false){const n=document.querySelector('#notice');n.textContent=message;n.hidden=false;clearTimeout(noticeTimer);if(!sticky)noticeTimer=setTimeout(()=>n.hidden=true,5500)}
function localGet(key,fallback={}){try{return JSON.parse(localStorage.getItem(key))??fallback}catch{return fallback}}
function localSet(key,value){try{localStorage.setItem(key,JSON.stringify(value))}catch{}}
function showDialog(content){document.querySelector('#detail-dialog')?.remove();const d=document.createElement('dialog');d.id='detail-dialog';d.innerHTML=content;document.body.append(d);d.showModal();d.addEventListener('click',e=>{if(e.target===d)d.close()});d.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>d.close());return d}
function fail(error){app.innerHTML=`<main class="shell"><a class="button" href="${esc(S.library)}">Back to Library</a><h1 style="margin-top:30px">Unable to open this view</h1><p>${esc(error.message)}</p></main>`}
