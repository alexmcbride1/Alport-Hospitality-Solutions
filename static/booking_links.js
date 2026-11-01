'use strict';
window.BookingLinks=(()=>{
 const el=id=>document.getElementById(id);let state=null;
 function render(d){state=d;el('websiteBookingUrl').value=d.url;el('websiteBookingCode').value=`<a href="${d.url}">Book a table</a>`;el('websiteBookingPreview').href=d.url;el('websiteBookingEnabled').checked=d.enabled;el('websiteBookingPhone').value=d.contact_phone;el('websiteBookingState').textContent=d.enabled?`Online bookings are enabled for ${d.site_name}.`:`Online bookings are paused for ${d.site_name}. Configure tables and sessions below, then enable the link.`;}
 async function load(){el('websiteBookingState').textContent='Loading website link…';try{render(await api('/api/bookings/website-link'));}catch(e){el('websiteBookingState').textContent=e.message;}}
 async function save(){const b=el('websiteBookingSave');b.disabled=true;try{render(await api('/api/bookings/website-link',{method:'POST',body:JSON.stringify({enabled:el('websiteBookingEnabled').checked,contact_phone:el('websiteBookingPhone').value.trim()})}));toast('Website booking settings saved.');}catch(e){el('websiteBookingState').textContent='Not saved: '+e.message;}finally{b.disabled=false;}}
 async function copy(id){const field=el(id);field.focus();field.select();try{await navigator.clipboard.writeText(field.value);toast(id==='websiteBookingCode'?'Button code copied.':'Booking link copied.');}catch(e){el('websiteBookingState').textContent='Link selected. Use Copy on your device to copy it.';}}
 return {load,save,copy};
})();
