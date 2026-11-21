/* SaaS shell uses the same publishing forms and APIs as the assessment app. */
let session = null, currentScreen = 'dashboard', registering = false, recovering = false;
let resetToken = new URLSearchParams(location.hash.slice(1)).get('reset') || '';
let verifiedNotice = '';
const authScreen = document.createElement('section'); authScreen.id='auth-screen'; authScreen.hidden=true;
authScreen.innerHTML=`<div class="auth-layout"><div class="auth-story"><div class="auth-brand">dispatch<b>.</b></div><div><span class="eyebrow" style="color:#bfe56b">Your publishing workspace</span><h1>One place for every story you share.</h1><p>Connect your channels, publish across platforms, and keep every result in view.</p><div class="network-chips"><span>YouTube</span><span>Instagram</span><span>TikTok</span><span>Mastodon</span><span>Bluesky</span><span>Telegram</span></div></div><footer>Built for creators, independent brands, and growing businesses.</footer></div><div class="auth-panel"><form id="auth-form"><div><h2 id="auth-heading">Welcome back</h2><p id="auth-description">Sign in to your publishing workspace.</p></div><div id="name-row" hidden><label for="auth-name">Your name</label><input id="auth-name" maxlength="80" autocomplete="name"></div><div><label for="auth-email">Email address</label><input id="auth-email" type="email" required maxlength="254" autocomplete="email"></div><div><label for="auth-password">Password</label><input id="auth-password" type="password" required minlength="12" maxlength="128" autocomplete="current-password"><p class="note">At least 12 characters.</p></div><button type="button" class="auth-toggle" id="forgot-password">Forgot password?</button><p class="error" id="auth-error" role="alert" hidden></p><button class="send" id="auth-submit">Sign in</button><p><span id="auth-prompt">New to Dispatch?</span> <button type="button" class="auth-toggle" id="auth-toggle">Create an account</button></p></form></div></div>`;
document.body.prepend(authScreen);
const sidebar=document.createElement('aside');sidebar.className='sidebar';sidebar.hidden=true;
sidebar.innerHTML=`<div class="brand">dispatch<b>.</b></div><button data-screen="dashboard">Overview</button><button data-screen="compose">Create a post</button><button data-screen="connections">Connected accounts</button><button data-screen="billing">Plan & billing</button><div class="sidebar-bottom">A private workspace for your channels and publishing history.</div>`;
document.body.prepend(sidebar);
const accountMenu=document.createElement('div');accountMenu.className='account-menu';accountMenu.hidden=true;
accountMenu.innerHTML=`<select id="workspace-switch" aria-label="Current workspace"></select><button class="connect" id="new-workspace">New workspace</button><button class="connect" id="logout">Sign out</button>`;
document.querySelector('header').append(accountMenu);
const verificationBanner=document.createElement('div');verificationBanner.className='global-notice';verificationBanner.hidden=true;verificationBanner.innerHTML='<span id="verification-message">Verify your email to activate publishing and account connections.</span> <button class="connect" type="button" id="resend-verification">Resend verification</button>';document.querySelector('main').prepend(verificationBanner);
const overview=document.createElement('section');overview.id='overview-screen';overview.hidden=true;
overview.innerHTML=`<div class="metric-grid"><div class="metric"><p>Publishing requests this month</p><strong id="metric-usage">0</strong><div class="usage-meter"><div id="usage-fill"></div></div></div><div class="metric"><p>Monthly allowance</p><strong id="metric-limit">50</strong></div><div class="metric"><p>Workspace plan</p><strong id="metric-plan">Free</strong></div></div><section class="card"><div class="card-body"><div class="result-head"><h2>Publishing activity</h2><button type="button" class="connect" id="refresh-history">Refresh</button></div><p class="note">Your latest 50 requests. Open a completed request to view its platform results.</p><div class="history-list" id="history-list"></div></div></section>`;
const billing=document.createElement('section');billing.id='billing-screen';billing.hidden=true;
billing.innerHTML=`<div class="plan-grid"><article class="card plan"><span class="plan-label">START HERE</span><h3>Free</h3><p>Your publishing essentials, in one workspace.</p><ul><li><span id="free-allowance">50</span> publishing requests per calendar month</li><li>Six supported platforms</li><li>Private connections and publishing history</li></ul><p>No payment required.</p></article><article class="card plan pro"><span class="plan-label">MORE ROOM TO PUBLISH</span><h3>Pro</h3><p>A larger publishing allowance for a growing channel.</p><ul><li><span id="pro-allowance">1000</span> publishing requests per calendar month</li><li>All Free features</li><li>Subscription management through Stripe</li></ul><button class="send" id="upgrade">Upgrade to Pro</button><p class="note">The configured price is shown in Stripe Checkout before you subscribe.</p></article></div><div class="billing-note" id="billing-state"></div><p class="note">One request can publish to up to three destinations. Failed and interrupted requests count toward usage. Replaying a completed request with the same request key does not count again. Allowances reset on the first day of each month in UTC.</p><div class="actions"><button class="connect" id="billing-portal">Manage billing</button><button class="connect" id="refresh-billing">Refresh plan</button></div><p class="error" id="billing-error" role="alert" hidden></p>`;
document.querySelector('main .intro').after(overview,billing);
const workspaceDialog=document.createElement('dialog');workspaceDialog.id='workspace-dialog';
workspaceDialog.innerHTML=`<form id="workspace-form"><h2>Create a workspace</h2><p>Connections, publishing history and billing are separate for each workspace.</p><div><label for="workspace-name">Workspace name</label><input id="workspace-name" required maxlength="80" placeholder="My brand"></div><p class="error" id="workspace-error" hidden></p><div class="dialog-actions"><button class="send" type="submit">Create workspace</button><button class="connect" type="button" id="workspace-cancel">Cancel</button></div></form>`;document.body.append(workspaceDialog);
async function saasRequest(path,method='GET',data){
 return jsonResponse(await fetch(path,{method,headers:{...apiHeaders(),...(data?{'Content-Type':'application/json'}:{})},...(data?{body:JSON.stringify(data)}:{})}));
}
function screen(name){
 currentScreen=name; document.body.dataset.screen=name;
 for(const button of sidebar.querySelectorAll('[data-screen]'))button.classList.toggle('active',button.dataset.screen===name);
 overview.hidden=name!=='dashboard';billing.hidden=name!=='billing';
 $('text-workspace').hidden=name!=='compose'&&name!=='connections';$('video-workspace').hidden=name!=='connections';
 document.querySelector('.tabs').hidden=name!=='compose';$('results').hidden=true;
 const titles={dashboard:['Workspace overview','Your channels and publishing activity, in one place.'],compose:['Create something worth sharing.','Write a message or upload a video, then review every delivery result.'],connections:['Your connected accounts','Connect the channels you want to publish to. Connections belong to this workspace.'],billing:['Your plan, your pace.','Manage this workspace’s publishing allowance and subscription.']};
 $('intro-title').textContent=titles[name][0];$('intro-description').textContent=titles[name][1];
 if(name==='connections'){configure();videoAccounts();}
 if(name==='compose'){selectTab(false);configure();}
 if(name==='dashboard'||name==='billing')refreshDashboard();
}
async function refreshDashboard(){
 if(!session?.authenticated)return;
 try{
  const data=await saasRequest('/dashboard');
  $('metric-usage').textContent=data.usage;$('metric-limit').textContent=data.limit;$('metric-plan').textContent=data.plan==='pro'?'Pro':'Free';
  $('usage-fill').style.width=Math.min(100,data.usage/Math.max(1,data.limit)*100)+'%';
  $('free-allowance').textContent=data.free_limit;$('pro-allowance').textContent=data.pro_limit;
  $('billing-state').textContent=data.billing_enabled?`Current plan: ${data.plan}. Subscription status: ${data.subscription_status}. Changes appear after Stripe confirms them.`:(session.user.admin?'Free accounts are available. Paid checkout is inactive until you configure Stripe.':'Free accounts are available. Pro subscriptions are not available yet. You can keep publishing on the Free plan.');
  $('upgrade').disabled=!data.billing_enabled||data.plan==='pro';$('billing-portal').disabled=!data.billing_enabled;
  $('history-list').replaceChildren();
  if(!data.history.length){const empty=document.createElement('div');empty.className='empty-state';empty.innerHTML='<div class="empty-symbol">＋</div><strong>Your next post starts here.</strong><p>Connect an account and create a post. Its results will appear here.</p>';const button=document.createElement('button');button.className='connect';button.textContent='Connect accounts';button.onclick=()=>screen('connections');empty.append(button);$('history-list').append(empty);}
  for(const item of data.history){
   const row=document.createElement('article');row.className='history-item';const info=document.createElement('div'),title=document.createElement('strong'),meta=document.createElement('small');
   title.textContent=item.title;meta.textContent=`${item.kind==='video'?'Video':'Text'} · ${item.created} UTC · ${item.status}`;info.append(title,meta);row.append(info);
   if(item.response){const button=document.createElement('button');button.className='connect';button.textContent='View results';button.onclick=()=>{report(item.response);$('results').scrollIntoView({behavior:'smooth'});};row.append(button);}else{const hint=document.createElement('small');hint.textContent='Processing or interrupted. Check your channel before retrying.';row.append(hint);}
   $('history-list').append(row);
  }
 }catch(e){$('history-list').textContent=e.message;}
}
async function bootstrap(){
 try{
  const verificationToken=new URLSearchParams(location.hash.slice(1)).get('verify');
  if(verificationToken){history.replaceState(null,'',location.pathname);const result=await saasRequest('/auth/verify-email','POST',{token:verificationToken});verifiedNotice=result.message;}
  session=await jsonResponse(await fetch('/auth/me'));window.dispatchUser=session.user||null;
  document.body.classList.remove('booting');
  if(!session.saas){await configure();return;}
  document.body.classList.toggle('saas',session.authenticated);
  authScreen.hidden=session.authenticated;document.querySelector('header').hidden=!session.authenticated;document.querySelector('main').hidden=!session.authenticated;sidebar.hidden=!session.authenticated;accountMenu.hidden=!session.authenticated;
  if(resetToken){authScreen.hidden=false;document.querySelector('header').hidden=true;document.querySelector('main').hidden=true;sidebar.hidden=true;accountMenu.hidden=true;setAuthMode('reset');return;}
  if(!session.authenticated){if(resetToken)setAuthMode('reset');else if(verifiedNotice)$('auth-description').textContent=verifiedNotice;return;}
  verificationBanner.hidden=!(session.verification_required&&!session.user.verified);
  $('workspace-switch').replaceChildren();for(const item of session.workspaces)$('workspace-switch').add(new Option(item.name,item.id));$('workspace-switch').value=session.workspace;
  document.querySelector("header .logo").textContent="Publishing workspace";
  document.querySelector("footer span:last-child").textContent="Your workspace. Your channels.";
  await configure();screen(currentScreen);
  if(new URLSearchParams(location.search).get('billing')==='success'){screen('billing');$('billing-state').textContent='Checkout returned successfully. Your plan updates after Stripe confirms the subscription. Use Refresh plan to check.';}
 }catch(e){document.body.classList.remove('booting');document.querySelector('header').hidden=true;document.querySelector('main').hidden=true;sidebar.hidden=true;accountMenu.hidden=true;authScreen.hidden=false;$('auth-error').hidden=false;$('auth-error').textContent=e.message;}
}
function setAuthMode(mode){
 registering=mode==='register';recovering=mode==='forgot';
 const reset=mode==='reset';
 $('name-row').hidden=!registering;$('auth-email').parentElement.hidden=reset;$('auth-email').required=!reset;
 $('auth-password').parentElement.hidden=recovering;$('auth-password').required=!recovering;
 $('auth-heading').textContent=reset?'Choose a new password':recovering?'Reset your password':registering?'Create your account':'Welcome back';
 $('auth-description').textContent=reset?'Enter a new password with at least 12 characters.':recovering?'Enter your email and we’ll send you a reset link.':registering?'Your first workspace is ready when you are.':'Sign in to your publishing workspace.';
 $('auth-submit').textContent=reset?'Save new password':recovering?'Send reset link':registering?'Create account':'Sign in';
 $('auth-prompt').textContent=reset||recovering?'Remember your password?':registering?'Already have an account?':'New to Dispatch?';
 $('auth-toggle').textContent=reset||recovering||registering?'Sign in':'Create an account';
 $('auth-password').autocomplete=registering||reset?'new-password':'current-password';$('forgot-password').hidden=reset||recovering||registering;
 $('auth-error').hidden=true;
}
$('auth-toggle').onclick=()=>{const back=registering||recovering||Boolean(resetToken);if(resetToken){resetToken='';history.replaceState(null,'',location.pathname);}setAuthMode(back?'login':'register');};
$('forgot-password').onclick=()=>setAuthMode('forgot');
$('auth-form').onsubmit=async event=>{
 event.preventDefault();$('auth-submit').disabled=true;$('auth-error').hidden=true;
 try{
  if(resetToken){const result=await saasRequest('/auth/reset-password','POST',{token:resetToken,password:$('auth-password').value});resetToken='';history.replaceState(null,'',location.pathname);$('auth-password').value='';session={saas:true,authenticated:false};window.dispatchUser=null;setAuthMode('login');$('auth-description').textContent=result.message;}
  else if(recovering){const result=await saasRequest('/auth/forgot-password','POST',{email:$('auth-email').value});$('auth-description').textContent=result.message;}
  else{await saasRequest(registering?'/auth/register':'/auth/login','POST',{email:$('auth-email').value,password:$('auth-password').value,name:$('auth-name').value});$('auth-password').value='';await bootstrap();}
 }catch(e){$('auth-error').textContent=e.message;$('auth-error').hidden=false;}
 finally{$('auth-submit').disabled=false;}
};
$('resend-verification').onclick=async()=>{$('resend-verification').disabled=true;try{const result=await saasRequest('/auth/resend-verification','POST');$('verification-message').textContent=result.message;}catch(e){$('verification-message').textContent=e.message;}finally{$('resend-verification').disabled=false;}};
sidebar.onclick=event=>{const button=event.target.closest('[data-screen]');if(button)screen(button.dataset.screen);};
$('logout').onclick=async()=>{try{await saasRequest('/auth/logout','POST');location.reload();}catch(e){alert(e.message);}};
$('workspace-switch').onchange=async()=>{try{await saasRequest('/workspaces/'+$('workspace-switch').value+'/switch','POST');location.reload();}catch(e){alert(e.message);}};
$('new-workspace').onclick=()=>{$('workspace-error').hidden=true;workspaceDialog.showModal();};$('workspace-cancel').onclick=()=>workspaceDialog.close();
$('workspace-form').onsubmit=async event=>{event.preventDefault();try{const data=await saasRequest('/workspaces','POST',{name:$('workspace-name').value});await saasRequest('/workspaces/'+data.id+'/switch','POST');location.reload();}catch(e){$('workspace-error').textContent=e.message;$('workspace-error').hidden=false;}};
async function billingAction(path,button){button.disabled=true;$('billing-error').hidden=true;try{const data=await saasRequest(path,'POST');const url=new URL(data.url);if(url.protocol!=='https:'||!['checkout.stripe.com','billing.stripe.com'].includes(url.hostname))throw Error('Unexpected billing destination.');location.assign(data.url);}catch(e){$('billing-error').hidden=false;$('billing-error').textContent=e.message;button.disabled=false;}}
$('upgrade').onclick=()=>billingAction('/billing/checkout',$('upgrade'));$('billing-portal').onclick=()=>billingAction('/billing/portal',$('billing-portal'));
$('refresh-history').onclick=refreshDashboard;$('refresh-billing').onclick=refreshDashboard;
bootstrap();
window.addEventListener('hashchange',()=>{
 const parameters=new URLSearchParams(location.hash.slice(1));
 if(parameters.has('reset')||parameters.has('verify')){resetToken=parameters.get('reset')||'';bootstrap();}
 else if(resetToken){resetToken='';setAuthMode('login');}
});
