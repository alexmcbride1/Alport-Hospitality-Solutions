/* Shared enhancement for the business and staff EHO screens. */
(() => {
  const modal = document.getElementById('ehoChecklistModal');
  const actions = document.querySelector('#eho .eho-actions');
  if (!modal || !actions || typeof window.openEhoChecklist !== 'function') return;
  for (const [kind, label] of [['daily','Check photos & history'],['pest','Pest control visits'],['extraction','Extraction fan cleaning']]) {
    const link = document.createElement('a'); link.className = 'btn';
    link.href = '/eho/evidence?kind=' + kind; link.textContent = label; actions.append(link);
  }
  const form = modal.querySelector('form');
  const group = document.createElement('div');
  const label = document.createElement('label'); label.htmlFor='ehoPhotoFiles';label.textContent='Photo evidence (optional)';
  const input = document.createElement('input'); input.id='ehoPhotoFiles';input.type='file';input.multiple=true;input.accept='image/jpeg,image/png,image/webp';
  const note = document.createElement('p');note.className='small';note.textContent='Attach up to 5 JPEG, PNG or WebP photos, 8 MB each / 24 MB total. On a phone, use the file picker to choose photos or take a photo if offered. Camera access is your choice. Photos are visible to staff at this venue.';
  group.append(label,input,note);form.insertBefore(group,form.querySelector('button[type="submit"],button.btn.primary'));
  let config=null, requestKey=null, busy=false;
  const originalOpen=window.openEhoChecklist;
  window.openEhoChecklist=async function(type) {
    if(busy)return;
    config=null;input.value='';
    try {
      const r=await fetch('/api/eho/evidence/config',{cache:'no-store'});const d=await r.json();
      if(!r.ok)throw Error(d.error||'Could not load evidence settings.');
      config=d;requestKey=crypto.randomUUID();originalOpen(type);
    } catch(e){toast(e.message);}
  };
  window.saveEhoChecklist=async function(e) {
    e.preventDefault();if(busy)return;
    if(!config){toast('Reopen the checks before saving.');return;}
    const files=[...input.files];
    if(files.length>5 || files.some(f=>f.size>8*1024*1024) || files.reduce((n,f)=>n+f.size,0)>24*1024*1024){toast('Use up to 5 photos, 8 MB each / 24 MB total.');return;}
    const items=ehoChecklistType==='Opening'?ehoData.opening_items:ehoData.closing_items, answers={};
    for(const [i,item] of items.entries()) {
      const chosen=document.querySelector(`input[name="ehoCheck${i}"]:checked`);
      if(!chosen){toast('Answer every checklist item.');return;}answers[item]=chosen.value==='yes';
    }
    const body=new FormData();body.set('site_id',config.site_id);body.set('request_key',requestKey);
    body.set('payload',JSON.stringify({check_type:ehoChecklistType,answers,problems:document.getElementById('ehoProblems').value,corrective_action:document.getElementById('ehoCorrective').value}));
    files.forEach(f=>body.append('files',f));
    busy=true;const button=form.querySelector('button.btn.primary');button.disabled=true;const old=button.textContent;button.textContent='Saving checks & photos…';
    try {
      const r=await fetch('/api/eho/evidence/daily',{method:'POST',headers:{'X-Evidence-CSRF':config.csrf},body});
      const d=await r.json();if(!r.ok)throw Error(d.error||'Could not save.');
      closeModal('ehoChecklistModal');toast('Checks and photos saved. View them in Check photos & history.');input.value='';loadEho();
    }catch(err){toast(err.message+' Your form is still here; you can retry.');}
    finally{busy=false;button.disabled=false;button.textContent=old;}
  };
})();
