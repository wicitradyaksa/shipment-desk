let intakeCsv='',intakePreview=null,intakeUser=null,intakeSending=false;
let intakeGeneration=0;
window.clearIntake=()=>{intakeGeneration++;intakeCsv='';intakePreview=null;intakeUser=null;$('#intake-view').innerHTML='';};
async function loadIntake(){
  if(!['Admin','Sales'].includes(me?.role))return;
  const userId=me.id;
  try{
    const d=await api('/api/intake');if(me?.id!==userId||currentView!=='intake')return;
    intakeUser=userId;
    $('#intake-view').innerHTML=`<div class="heading"><div><h1>Order intake</h1><p>Import up to 100 orders at a time into Sales opportunities.</p></div><a href="/api/intake/template">Download CSV template</a></div><details class="notice-board"><summary>Prepare the file</summary><p>Use UTF-8 CSV, up to 100 KB. Keep the template headers. Use a unique order_reference for each customer; the same customer/reference cannot be imported twice. Dates must include a timezone, for example 2026-12-01T09:00:00+08:00. Pickup must be in the future.</p><p>Standard orders receive a proposal price; exceptions go to Operations review. Import creates opportunities only. Sending proposals and client acceptance use the existing workflow.</p><p>Configured routes: ${d.routes.map(r=>escape(r.id)+' = '+escape(r.origin)+' → '+escape(r.destination)).join('; ')||'None'}</p><p>Configured cargo: ${d.cargo.map(c=>escape(c.id)+' = '+escape(c.name)).join('; ')||'None'}</p><p>Blank optional numeric fields default to zero. All row errors must be fixed before importing. Re-uploading an identical file returns its previous import result; use a new order reference for a new order.</p></details><label>Order CSV file<input id="intake-file" type="file" accept=".csv,text/csv"></label><button id="intake-check">Validate & preview prices</button><p id="intake-error" class="error" role="alert"></p><section id="intake-result"></section>`;
    intakeGeneration++;intakePreview=null;intakeCsv='';
    $('#intake-file').onchange=()=>{intakeGeneration++;intakePreview=null;intakeCsv='';$('#intake-result').innerHTML='';};
    $('#intake-check').onclick=async()=>{
      if(intakeSending)return;intakeSending=true;
      const generation=intakeGeneration;
      const button=$('#intake-check');button.disabled=true;
      try{
        const file=$('#intake-file').files[0];if(!file||file.size>100000)throw Error('Choose a UTF-8 CSV up to 100 KB.');
        const source=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());
        const result=await api('/api/intake/preview',{csv:source});
        if(me?.id!==userId||currentView!=='intake'||generation!==intakeGeneration)return;
        intakeCsv=source;intakePreview=result;renderIntakePreview();$('#intake-error').textContent='';
      }catch(e){intakePreview=null;if(me?.id===userId&&$('#intake-error')){$('#intake-error').textContent=e.message;$('#intake-result').innerHTML='';}}
      finally{intakeSending=false;if(button.isConnected)button.disabled=false;}
    };
  }catch(e){toast(e.message);}
}
function renderIntakePreview(){
  const d=intakePreview;const errors=d.rows.some(r=>r.status==='Error');
  $('#intake-result').innerHTML=`<h2>Preview · pricing rules v${d.version}</h2><div class="report-scroll"><table class="cost-table"><thead><tr><th>Row</th><th>Order reference</th><th>Customer</th><th>Status</th><th>Price / estimate</th><th>Next step or error</th></tr></thead><tbody>${d.rows.map(r=>`<tr><td>${r.row}</td><td>${escape(r.reference)}</td><td>${escape(r.customer)}</td><td>${escape(r.status)}</td><td>${r.total===null?'—':escape(amount(r.total,r.currency))}</td><td>${escape(r.message)}</td></tr>`).join('')}</tbody></table></div><label class="check-label"><input id="intake-confirm" type="checkbox"> I reviewed these orders, prices and Operations review requirements.</label><button id="intake-commit" ${errors?'disabled':''}>Create Sales opportunities</button><p class="muted">${errors?'Fix errors in the CSV and preview again.':'Previously imported orders will be skipped.'}</p>`;
  $('#intake-commit').onclick=async()=>{
    if(intakeSending)return;intakeSending=true;const button=$('#intake-commit');button.disabled=true;
    try{
      if(!$('#intake-confirm').checked)throw Error('Confirm the preview first.');
      const result=await api('/api/intake/commit',{csv:intakeCsv,preview_hash:d.preview_hash,confirmed:true});
      if(me?.id!==intakeUser||currentView!=='intake')return;
      $('#intake-result').innerHTML=`<h2>${result.retry?'Previous import recovered':'Import complete'}</h2><p>${result.created} opportunities created; ${result.skipped} previously imported orders skipped.</p><button id="intake-open">Open Sales & Operations</button>`;
      $('#intake-open').onclick=async()=>{await loadFlow();switchView('quotes');};$('#intake-error').textContent='';
    }catch(e){if($('#intake-error'))$('#intake-error').textContent=e.message;}
    finally{intakeSending=false;if(button.isConnected)button.disabled=false;}
  };
}
if(['Admin','Sales'].includes(me?.role)&&currentView==='intake')loadIntake();
