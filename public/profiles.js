let profileData=null,profileSelected=null;
window.clearProfiles=()=>{profileData=null;profileSelected=null;$('#profiles-view').innerHTML='';document.querySelectorAll('.profile-picker').forEach(p=>{delete p.closest('form').dataset.profilePicker;p.remove();});};
const editProfile=p=>me?.role==='Admin'||me?.role===(p.kind==='Customer'?'Sales':'Operations');
async function loadProfiles(){const userId=me?.id;try{const data=await api('/api/profiles');if(me?.id!==userId||currentView!=='profiles')return;profileData=data;renderProfiles();}catch(e){toast(e.message);}}
function renderProfiles(){
  const p=profileData.profiles.find(p=>p.id===profileSelected);
  const allowed=me.role==='Admin'?['Customer','Carrier']:me.role==='Sales'?['Customer']:['Carrier'];
  $('#profiles-view').innerHTML=`<div class="heading"><div><h1>Customers & carriers</h1><p>Keep standard contact details and instructions here. Copy them into new work, then review before saving.</p></div><button id="profile-new">New profile</button></div><div class="billing-layout"><section aria-label="Business profiles">${profileData.profiles.map(p=>`<button class="billing-item" data-profile="${p.id}"><strong>${escape(p.name)}</strong><span>${escape(p.kind)}${p.active?'':' · Archived'}</span></button>`).join('')||'<p>No saved profiles yet.</p>'}</section><section><h2>${p?escape(p.name):'New profile'}</h2><form id="profile-form"><fieldset ${p&&!editProfile(p)?'disabled':''}><label>Type<select name="kind" ${p?'disabled':''}>${(p?[p.kind]:allowed).map(k=>`<option>${k}</option>`).join('')}</select></label><div class="form-grid">${[['Name','name'],['Contact person','contact'],['Email','email'],['Phone','phone'],['Tax / registration ID','tax_id'],['Default pickup location','origin'],['Default delivery location','destination']].map(([l,k])=>field(l,k,p?.[k]||'',k==='email'?'email':'text',`maxlength="300" ${k==='name'?'required':''}`)).join('')}</div><label>Billing address<textarea name="address" maxlength="1500">${escape(p?.address||'')}</textarea></label><label>Standard instructions<textarea name="instructions" maxlength="1500">${escape(p?.instructions||'')}</textarea></label><button>Save profile</button></fieldset></form>${p&&editProfile(p)?`<form id="profile-archive"><label>Reason to ${p.active?'archive':'restore'}<input name="reason" required maxlength="1500"></label><button class="quiet">${p.active?'Archive':'Restore'} profile</button></form>`:''}<p id="profile-error" class="error" role="alert"></p>${p?`<details><summary>Profile history</summary>${profileData.history.filter(h=>h.profile_id===p.id).map(h=>`<p>${escape(date(h.time))} · ${escape(h.actor)}<br>${escape(h.message)}</p>`).join('')}</details>`:''}</section></div>`;
  $('#profile-new').onclick=()=>{profileSelected=null;renderProfiles();};
  document.querySelectorAll('[data-profile]').forEach(b=>b.onclick=()=>{profileSelected=b.dataset.profile;renderProfiles();});
  $('#profile-form').onsubmit=async e=>{e.preventDefault();try{const result=await api('/api/profiles/save',{...formObject(e.target),kind:p?.kind||e.target.elements.kind.value,id:p?.id,version:p?.version});profileSelected=result.id;await loadProfiles();}catch(err){$('#profile-error').textContent=err.message;}};
  if($('#profile-archive'))$('#profile-archive').onsubmit=async e=>{e.preventDefault();try{await api('/api/profiles/archive',{id:p.id,version:p.version,active:!p.active,reason:e.target.elements.reason.value});await loadProfiles();}catch(err){$('#profile-error').textContent=err.message;}};
}
async function attachProfilePicker(form,kind,mapping){
  if(form.dataset.profilePicker)return;
  form.dataset.profilePicker='loading';
  const userId=me?.id;
  const panel=document.createElement('div');panel.className='profile-picker';
  try{
    const d=await api('/api/profiles');if(!form.isConnected||me?.id!==userId)return;
    const rows=d.profiles.filter(p=>p.kind===kind&&p.active);
    panel.innerHTML=`<label>Saved ${kind.toLowerCase()}<select><option value="">Choose profile to copy</option>${rows.map(p=>`<option value="${p.id}">${escape(p.name)}</option>`).join('')}</select></label><label class="check-label"><input type="checkbox"> Replace the matching fields below with this profile's details.</label><button type="button" class="quiet">Copy profile details</button><p role="status"></p>`;
    panel.querySelector('button').onclick=()=>{
      const p=rows.find(p=>p.id===panel.querySelector('select').value);const status=panel.querySelector('[role="status"]');
      if(!p||!panel.querySelector('input').checked){status.textContent='Choose a profile and confirm replacing the matching fields.';return;}
      for(const [field,key] of Object.entries(mapping)){const input=form.elements.namedItem(field);if(input&&!input.disabled)input.value=p[key]||'';}
      status.textContent='Details copied. Review and save the form to apply them.';
    };
    form.prepend(panel);
  }catch(e){if(form.isConnected){panel.textContent='Saved profiles could not load. You can enter details manually.';form.prepend(panel);}}
}
new MutationObserver(()=>{
  if(!me||me.role==='Viewer')return;
  for(const [id,kind,mapping] of [
    ['quote-form','Customer',{customer:'name',email:'email',origin:'origin',destination:'destination',requirements:'instructions'}],
    ['dispatch-form','Carrier',{carrier:'name',phone:'phone',instructions:'instructions'}],
    ['invoice-edit','Customer',{customer_address:'address',customer_tax_id:'tax_id'}],
    ['create-form','Customer',{customer:'name',email:'email',origin:'origin',destination:'destination'}]
  ]){const form=$('#'+id);if(form&&!form.dataset.profilePicker&&!form.closest('[hidden]'))attachProfilePicker(form,kind,mapping);}
}).observe(document.body,{subtree:true,childList:true,attributes:true,attributeFilter:['hidden','open']});
