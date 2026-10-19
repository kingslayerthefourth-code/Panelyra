'use strict';
let book,checkState={results:{},jobs:[]},choices=new Set(),filterKey='panelyra:series:'+S.series,filters=localGet(filterKey,{q:'',sort:'new',filter:'all',page:0,edition:'Original',scroll:0});
document.body.classList.toggle('series-page',S.view!=='updates');
const chapterCount=n=>`${n} chapter${n===1?'':'s'}`;
const query=new URLSearchParams(location.search);if(query.has('library_search'))sessionStorage.setItem('panelyra:library-query',query.get('library_search'));
const libraryBack=S.library+'&library_search='+encodeURIComponent(sessionStorage.getItem('panelyra:library-query')||'')+(S.series?'#book-'+encodeURIComponent(S.series):'');
function remember(){localSet(filterKey,{...filters,scroll:scrollY})}addEventListener('pagehide',remember);
async function init(){if(S.view==='updates'){renderAll();await loadUpdates();return}book=await api('/api/series',{series:S.series});if(!book.editions[filters.edition])filters.edition=Object.keys(book.editions)[0];document.title=book.title+' · Panelyra';render();await loadUpdates();requestAnimationFrame(()=>scrollTo(0,filters.scroll||0))}
function render(){const editions=Object.keys(book.editions),rows=book.editions[filters.edition],valid=rows.filter(c=>c.verified&&c.healthy).sort((a,b)=>a.number-b.number),latest=valid.filter(c=>c.progress&&!c.progress.completed&&c.progress.percent>0).sort((a,b)=>b.progress.updated_at-a.progress.updated_at)[0],unread=valid.find(c=>!c.progress?.completed),start=latest||unread||valid[0],meta=book.metadata;const synopsis=meta.synopsis||meta.description;
app.innerHTML=`<header class="topline"><a class="brand" href="${esc(libraryBack)}">Panelyra</a><a class="button quiet" href="${esc(libraryBack)}">← Library</a></header><main class="shell"><section class="hero"><img class="cover" src="${esc(book.cover)}" alt="${esc(book.title)} cover"><div class="hero-info"><span class="eyebrow">Your library</span><h1>${esc(book.title)}</h1><p class="muted">${valid.length} saved chapter${valid.length===1?'':'s'}${rows.length>valid.length?' · '+(rows.length-valid.length)+' need review':''}</p><div class="pills">${['author','status','language'].filter(k=>meta[k]).map(k=>`<span class="pill">${esc(meta[k])}</span>`).join('')}${(Array.isArray(meta.genres)?meta.genres:[]).map(g=>`<span class="pill">${esc(g)}</span>`).join('')}</div>${synopsis?`<details class="description"><summary>Synopsis</summary><p>${esc(synopsis)}</p></details>`:''}<div class="hero-actions">${start?`<a class="button primary" href="${esc(start.url)}">${latest?'Continue chapter '+esc(latest.id):unread&&valid.some(c=>c.progress?.completed)?'Read chapter '+esc(unread.id):'Start reading'}</a>`:'<span class="muted">No readable chapters yet</span>'}</div></div></section><section aria-labelledby="chapters-heading"><div class="section-heading"><h2 id="chapters-heading">Saved chapters</h2></div><div class="series-update-bar"><div class="series-update-copy"><span id="update-summary"></span><span id="last-check" class="sticky-check">Not checked yet</span></div><div class="series-update-actions"><button id="check" class="quiet">Check for updates</button><button id="browse-source">Browse source chapters</button></div></div><div id="update-result" role="status" aria-live="polite"></div><div class="tools"><input id="search" aria-label="Search chapters" placeholder="Find a chapter" value="${esc(filters.q)}"><select id="filter" aria-label="Reading status"><option value="all">All chapters</option><option value="unread">Unread</option><option value="progress">In progress</option></select><select id="sort" aria-label="Chapter order"><option value="new">Newest first</option><option value="old">Oldest first</option></select>${editions.length>1?`<select id="edition" aria-label="Edition">${editions.map(e=>`<option value="${e}">${e==='Clean'?'Trimmed':'Original'}</option>`).join('')}</select>`:''}</div><div id="chapter-list" class="chapter-list"></div><div id="pager" class="pager"></div></section></main>`;
document.querySelector('.cover').onerror=e=>{e.target.style.visibility='hidden'};document.querySelector('#filter').value=filters.filter;document.querySelector('#sort').value=filters.sort;
for(const [id,key] of [['search','q'],['filter','filter'],['sort','sort']])document.querySelector('#'+id).addEventListener(id==='search'?'input':'change',e=>{filters[key]=e.target.value;filters.page=0;remember();renderRows()});if(editions.length>1){document.querySelector('#edition').value=filters.edition;document.querySelector('#edition').onchange=e=>{filters.edition=e.target.value;filters.page=0;remember();render();renderUpdate()}}document.querySelector('#browse-source').onclick=openSourcePanel;document.querySelector('#check').onclick=()=>startCheck(S.series);renderRows()}
function chapterSubtitle(c){
 const title=(c.title||'').trim();
 const norm=t=>t.toLowerCase().replace(/[^a-z0-9]/g,'');
 if(!title||norm(title)===norm('Chapter '+c.id))return '';
 const leading=title.split(/\bchapter\s*[0-9]/i)[0];
 if(norm(leading)&&norm(leading)===norm(book.title))return '';
 return title;
}
function renderRows(){let rows=book.editions[filters.edition].filter(c=>(c.id+' '+c.title).toLowerCase().includes(filters.q.toLowerCase())&&(filters.filter==='all'||filters.filter==='unread'&&!c.progress?.completed||filters.filter==='progress'&&c.progress?.percent>0&&!c.progress.completed));rows.sort((a,b)=>(filters.sort==='old'?1:-1)*((a.number??-1)-(b.number??-1)));const pages=Math.max(1,Math.ceil(rows.length/50));filters.page=Math.max(0,Math.min(pages-1,filters.page));document.querySelector('#chapter-list').innerHTML=rows.slice(filters.page*50,filters.page*50+50).map(c=>`<article class="chapter-row ${c.progress?.completed?'read':c.progress?.percent>0?'in-progress':''}"><div class="chapter-main"><strong>Chapter ${esc(c.id)}</strong><small>${!c.verified?'Identity needs review':!c.healthy?'Files need repair':c.progress?.completed?'Read':c.progress?.percent>0?'Reading · '+Math.round(c.progress.percent)+'%':'Unread'}${chapterSubtitle(c)?' · '+esc(chapterSubtitle(c)):''}</small></div><span class="chapter-meta">${c.pages} images · Saved</span><div class="row-actions">${c.healthy?`<a class="button quiet" href="${esc(c.url)}">Read →</a>`:'<span class="muted">Repair</span>'}<button class="quiet" data-more="${esc(c.id)}" aria-label="More for chapter ${esc(c.id)}">•••</button></div></article>`).join('')||'<p class="empty">No chapters match your filters.</p>';document.querySelector('#pager').hidden=pages===1;document.querySelector('#pager').innerHTML=`<button id="prev-page" ${filters.page===0?'disabled':''}>Previous</button><small>${chapterCount(rows.length)} · Page ${filters.page+1} of ${pages}</small><button id="next-page" ${filters.page>=pages-1?'disabled':''}>Next</button>`;document.querySelector('#prev-page').onclick=()=>{filters.page--;remember();renderRows()};document.querySelector('#next-page').onclick=()=>{filters.page++;remember();renderRows()};document.querySelectorAll('[data-more]').forEach(b=>b.onclick=()=>chapterMore(b.dataset.more))}
function chapterMore(id){const c=book.editions[filters.edition].find(c=>c.id===id);const d=showDialog(`<h2>Chapter ${esc(id)}</h2><p class="muted">${c.pages} images · ${filters.edition==='Clean'?'Trimmed':'Original'}</p>${!c.verified?'<p>Chapter identity is unverified. Existing files are preserved and this entry is excluded from update comparisons.</p>':''}<div class="tools"><button id="mark">Mark ${c.progress?.completed?'unread':'read'}</button><a class="button" href="${esc(url('/api/export',{series:S.series,edition:filters.edition,chapter:id}))}" download>Export CBZ</a><a class="button" href="${esc(S.library+'&series='+encodeURIComponent(S.series)+'&chapter='+encodeURIComponent(id)+'&manage=1')}">Files & trimming</a></div><div class="dialog-actions"><button data-close>Close</button></div>`);d.querySelector('#mark').onclick=async()=>{try{await post('/api/read-state',{series:S.series,chapter:id,edition:filters.edition,completed:!c.progress?.completed});book=await api('/api/series',{series:S.series});d.close();renderRows()}catch(e){notify(e.message,true)}}}
function sourceDialog(){const d=showDialog(`<h2>Series source</h2><p class="muted">Choose this title’s landing page. Checks only find chapters; nothing downloads automatically.</p><label>Series URL<input id="source-url" type="url" value="${esc(book.source?.url||'')}" placeholder="https://source.com/series/title"></label><label>Source language<select id="source-language"><option value="unknown">Not confirmed</option><option value="en">English</option><option value="ko">Korean</option></select></label><div class="dialog-actions"><button data-close>Cancel</button><button class="primary" id="save-source">Save source</button></div>`);d.querySelector('#source-language').value=book.source?.language||'unknown';d.querySelector('#save-source').onclick=async()=>{try{await post('/api/source',{series:S.series,url:d.querySelector('#source-url').value,language:d.querySelector('#source-language').value});book=await api('/api/series',{series:S.series});d.close();choices.clear();pendingSelections.clear();renderUpdate();notify('Source saved. Check for new chapters when ready.')}catch(e){notify(e.message,true)}}}
async function startCheck(series){try{await post('/api/update-check',{scope:series?'series':'all',series});await loadUpdates()}catch(e){notify(e.message,true)}}
function checkBusy(){return checkState.jobs.some(j=>['queued','running'].includes(j.state)&&(checkState.worker_running||Date.now()/1000-j.updated_at<15))}
let polling;async function loadUpdates(){try{checkState=await api('/api/updates',S.series?{series:S.series}:{});if(S.view==='updates')renderAllResults();else renderUpdate();clearTimeout(polling);if(checkBusy())polling=setTimeout(loadUpdates,1600)}catch(e){notify(e.message,true)}}
function statusText(r){return r.status==='ok'?(r.new_count?`${r.new_count} available chapters`:'No new chapters'):r.status==='partial'?`${r.new_count} available · source list may be incomplete`:r.error||'Availability is unverified'}
// Source browsing is deliberately separate from saved reading state.
let sourcePanel,sourceFilters={q:'',sort:'new',page:0},queueBusy=false;
const pendingSelections=new Set();
function currentResult(){return checkState.results[S.series]}
function sourceMatches(r){return !!r?.source&&r.source.url===book.source?.url&&r.source.language===book.source?.language}
function sourceRows(){const r=currentResult();return sourceMatches(r)?(r.chapters||[]).map(c=>({...c,availability:pendingSelections.has(String(c.number))?'queued':c.availability})):[]}
function pruneChoices(){const allowed=new Set(sourceRows().filter(c=>c.availability==='public_link').map(c=>String(c.number)));for(const n of choices)if(!allowed.has(n))choices.delete(n)}
function sourceMessage(r){
 if(!r)return 'No source check yet.';
 if(!sourceMatches(r))return 'Choose a source and check it before selecting chapters.';
 if(!['ok','partial'].includes(r.status))return r.error||'Source availability could not be verified. Try checking again.';
 return `${chapterCount(r.chapters.length)} listed by the source${r.complete?'.':' · list may be incomplete.'} Public links may not have downloadable images.`;
}
function renderUpdate(){
 const r=currentResult(),active=checkBusy(),button=document.querySelector('#check');
 button.disabled=active;button.textContent=active?'Checking…':'Check for updates';
 document.querySelector('#last-check').textContent=r?.last_successful_at?'Last checked '+new Date(r.last_successful_at*1000).toLocaleString():r?.checked_at?'Last attempt '+new Date(r.checked_at*1000).toLocaleString():'Not checked yet';
 const count=sourceRows().filter(c=>c.availability==='public_link').length;
 document.querySelector('#update-summary').textContent=active?'Checking this series…':r&&sourceMatches(r)&&['ok','partial'].includes(r.status)?`${count} not saved${r.complete?'':' · partial source list'}`:'Source availability';
 const interrupted=!active&&checkState.jobs.some(j=>['queued','running'].includes(j.state));
 const error=interrupted?'The check was interrupted. Use Check for updates to try again.':r&&(!sourceMatches(r)||!['ok','partial'].includes(r.status))?sourceMessage(r):'';
 document.querySelector('#update-result').innerHTML=error?`<p class="series-check-error">${esc(error)} <button class="quiet" id="recover-source">Source settings</button></p>`:'';
 document.querySelector('#recover-source')?.addEventListener('click',()=>{openSourcePanel();sourceDialog()});
 pruneChoices();if(sourcePanel?.open)renderSourcePanel();
}
function openSourcePanel(){
 if(sourcePanel?.open)return;
 const opener=document.activeElement;
 sourcePanel=document.createElement('dialog');sourcePanel.className='source-panel';sourcePanel.setAttribute('aria-labelledby','source-panel-title');
 sourcePanel.innerHTML=`<header class="source-panel-header"><div><h2 id="source-panel-title">Source chapters</h2><p>${esc(book.title)}</p></div><button class="quiet" id="close-source" aria-label="Close source chapters">✕</button></header><div class="source-panel-body"><div class="source-panel-info"><p id="source-description"></p><div class="source-links"><a id="source-link" class="subtle-link" target="_blank" rel="noreferrer">Open source</a><button id="source-settings" class="quiet">Source settings</button><button id="source-refresh" class="quiet">Refresh results</button></div><p id="source-refresh-error" role="status"></p></div><div class="source-panel-tools"><input id="source-search" type="search" aria-label="Search source chapters" placeholder="Find a source chapter"><select id="source-sort" aria-label="Source chapter order"><option value="new">Newest first</option><option value="old">Oldest first</option></select></div><div id="source-rows"></div><nav id="source-pager" class="pager" aria-label="Source chapter pages"></nav></div><footer class="source-panel-footer"><div class="source-selection"><span id="source-selection-count" role="status" aria-live="polite"></span><button id="source-clear" class="quiet">Clear</button><a class="subtle-link" href="${esc(S.downloads)}">View queue</a></div><p id="source-download-error" role="alert" hidden></p><button id="queue-selected" class="primary" disabled>Download selected</button><small>Downloads may open Chrome. Review chapters in Downloads before adding them to Library.</small></footer>`;
 document.body.append(sourcePanel);document.body.classList.add('source-panel-open');
 sourcePanel.addEventListener('close',()=>{document.body.classList.remove('source-panel-open');opener?.focus();sourcePanel.remove()},{once:true});
 sourcePanel.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();e.stopPropagation();sourcePanel.close()}},true);
 sourcePanel.querySelector('#close-source').onclick=()=>sourcePanel.close();
 sourcePanel.addEventListener('click',e=>{if(e.target===sourcePanel){const r=sourcePanel.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)sourcePanel.close()}});
 sourcePanel.querySelector('#source-settings').onclick=sourceDialog;
 sourcePanel.querySelector('#source-refresh').onclick=async()=>{
  const b=sourcePanel.querySelector('#source-refresh'),error=sourcePanel.querySelector('#source-refresh-error');b.disabled=true;error.textContent='';
  try{checkState=await api('/api/updates',{series:S.series});renderUpdate()}catch(e){error.textContent=e.message}finally{b.disabled=false}
 };
 for(const [id,key] of [['source-search','q'],['source-sort','sort']]){const el=sourcePanel.querySelector('#'+id);el.value=sourceFilters[key];el.addEventListener(key==='q'?'input':'change',e=>{sourceFilters[key]=e.target.value;sourceFilters.page=0;renderSourceRows()})}
 sourcePanel.querySelector('#source-clear').onclick=()=>{choices.clear();renderSourceRows()};
 sourcePanel.querySelector('#queue-selected').onclick=queueSourceSelection;
 sourcePanel.showModal();renderSourcePanel();sourcePanel.querySelector('#source-search').focus();
}
function renderSourcePanel(){
 pruneChoices();const r=currentResult(),d=sourcePanel;
 d.querySelector('#source-description').textContent=sourceMessage(r);
 const link=d.querySelector('#source-link');link.hidden=!book.source?.url;if(book.source?.url)link.href=book.source.url;
 renderSourceRows();
}
function renderSourceRows(){
 const q=sourceFilters.q.trim().toLowerCase();
 const rows=sourceRows().filter(c=>(`Chapter ${c.number} ${c.title||''}`).toLowerCase().includes(q));
 rows.sort((a,b)=>(sourceFilters.sort==='old'?1:-1)*(Number(a.number)-Number(b.number)));
 const pages=Math.max(1,Math.ceil(rows.length/50));sourceFilters.page=Math.max(0,Math.min(pages-1,sourceFilters.page));
 const list=sourcePanel.querySelector('#source-rows'),focused=document.activeElement?.getAttribute('data-source-number');
 const content=rows.slice(sourceFilters.page*50,sourceFilters.page*50+50).map(c=>{
  const selectable=c.availability==='public_link',number=String(c.number),label=({saved:'Saved',queued:'Queued',public_link:'Not saved',locked:'Locked'})[c.availability]||'Unverified';
  return `<label class="source-chapter-row ${selectable?'selectable':''}"><input type="checkbox" data-source-number="${esc(number)}" value="${esc(number)}" aria-label="Select chapter ${esc(number)}" ${!selectable||queueBusy?'disabled':''} ${choices.has(number)?'checked':''}><span>Chapter ${esc(number)}</span><small class="source-state state-${selectable?'available':c.availability==='saved'?'saved':'other'}">${label}</small></label>`;
 }).join('')||`<p class="empty">${q?'No source chapters match your search.':'No source chapters to display. Check for updates or review Source settings.'}</p>`;
 if(list.innerHTML!==content){list.innerHTML=content;if(focused)Array.from(list.querySelectorAll('[data-source-number]')).find(e=>e.dataset.sourceNumber===focused)?.focus({preventScroll:true})}
 list.querySelectorAll('input').forEach(el=>el.onchange=()=>{el.checked?choices.add(el.value):choices.delete(el.value);renderSourceFooter()});
 const nav=sourcePanel.querySelector('#source-pager');nav.hidden=pages===1;
 nav.innerHTML=`<button id="source-prev" ${sourceFilters.page===0?'disabled':''}>Previous</button><small>${chapterCount(rows.length)} · ${sourceFilters.page+1} / ${pages}</small><button id="source-next" ${sourceFilters.page===pages-1?'disabled':''}>Next</button>`;
 for(const [id,delta] of [['source-prev',-1],['source-next',1]])nav.querySelector('#'+id).onclick=()=>{sourceFilters.page+=delta;renderSourceRows();sourcePanel.querySelector('.source-panel-body').scrollTop=0;sourcePanel.querySelector('#source-search').focus({preventScroll:true})};
 renderSourceFooter();
}
function renderSourceFooter(){
 const d=sourcePanel;d.querySelector('#source-selection-count').textContent=`${choices.size} selected`;
 d.querySelector('#source-clear').disabled=queueBusy||choices.size===0;
 const button=d.querySelector('#queue-selected');button.disabled=queueBusy||choices.size===0;button.textContent=queueBusy?'Adding to queue…':'Download selected';
}
async function queueSourceSelection(){
 if(queueBusy)return;pruneChoices();if(!choices.size){renderSourceRows();return}
 queueBusy=true;const selected=[...choices],error=sourcePanel.querySelector('#source-download-error');error.hidden=true;renderSourceRows();
 try{
  await post('/api/queue-selected',{series:S.series,chapters:selected});
  selected.forEach(n=>{pendingSelections.add(n);choices.delete(n)});
  notify('Added to Downloads. Review completed chapters there.');renderUpdate();
 }catch(e){error.textContent=e.message;error.hidden=false;if(!error.isConnected)notify(e.message,true)}
 finally{queueBusy=false;if(sourcePanel?.open)renderSourceRows()}
}

function renderAll(){app.innerHTML=`<header class="topline"><a class="brand" href="${esc(libraryBack)}">Panelyra</a><a class="button quiet" href="${esc(libraryBack)}">← Library</a></header><main class="shell"><span class="eyebrow">Library</span><div class="section-heading"><h1>Chapter updates</h1><button id="check-all">Check all series</button></div><p class="muted">Checks only discover chapters. Choose downloads from each series page.</p><div id="all-results"></div></main>`;document.querySelector('#check-all').onclick=()=>startCheck(null)}
function renderAllResults(){const active=checkBusy();document.querySelector('#check-all').disabled=active;document.querySelector('#check-all').textContent=active?'Checking…':'Check all series';document.querySelector('#all-results').innerHTML=Object.entries(checkState.results).map(([key,r])=>`<section class="update-box"><div class="update-head"><a class="subtle-link" href="${esc(url('/series',{series:key}))}">${esc(key.replaceAll('-',' '))}</a><small>${new Date(r.checked_at*1000).toLocaleString()}</small></div><p>${esc(statusText(r))}</p></section>`).join('')||'<p class="empty">No checks yet. Open a series to check it individually.</p>'}
init().catch(fail);
