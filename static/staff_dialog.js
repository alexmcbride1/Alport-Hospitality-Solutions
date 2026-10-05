'use strict';
// On-page dialogs work in browsers that disable window.prompt/confirm.
window.staffQuestion=function(message,options={}){
 return new Promise(resolve=>{
  const dialog=document.createElement('dialog');dialog.style.cssText='border:1px solid #b9cec0;border-radius:16px;padding:24px;width:min(520px,90vw);color:#183a35;font:16px system-ui;box-shadow:0 20px 80px #183a3555';
  const form=document.createElement('form');form.method='dialog';const label=document.createElement('label');label.textContent=message;label.style.cssText='display:block;line-height:1.6';form.append(label);
  let input;if(options.input){input=document.createElement('textarea');input.value=options.value||'';input.readOnly=!!options.readonly;input.maxLength=1000;input.style.cssText='box-sizing:border-box;width:100%;min-height:110px;margin:16px 0;padding:10px;border:1px solid #b9cec0;border-radius:8px;font:inherit';label.append(input);}
  const actions=document.createElement('div');actions.style.cssText='display:flex;justify-content:flex-end;gap:10px;margin-top:18px';
  const cancel=document.createElement('button');cancel.type='button';cancel.textContent=options.readonly?'Close':'Cancel';
  const submit=document.createElement('button');submit.type='submit';submit.textContent=options.readonly?'Done':options.input?'Save':'Continue';
  for(const b of [cancel,submit])b.style.cssText='padding:10px 16px;border:1px solid #a9c4b3;border-radius:8px;background:#edf5ee;color:#183a35;font:inherit;cursor:pointer';
  let finished=false;const finish=value=>{if(finished)return;finished=true;dialog.close();dialog.remove();resolve(value);};
  cancel.onclick=()=>finish(null);dialog.addEventListener('cancel',e=>{e.preventDefault();finish(null);});form.onsubmit=e=>{e.preventDefault();finish(options.input?input.value:true);};actions.append(cancel,submit);form.append(actions);dialog.append(form);document.body.append(dialog);dialog.showModal();if(input){input.focus();if(options.readonly)input.select();}
 });
};
