'use strict';
(() => {
    const $ = id => document.getElementById(id);
    const state = {config:null, site:null, generation:0, rows:new Map(), latest:0, oldest:0,
        timer:null, controller:null, loaded:false, busy:false, sending:false, ready:false, drafts:new Map(), attempts:new Map()};
    const status = message => { $('chatStatus').textContent = message; };
    function controls(ready) {
        state.ready = ready;
        $('chatBody').disabled = !ready || state.sending;
        $('sendChat').disabled = !ready || state.sending;
        $('chatSite').disabled = !state.config || state.sending;
    }
    async function api(path, data, signal) {
        const response = await fetch('/api/team-chat' + path, {
            method:data ? 'POST' : 'GET', cache:'no-store', signal,
            headers:data ? {'Content-Type':'application/json','X-Chat-CSRF':state.config.csrf} : {},
            body:data ? JSON.stringify(data) : undefined
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok || response.redirected) {
            const error = new Error(result.error || 'Unable to connect. Please try again.');
            error.status = response.status; throw error;
        }
        return result;
    }
    function clearPrivateContent() {
        state.rows.clear(); $('chatMessages').replaceChildren(); $('accessUsers').replaceChildren();
        $('chatBody').value = ''; $('chatTasks').replaceChildren(); $('chatMentions').replaceChildren(); $('taskForm').hidden=true; state.drafts.clear(); state.attempts.clear();
        $('chatRoom').hidden = true; controls(false);
    }
    function failure(error) {
        if (error.name === 'AbortError') return;
        controls(false);
        if (error.status === 401 || error.status === 403) {
            clearTimeout(state.timer); clearPrivateContent(); state.site = null;
            $('chatSite').value = ''; status(error.message + ' Reload to check your access.');
        } else status(error.message + ' Your unsent message is kept in this tab.');
    }
    function element(row) {
        const article = document.createElement('article');
        article.className = 'message' + (row.user_id === state.config.user.id ? ' mine' : '');
        article.dataset.messageId = String(row.id);
        const heading = document.createElement('header'), name = document.createElement('strong');
        name.textContent = row.sender_name + (row.user_id === state.config.user.id ? ' (you)' : '');
        const time = document.createElement('time'); time.dateTime = row.created_at;
        time.textContent = new Date(row.created_at).toLocaleString(undefined, {dateStyle:'medium',timeStyle:'short'});
        const body = document.createElement('p'); body.textContent = row.body;
        heading.append(name,time); article.append(heading,body);
        if(row.mentions?.length){const mentions=document.createElement('small');mentions.textContent='Mentions: '+row.mentions.map(p=>'@'+p.name).join(', ');article.append(mentions);}
        const task=document.createElement('button');task.type='button';task.textContent='Create task';
        task.addEventListener('click',()=>{ $('taskMessage').textContent=row.id; $('taskTitle').value=row.body.slice(0,200); $('taskStatus').textContent=''; $('taskForm').hidden=false; $('taskTitle').focus(); });
        article.append(task); return article;
    }
    function render(rows, mode) {
        const log = $('chatMessages'), nearBottom = log.scrollHeight-log.scrollTop-log.clientHeight < 90;
        const previousHeight = log.scrollHeight, previousTop = log.scrollTop;
        const fresh = rows.filter(row => !state.rows.has(row.id));
        if (fresh.length) log.querySelector('.empty')?.remove();
        for (const row of fresh) state.rows.set(row.id,row);
        log.setAttribute('aria-live', mode === 'new' ? 'polite' : 'off');
        const fragment = document.createDocumentFragment();
        for (const row of fresh) fragment.append(element(row));
        if (mode === 'older') log.prepend(fragment); else log.append(fragment);
        if (!state.rows.size && !log.querySelector('.empty')) {
            const empty = document.createElement('p'); empty.className='empty';
            empty.textContent='No messages yet. Start the conversation with your team.';log.append(empty);
        }
        if (state.rows.size) {
            state.latest = Math.max(...state.rows.keys()); state.oldest = Math.min(...state.rows.keys());
        }
        if (mode === 'older') log.scrollTop = previousTop + log.scrollHeight-previousHeight;
        else if (mode === 'initial' || nearBottom) log.scrollTop = log.scrollHeight;
        else if (fresh.length) $('newMessages').hidden=false;
        $('messageCount').textContent=state.rows.size+' messages loaded';
    }
    function schedule(delay=5000) {
        clearTimeout(state.timer);
        if (state.site && !document.hidden) state.timer=setTimeout(() => refresh(),delay);
    }
    async function refresh(initial=false) {
        if (!state.site || state.busy) return;
        initial = initial || !state.loaded;
        const generation=state.generation, site=state.site;
        state.busy=true;
        try {
            const data=await api('/sites/'+site+'/messages'+(initial?'':'?after='+state.latest),null,state.controller.signal);
            if (generation!==state.generation) return;
            render(data.messages,initial?'initial':'new');state.loaded=true;
            if(initial) $('olderMessages').hidden=!data.has_more;
            if(!document.hidden && !data.has_more && $('chatMessages').scrollHeight-$('chatMessages').scrollTop-$('chatMessages').clientHeight<90 && state.latest) await api('/sites/'+site+'/read',{last_id:state.latest},state.controller.signal);
            if(generation!==state.generation)return;
            if(!$('chatTasks').contains(document.activeElement))await loadTasks();await unread();
            if(generation!==state.generation)return;
            controls(true);status('Connected to '+data.site.name+'. Messages refresh automatically.');
            schedule(!initial && data.has_more ? 100 : 5000);
        } catch(error) {
            if(generation===state.generation){failure(error);schedule();}
        } finally {if(generation===state.generation)state.busy=false;}
    }
    async function chooseSite() {
        if(state.site) state.drafts.set(state.site,$('chatBody').value);
        clearTimeout(state.timer);state.controller?.abort();state.controller=new AbortController();
        state.generation++;state.loaded=false;state.busy=false;state.rows.clear();state.latest=0;state.oldest=0;
        state.site=$('chatSite').value||null;
        $('chatMessages').replaceChildren();$('accessUsers').replaceChildren();$('chatTasks').replaceChildren();$('chatMentions').replaceChildren();$('taskAssignee').replaceChildren();$('taskForm').hidden=true;$('chatAccess').open=false;
        $('chatBody').value=state.drafts.get(state.site)||'';$('newMessages').hidden=true;$('olderMessages').hidden=true;
        $('chatRoom').hidden=!state.site;$('sendStatus').textContent='';controls(false);
        if(!state.site){status('Choose a site to open its chat.');return;}
        const name=state.config.sites.find(site=>String(site.id)===state.site).name;
        $('roomName').textContent=name;$('recipientSite').textContent=name;
        $('chatAccess').hidden=!state.config.owner;status('Loading '+name+'…');
        const gen=state.generation;
        try {const people=await api('/sites/'+state.site+'/participants',null,state.controller.signal);if(gen!==state.generation)return;
            for(const person of people.users){$('taskAssignee').append(new Option(person.name,person.id));$('chatMentions').append(new Option(person.name,person.id));}
        }catch(error){if(gen===state.generation)failure(error);return;}
        await refresh(true);
    }
    $('chatSite').addEventListener('change',chooseSite);
    $('refreshChat').addEventListener('click',()=>refresh());
    $('newMessages').addEventListener('click',()=>{$('chatMessages').scrollTop=$('chatMessages').scrollHeight;$('newMessages').hidden=true;});
    $('chatMessages').addEventListener('scroll',()=>{const log=$('chatMessages');if(log.scrollHeight-log.scrollTop-log.clientHeight<90)$('newMessages').hidden=true;});
    $('olderMessages').addEventListener('click',async()=>{
        if(!state.site)return;
        const generation=state.generation,button=$('olderMessages');button.disabled=true;
        try {
            const data=await api('/sites/'+state.site+'/messages?before='+state.oldest,null,state.controller.signal);
            if(generation!==state.generation)return;
            render(data.messages,'older');button.hidden=!data.has_more;
        }catch(error){if(generation===state.generation)failure(error);}
        finally{button.disabled=false;}
    });
    $('chatForm').addEventListener('submit',async event=>{
        event.preventDefault();if(!state.site||!state.ready||state.sending)return;
        const body=$('chatBody').value.trim();if(!body)return;const mentions=Array.from($('chatMentions').selectedOptions,x=>Number(x.value));
        const site=state.site,generation=state.generation;
        let attempt=state.attempts.get(site);
        if(!attempt||attempt.body!==body||JSON.stringify(attempt.mentions)!==JSON.stringify(mentions)){attempt={body,mentions,request_key:crypto.randomUUID()};state.attempts.set(site,attempt);}
        state.sending=true;controls(false);$('sendStatus').textContent='Sending to '+$('roomName').textContent+'…';
        try {
            await api('/sites/'+site+'/messages',attempt,state.controller.signal);
            if(generation!==state.generation)return;
            $('chatBody').value='';for(const o of $('chatMentions').options)o.selected=false;state.drafts.delete(site);state.attempts.delete(site);$('sendStatus').textContent='Message sent.';
        } catch(error){if(generation===state.generation){$('sendStatus').textContent='Send not confirmed. '+error.message+' Retry the same message to avoid duplicates.';failure(error);}}
        finally {
            state.sending=false;
            if(generation===state.generation && state.site){await refresh();$('chatBody').focus();}
        }
    });
    async function loadAccess(){
        if(!state.site)return;
        const site=state.site,generation=state.generation;
        try{
            const data=await api('/sites/'+site+'/access',null,state.controller.signal);
            if(generation!==state.generation)return;
            $('accessUsers').replaceChildren();
            for(const person of data.users){
                const row=document.createElement('div');row.className='access-user';
                const label=document.createElement('p');label.textContent=person.name;
                const detail=document.createElement('small');detail.textContent=person.role+' · '+person.source+' · '+(person.enabled?'Has access':'No access');label.append(detail);row.append(label);
                if(person.editable){
                    const button=document.createElement('button');button.type='button';button.textContent=person.enabled?'Remove access':'Allow access';
                    button.setAttribute('aria-label',button.textContent+' for '+person.name);
                    button.addEventListener('click',async()=>{
                        button.disabled=true;
                        try{await api('/sites/'+site+'/access',{user_id:person.id,enabled:!person.enabled},state.controller.signal);if(generation===state.generation)await loadAccess();}
                        catch(error){if(generation===state.generation)failure(error);}
                        finally{button.disabled=false;}
                    });row.append(button);
                }
                $('accessUsers').append(row);
            }
        }catch(error){if(generation===state.generation)failure(error);}
    }
    $('loadChatAccess').addEventListener('click',loadAccess);
    document.addEventListener('visibilitychange',()=>{clearTimeout(state.timer);if(!document.hidden&&state.site)refresh();});
    window.addEventListener('pagehide',()=>{clearTimeout(state.timer);state.controller?.abort();state.generation++;clearPrivateContent();});
    window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
    async function unread(){
        const gen=state.generation;const d=await api('/unread');if(gen!==state.generation)return;
        $('chatUnread').textContent=d.sites.map(s=>s.name+': '+s.unread+' unread'+(s.mentions?' · '+s.mentions+' mentions':'')).join(' | ');
        for(const o of $('chatSite').options){const s=d.sites.find(x=>String(x.id)===o.value);if(s)o.textContent=s.name+(s.unread?' ('+s.unread+' unread)':'');}
    }
    async function loadTasks(){
        const gen=state.generation,site=state.site;if(!site)return;
        const d=await api('/sites/'+site+'/tasks',null,state.controller.signal);if(gen!==state.generation)return;
        $('chatTasks').replaceChildren();
        for(const task of d.tasks){const box=document.createElement('article');box.className='task-card';
            const title=document.createElement('h3');title.textContent=task.title;
            const detail=document.createElement('p');detail.textContent=task.assignee+' · Due '+task.due_date+' · '+task.status;
            const audit=document.createElement('small');audit.textContent='Last updated by '+task.updated_name+' · '+new Date(task.updated_at).toLocaleString();box.append(title,detail,audit);
            if(state.config.owner||[task.assignee_id,task.created_by].includes(state.config.user.id)){
                const select=document.createElement('select');select.setAttribute('aria-label','Status for '+task.title);
                for(const status of ['Open','In progress','Done'])select.append(new Option(status,status));select.value=task.status;
                select.addEventListener('change',async()=>{select.disabled=true;try{await api('/sites/'+site+'/tasks/'+task.id,{status:select.value,version:task.version},state.controller.signal);if(gen===state.generation)await loadTasks();}catch(e){if(gen===state.generation){if(e.status===401||e.status===403)failure(e);else{status(e.message);await loadTasks();}}}});box.append(select);
            }
            const history=document.createElement('button');history.type='button';history.textContent='Task history';
            history.addEventListener('click',async()=>{try{const h=await api('/sites/'+site+'/tasks/'+task.id+'/history',null,state.controller.signal);if(gen!==state.generation)return;const list=document.createElement('ul');for(const event of h.events){const li=document.createElement('li');li.textContent=event.actor+' · '+event.status+' · '+new Date(event.created_at).toLocaleString();list.append(li);}history.replaceWith(list);}catch(e){if(gen===state.generation)status(e.message);}});box.append(history);$('chatTasks').append(box);
        }
        if(!d.tasks.length)$('chatTasks').textContent='No tasks yet.';
    }
    $('cancelTask').addEventListener('click',()=>{$('taskForm').hidden=true;});
    $('taskForm').addEventListener('submit',async e=>{e.preventDefault();const site=state.site,gen=state.generation;const button=e.submitter;button.disabled=true;
        try{await api('/sites/'+site+'/tasks',{message_id:Number($('taskMessage').textContent),title:$('taskTitle').value,assignee_id:Number($('taskAssignee').value),due_date:$('taskDue').value},state.controller.signal);if(gen===state.generation){$('taskForm').hidden=true;await loadTasks();status('Task created.');}}
        catch(error){if(gen===state.generation)$('taskStatus').textContent=error.message;}finally{button.disabled=false;}
    });
    async function init(){
        try{
            state.config=await api('/config');$('chatSite').replaceChildren(new Option('Choose a site…',''));
            for(const site of state.config.sites)$('chatSite').append(new Option(site.name,site.id));
            $('chatSite').disabled=!state.config.sites.length;
            await unread();const selected=new URLSearchParams(location.search).get('site');if(selected&&state.config.sites.some(s=>String(s.id)===selected)){$('chatSite').value=selected;await chooseSite();return;}
            status(state.config.sites.length?'Choose a site to open its chat.':'No site chat access yet. Ask your Owner to assign you to a site.');
        }catch(error){failure(error);}
    }
    const unreadTimer=setInterval(()=>{if(!document.hidden&&!state.site&&state.config)unread().catch(e=>status(e.message));},15000);
    window.addEventListener('pagehide',()=>{clearInterval(unreadTimer);$('chatUnread').textContent='';});
    init();
})();
