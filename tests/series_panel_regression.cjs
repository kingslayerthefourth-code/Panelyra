// Offline state/queue regressions; browser layout is checked separately.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const sourcePath = process.argv[2] || path.join(__dirname,'../panelyra/web/series.js');
const nodes=new Map();
function node(id){if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',hidden:false,disabled:false,querySelector:s=>node(s),querySelectorAll:()=>[],focus(){},addEventListener(){}});return nodes.get(id)}
const ctx=vm.createContext({console,URLSearchParams,Set,Date,Map,Promise,setTimeout,clearTimeout,
 S:{series:'book',view:'series',library:'/',downloads:'/downloads'},
 document:{body:{classList:{toggle(){}}},activeElement:null,querySelector:s=>node(s)},
 localGet:(_k,f)=>f,localSet(){},addEventListener(){},location:{search:''},sessionStorage:{getItem:()=>''},
 esc:x=>String(x??'').replaceAll('<','&lt;'),notify(){},post:async()=>({}),fail(e){throw e}
});
vm.runInContext(fs.readFileSync(sourcePath,'utf8').replace('init().catch(fail);',''),ctx);
const run=s=>vm.runInContext(s,ctx);
run(`book={title:'Im Really Not The Devils Lackey',source:{url:'https://example.com/book',language:'en'}};
 checkState={jobs:[],results:{book:{source:book.source,status:'partial',complete:false,chapters:[]}}};
 sourcePanel={open:true,querySelector:s=>document.querySelector(s)};`);
let tests=0;function test(name,fn){fn();tests++;console.log('PASS '+name)}
test('235 source chapters paginate to 50; saved entries disabled',()=>{
 run(`currentResult().chapters=Array.from({length:235},(_,i)=>({number:String(i),title:'Chapter '+i,availability:i===234?'saved':'public_link'}));renderSourcePanel()`);
 const html=node('#source-rows').innerHTML;
 assert.equal((html.match(/data-source-number=/g)||[]).length,50);assert.match(html,/Select chapter 234[^>]+disabled/);assert.match(node('#source-pager').innerHTML,/1 \/ 5/);
 assert.equal(node('#queue-selected').disabled,true);
});
test('selection persists through pagination, filtering and refresh',()=>{
 run(`choices.add('233');sourceFilters.page=1;renderSourceRows();sourceFilters.q='chapter 2';renderSourceRows();renderSourcePanel()`);
 assert.equal(run("choices.has('233')"),true);assert.equal(node('#source-selection-count').textContent,'1 selected');
});
test('refresh removes saved and queued selections',()=>{
 run(`choices.add('232');currentResult().chapters.find(c=>c.number==='233').availability='saved';currentResult().chapters.find(c=>c.number==='232').availability='queued';renderSourcePanel()`);
 assert.equal(run('choices.size'),0);assert.equal(node('#queue-selected').disabled,true);
});
test('fractional chapter order is numeric',()=>{
 run(`currentResult().chapters=['1','10','2.5','2'].map(number=>({number,availability:'public_link'}));sourceFilters={q:'',sort:'new',page:0};renderSourceRows()`);
 assert.deepEqual([...node('#source-rows').innerHTML.matchAll(/data-source-number="([^"]+)"/g)].map(x=>x[1]),['10','2.5','2','1']);
 assert.equal(node('#source-pager').hidden,true);
});
test('empty search retains selection, disables pagination',()=>{
 run(`choices.add('10');sourceFilters.q='nonexistent';renderSourceRows()`);
 assert.match(node('#source-rows').innerHTML,/No source chapters match/);assert.equal(run("choices.has('10')"),true);
});
test('failed check never leaves actionable stale selection',()=>{
 run(`currentResult().status='failed';currentResult().error='Offline. Try again.';currentResult().chapters=[];renderUpdate()`);
 assert.equal(run('choices.size'),0);assert.match(node('#update-result').innerHTML,/Offline/);assert.equal(node('#queue-selected').disabled,true);
});
test('source changes prevent using results from old mapping',()=>{
 run(`currentResult().chapters=[{number:'10',availability:'public_link'}];choices.add('10');book.source={url:'https://example.com/other',language:'en'};renderUpdate()`);
 assert.equal(run('sourceRows().length'),0);assert.equal(run('choices.size'),0);
});
test('redundant site titles hidden; meaningful subtitles retained',()=>{
 assert.equal(run(`chapterSubtitle({id:'200',title:'I’m Really Not the Devil’s Lackey Chapter 200 – Thunderscans EN'})`),'');
 assert.equal(run(`chapterSubtitle({id:'200',title:'The final battle'})`),'The final battle');assert.equal(run('chapterCount(1)'),'1 chapter');
});
(async()=>{
 run(`book.source={url:'https://example.com/book',language:'en'};currentResult().source=book.source;currentResult().status='ok';currentResult().chapters=[{number:'10',availability:'public_link'}];sourceFilters.q='';choices.add('10')`);
 let calls=0,resolve;ctx.post=()=>{calls++;return new Promise(r=>resolve=r)};
 const first=run('queueSourceSelection()');await run('queueSourceSelection()');assert.equal(calls,1);assert.equal(node('#queue-selected').disabled,true);
 resolve({job_id:'fake'});await first;assert.equal(run('choices.size'),0);assert.equal(run("sourceRows()[0].availability"),'queued');
 run(`choices.add('10')`);await run('queueSourceSelection()');assert.equal(calls,1);tests++;console.log('PASS in-flight and stale-results duplicate submissions suppressed');
 run(`pendingSelections.clear();choices.add('10')`);ctx.post=async()=>{throw Error('Source language needs confirmation')};await run('queueSourceSelection()');
 assert.equal(node('#source-download-error').hidden,false);assert.equal(run("choices.has('10')"),true);assert.equal(node('#queue-selected').disabled,false);tests++;console.log('PASS queue error visible; selection retained for retry');
 console.log(`${tests} frontend regressions passed`);
})().catch(e=>{console.error(e);process.exitCode=1});
