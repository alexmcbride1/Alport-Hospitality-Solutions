'use strict';
const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function message(t,error=false){$('message').hidden=false;$('message').className='notice'+(error?' error':'');$('message').textContent=t;}
async function api(path,data,header){let r=await fetch(path,{method:data?'POST':'GET',headers:data instanceof FormData?{[header]:window.PAGE_CSRF}:{'Content-Type':'application/json',[header||'X-Staff-CSRF']:window.PAGE_CSRF},body:data?(data instanceof FormData?data:JSON.stringify(data)):undefined});if(r.redirected){location.href=r.url;throw Error('Sign in again.');}let d=await r.json();if(!r.ok)throw Error(d.error||'Request failed');return d;}
async function busy(button,fn){if(button)button.disabled=true;try{await fn();}catch(e){message(e.message,true);}finally{if(button)button.disabled=button.dataset.keepDisabled==='true';}}
function options(id,rows,label,blank=false){$(id).innerHTML=(blank?'<option value="">None</option>':'')+rows.map(r=>`<option value="${r.id}">${esc(label(r))}</option>`).join('');}
function formData(form){return Object.fromEntries(new FormData(form));}
