"use strict";
let setupRevision=0;
let contactRevision=0;
function setupNotice(text){$("setup-message").textContent=text;$("setup-message").hidden=false;}
function stage(name){document.querySelectorAll(".setup-stage").forEach(s=>s.hidden=s.id!=="stage-"+name);}
document.querySelectorAll("[data-stage]").forEach(b=>b.onclick=()=>stage(b.dataset.stage));
function addSource(value={}){
  const row=node("div",undefined,"source-row");
  const phone=node("input");phone.type="tel";phone.placeholder="+91…";phone.required=true;phone.value=value.phone||"";phone.setAttribute("aria-label","Source phone number");
  const label=node("input");label.placeholder="Supplier or buyer name";label.required=true;label.value=value.label||"";label.setAttribute("aria-label","Source label");
  const purpose=node("select");purpose.setAttribute("aria-label","Source purpose");for(const p of ["inventory","buyer","both"]){const option=node("option",p);option.value=p;purpose.append(option);}purpose.value=value.purpose||"inventory";
  const enabled=node("input");enabled.type="checkbox";enabled.checked=value.active!==false;enabled.setAttribute("aria-label","Source enabled");enabled.title="Enable this source";
  const remove=node("button","Remove","button");remove.type="button";remove.onclick=()=>row.remove();row.append(phone,label,purpose,enabled,remove);$("sources").append(row);
}
$("add-source").onclick=()=>addSource();addSource();
const budgetLabel=node("label","Daily paid classification limit (UTC)");
const budgetInput=node("input");Object.assign(budgetInput,{name:"classifier_daily_limit",type:"number",min:"1",max:"10000",value:"200",required:true});budgetLabel.append(budgetInput);
$("business-form").insertBefore(budgetLabel,$("business-form").querySelector("fieldset"));
const contactSection=node("section",undefined,"security-summary");contactSection.append(node("h3","Confirm a contact’s timezone"),node("p","Confirm the timezone with the contact before saving it. A country code does not establish where somebody currently is. Scheduling is a later capability; this saves its verified contact data."));
const contactForm=node("form",undefined,"setup-form");contactForm.id="contact-form";
for(const [name,label,placeholder] of [["phone","Contact number","+1…"],["label","Contact name","Buyer or supplier"],["timezone","Confirmed IANA timezone","America/New_York"]]){
  const wrap=node("label",label);const input=node("input");Object.assign(input,{name,placeholder,required:true});wrap.append(input);contactForm.append(wrap);
}
const lookup=node("button","Check stored timezone","button");lookup.type="button";
const confirm=node("button","Save confirmed timezone","button primary");confirm.type="submit";
const contactMessage=node("p",undefined,"quiet");contactMessage.setAttribute("role","status");
contactForm.append(lookup,confirm);contactSection.append(contactForm,contactMessage);$("stage-business").append(contactSection);
contactForm.elements.namedItem("phone").addEventListener("input",()=>{contactRevision=0;});
lookup.onclick=()=>run(lookup,async()=>{
  try{const phone=contactForm.elements.namedItem("phone").value;const result=await api("/api/contacts/hint?phone="+encodeURIComponent(phone));contactRevision=result.revision||0;contactMessage.textContent=result.can_schedule?`Confirmed timezone: ${result.timezone}`:`Country hint: ${result.country_hint||"unknown"}. No confirmed timezone.`;}catch(error){contactMessage.textContent=error.message;}
});
contactForm.onsubmit=e=>{e.preventDefault();run(confirm,async()=>{
  try{const data=Object.fromEntries(new FormData(contactForm));const stored=await api("/api/contacts/confirm",{...data,expected_revision:contactRevision});contactRevision=stored.revision;contactMessage.textContent="Confirmed timezone saved to Google Sheets.";}catch(error){contactMessage.textContent=error.message;}
});};
async function loadOnboarding(){
  try{const state=await api("/api/onboarding");if(state.setup){setupRevision=state.setup.revision;const data=state.setup.data;for(const [key,value] of Object.entries(data)){const input=$("business-form").elements.namedItem(key);if(input && !Array.isArray(value))input.value=value;}$("sources").replaceChildren();data.sources.forEach(addSource);}setupNotice(state.sheets_connected?"Google Sheets connection verified. Review daily settings below.":"Connect your Apps Script endpoint first. No business records are stored locally.");}catch(e){setupNotice(e.message);}
}
document.querySelector('[data-view="onboarding"]').addEventListener("click",loadOnboarding);
$("connect-form").onsubmit=e=>{e.preventDefault();run(e.submitter,async()=>{try{const payload=Object.fromEntries(new FormData(e.target));await api("/api/onboarding/connect",payload);for(const input of e.target.querySelectorAll('input[type="password"]'))input.value="";setupNotice("Connection verified. Secrets saved in the OS credential store.");stage("business");await refresh();}catch(error){setupNotice(error.message);}});};
$("business-form").onsubmit=e=>{e.preventDefault();run(e.submitter,async()=>{try{const data=Object.fromEntries(new FormData(e.target));data.margin_pct=Number(data.margin_pct);data.classifier_daily_limit=Number(data.classifier_daily_limit);data.poll_seconds=60;data.sources=[...$("sources").children].map(row=>({phone:row.children[0].value,label:row.children[1].value,purpose:row.children[2].value,active:row.children[3].checked}));const record=await api("/api/onboarding/setup",{setup:data,expected_revision:setupRevision});setupRevision=record.revision;setupNotice("Business settings saved to Google Sheets.");stage("bridge");}catch(error){setupNotice(error.message);}});};
for(const operation of ["start","stop"])$("runner-"+operation).onclick=()=>run($("runner-"+operation),async()=>{try{const result=await api("/api/runner/"+operation,{});$("runner-state").textContent=result.state;}catch(error){$("runner-state").textContent=error.message;}});
