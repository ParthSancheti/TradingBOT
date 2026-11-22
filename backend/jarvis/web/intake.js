"use strict";
function dataView(id,title){
  const nav=node("button",title,"nav-item");nav.dataset.view=id;document.querySelector(".rail nav").append(nav);
  const section=node("section",undefined,"view");section.id="view-"+id;section.hidden=true;
  const heading=node("div",undefined,"section-heading");heading.append(node("h2",title));section.append(heading);
  document.querySelector(".content").insertBefore(section,document.querySelector(".desk-footer"));
  nav.onclick=()=>{
    document.querySelectorAll(".view").forEach(v=>v.hidden=v!==section);
    document.querySelectorAll(".nav-item").forEach(b=>b.classList.toggle("selected",b===nav));
    $("crumb").textContent=$("view-title").textContent=title;
    load();
  };
  let load=()=>{};return {section,heading,onOpen:fn=>{load=fn;}};
}
const intakeView=dataView("intake","Trade intake");
intakeView.section.append(node("p","Up to 20 recently updated messages from your allowed sources. These are review records; supplier offers are not owned inventory.","section-intro"));
const intakeRefresh=node("button","Refresh intake","button");intakeView.heading.append(intakeRefresh);
const intakeCards=node("div",undefined,"task-list");intakeView.section.append(intakeCards);
async function loadIntake(){
  try{
    const state=await api("/api/intake");intakeCards.replaceChildren();
    if(!state.messages.length)intakeCards.append(node("p","No messages imported. Complete setup and start observation to import verified text.","quiet"));
    for(const record of state.messages){
      const m=record.data,card=node("article",undefined,"task");
      card.append(node("h3",m.triage?.category||m.state),node("p",m.phone,"quiet"),node("p",m.text,"message-evidence"));
      card.append(node("small",`${m.state} · revision ${record.revision} · ${m.triage?.reason||"Review pending"}`));intakeCards.append(card);
    }
  }catch(error){intakeCards.replaceChildren(node("p",error.message,"notice"));}
}
intakeView.onOpen(loadIntake);intakeRefresh.onclick=()=>run(intakeRefresh,loadIntake);

const marketView=dataView("market","News & exchange rate");
marketView.section.append(node("p","Timestamped provider data. News does not automatically change prices. Exchange rates are indicative.","section-intro"));
const marketRefresh=node("button","Refresh provider data","button");marketView.heading.append(marketRefresh);
const marketCards=node("div",undefined,"task-list");marketView.section.append(marketCards);
async function loadMarket(){
  try{
    const state=await api("/api/market");marketCards.replaceChildren();
    const fx=state.fx?.data;
    if(fx){const card=node("article",undefined,"task");card.append(node("h3",`${fx.pair}: ${fx.rate}`),node("p",`${fx.source} · as of ${new Date(fx.as_of).toLocaleString()} · ${fx.kind}`));
      if(Date.now()-Date.parse(fx.as_of)>300000)card.append(node("strong","Stale rate — not usable for a new quote"));marketCards.append(card);}
    else marketCards.append(node("p","No verified FX snapshot. Add an entitled provider key in setup.","quiet"));
    const articles=state.news?.data?.articles||[];
    if(!articles.length)marketCards.append(node("p","No recent sourced news saved. Provider credentials and production entitlement are required.","quiet"));
    for(const article of articles){
      let url;try{url=new URL(article.url);if(url.protocol!=="https:" || url.username || url.password)continue;}catch{continue;}
      const card=node("article",undefined,"task");const link=node("a",article.title);link.href=url.href;link.target="_blank";link.rel="noopener noreferrer";card.append(link,node("p",`${article.source||"Publisher"} · ${new Date(article.published_at).toLocaleString()}`,"quiet"));marketCards.append(card);
    }
  }catch(error){marketCards.replaceChildren(node("p",error.message,"notice"));}
}
marketView.onOpen(loadMarket);marketRefresh.onclick=()=>run(marketRefresh,async()=>{const result=await api("/api/market/refresh",{});await loadMarket();report(`FX: ${result.fx}. News: ${result.news}.`);});
