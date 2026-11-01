// Run with node tests/test_finance_export_ui.cjs; no browser/test packages required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const template = fs.readFileSync(path.join(root,'templates/app.html'),'utf8');
const loader = template.match(/let financeReportMonth=null[^\n]*\nasync function loadFinance\(\)[^\n]*/)[0];
const navigation = template.match(/function changeMonth\(n\)[^\n]*/)[0];
const downloader = fs.readFileSync(path.join(root,'static/alport-finance-export.js'),'utf8');
const data = {month:'2026-10', revenue:1500, gross_profit:1300, gross_margin:86.7, labour:80, overheads:120, cogs:200, ebitda:1100, ebitda_margin:73.3};
function setup() {
  const elements = {};
  const get = id => elements[id] ||= {disabled:false,textContent:'',innerHTML:'',dataset:{siteId:'1'},setAttribute(){},removeAttribute(){}};
  let clicks=0, requested='';
  const context = vm.createContext({$, document:{getElementById:get,body:{appendChild(){}},createElement(){return {click(){clicks++},remove(){}}}},
    api:async()=>data, money:String, toast(){}, mp:()=> '2026-10', month:new Date(2026,0,31), Date,
    fetch:async url=>{requested=url;return {ok:true,redirected:false,headers:{get:()=> 'application/pdf'},blob:async()=>({})}},
    URL:{createObjectURL:()=> 'blob:test', revokeObjectURL(){}}, URLSearchParams, setTimeout(){}});
  function $(id){return get(id)}
  vm.runInContext(loader+'\n'+navigation+'\n'+downloader, context);
  return {context,get,clicks:()=>clicks,requested:()=>requested};
}
(async()=>{
  let t=setup(); await t.context.loadFinance();
  assert.equal(t.get('financeExport').disabled,false);
  await t.context.exportFinancePDF();
  assert.equal(t.clicks(),1); assert.match(t.requested(),/month=2026-10&site_id=1/);
  assert.match(t.get('financeExportStatus').textContent,/download started/);

  t=setup(); t.context.api=async()=>{throw Error('Offline')}; await t.context.loadFinance();
  assert.equal(t.get('financeExport').disabled,true); await t.context.exportFinancePDF(); assert.equal(t.clicks(),0);

  t=setup(); await t.context.loadFinance(); t.context.fetch=async()=>({ok:false,json:async()=>({error:'Venue changed'})});
  await t.context.exportFinancePDF(); assert.equal(t.clicks(),0); assert.equal(t.get('financeExportStatus').textContent,'Venue changed');

  t=setup(); await t.context.loadFinance(); t.context.fetch=async()=>({ok:true,redirected:true,json:async()=>{throw Error()}});
  await t.context.exportFinancePDF(); assert.equal(t.clicks(),0); assert.match(t.get('financeExportStatus').textContent,/sign-in/);

  t=setup(); let pending=[];t.context.api=()=>new Promise(resolve=>pending.push(resolve));
  const first=t.context.loadFinance(), second=t.context.loadFinance();
  pending[1]({...data,month:'2026-11'});await second;pending[0](data);await first;
  assert.equal(t.get('fMonth').textContent,'2026-11');
  assert.equal(vm.runInContext('financeReportMonth',t.context),'2026-11');

  t=setup();t.context.changeMonth(1);assert.equal(t.context.month.getMonth(),1);assert.equal(t.context.month.getDate(),1);
  console.log('6 finance UI checks passed: download, load failure, server error, expired sign-in, stale responses, month navigation.');
})().catch(error=>{console.error(error);process.exitCode=1});
