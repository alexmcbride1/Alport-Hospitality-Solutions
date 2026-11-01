'use strict';
(() => {
 const links=[...document.querySelectorAll('[data-step]')], panels=[...document.querySelectorAll('.tour-panel')];
 function select(id){if(!panels.some(p=>p.id===id))return;for(const panel of panels)panel.hidden=panel.id!==id;for(const a of links){if(a.dataset.step===id)a.setAttribute('aria-current','step');else a.removeAttribute('aria-current');}}
 for(const a of links)a.addEventListener('click',()=>select(a.dataset.step));
 window.addEventListener('hashchange',()=>select(location.hash.slice(1)));select(panels.some(p=>p.id===location.hash.slice(1))?location.hash.slice(1):'tour-today');
 const input=document.getElementById('venueCount'),box=document.getElementById('priceCalculator'),result=document.getElementById('priceResult');
 const format=n=>new Intl.NumberFormat('en-GB',{style:'currency',currency:'GBP',maximumFractionDigits:2}).format(n);
 function price(){const n=Number(input.value);if(!Number.isInteger(n)||n<1||n>100){result.textContent='Enter a whole number of venues from 1 to 100. Contact Alport for larger groups.';return;}const net=n*Number(box.dataset.price),term=Number(box.dataset.term),gross=net*(1+Number(box.dataset.vat));result.textContent=n+' venue'+(n===1?'':'s')+': '+format(net)+' + VAT per month ('+format(gross)+' including VAT). Minimum '+term+'-month subscription total: '+format(net*term)+' + VAT. Unlimited users included.';}
 input.addEventListener('input',price);price();
})();
