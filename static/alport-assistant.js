'use strict';
(() => {
  const el = id => document.getElementById(id), page = document.body.dataset.aiPage;
  let config, passphrase = '', version = 0, busy = false;
  const status = (message, error = false) => { el('status').textContent = message; el('status').classList.toggle('ai-error', error); };
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
  async function api(path, data) {
    const options = {credentials:'same-origin', cache:'no-store', headers:{'Accept':'application/json'}};
    if (data !== undefined) { options.method='POST'; options.headers['Content-Type']='application/json'; options.headers['X-AI-CSRF']=document.querySelector('meta[name=ai-csrf]').content; options.body=JSON.stringify({...data,site_id: data.site_id ?? config.site?.id}); }
    const response = await fetch('/api/assistant/'+path,options);
    const result = await response.json().catch(() => ({error:'Please reload and sign in again.'}));
    if (!response.ok) throw new Error(result.error || 'Request failed. Please try again.');
    return result;
  }
  const handle = fn => async event => { if(event)event.preventDefault(); try { await fn(event); } catch(error) {status(error.message,true);} };
  function link(label,url) {const a=node('a',label);a.href=url;return a;}
  function sources(parent,items) {
    const box=node('div',undefined,'ai-sources');
    for(const s of items || []) {
      const url=s.url || ''; if(!(url.startsWith('/')&&!url.startsWith('//'))&&!url.startsWith('https://'))continue;
      const a=link(s.label || s.title || 'Source',url);a.target='_blank';a.rel='noopener noreferrer';box.append(a);
    }
    parent.append(box);
  }
  function messages(items) {
    const box=el('messages');box.replaceChildren();
    if(!items.length)box.append(node('p','Your private conversation starts here. Ask about today’s work or choose a suggestion.','ai-small'));
    for(const m of items){const card=node('article',undefined,'ai-message '+m.role);card.append(node('strong',m.role==='user'?'YOU':'YOUR ASSISTANT'),node('p',m.text));sources(card,m.sources);box.append(card);}
    box.scrollTop=box.scrollHeight;
  }
  function lock(){passphrase=''; if(el('messages'))el('messages').replaceChildren();if(el('question'))el('question').value='';if(el('conversation'))el('conversation').hidden=true;if(el('vault'))el('vault').hidden=false;if(el('lock'))el('lock').hidden=true;}
  window.addEventListener('pagehide',lock);
  // Inactive tabs lock after 10 minutes. No credentials or chats go into browser storage.
  let idleTimer;
  function touch(){clearTimeout(idleTimer);idleTimer=setTimeout(()=>{if(!busy){lock();status('Your private chat locked after inactivity.');}else touch();},600000);}
  document.addEventListener('pointerdown',touch);document.addEventListener('keydown',touch);
  async function overview(){const snap=await api('overview');const box=el('overview');box.replaceChildren();for(const s of snap.sources){const card=node('div',undefined,'ai-record');card.append(node('b',s.label),node('p',s.records.length+' record groups in this snapshot.'));if(s.records.length){const first=s.records[0];card.append(node('p',Object.entries(first).filter(([k])=>k!=='id').map(([k,v])=>k.replaceAll('_',' ')+': '+v).join(' · ')));}const detail=node('details');detail.id='record-'+s.id;detail.append(node('summary','Review records'));if(s.records.length){const table=node('table');const keys=Object.keys(s.records[0]);const head=node('tr');keys.forEach(k=>head.append(node('th',k.replaceAll('_',' '))));table.append(head);for(const row of s.records){const tr=node('tr');keys.forEach(k=>tr.append(node('td',String(row[k]??''))));table.append(tr);}const wrap=node('div',undefined,'ai-record-table');wrap.append(table);detail.append(wrap);}else detail.append(node('p','No matching records in this bounded snapshot.'));if(new URLSearchParams(location.search).get('record')===s.id)detail.open=true;card.append(detail);box.append(card);}box.append(node('p','Fetched '+new Date(snap.as_of).toLocaleString('en-GB'),'ai-small'));}
  function showBriefing(data){const box=el('briefing');box.replaceChildren();if(!data.available){box.append(node('p',data.message || 'No briefing prepared for today. Use Prepare today, or configure the scheduled job.'));return;}const b=data.briefing;box.append(node('p',b.nation+' · '+b.day,'ai-small'),node('div',b.text,'ai-briefing-text'));sources(box,b.sources);box.append(node('p','AI-generated summary. Check the linked official guidance before acting.','ai-small'));}
  async function chatPage(){
    el('permissions').textContent='Role: '+config.role+'. Additional site records: '+(config.capabilities.join(', ')||'none')+'. Personal records require an active employee link.';
    el('createFields').hidden=config.vault;el('unlock').textContent=config.vault?'Unlock my conversation':'Create my private vault';
    el('vaultForm').addEventListener('submit',handle(async()=>{
      const value=el('passphrase').value;
      if(!config.vault&&(value!==el('confirmPassphrase').value||!el('privacyConsent').checked))throw new Error('Confirm the same passphrase and acknowledge the privacy information.');
      el('unlock').disabled=true;
      try{const data=await api('vault',{passphrase:value,create:!config.vault});passphrase=value;version=data.version;config.vault=true;el('passphrase').value='';el('confirmPassphrase').value='';el('createFields').hidden=true;el('unlock').textContent='Unlock my conversation';el('vault').hidden=true;el('conversation').hidden=false;el('lock').hidden=false;messages(data.messages);status(config.configured?'Your private assistant is ready.':'Vault ready. AI replies need server configuration.');touch();}finally{el('unlock').disabled=false;}
    }));
    el('lock').addEventListener('click',()=>{lock();status('Conversation locked.');});
    el('chatForm').addEventListener('submit',handle(async()=>{
      if(busy)return;if(!passphrase)throw new Error('Unlock your conversation first.');busy=true;el('send').disabled=true;el('lock').disabled=true;el('siteSelect').disabled=true;el('clearChat').disabled=true;status('Reviewing your permitted records. This can take a minute or two…');
      try{const data=await api('chat',{passphrase,message:el('question').value,version});version=data.version;messages(data.messages);el('question').value='';status('Reply saved privately. Review suggestions before acting.');}finally{busy=false;el('send').disabled=false;el('lock').disabled=false;el('siteSelect').disabled=false;el('clearChat').disabled=false;touch();}
    }));
    document.querySelectorAll('[data-prompt]').forEach(b=>b.addEventListener('click',()=>{el('question').value=b.dataset.prompt;el('question').focus();}));
    el('clearChat').addEventListener('click',handle(async()=>{if(!window.confirm('Delete your private conversation for this venue? This cannot be undone here.'))return;await api('clear',{passphrase,confirm:true});version=0;messages([]);status('Conversation deleted. Backups may retain encrypted copies until they expire.');}));
    await overview();if(config.owner){showBriefing(await api('briefing'));el('briefingRefresh').addEventListener('click',handle(async()=>{el('briefingRefresh').disabled=true;status('Preparing today’s official-source briefing…');try{showBriefing(await api('briefing',{}));status('Briefing ready. Check the source dates and jurisdiction.');}finally{el('briefingRefresh').disabled=false;}}));}
  }
  async function setupPage(){
    async function load(){const data=await api('setup');const box=el('steps');box.replaceChildren();let reviewed=0,skipped=0;data.steps.forEach((s,index)=>{if(s.status==='reviewed')reviewed++;if(s.status==='skipped')skipped++;const card=node('section',undefined,'ai-card ai-step'),body=node('div',undefined,'ai-step-content');card.append(node('span',String(index+1),'ai-step-number'),body);body.append(node('h2',s.label),node('p',s.help));const actions=node('div',undefined,'ai-step-actions');actions.append(link('Open this step',s.url));const select=node('select');select.setAttribute('aria-label',s.label+' progress');for(const [value,label] of [['pending','To do'],['reviewed','Reviewed'],['skipped','Skip for now']]){const o=node('option',label);o.value=value;o.selected=value===s.status;select.append(o);}select.addEventListener('change',handle(async()=>{await api('setup',{step:s.key,status:select.value});status(select.value==='skipped'?'Saved. Features relying on this information may be limited.':'Progress saved.');await load();}));actions.append(select);body.append(actions);if(s.status==='skipped')body.append(node('p','Skipped — revisit before relying on this feature.','ai-small'));box.append(card);});el('progress').value=reviewed;el('progressLabel').textContent=reviewed+' reviewed · '+skipped+' skipped · '+(data.steps.length-reviewed-skipped)+' to do';const connections=el('connections');connections.replaceChildren();for(const [key,val] of Object.entries(data.connections)){const card=node('div',undefined,'ai-record');card.append(node('b',key.toUpperCase()));if(key==='tills'){card.append(node('p',val.length?val.map(x=>x.provider+': '+x.status+(x.last_sync?' (last sync '+x.last_sync+')':'')).join('\n'):'No till connected. Use reviewed manual inputs.'));}else card.append(node('p',val));connections.append(card);}}
    el('finishSetup').addEventListener('click',handle(async()=>{await api('setup/finish',{});location.assign('/app');}));
    el('nation').value=config.settings.nation;el('briefingEnabled').checked=!!config.settings.briefing_enabled;
    el('settingsForm').addEventListener('submit',handle(async()=>{await api('settings',{nation:el('nation').value,briefing_enabled:el('briefingEnabled').checked});status('Briefing preferences saved.');}));await load();
  }
  async function accessPage(){
    const data=await api('access');const users=data.users.filter(u=>u.role!=='Owner');for(const u of users){const option=node('option',u.name+' · '+u.email);option.value=u.id;el('targetUser').append(option);}
    const checks=[...document.querySelectorAll('[name=capability]')];
    function load(){const grant=data.grants.find(r=>r.user_id===Number(el('targetUser').value));el('assistantRole').value=grant?.role||'Staff';el('accessEnabled').checked=grant?!!grant.enabled:true;const caps=grant?JSON.parse(grant.capabilities):[];checks.forEach(c=>c.checked=caps.includes(c.value));}
    el('targetUser').addEventListener('change',load);load();
    el('preset').addEventListener('click',()=>{const presets={'Staff':[],'Supervisor':['bookings','team'],'Bar manager':['stock','orders'],'Kitchen manager':['stock','orders','bookings'],'General manager':['stock','bookings','sales','team','orders']};checks.forEach(c=>c.checked=presets[el('assistantRole').value].includes(c.value));status('Suggested permissions selected. Review them, then save. Stock access covers the whole venue, not just a department.');});
    el('accessForm').addEventListener('submit',handle(async()=>{const values={user_id:Number(el('targetUser').value),role:el('assistantRole').value,enabled:el('accessEnabled').checked,capabilities:checks.filter(c=>c.checked).map(c=>c.value)};await api('access',values);const existing=data.grants.find(r=>r.user_id===values.user_id);const saved={...values,capabilities:JSON.stringify(values.capabilities)};if(existing)Object.assign(existing,saved);else data.grants.push(saved);status('Assistant access saved for this venue. Private conversations remain inaccessible.');}));
  }
  handle(async()=>{config=await api('config');if(!config.site){const placeholder=node('option','Choose your site…');placeholder.value='';placeholder.disabled=true;placeholder.selected=true;el('siteSelect').append(placeholder);}for(const s of config.sites){const o=node('option',s.name);o.value=s.id;o.selected=s.id===config.site?.id;el('siteSelect').append(o);}el('siteSelect').addEventListener('change',handle(async()=>{lock();await api('site',{site_id:Number(el('siteSelect').value)});location.reload();}));if(!config.site){status(config.sites.length?'Choose a site to continue. No venue information is loaded until you select one.':'No sites are assigned to this account. The owner can add a site or assign your access.');return;}status(config.configured?'Your venue workspace is ready.':'AI connection needs administrator setup. You can still review setup and permissions.');if(page==='chat')await chatPage();if(page==='setup')await setupPage();if(page==='access')await accessPage();}) ();
})();
