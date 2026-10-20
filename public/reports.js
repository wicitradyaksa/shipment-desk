let reportData=null,reportRequest=0,reportFilters={start:'',end:'',currency:''};
window.clearReports=()=>{reportRequest++;reportData=null;reportFilters={start:'',end:'',currency:''};$('#reports-view').innerHTML='';};
async function loadReports(filters=reportFilters){
  if(me?.role!=='Admin')return;
  const request=++reportRequest,userId=me.id;
  const button=$('#report-apply');if(button)button.disabled=true;
  try{
    const data=await api('/api/reports?'+new URLSearchParams(filters));
    if(request!==reportRequest||me?.id!==userId||me?.role!=='Admin'||currentView!=='reports')return;
    reportData=data;reportFilters=data.filters;renderReports();
  }catch(e){
    if(request!==reportRequest||me?.id!==userId||currentView!=='reports')return;
    if($('#report-error'))$('#report-error').textContent=e.message;
    else $('#reports-view').innerHTML=`<p class="error" role="alert">${escape(e.message)}</p><button id="report-retry">Retry reports</button>`;
    if($('#report-retry'))$('#report-retry').onclick=()=>loadReports();
  }finally{if(request===reportRequest&&$('#report-apply'))$('#report-apply').disabled=false;}
}
function reportTable(key,t){
  const link='/api/reports/export?'+new URLSearchParams({...reportFilters,table:key});
  const columns=t.columns;
  const body=rows=>rows.map(r=>`<tr>${columns.map(c=>`<td>${r[c.key]===null?'—':escape(String(r[c.key]??''))}</td>`).join('')}</tr>`).join('');
  const buckets=key.startsWith('profit_')?[
    {label:'Current billing approval',rows:t.rows.filter(r=>r.basis==='Approved')},
    {label:'Provisional · excluded from approved profit',rows:t.rows.filter(r=>r.basis==='Provisional')}
  ]:[{label:null,rows:t.rows}];
  return `<section class="report-section" aria-labelledby="report-${key}"><div class="row"><h2 id="report-${key}">${escape(t.title)}</h2><a href="${escape(link)}">Export CSV</a></div>${buckets.map(b=>`${b.label?`<h3>${escape(b.label)}</h3>`:''}${b.rows.length?`<div class="report-scroll" tabindex="0" aria-label="${escape(t.title)} table"><table class="cost-table"><thead><tr>${columns.map(c=>`<th scope="col">${escape(c.label)}</th>`).join('')}</tr></thead><tbody>${body(b.rows)}</tbody></table></div>`:'<p class="muted">No matching records.</p>'}`).join('')}</section>`;
}
function renderReports(){
  const d=reportData,f=d.filters;
  $('#reports-view').innerHTML=`<div class="heading"><div><h1>Reports</h1><p>Financial summaries and operations results from recorded transactions.</p></div><button id="report-refresh" class="quiet">Refresh reports</button></div><form id="report-filters" class="work-toolbar">${field('From (inclusive)','start',f.start,'date')}${field('To (inclusive)','end',f.end,'date')}<label>Currency<select name="currency"><option value="">All currencies · separate totals</option>${d.currencies.map(c=>`<option ${c===f.currency?'selected':''}>${escape(c)}</option>`).join('')}</select></label><button id="report-apply">Apply filters</button><button type="button" id="report-reset" class="quiet">All dates & currencies</button></form><p id="report-error" class="error" role="alert"></p><p class="muted">Balances as of ${escape(d.as_of)} (host calendar date). Generated ${escape(date(d.generated_at))}. Monetary values remain in their recorded currencies. No exchange conversion.</p><details class="notice-board"><summary>Report definitions and date filters</summary><p>Profit and Operations use shipment creation dates; months use UTC creation dates. Receivables use invoice document dates, payables use supplier bill dates, and cash uses recorded payment dates. Blank dates include all records. Aging always shows today's remaining balances, including all active payments, even when payment dates fall outside the document filter. Clear date filters to reconcile the full Payments queue.</p><p>Approved profit includes only shipments with current billing approval. Revenue is reduced by issued credit-note net amounts. Provisional figures include other shipment costs and known accepted revenue, shown separately. Missing commercial terms or a mismatch between credited invoice currency and shipment terms display — for revenue, profit and margin. Margin is calculated from grouped profit divided by grouped revenue; zero revenue displays —.</p><p>Payables include only bills with current matching Admin approval. Outdated, draft, returned and cancelled bills are excluded. Cash is net recorded movement, without an opening bank balance, deducts active customer refunds and excludes all reversed entries. Refund outflows are shown separately.</p><p>On-time delivery compares recorded Dispatch delivery timestamps with the current expected delivery time. Delivered shipments missing timestamps are excluded from that percentage and listed separately. Exception/delay counts include saved alerts, current exceptions, current overdue deliveries and recorded late deliveries; each shipment is counted once in the combined figure. Historical original ETAs are not reconstructed.</p><p>Delivery-to-invoice uses the recorded delivery and actual invoice issuance timestamps. Invoice-to-paid uses invoice document date and the date active, non-reversed receipts reach the full invoice total. Invoices with credits or refunds, partial/unpaid invoices and missing or negative duration samples are excluded; sample counts are shown. CSV exports use the same filters, and profit CSVs include both clearly labeled bases. Negative amounts are supported; customer text is protected against spreadsheet formulas.</p></details><div class="report-jumps">${Object.entries(d.tables).map(([k,t])=>`<a href="#report-${k}">${escape(t.title)}</a>`).join('')}</div>${Object.entries(d.tables).map(([k,t])=>reportTable(k,t)).join('')}`;
  $('#report-filters').onsubmit=e=>{e.preventDefault();loadReports(formObject(e.target));};
  $('#report-reset').onclick=()=>loadReports({start:'',end:'',currency:''});
  $('#report-refresh').onclick=()=>loadReports();
}
if(me?.role==='Admin'&&currentView==='reports')loadReports();
