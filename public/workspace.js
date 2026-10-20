let workData = null, workScope = 'mine', workTeam = 'All', workLoading = false;
let workGeneration = 0;
window.clearWorkspace = () => {
  workGeneration++; workData=null; workScope='mine'; workTeam='All'; workLoading=false;
  $('#workspace-view').innerHTML='';
};
async function loadWorkspace() {
  if (workLoading) return;
  workLoading = true;
  const generation=workGeneration, userId=me?.id;
  try {
    const data = await api('/api/workspace');
    if (!me || me.id!==userId || generation!==workGeneration || currentView !== 'workspace') return;
    workData = data;
    renderWorkspace();
  } catch (error) {
    if (me?.id===userId && generation===workGeneration && currentView === 'workspace') {
      const status = $('#work-sync');
      if (status) status.textContent = `Updates paused: ${error.message}`;
      else $('#workspace-view').innerHTML = `<p role="alert">${escape(error.message)}</p><button id="work-retry">Retry</button>`;
      if ($('#work-retry')) $('#work-retry').onclick = loadWorkspace;
    }
  } finally { if(generation===workGeneration)workLoading = false; }
}
function renderWorkspace() {
  const d = workData;
  const rows = d.items.filter(i => (workTeam === 'All' || i.team === workTeam) &&
    (workScope === 'all' || i.owner_id === me.id || !i.owner_id));
  $('#workspace-view').innerHTML = `<div class="heading"><div><h1>My work</h1><p>Next actions across sales, deliveries and billing. Finish the step and it leaves this queue automatically.</p></div><button id="work-refresh" class="quiet">Refresh work</button></div>
    <div class="work-toolbar"><label>Show<select id="work-scope"><option value="mine">Mine and shared work</option><option value="all">All work I can access</option></select></label><label>Team<select id="work-team"><option>All</option>${[...new Set(d.items.map(i=>i.team))].sort().map(t=>`<option>${escape(t)}</option>`).join('')}</select></label><p id="work-sync" role="status">Updated ${escape(date(d.generated_at))} · Refreshes every 30 seconds</p></div>
    ${d.host ? `<details class="host-status"><summary>Host and backups</summary><p>${d.host.secure?'HTTPS is enabled. Staff access still depends on your private network.':'Local access only. Private-network access has not been connected.'}</p><p>${d.host.backup.last_backup?'Latest local backup: '+escape(date(d.host.backup.last_backup)):'Waiting for the first automatic backup.'}</p>${d.host.backup.error?`<p role="alert" class="error">${escape(d.host.backup.error)}</p>`:''}<p>Keep this computer running. Daily backups stay on this computer; copy them to separate protected storage.</p></details>`:''}
    ${d.notices.length?`<section class="notice-board"><h2>Team notifications</h2>${d.notices.map((n,index)=>`<div class="notice"><button class="notice-link" data-work-notice="${index}">${escape(n.message)}<small>${escape(date(n.created))}</small></button><button class="quiet" data-work-read="${index}">Mark read</button></div>`).join('')}</section>`:''}
    <section class="work-board" aria-label="Next actions"><div class="row"><h2>${rows.length} next action${rows.length===1?'':'s'}</h2><span>${rows.filter(i=>i.urgent).length} need attention</span></div>${rows.length?rows.map(i=>`<div class="work-row"><button class="work-item ${i.urgent?'work-urgent':''}" data-work-item="${d.items.indexOf(i)}"><span><strong>${escape(i.title)}</strong><small>${escape(i.reference)} · ${escape(i.team)} · ${escape(i.owner_name||'Shared team queue')}${i.task?' · '+escape(i.task.waiting):''}${i.followup_due?' · Follow-up due':''}${i.task_overdue?' · Task overdue':''}</small></span><span>${i.urgent?'Needs attention':i.due?'Due '+escape(i.due.slice(0,10)):'Open'}</span></button>${i.task?`<button class="quiet" data-work-manage="${d.items.indexOf(i)}">Manage task</button>`:''}</div>`).join(''):`<div class="empty"><h2>${me.role==='Viewer'?'Read-only workspace':'No pending actions in this view'}</h2><p>${me.role==='Viewer'?'Open Shipments or Dispatch to follow progress.':'New handoffs and approval requests will appear here automatically.'}</p></div>`}</section>`;
  if(typeof renderProductivity==='function')renderProductivity(d);
  $('#work-scope').value=workScope; $('#work-team').value=workTeam;
  $('#work-scope').onchange=e=>{workScope=e.target.value;renderWorkspace();};
  $('#work-team').onchange=e=>{workTeam=e.target.value;renderWorkspace();};
  $('#work-refresh').onclick=loadWorkspace;
  document.querySelectorAll('[data-work-item]').forEach(b=>b.onclick=()=>openWork(d.items[Number(b.dataset.workItem)]));
  document.querySelectorAll('[data-work-notice]').forEach(b=>b.onclick=()=>openWork({view:'quotes',record_id:d.notices[Number(b.dataset.workNotice)].quote_id}));
  document.querySelectorAll('[data-work-read]').forEach(b=>b.onclick=async()=>{
    b.disabled=true;
    try {await api('/api/notices/read',{id:d.notices[Number(b.dataset.workRead)].id});await loadWorkspace();}
    catch(e){toast(e.message);b.disabled=false;}
  });
}
async function openWork(item) {
  if(item.view==='adjustments')creditSelected=item.record_id;
  if(item.view==='quotes'){quoteSelected=item.record_id;await loadFlow();}
  if(item.view==='dispatch'){dispatchSelected=item.record_id;dispatchFilter='all';dispatchSearch='';}
  if(item.view==='billing'){billingSelected=item.record_id;billingFilter='all';billingSearch='';}
  if(item.view==='invoices')invoiceSelected=item.record_id;
  if(item.view==='payments'){paymentSelected=item.record_id;paymentFilter='All';paymentSearch='';}
  if(item.view==='payables'){payableSelected=item.record_id;payableFilter='All';}
  switchView(item.view);
}
setInterval(()=>{if(me && currentView==='workspace' && !busy && !document.querySelector('dialog[open]'))loadWorkspace();},30000);
if(me && currentView==='workspace')loadWorkspace();
