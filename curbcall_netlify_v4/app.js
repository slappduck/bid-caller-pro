const SUPABASE_URL = "https://novwdthapkorstdtloky.supabase.co";
const SUPABASE_ANON_KEY = "sb_publishable_sUuQHkrN_wsJqakUMfL_VA_58koZ76C";
const SERVER = "https://bid-caller-pro.onrender.com";
const PORTAL_URL = "https://billing.stripe.com/p/login/3cIcN4an28420Yad2fejK00";
const SUPPORT_EMAIL = "support@curbcallpro.com";
// Cloudflare Turnstile site key (public, safe to ship in the bundle -- it is
// not a secret, the matching secret key lives only in the Supabase dashboard's
// Auth -> Attack Protection setting). Empty means "not configured yet": every
// captcha call below becomes a no-op and auth works exactly as it does today.
// Re-enabled 2026-09-25: root cause of the invalid-input-response failures
// was Supabase's Bot and Abuse Protection provider being set to hCaptcha
// while this widget issues Turnstile tokens. Fixed by switching the
// provider to Turnstile and re-pasting the matching secret key.
const TURNSTILE_SITE_KEY = "0x4AAAAAAFC7iEkah_AbfG1l";
// Residential Leads is hidden until the permit feed covers somewhere a
// customer actually works. It is wired to three cities -- Austin TX,
// Cambridge MA and Baton Rouge LA -- so for every contractor on the list
// today the tab is permanently empty, and an always-empty tab costs more
// credibility than a missing one. Everything behind it is intact: flip this
// to true when the coverage is there.
const LEADS_ENABLED = false;
// Keep identical to _REFERRAL_SEP in license_server.py and REFERRAL_SEP in
// index.html — the separator client_reference_id is split on server-side.
const REFERRAL_SEP = "~ref~";
(function stashReferral(){
  try{
    const ref=new URLSearchParams(location.search).get("ref");
    if(ref) localStorage.setItem("pending_ref", ref);
  }catch(e){}
})();

// Leaflet and supabase-js load from CDNs. The service worker caches both, but
// a very first run on a dead connection (or a blocked/ad-filtered CDN) can
// still leave them missing — and calling supabase.createClient() on a missing
// library throws right here, at the top level, killing this entire script.
// That failure mode is the worst one this app has: a contractor parked on a
// job site with no bars would get a dead sign-in screen instead of the bids
// already saved on their phone. So detect the libraries rather than assume
// them, and let the app fall back to a read-only local mode.
const HAS_SB = typeof supabase !== "undefined" && supabase && typeof supabase.createClient === "function";
const HAS_MAPS = typeof L !== "undefined" && L && typeof L.map === "function";
// Captured before Supabase's own async init can consume and strip the hash
// or query string. If someone lands here from a password reset email, this
// is how the PASSWORD_RECOVERY handling below can tell "the link worked"
// from "it didn't" -- an expired or already-used link, or (worse) a
// session already sitting in this browser racing ahead of it -- rather
// than silently treating either as an ordinary sign-in. Checked in both
// places because switching to PKCE below moves recovery links from a
// "#access_token=...&type=recovery" fragment to a "?code=...&type=recovery"
// query string.
const CAME_FROM_RESET_LINK = /type=recovery/.test(window.location.hash)
  || /type=recovery/.test(window.location.search)
  || /error_description/.test(window.location.hash)
  || /error_description/.test(window.location.search);
// The stateless recovery path below (verifyOtp with a token_hash) needs
// this read early too, for the same reason CAME_FROM_RESET_LINK is: whatever
// consumes the URL first wins, and Supabase's own client-side init runs
// asynchronously right after this file starts executing.
const RESET_TOKEN_HASH = (() => {
  const p = new URLSearchParams(window.location.search);
  return p.get("type") === "recovery" ? p.get("token_hash") : null;
})();
// Auth Logs (Authentication -> Logs -> Auth Logs) showed every failed
// reset attempt recording a "login" audit event -- the recovery grant
// taking effect -- immediately followed by an unexplained "logout" about
// a second later, before the person had even seen the new-password form
// long enough to use it. Nothing in this file calls signOut() on that
// path (checked every sb.auth.* call site), single-session-per-user is
// off, and time-boxed/inactivity session limits are locked off on this
// plan -- none of the usual explanations fit. What's left is the flow:
// Supabase's default email link points at Supabase's OWN /auth/v1/verify
// endpoint, which consumes the single-use token and issues a session on
// a bare HTTP GET -- no JS required -- so anything that fetches the link
// before the person taps it (a mail provider's own link-safety scanner is
// the common cause) burns it, and reuse-detection then revokes the session
// it just issued, which is exactly a login immediately followed by a
// logout. Switching flowType to PKCE moved the token into a code_verifier
// this browser keeps to itself, which defeats a scanner -- but it turned
// out to have the identical dependency on THIS BROWSER'S storage that the
// implicit flow already had (see authMode below), and a reset link is
// routinely opened somewhere else entirely -- Gmail's in-app browser is
// the common case -- where that storage never existed in the first place.
// RESET_TOKEN_HASH + verifyOtp (below, near showPasswordReset) is what
// actually fixes both problems at once: it's a single stateless API call
// that needs nothing but the emailed token itself, and it only runs when
// this JS executes, not on a bare GET of the link -- so it requires
// pointing the "Reset Password" email template at this app directly
// (`{{ .SiteURL }}/app.html?token_hash={{ .TokenHash }}&type=recovery`)
// instead of Supabase's default template, which still routes through
// /auth/v1/verify first.
const sb = HAS_SB ? supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
  auth:{flowType:"pkce"},
}) : null;

// ── storage helpers ──
// set() reports whether the write actually landed. It used to swallow every
// error, which matters because the three feeds only ever grow — upcoming and
// leads never prune at all — so hitting the ~5MB localStorage quota is a
// question of when, not if. Past that point starring a bid or typing a note
// silently did nothing and the work vanished on reload with no explanation.
let storageWarned=false;
// Declared here rather than beside the code that uses them, which sits
// several hundred lines further down. onAuthStateChange fires early and calls
// onboardingNeeded(), and DEVICE_KEYS is built just below and names
// TERMS_METHOD_KEY; a function declaration hoists but a const does not, so
// keeping these next to their use would put them in the temporal dead zone.
// TERMS_METHOD_KEY did exactly that once and threw "Cannot access
// 'TERMS_METHOD_KEY' before initialization" at load, taking the whole app
// down. Leave them here.
//
// TERMS_METHOD_KEY holds which signup route the person took, until a session
// exists to attach it to: the checkbox is ticked before Supabase has issued
// anything, and Google and magic-link only come back with a session after a
// round trip through another site.
const TERMS_METHOD_KEY="pending_terms_method";
const ONBOARD_KEY="onboarded";
const HOME_LOC_KEY="home_location";
const LAST_VISIT_KEY="last_visit_at";
let prevVisitAt=0;  // previous launch's time (ms), for Home's "new since"
const HOME_RADIUS_KEY="home_radius";
let onbRadius=50;

const store = {
  get(k,d){try{return JSON.parse(localStorage.getItem(k))??d;}catch{return d;}},
  set(k,v){
    try{localStorage.setItem(k,JSON.stringify(v));return true;}
    catch(e){
      // Warn once per session: a failed write usually comes in bursts and a
      // toast per key would bury the app.
      if(!storageWarned){
        storageWarned=true;
        try{toast("Storage full — tap Clear all bids on the Bids tab to free space.");}catch(_){}
      }
      return false;
    }
  }
};
// ── Whose data is cached on this device ──
//
// Sign-out ends the session and clears nothing else, so the previous
// account's bids, starred jobs, notes, pipeline, company profile and saved
// searches all stay in localStorage. Sign up as someone new on the same
// browser and the Bids tab opens already full of the last person's work.
//
// It does not stop at looking wrong. syncPullFeeds() finds no server row for
// a brand-new account and treats whatever this browser holds as the feed to
// seed it with, so the previous user's bids get UPLOADED into the new
// account and become its permanent server-side copy. On a shared office
// computer that is one contractor's pipeline silently handed to another.
//
// So the device records which account its cached data belongs to, and a
// different account signing in starts clean. Same account signing back in
// keeps its cache, which is the common case and costs a re-sync otherwise.
const DATA_OWNER_KEY="data_owner_email";
// The account id, which is what actually identifies an account. The email
// alone does not: deleting an account and signing up again with the same
// address produces a DIFFERENT account, and matching on the address read
// that as the same person and kept the old cache. The new account then
// opened full of the deleted one's bids, and syncPullFeeds() uploaded them
// into it. Kept alongside the address rather than replacing it so devices
// that only ever stored an address are not all wiped on their next sign-in.
const DATA_OWNER_ID_KEY="data_owner_id";

// Everything else is user data and goes. An allowlist rather than a list of
// keys to delete, so a key added later is cleared by default instead of
// quietly becoming the next thing that leaks between accounts.
//
// "sb-" is Supabase's own session token. Deleting it here would sign the user
// out during the very sign-in that triggered this.
const DEVICE_KEYS=new Set([
  "device_id",       // identifies the browser for trials, not the person
  "has_account",     // deliberately outlives sign-out: offer Sign in, not Create
  "bid_id_scheme",   // a data-migration marker
  "run_durations",   // local performance timings
  DATA_OWNER_KEY,
  DATA_OWNER_ID_KEY,
  // Written by the signup form seconds ago and consumed by
  // applyPendingSignupName() on this very sign-in. It belongs to the person
  // arriving, not the one who left, so clearing it here would throw away the
  // name they just typed.
  "pending_signup_name",
  // Ticked seconds ago by the person now signing in, and not yet posted to
  // the server. Clearing it here would silently lose the consent record for
  // the very account being created.
  TERMS_METHOD_KEY,
]);

function clearUserScopedData(){
  let keys=[],removed=0;
  try{for(let i=0;i<localStorage.length;i++)keys.push(localStorage.key(i));}
  catch(_){return 0;}
  for(const k of keys){
    if(!k||DEVICE_KEYS.has(k)||k.startsWith("sb-"))continue;
    try{localStorage.removeItem(k);removed++;}catch(_){}
  }
  return removed;
}

// True when the caller should stop and let the reload take over.
//
// Takes the user, not the email. An account is its id: an address can be
// reused by a different account entirely, which is exactly what happens when
// someone deletes their account and signs up again -- and that read as "same
// person, keep their cache" until this compared ids.
//
// Devices that were claimed before ids were stored hold only an address, so
// those still fall back to comparing addresses and record the id as they go.
// Comparing an absent stored id against a real one would treat every existing
// customer as a stranger and wipe the cache out from under all of them.
function claimDeviceFor(user){
  const id=((user&&user.id)||"").trim();
  const who=((user&&user.email)||"").trim().toLowerCase();
  if(!id&&!who)return false;
  const ownerId=(store.get(DATA_OWNER_ID_KEY,"")||"").trim();
  const owner=(store.get(DATA_OWNER_KEY,"")||"").trim().toLowerCase();
  const same=(ownerId&&id)?(ownerId===id):(!!owner&&owner===who);
  if(same){
    // Upgrade a legacy claim in passing, so the next comparison is exact.
    if(id&&!ownerId)store.set(DATA_OWNER_ID_KEY,id);
    return false;
  }
  const removed=clearUserScopedData();
  store.set(DATA_OWNER_KEY,who);
  if(id)store.set(DATA_OWNER_ID_KEY,id);
  // Reload rather than reset every in-memory copy by hand: bidData,
  // savedBids, notes, pipeline and the rest are already populated by the
  // time this runs, and missing one would put the cleared data straight back
  // on screen. The owner is recorded above first, so the reload does not
  // come back through here.
  // Only when something was actually carrying over. A brand-new account on a
  // clean browser removes nothing and must not bounce through a reload on the
  // first screen it ever shows.
  if(removed){location.reload();return true;}
  return false;
}

function deviceId(){
  let id=store.get("device_id",null);
  if(!id){id="web-"+Math.random().toString(36).slice(2)+Date.now().toString(36);store.set("device_id",id);}
  return id;
}
function licenseKey(){return store.get("license_key","");}

let bidData=store.get("last_feed",{});
let saved=store.get("saved",{});
let pipeline=store.get("pipeline",{}); // bid id -> "submitted"|"won"|"lost"|"passed"
let notes=store.get("notes",{}); // bid id -> freeform text, private/local only
// Declared up here with the other per-bid state rather than beside the code
// that uses them: bidCard() reads both, and a card drawn before a later
// "let" line has run would throw instead of rendering.
let bidPrep=store.get("bid_prep",{}); // bid id -> Prepare-bid workspace
let dismissed=store.get("dismissed",{}); // bid id -> true, hidden from the feed even if a rescan brings it back
let companyProfile=store.get("company_profile",{}); // used to personalize AI proposal drafts

// Re-keys locally stored bids from the old truncated-text id to the hashed one
// (see bidId). Runs once; without it, everything a user had already starred,
// noted, or marked Submitted would look like it belonged to nothing.
// Rows pulled down from Supabase are already keyed the desktop way, so keys
// that are already a 12-char hash are left exactly as they are.
function migrateBidIds(){
  if(store.get("bid_id_scheme","")==="md5")return;
  const remap={};
  const note=(city,b)=>{
    if(!b)return;
    const from=legacyBidId(city||"",b),to=bidId(city||"",b);
    if(from!==to)remap[from]=to;
  };
  for(const c in bidData)(bidData[c]||[]).forEach(b=>note(c,b));
  for(const id in saved)note(saved[id]&&saved[id]._city,saved[id]);
  const alreadyHashed=(k)=>/^[0-9a-f]{12}$/.test(k);
  const rekey=(obj)=>{
    const out={};
    for(const k in obj)out[alreadyHashed(k)?k:(remap[k]||k)]=obj[k];
    return out;
  };
  saved=rekey(saved);pipeline=rekey(pipeline);notes=rekey(notes);dismissed=rekey(dismissed);
  store.set("saved",saved);store.set("pipeline",pipeline);store.set("notes",notes);
  store.set("dismissed",dismissed);
  store.set("bid_id_scheme","md5");
}
migrateBidIds();

let cityFilter="All";
let pipelineFilter="All";
let currentUser=null;

// ── Cloud sync (Supabase) for saved bids — mirrors desktop's data_sync.py.
// Saved/pipeline/notes are three separate localStorage keys here (unlike
// desktop's single unified record), so these helpers combine them into one
// row on push and split them back out on pull. Every call fails soft —
// signed out, offline, or the sync tables not created yet just means the
// local copy keeps being the source of truth.
function findBidWithCity(id){
  for(const c in bidData){const b=bidData[c].find(x=>bidId(c,x)===id);if(b)return[c,b];}
  if(saved[id])return[saved[id]._city||"",saved[id]];
  return[null,null];
}
function bidRowForSync(id,city){
  const b=saved[id];
  if(!b)return null;
  return{
    bid_id:id,city:city||b._city||"",
    title:b.title||"",scope:b.scope||"",value:b.value||"",
    deadline:b.deadline||"",contact:b.contact||"",email:b.email||"",
    phone:b.phone||"",url:b.url||"",status:b.status||"",
    pipeline:pipeline[id]||"",note:notes[id]||"",
    saved_at:b.saved_at||new Date().toISOString().slice(0,10),
    ...(prepColumnMissing?{}:{prep:bidPrep[id]||null}),
  };
}
// The prep column is added by supabase_sync_schema.sql. A project that hasn't
// re-run it rejects the whole row for naming an unknown column, which would
// stop saved bids syncing at all -- so on that error, drop the field and send
// the row again, and stop sending it for the rest of the session.
let prepColumnMissing=false;
async function pushSavedBid(id,city){
  if(!sb||!currentUser)return;
  const row=bidRowForSync(id,city);
  if(!row)return;
  try{
    const r=await sb.from("saved_bids").upsert(row,{onConflict:"user_id,bid_id"});
    if(r&&r.error&&"prep" in row&&/prep/i.test(String(r.error.message||""))){
      prepColumnMissing=true;
      delete row.prep;
      await sb.from("saved_bids").upsert(row,{onConflict:"user_id,bid_id"});
    }
  }catch(e){}
}
async function deleteSavedBidCloud(id){
  if(!sb||!currentUser)return;
  try{await sb.from("saved_bids").delete().eq("bid_id",id);}catch(e){}
}
async function syncPullSavedBids(){
  if(!sb||!currentUser)return;
  const{data,error}=await sb.from("saved_bids").select("*");
  if(error||!data)return; // couldn't reach the cloud — leave local data alone
  // A row's stored bid_id is not trusted — it is recomputed from the row's own
  // content. Rows written by older versions of this web app carry the old
  // truncated-text id, and taking them at face value would undo the local
  // migration on every sync and leave those bids permanently unmatchable
  // against the feed. Recomputing also repairs the cloud copy: the row is
  // re-pushed under the correct id and the stale one deleted, so the two apps
  // converge on one key per bid instead of accumulating duplicates.
  const stale=[];
  const cloudIds=new Set();
  data.forEach(row=>{
    const id=bidId(row.city||"",{title:row.title,scope:row.scope});
    cloudIds.add(id);
    if(row.bid_id&&row.bid_id!==id)stale.push({old:row.bid_id,id});
    saved[id]={
      title:row.title,scope:row.scope,value:row.value,deadline:row.deadline,
      contact:row.contact,email:row.email,phone:row.phone,url:row.url,
      status:row.status,_city:row.city,saved_at:row.saved_at,
    };
    if(row.pipeline)pipeline[id]=row.pipeline;else delete pipeline[id];
    if(row.note)notes[id]=row.note;else delete notes[id];
    // Newer edit wins, so preparing on the phone and the desktop on the same
    // day doesn't let a stale copy overwrite the latest checklist.
    const cp=row.prep&&typeof row.prep==="object"?row.prep:null;
    if(cp&&(!bidPrep[id]||(cp.updated||0)>(bidPrep[id].updated||0)))bidPrep[id]=cp;
  });
  store.set("bid_prep",bidPrep);
  const localOnlyIds=Object.keys(saved).filter(id=>!cloudIds.has(id));
  store.set("saved",saved);store.set("pipeline",pipeline);store.set("notes",notes);
  for(const id of localOnlyIds)await pushSavedBid(id,saved[id]._city);
  for(const s of stale){
    await pushSavedBid(s.id,saved[s.id]&&saved[s.id]._city);
    await deleteSavedBidCloud(s.old);
  }
  if(document.getElementById("screen-feed").classList.contains("active"))renderFeed();
  if(document.getElementById("screen-saved").classList.contains("active"))renderSaved();
}

// Every field the Account screen owns. Kept as one list so push and pull
// cannot drift: avatar_url was added to the table and to the UI but to
// neither of these, so a profile photo lived only in localStorage on the
// device that uploaded it and never appeared anywhere else.
const COMPANY_FIELDS=["name","contact","phone","email","specialty","avatar_url"];

async function pushCompanyProfile(){
  // Silence here is what made this so hard to diagnose: a profile that never
  // reached the server looked identical to one that did. Every failure path
  // now says something.
  if(!sb){toast("Not connected to the account service");return;}
  if(!currentUser){toast("Sign in again to save your company info");return;}
  const row={};
  COMPANY_FIELDS.forEach(k=>{row[k]=companyProfile[k]||"";});
  row.bid_info=bidInfo();
  // Send user_id explicitly rather than leaning on the column's DEFAULT
  // auth.uid(). "create table if not exists" never alters an existing table,
  // so a project whose company_profiles predates that default silently
  // inserted NULL -- which then failed the RLS check auth.uid() = user_id and
  // reported only "violates row-level security policy". Stating it is also
  // simply more honest about what row we mean.
  if(currentUser&&currentUser.id)row.user_id=currentUser.id;
  let err=null;
  try{const r=await sb.from("company_profiles").upsert(row,{onConflict:"user_id"});err=r&&r.error;}
  catch(e){err=e;}
  if(!err)return;
  // avatar_url arrives with a later migration in supabase_sync_schema.sql.
  // On a project that has not run it, PostgREST rejects the WHOLE row for the
  // one unknown column -- so including the photo would stop the name, contact
  // and phone syncing too. Retry without it: the photo stays local until the
  // migration runs, everything else still follows the account.
  console.warn("[curbcall] company profile push failed:", err);
  // bid_info arrives with a later migration too; same treatment.
  const rest={};
  COMPANY_FIELDS.filter(k=>k!=="avatar_url")
    .forEach(k=>{rest[k]=companyProfile[k]||"";});
  const errText=String((err&&(err.message||err.details||err.hint))||"");
  if(/avatar_url/.test(errText)&&!/bid_info/.test(errText)){rest.bid_info=bidInfo();}
  if(/bid_info/.test(errText)&&!/avatar_url/.test(errText)){rest.avatar_url=companyProfile.avatar_url||"";}
  if(currentUser&&currentUser.id)rest.user_id=currentUser.id;
  let err2=null;
  try{const r=await sb.from("company_profiles").upsert(rest,{onConflict:"user_id"});err2=r&&r.error;}
  catch(e){err2=e;}
  if(!err2)return;
  console.warn("[curbcall] retry without avatar_url also failed:", err2);
  const msg=(err2&&(err2.message||err2.hint||err2.code))||"unknown error";
  toast("Couldn't save company info: "+String(msg).slice(0,90));
}
async function syncPullCompanyProfile(){
  if(!sb||!currentUser)return;
  const{data,error}=await sb.from("company_profiles").select("*").maybeSingle();
  if(error)return;
  // Any field counts, not just name. Gating on name meant someone who set a
  // photo and a contact but left the company name blank had the stored row
  // ignored on every other device -- and the else-branch then pushed the
  // empty local copy back over it.
  const remoteInfo=data&&data.bid_info&&typeof data.bid_info==="object"?data.bid_info:null;
  const hasRemote=data&&(COMPANY_FIELDS.some(k=>data[k])||(remoteInfo&&Object.keys(remoteInfo).length));
  const hasLocal=COMPANY_FIELDS.some(k=>companyProfile[k])||bidInfoCount()>0;
  if(hasRemote){
    const next={};
    COMPANY_FIELDS.forEach(k=>{next[k]=data[k]||"";});
    // A project without the bid_info column keeps this device's copy.
    next.bid_info=remoteInfo&&Object.keys(remoteInfo).length?remoteInfo:bidInfo();
    companyProfile=next;
    store.set("company_profile",companyProfile);
    updateUserChip();
    if(document.getElementById("screen-account").classList.contains("active"))renderAccount();
  }else if(hasLocal){
    pushCompanyProfile();
  }
}

// The name typed at signup becomes the Company Info "Contact Person" --
// applied once, and only if that field is still empty, so it can never
// clobber something the user later typed themselves in Account.
function applyPendingSignupName(){
  const name=store.get("pending_signup_name","");
  if(name&&!companyProfile.contact){
    companyProfile.contact=name;
    store.set("company_profile",companyProfile);
    pushCompanyProfile();
  }
  store.set("pending_signup_name","");
}

// Admin status only ever gates whether the Admin entry point is SHOWN --
// every actual admin action still requires the real admin token, checked
// server-side. This just decides whether to bother offering it.
let isAdmin=false;
async function checkAdminStatus(){
  if(!sb||!currentUser)return;
  try{
    const token=await getSupabaseToken();
    if(!token)return;
    const r=await fetch(SERVER+"/admin/whoami",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({supabase_token:token})});
    const d=await r.json();
    isAdmin=!!(d&&d.is_admin);
    if(document.getElementById("screen-account").classList.contains("active"))renderAccount();
  }catch(e){isAdmin=false;}
}

// ── Feed sync ──
// Starred bids, the company profile and saved searches already follow the
// account. The three FEEDS did not: they lived only in this browser's
// localStorage, so opening the app in a different browser showed an empty
// Bids tab even though the account was the same. One row per user, each feed
// a jsonb blob -- a feed is read and replaced wholesale, never queried
// field-by-field, so a row per bid would buy nothing and cost a lot.
// lead_status rides along because a Leads feed without the statuses set on
// it is half the information.
const FEED_TABLE="user_feeds";
let feedsUpdatedAt=store.get("feeds_updated_at","");
let feedPushTimer=null;

// A single scan writes several of these keys in quick succession; debounce so
// that lands as one upload rather than three.
function queueFeedPush(){
  feedsUpdatedAt=new Date().toISOString();
  store.set("feeds_updated_at",feedsUpdatedAt);
  if(!sb||!currentUser)return;
  clearTimeout(feedPushTimer);
  feedPushTimer=setTimeout(pushFeeds,1500);
}

async function pushFeeds(){
  if(!sb||!currentUser)return;
  try{
    await sb.from(FEED_TABLE).upsert({
      bids:bidData,upcoming:upcomingData,leads:leadsData,
      lead_status:leadStatus,dismissed:dismissed,
      updated_at:feedsUpdatedAt||new Date().toISOString(),
    },{onConflict:"user_id"});
  }catch(e){/* a failed feed upload costs nothing local — try again next scan */}
}

// Whether the server has been asked yet about this account's feed, and what
// it said. maybeFirstScan() used to decide by looking at bidData, which on a
// new device is empty for the first second or two of every launch simply
// because the answer has not arrived -- so an existing customer signing in on
// a new phone got an unrequested 90-second scan of wherever they happened to
// be. showApp() starts autoFillZip() and syncPullFeeds() in the same tick and
// the geolocation callback usually wins.
let feedSyncState="pending";   // pending | empty | has-bids | failed
async function syncPullFeeds(){
  if(!sb||!currentUser){feedSyncState="failed";return;}
  let data;
  try{
    const res=await sb.from(FEED_TABLE).select("*").maybeSingle();
    if(res.error){feedSyncState="failed";return;}  // table not created yet -> feature is simply inert
    data=res.data;
  }catch(e){feedSyncState="failed";return;}
  // The column is "bids" -- same one read below. An account with any of the
  // three feeds is an existing customer, not a new one.
  feedSyncState=(data&&(Object.keys(data.bids||{}).length
                        ||Object.keys(data.upcoming||{}).length
                        ||Object.keys(data.leads||{}).length))
    ?"has-bids":"empty";
  if(!data){
    // Nothing stored yet: seed the account from whatever this browser has,
    // so the first device to sign in doesn't lose its feed to an empty cloud.
    if(Object.keys(bidData).length||Object.keys(upcomingData).length
       ||Object.keys(leadsData).length)pushFeeds();
    // A genuinely new account. This is the one case a first scan is FOR.
    maybeFirstScan();
    return;
  }
  // Last writer wins, by the timestamp the writing device recorded. Only
  // adopt a strictly newer copy, so a stale browser opening later can't
  // overwrite the scan you just ran somewhere else.
  if(feedsUpdatedAt&&data.updated_at&&data.updated_at<=feedsUpdatedAt)return;
  bidData=data.bids||{};
  upcomingData=data.upcoming||{};
  leadsData=data.leads||{};
  leadStatus=data.lead_status||{};
  // Added after bids/upcoming/leads/lead_status already synced -- a bid
  // removed on one device kept reappearing on every other signed-in device,
  // since the underlying feed synced but which bids had been dismissed from
  // it never did. data.dismissed is undefined against a row written before
  // this column existed, not an empty dismissal -- falling back to {} there
  // would un-hide every bid this device had already removed.
  dismissed=data.dismissed||dismissed;
  store.set("last_feed",bidData);
  store.set("upcoming_feed",upcomingData);
  store.set("leads_feed",leadsData);
  store.set("lead_status",leadStatus);
  store.set("dismissed",dismissed);
  feedsUpdatedAt=data.updated_at||new Date().toISOString();
  store.set("feeds_updated_at",feedsUpdatedAt);
  // The sync has now answered, so the first-scan decision can be made on
  // fact. Without this the only caller is the geolocation callback, which
  // fires first and finds "pending" -- correct, and then nothing tries again.
  maybeFirstScan();
  const active=(id)=>document.getElementById(id).classList.contains("active");
  if(active("screen-feed"))renderFeed();
  // Unconditional, unlike the renderFeed() above: a pull can land while
  // Find is the visible screen (this runs at sign-in, before any tab has
  // necessarily been touched), and the summary there would otherwise show
  // whatever this device had before the sync.
  else renderScanSummary();
  if(active("screen-upcoming"))renderUpcoming();
  if(active("screen-leads"))renderLeads();
  showFeedBadge(Object.keys(bidData).length>0);
}

window.setPipelineStatus=function(id,status){
  if(status)pipeline[id]=status;else delete pipeline[id];
  store.set("pipeline",pipeline);
  // Setting a status on a bid that isn't starred yet saves it too —
  // tracking a bid's status is itself a reason to keep it (same rule as
  // the desktop app, so the two stay consistent when synced).
  if(status&&!saved[id]){
    const[c,b]=findBidWithCity(id);
    if(b){saved[id]={...b,_city:c||""};store.set("saved",saved);}
  }
  if(saved[id])pushSavedBid(id,saved[id]._city);
};
window.refreshAfterPipeline=function(city,id){
  if(document.getElementById("screen-feed").classList.contains("active"))renderFeed();
  if(document.getElementById("screen-saved").classList.contains("active"))renderSaved();
  const b=findBid(id);if(b)openDetail(city,b);
};

function toast(msg){
  const t=document.getElementById("toast");
  t.textContent=msg;t.classList.add("show");
  clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove("show"),2600);
}
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
// Every empty-state card (.empty) still had CSS for an .icon div sized for
// an emoji, but nothing ever filled it after emoji icons were replaced
// with this SVG sprite -- they'd silently rendered as bare text ever since.
function emptyHTML(icon,title,body){return `<div class="empty"><div class="icon"><svg class="icon-svg"><use href="#${icon}"/></svg></div><h3>${title}</h3><p>${body}</p></div>`;}
// Plan feature lists used a CSS ::before Unicode checkmark, a different
// visual language from the SVG icon set used everywhere else on this screen.
function planLi(text){return `<li><svg class="icon-svg"><use href="#i-check"/></svg>${text}</li>`;}
// County names arrive normalised for matching ("st clair", "de kalb"), which
// is right for a lookup key and wrong on a card.
function titleCase(s){return String(s==null?"":s).replace(/\b[a-z]/g,c=>c.toUpperCase());}

// Returns the URL only if it uses a scheme we actually intend to open, else "".
// A bid's url/email/phone are extracted by the AI out of whatever page a search
// engine pointed us at, so they are untrusted input. Escaping alone is not
// enough here: href="javascript:..." needs no quotes to break anything, and it
// would run in this app's origin, where the Supabase session and licence key
// are kept. Callers still esc() the result — safeUrl gates the scheme, esc()
// gates the quoting, and both are needed.
function safeUrl(u){
  const s=String(u==null?"":u).trim();
  if(!s)return"";
  // Browsers strip control characters before resolving the scheme, so
  // "java\tscript:alert(1)" is a live javascript: URL to them even though a
  // naive prefix test says otherwise. Refuse anything containing them.
  if(/[\u0000-\u001F\u007F]/.test(s))return"";
  return /^(https?:\/\/|mailto:|tel:)/i.test(s)?s:"";
}

// ── Offline state ──
// Everything the app already knows (feed, saved bids, pipeline, notes) lives in
// localStorage, so with no connection the app is still genuinely useful — it
// just can't scan, sync, or sign in. Track that state explicitly so the UI can
// say so instead of failing with generic "couldn't reach the server" errors.
let offlineMode=!HAS_SB; // libraries missing entirely — hard offline
// offlineMode is set once, from whatever happened to be true the instant the
// page loaded (did supabase-js's CDN script load in time?). It used to just
// stay however it started for the rest of the session — a single slow CDN
// load on a flaky moment of connection left someone parked on "Offline —
// showing your saved bids" for the whole session even after their signal
// came back fully, with nothing on screen but static text explaining why.
// reconnecting is the actual fix: a real request to our own server, not a
// re-read of the same stale flag, so "am I offline" only ever answers with
// live information.
let reconnecting=false;
function isOffline(){return offlineMode||!navigator.onLine;}
async function tryReconnect(){
  if(reconnecting)return;
  reconnecting=true;
  const btn=document.getElementById("offline-reconnect");
  if(btn){btn.disabled=true;btn.textContent="Checking…";}
  try{
    await fetchWithTimeout(SERVER+"/health",{},8000);
    offlineMode=false;
    updateOfflineBar();
    toast("Back online");
    // These all early-return under isOffline(), so a stuck offlineMode meant
    // they silently never ran even once real signal came back — kick them
    // off now instead of waiting for the next natural trigger.
    autoFillZip();autoUnlock();checkSavedSearches();
    syncPullSavedBids();syncPullCompanyProfile();
  }catch(e){
    toast("Still can't reach the server");
  }finally{
    reconnecting=false;
    if(btn){btn.disabled=false;btn.textContent="Reconnect";}
  }
}
function updateOfflineBar(){
  const bar=document.getElementById("offline-bar");
  if(!bar)return;
  const off=isOffline();
  bar.classList.toggle("show",off);
  const msg=offlineMode
    ?"Offline — showing your saved bids. Scanning and sync need a connection."
    :"You're offline — saved bids still work. Scans will resume when you're back.";
  bar.innerHTML=`<span>${esc(msg)}</span> <button id="offline-reconnect" type="button">Reconnect</button>`;
  const btn=document.getElementById("offline-reconnect");
  if(btn)btn.onclick=tryReconnect;
  // Map tiles come from a CDN that isn't cached, so an offline map is just a
  // grey box promising something it can't deliver — hide it rather than show it.
  document.querySelectorAll(".map-card").forEach(c=>c.classList.toggle("offline",off));
}
// The browser's own signal is a real, useful trigger to re-check -- but it
// only means a network interface exists, not that our server is actually
// reachable, so this attempts the same real reconnect check rather than
// trusting the event alone.
window.addEventListener("online",tryReconnect);
window.addEventListener("offline",updateOfflineBar);
// Sign-in has two different failure modes and they need different advice:
// no connection, or the auth library never loaded (usually a bad cached copy).
// Telling someone with full signal that they are offline sends them hunting
// for the wrong problem.
function signInAvailable(){
  if(!HAS_SB){
    setMsg("Couldn't load the sign-in service. Tap Reload below to fetch a fresh copy.","err");
    const card=document.getElementById("auth-card");
    if(card&&!document.getElementById("lib-reload-btn")){
      const b=document.createElement("button");
      b.id="lib-reload-btn";b.className="btn-primary";b.textContent="Reload the app";
      b.onclick=()=>{
        try{sessionStorage.removeItem("lib_reload_tried");}catch(e){}
        (self.caches?caches.keys():Promise.reject())
          .then(keys=>Promise.all(keys.map(k=>caches.delete(k))))
          .catch(()=>{}).then(()=>location.reload());
      };
      card.appendChild(b);
    }
    return false;
  }
  if(!navigator.onLine){setMsg("You're offline — signing in needs a connection.","err");return false;}
  return true;
}

// Guard for actions that genuinely cannot work without a connection.
function requireOnline(what){
  if(!isOffline())return true;
  toast(`${what} needs a connection — you're offline.`);
  return false;
}

// ── Auth screen logic ──
// signup | signin. A first-time visitor arriving from the marketing site needs
// an account, so signup is the right default for them -- but showing "Create
// your free account" to someone who already HAS one, every time they open the
// app, invites them to make a second account by mistake.
//
// Keyed off its own flag rather than last_user_email, which is deliberately
// cleared on sign-out (see onAuthStateChange) because offline boot treats it
// as "this device belongs to a signed-in user". Having an account is a fact
// about the person that outlives any one session, so it is stored separately
// and never cleared.
let authMode = store.get("has_account", false) ? "signin" : "signup";

function setAuthMode(mode){
  authMode=mode;
  const isSignup=mode==="signup";
  document.getElementById("auth-title").textContent=isSignup?"Create your free account":"Welcome back";
  document.getElementById("auth-sub").textContent=isSignup?"7-day free trial \u2014 no card required":"Sign in to your account";
  document.getElementById("email-btn").textContent=isSignup?"Create Account":"Sign In";
  document.getElementById("password-wrap").style.display=isSignup?"block":"block";
  document.getElementById("magic-btn").style.display=isSignup?"block":"block";
  document.getElementById("auth-toggle").innerHTML=isSignup
    ?'Already have an account? <a id="toggle-link">Sign in</a>'
    :'New here? <a id="toggle-link">Create free account</a>';
  document.getElementById("toggle-link").onclick=()=>setAuthMode(isSignup?"signin":"signup");
  document.getElementById("auth-msg").textContent="";
  document.getElementById("forgot-wrap").style.display=isSignup?"none":"block";
  // Agreeing to Terms/Privacy is what a NEW account means -- signing back
  // into an existing one isn't re-agreeing to anything.
  document.getElementById("terms-check-wrap").style.display=isSignup?"flex":"none";
  document.getElementById("name-wrap").style.display=isSignup?"block":"none";
  // The 8-character rule only binds when a password is being chosen. Showing
  // it to someone signing in reads as a demand rather than a hint.
  const hint=document.getElementById("pw-hint");
  if(hint)hint.style.display=isSignup?"block":"none";
  // Tells a password manager whether to offer a saved password or generate
  // one; without it phones offer the wrong thing on one of the two screens.
  const pw=document.getElementById("auth-password");
  if(pw)pw.setAttribute("autocomplete",isSignup?"new-password":"current-password");
}

// Reveal toggle. Kept out of setAuthMode so flipping between sign-in and
// sign-up does not silently re-hide a password the user chose to see.
(function(){
  const btn=document.getElementById("pw-toggle");
  const pw=document.getElementById("auth-password");
  if(!btn||!pw)return;
  btn.onclick=()=>{
    const shown=pw.type==="text";
    pw.type=shown?"password":"text";
    btn.textContent=shown?"Show":"Hide";
    btn.setAttribute("aria-label",shown?"Show password":"Hide password");
    pw.focus();
  };
})();

// A submit that looks identical while it waits invites a second tap, and a
// second signUp for the same address comes back as a rate-limit error that
// reads like a real failure.
function setAuthBusy(on,label){
  const b=document.getElementById("email-btn");
  const m=document.getElementById("magic-btn");
  if(!b)return;
  if(on){
    b.dataset.idle=b.dataset.idle||b.textContent;
    b.textContent=label||"Working...";
  }else if(b.dataset.idle){
    b.textContent=b.dataset.idle;
    delete b.dataset.idle;
  }
  b.disabled=on;
  if(m)m.disabled=on;
}

// document.getElementById(id).onclick=... throws immediately if id isn't on
// the page -- and because every statement below is top-level code that runs
// once, at script-parse time, that throw doesn't just skip the one handler:
// it kills the rest of this file outright, silently taking down every
// handler wired after it. That already happened once, to the admin console,
// before it was removed (see ServiceWorkerTests in test_frontend_structure.py).
// byId() is the same lookup with nowhere left for that to happen: a missing
// id gets a shared stand-in instead of null, so wiring a handler onto it is a
// quiet no-op -- never a crash -- and every renaming of an element id from
// here on is at worst one dead button, not a dead app.
const _NULL_EL={addEventListener(){}};
function byId(id){return document.getElementById(id)||_NULL_EL;}

byId("toggle-link").onclick=()=>setAuthMode("signin");

// The markup is written in the signup state, so a returning visitor whose
// authMode started as "signin" would otherwise see signup's title, button,
// name field and Terms checkbox. Applying the mode once at boot is what makes
// the screen match the variable.
setAuthMode(authMode);

// Where Supabase sends the browser back to after an emailed link.
//
// Never window.location.href. That carries whatever fragment happens to be in
// the address bar, and Supabase appends its own "#access_token=..." to
// whatever it is given. A URL already ending in "#" therefore comes back
// doubled -- app.html##access_token=... -- and the key parses as
// "#access_token" rather than "access_token", so supabase-js finds no token,
// establishes no session, fires no PASSWORD_RECOVERY, and drops the user on
// the signup screen with no explanation. A real reset link did exactly that.
//
// The stray "#" is trivially easy to acquire: any <a href="#"> in the app puts
// one there. Origin plus pathname is the only part of the current URL that is
// ever wanted here.
function authReturnUrl(){
  return window.location.origin+window.location.pathname;
}

// ── Turnstile (bot protection on sign-in/sign-up/magic-link/reset) ──
// Entirely inert while TURNSTILE_SITE_KEY is empty: the widget never renders,
// turnstileToken() always returns undefined, and Supabase's auth calls below
// go out with no captchaToken option -- identical to today's behavior. Once a
// real site key is set and CAPTCHA is turned on in the Supabase dashboard,
// this becomes required automatically; Supabase rejects the request if the
// token is missing, so there is no second flag to flip here.
let turnstileWidgetId=null;
let _turnstileToken="";
function turnstileToken(){return _turnstileToken||undefined;}
// Named so the Turnstile <script>'s ?onload= callback can find it. That
// script is loaded with defer (see app.html), which runs it after every
// plain script the parser already encountered -- this file included -- so
// window.onloadTurnstile below is guaranteed to exist first.
window.onloadTurnstile=function(){
  const el=document.getElementById("turnstile-widget");
  if(!el||!TURNSTILE_SITE_KEY||!window.turnstile)return;
  turnstileWidgetId=window.turnstile.render(el,{
    sitekey:TURNSTILE_SITE_KEY,
    theme:"dark",
    callback:t=>{_turnstileToken=t;},
    "error-callback":()=>{_turnstileToken="";},
    "expired-callback":()=>{_turnstileToken="";},
  });
};
// Belt and suspenders: if window.turnstile is somehow already sitting there
// by the time this line runs (a cached/instant script load, a browser that
// doesn't order defer scripts as expected), the callback above already
// missed its moment -- Cloudflare only calls it once. Render directly here
// instead of waiting for a callback that already happened.
if(window.turnstile)window.onloadTurnstile();
// Tokens are single-use and short-lived -- call after every attempt (pass or
// fail) so the next submit always carries a fresh one instead of a spent one.
function resetTurnstile(){
  _turnstileToken="";
  if(window.turnstile&&turnstileWidgetId!=null)window.turnstile.reset(turnstileWidgetId);
}
// Every auth call below sends captchaToken() along, and Supabase (with bot
// protection on) rejects the request outright if that comes back empty --
// with a raw, technical message ("captcha protection: request disallowed (no
// captcha_token found)") that means nothing to someone trying to sign in.
// The widget can come back empty for two different reasons: it just hasn't
// finished its auto-check yet (transient -- the common case, usually clears
// within a second of page load, well before anyone finishes typing), or the
// challenges.cloudflare.com script never loaded at all because an ad
// blocker, privacy browser, or restrictive network blocked it (not
// transient -- no amount of waiting fixes this). Can't tell those apart up
// front, so wait out the transient case first -- nobody should have to tap
// Sign In a second time for what was only ever a timing gap -- and only
// surface the "something's actually wrong" message once that grace period
// has passed with still nothing.
async function captchaBlocking(){
  if(!TURNSTILE_SITE_KEY||turnstileToken())return false;
  setMsg("Verifying...","");
  for(let waited=0;waited<2000&&!turnstileToken();waited+=100){
    await new Promise(r=>setTimeout(r,100));
  }
  if(turnstileToken())return false;
  setMsg("Still verifying you're not a robot -- give it a second and try again. If this keeps happening, an ad blocker or privacy browser may be blocking the check; allow challenges.cloudflare.com and reload.","err");
  return true;
}
// Safety net for the rarer case that slips past captchaBlocking() anyway (a
// token that goes stale in the moment between the check above and Supabase
// receiving it): same message, so Supabase's raw error text never reaches
// the screen.
function friendlyAuthError(message){
  return /captcha/i.test(message||"")
    ? "That verification check failed. If you use an ad blocker or privacy browser, allow challenges.cloudflare.com and try again."
    : message;
}
// Safari (and other browsers) can restore this whole page from the
// back/forward cache instead of reloading it -- same DOM, same "Success!"
// checkmark still showing on the widget, but the token that earned it is
// however old the tab has been sitting in history. Turnstile tokens expire
// in ~5 minutes and are single-use, so that frozen "Success" is frequently
// stale by the time someone submits. event.persisted is how a bfcache
// restore is detected; force a fresh token so the visible state matches
// what's actually valid.
window.addEventListener("pageshow",e=>{if(e.persisted)resetTurnstile();});

// ── Forgot password ──
byId("forgot-link").onclick=async()=>{
  const email=document.getElementById("auth-email").value.trim();
  if(!email){setMsg("Enter your email above first, then tap this again","err");return;}
  if(await captchaBlocking())return;
  setMsg("Sending reset link...","");
  const{error}=await sb.auth.resetPasswordForEmail(email,{redirectTo:authReturnUrl(),captchaToken:turnstileToken()});
  resetTurnstile();
  if(error){setMsg(friendlyAuthError(error.message),"err");return;}
  setMsg("Check your email for a password reset link.","ok");
};
// Supabase redirects back with a recovery session — catch that and let the
// user set a new password instead of dropping them straight into the app.
//
// _sawPasswordRecovery latches permanently once this fires: a real recovery
// session can still throw a second, unrelated auth event afterward (a token
// refresh, another tab's SIGNED_IN), and the CAME_FROM_RESET_LINK handler
// below must not mistake that follow-up for a failed link and sign the
// person back out from under the very form they're about to submit.
let _sawPasswordRecovery=false;
// Held so Update Password can re-establish the session itself if it's gone
// missing by the time someone clicks it -- see the click handler below.
let _recoverySession=null;
if(sb)sb.auth.onAuthStateChange((event,session)=>{
  if(event==="PASSWORD_RECOVERY"){_sawPasswordRecovery=true;_recoverySession=session;showPasswordReset();}
});
// Stateless recovery: token_hash + verifyOtp needs no code_verifier and no
// prior localStorage in this browser, unlike the PKCE/implicit auto-detect
// above -- which is the point, since a reset link is routinely opened in a
// different browser or app webview than the one that requested it, where
// neither exists yet. Requires the "Reset Password" email template to link
// here with ?token_hash=...&type=recovery instead of Supabase's default
// /auth/v1/verify redirect -- see the comment near RESET_TOKEN_HASH above.
// Failure (expired/already-used link) is left to the CAME_FROM_RESET_LINK
// fallback further down, which fires on this same no-session load and shows
// the "didn't work" message once, already guarded by its own flag.
if(sb&&RESET_TOKEN_HASH){
  sb.auth.verifyOtp({type:"recovery",token_hash:RESET_TOKEN_HASH}).then(({data,error})=>{
    if(!error&&data&&data.session){
      _sawPasswordRecovery=true;
      _recoverySession=data.session;
      showPasswordReset();
    }
  });
}
function showPasswordReset(){
  document.getElementById("auth-screen").style.display="flex";
  document.getElementById("app").style.display="none";
  const card=document.getElementById("auth-card");
  card.innerHTML=`
    <div class="auth-title">Set a new password</div>
    <div class="auth-sub">Enter a new password for your account.</div>
    <div class="field-label">New Password</div>
    <input class="input" id="new-password" type="password" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;" />
    <div class="field-hint">At least 8 characters</div>
    <button class="btn-primary" id="new-password-btn">Update Password</button>
    <div class="auth-msg" id="auth-msg"></div>`;
  document.getElementById("new-password-btn").onclick=async()=>{
    const password=document.getElementById("new-password").value;
    // Eight, like signup and like changing it from the Account screen. This
    // was six, so the reset link was the one way into the app to set a
    // password weaker than the app otherwise allows.
    if(!password||password.length<8){setMsg("Password must be at least 8 characters","err");return;}
    setMsg("...","");
    // Bypasses supabase-js's own session bookkeeping for the actual update
    // call -- updateUser() kept finding no session at all on the device this
    // was failing on. But the token has to be read fresh right here, not
    // the one snapshotted when the recovery screen first appeared: the
    // client auto-refreshes tokens in the background (autoRefreshToken is on
    // by default), and a refresh invalidates the old session server-side.
    // Someone who takes a few seconds to type a password can easily submit
    // with a session_id that's already been rotated out from under the
    // snapshot, which fails differently (and more confusingly) than a
    // missing session: the JWT still looks fine, Supabase just no longer has
    // a session row for it. getSession() returns whatever the client
    // currently has -- including any background refresh -- so prefer that,
    // and fall back to the original snapshot only if the client has nothing
    // at all.
    const{data:{session:curSession}}=await sb.auth.getSession();
    const token=(curSession&&curSession.access_token)
      ||(_recoverySession&&_recoverySession.access_token);
    if(!token){
      setMsg("This reset link isn't valid anymore. Request a new one below.","err");
      return;
    }
    let res;
    try{
      res=await fetch(SUPABASE_URL+"/auth/v1/user",{
        method:"PUT",
        headers:{
          "Content-Type":"application/json",
          "apikey":SUPABASE_ANON_KEY,
          "Authorization":"Bearer "+token,
        },
        body:JSON.stringify({password}),
      });
    }catch(e){
      setMsg("Couldn't reach the server. Check your connection and try again.","err");
      return;
    }
    if(!res.ok){
      let msg="That didn't work. Request a new reset link and try again.";
      try{
        const body=await res.json();
        if(body&&(body.msg||body.message))msg=body.msg||body.message;
      }catch(e){}
      setMsg(msg,"err");
      return;
    }
    setMsg("Password updated! Redirecting...","ok");
    setTimeout(()=>window.location.href=window.location.pathname,1200);
  };
}

// The name field's value has to survive whatever comes next -- a full page
// redirect for Google OAuth, or an email confirmation link that reopens the
// app fresh -- so it's stashed in localStorage rather than a JS variable,
// and consumed once, the first time a real session appears after signup.
function captureTermsAcceptance(method){
  if(authMode!=="signup")return;
  try{localStorage.setItem(TERMS_METHOD_KEY,method);}catch(e){}
}

// Post it once a session exists. Sends the token, never a user id: the server
// decides who this is, so a record cannot be written for somebody else.
//
// Guarded against itself. onAuthStateChange fires more than once for a single
// sign-in -- INITIAL_SESSION and then SIGNED_IN -- and this is async: it reads
// the flag, then awaits a token and a round trip, and only clears the flag
// once the server confirms. Two overlapping calls both saw the flag still set
// and both posted, so one signup wrote two identical consent rows. The flag
// cannot be cleared up front instead; a failed request would then lose the
// record entirely, which is the one outcome worse than a duplicate.
let termsFlushInFlight=false;
async function flushTermsAcceptance(){
  if(termsFlushInFlight)return;
  let method="";
  try{method=localStorage.getItem(TERMS_METHOD_KEY)||"";}catch(e){}
  if(!method)return;
  termsFlushInFlight=true;
  try{
    const token=await getSupabaseToken();
    if(!token)return;
    const r=await fetch(SERVER+"/terms/accept",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({supabase_token:token,method})});
    // Only forget it once the server says it is stored. A failed write that
    // clears the flag would leave an account with no record and nothing to
    // say so -- worse than no feature, because it looks like evidence exists.
    if(r.ok){try{localStorage.removeItem(TERMS_METHOD_KEY);}catch(e){}}
  }catch(e){/* keep the flag; the next sign-in tries again */}
  finally{termsFlushInFlight=false;}
}

// ── Re-accepting updated Terms ──────────────────────────────────────────────
//
// The Terms and the Privacy Policy change. Somebody who agreed in June has
// not agreed to a September rewrite, and "continued use means you accept" is
// a much weaker thing to rely on than a record of them clicking. So an
// existing account is asked once per published version.
//
// Never blocks on a failed check. The server answers "unknown" when it cannot
// look the answer up, and unknown means do not prompt -- interrupting a
// paying customer because a database call timed out is worse than a late
// re-acceptance, which the Terms already cover.
async function checkTermsCurrent(){
  try{
    const token=await getSupabaseToken();
    if(!token)return;
    const r=await fetchWithTimeout(SERVER+"/terms/status",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({supabase_token:token})},15000);
    if(!r.ok)return;
    const d=await r.json();
    if(d&&d.ok&&d.needs_acceptance)showTermsUpdate(d);
  }catch(e){/* offline or slow: ask again next launch */}
}

function showTermsUpdate(info){
  document.getElementById("app").style.display="none";
  document.getElementById("auth-screen").style.display="flex";
  const card=document.getElementById("auth-card");
  card.style.display="";
  const onb=document.getElementById("onboard-card");
  if(onb)onb.style.display="none";
  const sent=document.getElementById("auth-sent-card");
  if(sent)sent.style.display="none";
  card.innerHTML=`
    <div class="auth-title">We&rsquo;ve updated our Terms</div>
    <div class="auth-sub">Please review and accept to keep using CurbCall Pro.
      Your account and saved bids are untouched.</div>
    <div class="field-hint" style="margin:0.6rem 0 0.9rem;">
      <a href="terms.html" target="_blank" rel="noopener">Terms of Service</a>
      (${esc(info.terms_version||"")}) &nbsp;&middot;&nbsp;
      <a href="privacy.html" target="_blank" rel="noopener">Privacy Policy</a>
      (${esc(info.privacy_version||"")})
    </div>
    <button class="btn-primary" id="terms-accept-btn">I agree</button>
    <button class="btn-ghost" id="terms-decline-btn">Not now &mdash; sign out</button>
    <div class="auth-msg" id="auth-msg"></div>`;
  document.getElementById("terms-accept-btn").onclick=async()=>{
    const btn=document.getElementById("terms-accept-btn");
    btn.disabled=true;btn.textContent="Saving...";
    let saved=false;
    try{
      const token=await getSupabaseToken();
      const r=await fetchWithTimeout(SERVER+"/terms/accept",{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({supabase_token:token,method:"reaccept"})},15000);
      saved=r.ok;
    }catch(e){saved=false;}
    if(!saved){
      // Do not wave them through on a failed write. The whole point is the
      // record; letting them in without one leaves an account that looks
      // like it agreed and cannot be shown to have.
      btn.disabled=false;btn.textContent="I agree";
      setMsg(offlineOrServer(),"err");
      return;
    }
    card.innerHTML="";
    document.getElementById("auth-screen").style.display="none";
    showApp();
  };
  document.getElementById("terms-decline-btn").onclick=async()=>{
    try{await sb.auth.signOut();}catch(e){}
    location.reload();
  };
}

function capturePendingSignupName(){
  if(authMode!=="signup")return;
  // Two fields, one stored value: everything downstream -- the proposal
  // drafts, the Account screen, the contact line on a bid -- wants a person's
  // name as they would write it, not two columns. Joined with whichever
  // halves were actually filled in, so someone who gives only a first name
  // does not get a trailing space stored.
  const first=(document.getElementById("auth-first").value||"").trim();
  const last=(document.getElementById("auth-last").value||"").trim();
  const name=[first,last].filter(Boolean).join(" ");
  if(name)store.set("pending_signup_name",name);
}

byId("google-btn").onclick=async()=>{
  if(!signInAvailable())return;
  if(authMode==="signup"&&!document.getElementById("terms-check").checked){
    setMsg("Please agree to the Terms and Privacy Policy to continue","err");return;
  }
  captureTermsAcceptance("google");
  capturePendingSignupName();
  setMsg("","");
  const{error}=await sb.auth.signInWithOAuth({
    provider:"google",
    options:{redirectTo:authReturnUrl()}
  });
  if(error)setMsg(error.message,"err");
};

byId("email-btn").onclick=async()=>{
  if(!signInAvailable())return;
  const email=document.getElementById("auth-email").value.trim();
  const password=document.getElementById("auth-password").value;
  if(!email){setMsg("Enter your email","err");return;}
  if(!password||password.length<8){setMsg("Password must be at least 8 characters","err");return;}
  if(authMode==="signup"&&!document.getElementById("terms-check").checked){
    setMsg("Please agree to the Terms and Privacy Policy to continue","err");return;
  }
  if(await captchaBlocking())return;
  captureTermsAcceptance("signup_form");
  capturePendingSignupName();
  setMsg("","");
  setAuthBusy(true,authMode==="signup"?"Creating account...":"Signing in...");
  let res;
  try{
    res=authMode==="signup"
      ? await sb.auth.signUp({email,password,options:{captchaToken:turnstileToken()}})
      : await sb.auth.signInWithPassword({email,password,options:{captchaToken:turnstileToken()}});
  }catch(e){
    setAuthBusy(false);
    resetTurnstile();
    setMsg("Couldn't reach the server. Check your connection and try again.","err");
    return;
  }
  setAuthBusy(false);
  resetTurnstile();
  if(res.error){setMsg(friendlyAuthError(res.error.message),"err");return;}
  if(authMode==="signup"&&!res.data.session){
    // Supabase never errors signUp() for an email that's already registered
    // -- that's deliberate, to stop a stranger from using this form to find
    // out who has an account here. It instead returns a fake user with an
    // empty identities array and no session, indistinguishable from a real
    // new signup unless you check for exactly that. Whoever owns this email
    // already knows the answer, so telling them plainly is not the
    // enumeration risk it would be for someone guessing addresses.
    if(res.data.user&&Array.isArray(res.data.user.identities)&&res.data.user.identities.length===0){
      setMsg("You already have an account with this email. Try signing in instead.","err");
      return;
    }
    showEmailSent(email);
    return;
  }
  // session is live — onAuthStateChange will fire
};

byId("magic-btn").onclick=async()=>{
  if(!signInAvailable())return;
  const email=document.getElementById("auth-email").value.trim();
  if(!email){setMsg("Enter your email first","err");return;}
  if(authMode==="signup"&&!document.getElementById("terms-check").checked){
    setMsg("Please agree to the Terms and Privacy Policy to continue","err");return;
  }
  if(await captchaBlocking())return;
  captureTermsAcceptance("magic_link");
  capturePendingSignupName();
  setMsg("Sending...","");
  const{error}=await sb.auth.signInWithOtp({email,options:{emailRedirectTo:authReturnUrl(),captchaToken:turnstileToken()}});
  resetTurnstile();
  if(error){setMsg(friendlyAuthError(error.message),"err");return;}
  showEmailSent(email);
};

// Swap the form out for the "we sent it" panel. Hiding the form matters as
// much as showing the message: leaving it up invites a second signup with
// the same address, which Supabase answers with a rate-limit error that
// looks like a failure.
function showEmailSent(email){
  const card=document.getElementById("auth-card");
  const sent=document.getElementById("auth-sent-card");
  const who=document.getElementById("auth-sent-email");
  if(!card||!sent)return;
  if(who)who.textContent=email;
  card.style.display="none";
  sent.style.display="";
  setMsg("","");
}

(function(){
  const back=document.getElementById("auth-sent-back");
  if(!back)return;
  back.onclick=()=>{
    const card=document.getElementById("auth-card");
    const sent=document.getElementById("auth-sent-card");
    if(sent)sent.style.display="none";
    if(card)card.style.display="";
    const f=document.getElementById("auth-email");
    if(f){f.value="";f.focus();}
  };
})();

function setMsg(msg,cls){
  const el=document.getElementById("auth-msg");
  el.textContent=msg;
  el.className="auth-msg"+(cls?" "+cls:"");
}

// ── Session management ──
let _resetLinkFailureShown=false;
if(sb)sb.auth.onAuthStateChange((event,session)=>{
  if(event==="PASSWORD_RECOVERY")return; // handled separately — don't drop into the app mid-reset
  if(CAME_FROM_RESET_LINK&&!_resetLinkFailureShown&&!_sawPasswordRecovery){
    // A reset-password link brought this browser here, but Supabase never
    // turned it into a PASSWORD_RECOVERY session -- the link was expired or
    // already used, or (if a session is present) an unrelated one already
    // signed in on this device raced ahead of it. Either way, silently
    // showing the app under whatever session happens to exist would be
    // wrong: it would look like the reset worked when nothing was reset,
    // or worse, drop someone into a different account than the one they
    // meant to recover. Sign out and say plainly that the link didn't work.
    // Guarded by a flag, not just re-checked each time: signOut() below
    // fires its own SIGNED_OUT event right back into this same listener,
    // and CAME_FROM_RESET_LINK never resets on its own since it's read
    // once from the URL at page load.
    _resetLinkFailureShown=true;
    sb.auth.signOut();
    showAuth();
    setMsg("That password reset link didn't work — it may have expired or already been used. Request a new one below.","err");
    return;
  }
  if(session){
    // Before anything reads or uploads cached data: if this browser's data
    // belongs to a different account, drop it. Must come first -- showApp()
    // leads to syncPullFeeds(), which would push the old user's bids into
    // this new account.
    if(claimDeviceFor(session.user))return;
    currentUser=session.user;
    // Remembered so a later offline start knows this device belongs to a
    // signed-in user and can go straight to their cached bids.
    store.set("last_user_email",session.user.email||"");
    // Never cleared on sign-out: next launch should offer Sign in, not Create
    // account, to someone who has been here before.
    store.set("has_account",true);
    applyPendingSignupName();
    flushTermsAcceptance();
    checkTermsCurrent();
    checkAdminStatus();
    routeAfterAuth();
  } else {
    currentUser=null;
    store.set("last_user_email","");
    showAuth();
  }
});

function showAuth(){
  document.getElementById("auth-screen").style.display="flex";
  document.getElementById("app").style.display="none";
}

// ── Offline boot ──
// Reached only when supabase-js itself never loaded, so there is no way to
// verify a session. A device that was signed in before is trusted to still be
// signed in and shown its own locally-cached data (which never left this phone
// anyway); anyone else gets told plainly why they can't sign in yet.
function bootOffline(){
  const knownEmail=store.get("last_user_email","");
  if(!knownEmail){
    showAuth();
    const card=document.getElementById("auth-card");
    if(card)card.innerHTML=`
      <div class="auth-title">You're offline</div>
      <div class="auth-sub">Signing in needs a connection. Reconnect and reopen CurbCall Pro to get started.</div>
      <button class="btn-primary" onclick="location.reload()">Try again</button>`;
    return;
  }
  currentUser={email:knownEmail};
  document.getElementById("auth-screen").style.display="none";
  document.getElementById("app").style.display="flex";
  updateUserChip();
  updateOfflineBar();
  renderFeed();
  toast("Offline — showing your saved bids");
}

function updateUserChip(){
  const email=currentUser?.email||"";
  // Company Info's "Contact Person" is the closest thing this app already
  // has to a display name -- reused here rather than adding a second,
  // near-duplicate name field, so the chip shows a real name instead of a
  // raw email address once one's been set (Account -> Company Info -> Edit).
  const displayName=(companyProfile.contact||"").trim();
  const chip=document.getElementById("user-chip");
  const avatar=companyProfile.avatar_url;
  // The chevron is what tells anyone this pill goes somewhere. Without it an
  // email address in the corner reads as a label, and Account — which holds
  // billing, the company profile and the diagnostics card — looks unreachable.
  // The email goes in its own element so IT truncates, not the chip: as a bare
  // text node it was an anonymous flex item and the ellipsis ate the chevron,
  // which is the one part that says this pill is a button.
  chip.innerHTML=(avatar?`<img src="${esc(avatar)}" alt="">`:"")
    +`<span class="who">${esc(displayName||email||"Account")}</span>`
    +`<span class="chev" aria-hidden="true">›</span>`;
}
// showApp() is not a once-per-launch call: onAuthStateChange fires again on
// every token refresh (hourly, and on returning to a backgrounded tab), and
// each one comes back through routeAfterAuth() to here. The setup that
// decides where the user STARTS -- the map's default view, the saved home
// radius, the GPS auto-locate -- must only happen the first time, or an
// hourly refresh yanks the map back to the middle of the country, jumps it
// to their GPS position, and resets the radius pill they just picked.
let _appShownOnce=false,_autoLocated=false;
function showApp(){
  const firstShow=!_appShownOnce;
  _appShownOnce=true;
  // Home's "new since you were last here": read the previous launch's time
  // before stamping this one, so the count covers the gap between visits.
  if(firstShow){
    prevVisitAt=store.get(LAST_VISIT_KEY,0);
    store.set(LAST_VISIT_KEY,Date.now());
  }
  document.getElementById("auth-screen").style.display="none";
  document.getElementById("app").style.display="flex";
  updateUserChip();
  updateOfflineBar();
  renderFeed();
  // Bid cards show a ballpark once the going rates are in; they arrive a
  // moment after the first render, so draw the cards once more then.
  if(firstShow){
    loadRates(homeState()).then(d=>{if(d)renderFeed();});
    // MoDOT's own results answer "did you win?" for state jobs.
    loadBidResults(homeState()).then(d=>{if(d&&document.getElementById("screen-home")?.classList.contains("active"))renderHome();});
    setTimeout(checkSavedBids,8000);
    // Push subscriptions can be rotated by the browser; re-register quietly.
    setTimeout(()=>{if("Notification"in window&&Notification.permission==="granted"&&store.get("push_on",false))subscribePush();},12000);
    setInterval(checkSavedBids,BID_WATCH_EVERY_MS/6);
  }
  // The map only ever got created inside detectLocation()'s geolocation
  // success callback — if a user denies the location prompt (extremely
  // common), findMap never gets initialized and there's nothing to click,
  // ever. Always show a default view first so the map (and its click
  // handler) exists regardless of whether geolocation succeeds.
  // Onboarding asked where they work; this is where that answer earns its
  // keep. Without it loc-input starts empty on every single launch and a
  // returning contractor retypes their own town to run the scan they came
  // for. Only prefilled when empty, so a location typed this session wins.
  const homeLoc=store.get(HOME_LOC_KEY,"");
  const locBox=document.getElementById("loc-input");
  if(locBox&&!locBox.value&&homeLoc)locBox.value=homeLoc;
  const homeR=store.get(HOME_RADIUS_KEY,0);
  if(homeR&&firstShow){
    radius=homeR;
    // Repaint the pills to match, or the highlighted one and the radius the
    // scan actually uses disagree.
    document.querySelectorAll("#radius-row .radius-btn").forEach(x=>{
      x.classList.toggle("active",+x.dataset.r===homeR);
    });
  }
  if(!findMap)updateFindMap(39.5,-98.35,"Click the map to set your location");
  if(leadsPicker&&!leadsPicker.hasMap())leadsPicker.show(39.5,-98.35,"Click the map to set your location");
  // Everything below needs the network. Offline, the app still opens to a
  // fully readable feed — it just doesn't waste a launch firing requests that
  // are all going to time out.
  if(isOffline())return;
  // Its own flag rather than firstShow: a launch that started offline returns
  // above, and this should still run once on the first ONLINE pass.
  if(!_autoLocated){_autoLocated=true;autoFillZip();}
  autoUnlock();
  checkSavedSearches();
  lastSyncAt=new Date();
  syncPullSavedBids();
  syncPullCompanyProfile();
  syncPullSavedSearches();
  syncPullFeeds();
}

// Leaflet computes its tile grid from the container's size at the moment
// invalidateSize() runs. A flat setTimeout mostly works, but on a throttled
// device (Low Power Mode, a busy tab) the browser can still be mid-layout
// when it fires, leaving Leaflet with a stale, too-narrow tile grid: tiles
// render for the width it *thought* it had when hidden, and the newly-
// revealed rest of the map stays a blank grey box (the bid map, both
// halves not overlapping any actual city, is exactly this). Double rAF
// guarantees the browser has actually finished laying out a frame before
// asking; the delayed call after it is a safety net for slower devices
// where even that isn't quite enough.
function mapSettle(obj){
  if(!obj)return;
  requestAnimationFrame(()=>requestAnimationFrame(()=>obj.invalidateSize()));
  setTimeout(()=>obj.invalidateSize(),300);
  observeMapSize(obj);
}

// Both calls above are guesses about when layout finishes, and on a cold load
// over mobile data -- Leaflet's CSS still arriving, fonts still swapping --
// the container can still be zero-height when the last one fires. Leaflet
// then has a tile grid for a 0x0 viewport, requests nothing, and reports no
// error: addTileRetry never hears about it because no tile ever failed. The
// map just sits there blank, intermittently, which is exactly how it was
// reported.
//
// A ResizeObserver replaces the guess with the actual event. It fires when
// the container first gets real dimensions, however late that is, and again
// on rotation or when the on-screen keyboard resizes the viewport.
const _mapSizeObserved=new WeakSet();
function observeMapSize(obj){
  if(!obj||typeof ResizeObserver==="undefined")return;
  if(_mapSizeObserved.has(obj))return;
  const el=obj.getContainer&&obj.getContainer();
  if(!el)return;
  _mapSizeObserved.add(obj);
  let lastW=0,lastH=0;
  const ro=new ResizeObserver(()=>{
    const w=el.clientWidth,h=el.clientHeight;
    // Zero means still hidden -- invalidateSize() there would just re-cache
    // the same useless grid. Wait for real dimensions.
    if(!w||!h)return;
    if(w===lastW&&h===lastH)return;
    lastW=w;lastH=h;
    // invalidateSize does not itself change the container's size, so this
    // cannot feed back into the observer.
    obj.invalidateSize();
  });
  try{ro.observe(el);}catch(e){/* observer unsupported -- timers still apply */}
}

// ── Nav ──
// A named function instead of "find the nav-btn and simulate a click on it"
// -- Account no longer has a bottom-nav button (reached via the top-right
// user chip instead), so goTo('account') would otherwise call .click() on
// a nav-btn that doesn't exist and throw. Switching screens directly here
// works regardless of whether a nav-btn exists for that screen.
function switchScreen(s){
  if(s==="leads"&&!LEADS_ENABLED)s="scan";
  // A modal is a bottom sheet 85% of the screen tall, so it covers the nav
  // and a person cannot normally navigate out from under one. Code can,
  // though -- a refused request calls goTo("account") -- and that would
  // leave the sheet hanging over a screen it has nothing to do with.
  if(typeof closeModal==="function")closeModal();
  document.querySelectorAll(".nav-btn").forEach(x=>x.classList.toggle("active",x.dataset.s===s));
  document.querySelectorAll(".screen").forEach(x=>x.classList.remove("active"));
  document.getElementById("screen-"+s).classList.add("active");
  if(s==="feed"){renderFeed();showFeedBadge(false);}
  if(s==="saved")renderSaved();
  if(s==="account")renderAccount();
  if(s==="upcoming")renderUpcoming();
  if(s==="leads"&&LEADS_ENABLED)renderLeads();
  if(s==="home")renderHome();
  // Every other screen refreshes its own content on switching to it; Find
  // only ever got this from showApp()'s initial renderFeed() call, so it
  // relied on load order instead of being self-contained like the rest.
  if(s==="scan")renderScanSummary();
  // The map's own invalidateSize() call (in show()) only ever runs once,
  // at creation time -- for any tab that isn't the default visible one,
  // that happens while the tab is still hidden (display:none), so
  // Leaflet measures a zero-size container and the map renders half-
  // blank until something re-triggers a size recalculation. Re-running
  // it here, now that the tab has actually just become visible, is what
  // was previously only happening by accident when a user clicked the
  // map (which incidentally re-ran show() -> invalidateSize()).
  if(s==="scan")mapSettle(findMap);
  if(s==="leads"&&leadsPicker)mapSettle(leadsPicker);
}
if(!LEADS_ENABLED){
  // Remove the tab rather than hide it, so the remaining five share the bar
  // evenly instead of leaving a gap, and nothing can tab-focus an invisible
  // control.
  const lb=document.querySelector('.nav-btn[data-s="leads"]');
  if(lb)lb.remove();
}
document.querySelectorAll(".nav-btn").forEach(b=>{
  b.onclick=()=>{switchScreen(b.dataset.s);closeNavMenu();};
});
function goTo(s){switchScreen(s);}

// ── Desktop nav dropdown ──
// Mobile's bottom tab bar is always visible and never touches this. At the
// desktop breakpoint the same #nav-menu becomes a dropdown (see styles.css);
// .nav-trigger doesn't even render below that width, so none of this fires
// from a tap there.
const navTrigger=document.getElementById("nav-trigger");
function closeNavMenu(){
  document.body.classList.remove("nav-open");
  if(navTrigger)navTrigger.setAttribute("aria-expanded","false");
}
if(navTrigger){
  navTrigger.onclick=(e)=>{
    e.stopPropagation();
    const open=document.body.classList.toggle("nav-open");
    navTrigger.setAttribute("aria-expanded",open?"true":"false");
  };
  document.addEventListener("click",(e)=>{
    if(!document.body.classList.contains("nav-open"))return;
    if(e.target.closest("#nav-menu")||e.target.closest("#nav-trigger"))return;
    closeNavMenu();
  });
  document.addEventListener("keydown",(e)=>{
    if(e.key==="Escape")closeNavMenu();
  });
}

// ── Radius (scoped per row so Find and Upcoming don't fight over one value) ──
// 125, not 25. Benchmarked over eight metros on the same day: at 25 miles a
// scan reads 88 towns and finds 2 bids, and seven of the eight metros return
// an EMPTY board -- Kansas City reads 27 towns and finds nothing. At 125
// miles the same engine reads 616 towns, finds 32, and no metro comes back
// empty. Quality holds: 32 of 32 carry a deadline, 31 of 32 a contact.
//
// It is arithmetic, not a parser bug. A town lets roughly one job in this
// trade per year and 56% of municipal portals have nothing posted on any
// given day, so a five-town radius cannot fill a board however well the
// engine reads. A contractor will drive 100 miles for a curb job; the narrow
// default was hiding work they would happily take.
let radius=125;
document.querySelectorAll("#radius-row .radius-btn").forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll("#radius-row .radius-btn").forEach(x=>x.classList.remove("active"));
    b.classList.add("active");radius=parseInt(b.dataset.r);
    drawFindRadiusCircle();
  };
});
// 125, matching Find. Planned public work is spread as thinly as open bids
// -- roughly one relevant item per town per year -- so a 25-mile default
// returned an empty board. Leads below deliberately keeps 25: a homeowner's
// driveway a hundred miles away is not a job anyone drives to.
let upRadius=125;
document.querySelectorAll("#up-radius-row .radius-btn").forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll("#up-radius-row .radius-btn").forEach(x=>x.classList.remove("active"));
    b.classList.add("active");upRadius=parseInt(b.dataset.r);
  };
});
let leadsRadius=25;
document.querySelectorAll("#leads-radius-row .radius-btn").forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll("#leads-radius-row .radius-btn").forEach(x=>x.classList.remove("active"));
    b.classList.add("active");leadsRadius=parseInt(b.dataset.r);
  };
});

byId("clear-feed").onclick=()=>{
  if(confirm("Clear all bids? (Saved bids are not affected.)"))
  {bidData={};store.set("last_feed",bidData);queueFeedPush();cityFilter="All";renderFeed();}
};

// ── Saved searches (auto-alerts) ──
let savedSearches=store.get("saved_searches",[]);
async function pushSavedSearch(location,radiusVal){
  if(!sb||!currentUser)return;
  try{await sb.from("saved_searches").insert({location,radius:radiusVal});}catch(e){}
}
async function deleteSavedSearchCloud(location,radiusVal){
  if(!sb||!currentUser)return;
  try{await sb.from("saved_searches").delete().eq("location",location).eq("radius",radiusVal);}catch(e){}
}
async function syncPullSavedSearches(){
  if(!sb||!currentUser)return;
  const{data,error}=await sb.from("saved_searches").select("*");
  if(error||!data)return;
  const sig=s=>`${s.location.trim().toLowerCase()}|${s.radius}`;
  const cloudSigs=new Set(data.map(sig));
  const localOnly=savedSearches.filter(s=>!cloudSigs.has(sig(s)));
  savedSearches=data.map(r=>({location:r.location,radius:r.radius})).concat(localOnly);
  store.set("saved_searches",savedSearches);
  renderSavedSearches();
  for(const s of localOnly)await pushSavedSearch(s.location,s.radius);
}
byId("save-search-btn").onclick=()=>{
  const location=document.getElementById("loc-input").value.trim();
  if(!location){toast("Enter a ZIP or city first");return;}
  const exists=savedSearches.some(s=>s.location.toLowerCase()===location.toLowerCase()&&s.radius===radius);
  if(exists){toast("Already saved");return;}
  savedSearches.push({location,radius});
  store.set("saved_searches",savedSearches);
  pushSavedSearch(location,radius);
  toast("Saved — we'll check this every time you open the app");
  renderSavedSearches();
};
function renderSavedSearches(){
  const wrap=document.getElementById("saved-searches-wrap");
  if(!savedSearches.length){wrap.innerHTML="";return;}
  wrap.innerHTML=`<div class="field-label" style="margin-top:1.2rem;">Auto-Alert Searches</div>`+
    savedSearches.map((s,i)=>`<div class="ss-row">
      <div class="ss-info"><div class="ss-loc">${esc(s.location)}</div><div class="ss-radius">${s.radius} mi radius</div></div>
      <button class="ss-scan" data-i="${i}" data-act="scan">Scan</button>
      <button data-i="${i}" data-act="del">&#x2715;</button>
    </div>`).join("");
  wrap.querySelectorAll("button").forEach(btn=>{
    btn.onclick=()=>{
      const i=parseInt(btn.dataset.i,10);
      if(btn.dataset.act==="del"){
        const removed=savedSearches[i];
        savedSearches.splice(i,1);store.set("saved_searches",savedSearches);renderSavedSearches();
        deleteSavedSearchCloud(removed.location,removed.radius);
      }else{
        const s=savedSearches[i];
        document.getElementById("loc-input").value=s.location;radius=s.radius;
        document.querySelectorAll("#radius-row .radius-btn").forEach(x=>x.classList.toggle("active",parseInt(x.dataset.r,10)===s.radius));
        runScan();
      }
    };
  });
}
renderSavedSearches();

function showFeedBadge(show){
  const b=document.getElementById("feed-badge");
  if(b)b.style.display=show?"block":"none";
}
// ── Bid alert notifications ──
// Two paths. While the app is open it notifies directly (saved searches,
// saved-bid addenda). With it closed, the server sends real push
// notifications (license_server/push.py): an addendum or new document on a
// saved bid, a bid due tomorrow, new bids for a saved search. Turning alerts
// on here asks the browser for permission and registers this device for
// push in push_subscriptions. On iPhone, push only works once the app has
// been added to the Home Screen.
function pushSupported(){return "serviceWorker"in navigator&&"PushManager"in window&&"Notification"in window;}
function isIOSBrowserTab(){
  const ios=/iP(hone|ad|od)/.test(navigator.userAgent)||(navigator.platform==="MacIntel"&&navigator.maxTouchPoints>1);
  const standalone=window.matchMedia&&window.matchMedia("(display-mode: standalone)").matches||navigator.standalone;
  return ios&&!standalone;
}
function notifPermissionLabel(){
  if(isIOSBrowserTab())return"On iPhone: tap Share, then Add to Home Screen, and open CurbCall from there to turn on alerts";
  if(!("Notification"in window))return"Not supported in this browser";
  if(Notification.permission==="granted")return store.get("push_on",false)
    ?"On: addenda and due-tomorrow reminders for saved bids, and new bids for saved searches, even with the app closed"
    :"On while the app is open. Tap to also get them with the app closed";
  if(Notification.permission==="denied")return"Blocked — turn back on in your browser's site settings";
  return"Tap to turn on: addenda on saved bids, due-tomorrow reminders and new bids, even with the app closed";
}
function b64uToBytes(s){
  const pad="=".repeat((4-s.length%4)%4),raw=atob((s+pad).replace(/-/g,"+").replace(/_/g,"/"));
  const out=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)out[i]=raw.charCodeAt(i);return out;
}
// Registers this device for push. Never throws; returns true when the
// server can now reach it.
async function subscribePush(){
  if(!pushSupported()||!sb||!currentUser)return false;
  try{
    const reg=await navigator.serviceWorker.ready;
    let sub=await reg.pushManager.getSubscription();
    if(!sub){
      const r=await fetchWithTimeout(SERVER+"/push/vapid-public-key",{},15000);
      const d=await r.json();
      if(!d||!d.ok||!d.key)return false;
      sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:b64uToBytes(d.key)});
    }
    const j=sub.toJSON();
    if(!j.endpoint||!j.keys)return false;
    const{error}=await sb.from("push_subscriptions").upsert(
      {user_id:currentUser.id,endpoint:j.endpoint,p256dh:j.keys.p256dh,auth:j.keys.auth},{onConflict:"user_id,endpoint"});
    if(error){console.warn("[curbcall] push subscription not saved:",error);return false;}
    store.set("push_on",true);
    return true;
  }catch(e){console.warn("[curbcall] push subscribe failed:",e);return false;}
}
async function toggleNotifPermission(){
  if(isIOSBrowserTab()){toast("Add CurbCall to your Home Screen first (Share, then Add to Home Screen), then turn alerts on from there.");return;}
  if(!("Notification"in window)){toast("Notifications aren't supported in this browser.");return;}
  if(Notification.permission==="denied"){toast("Blocked in browser settings — enable there to turn back on.");return;}
  const perm=Notification.permission==="granted"?"granted":await Notification.requestPermission();
  const el=document.getElementById("notif-status");
  if(perm==="granted"){
    const pushed=await subscribePush();
    if(el)el.textContent=notifPermissionLabel();
    toast(pushed?"Alerts on, even with the app closed":"Alerts on while the app is open");
    fireNotification("CurbCall Pro","Alerts are on. You'll hear about addenda, due dates and new bids here.");
  }else if(el)el.textContent=notifPermissionLabel();
}
async function fireNotification(title,body){
  if(!("Notification"in window)||Notification.permission!=="granted")return;
  try{
    if("serviceWorker"in navigator){
      const reg=await navigator.serviceWorker.getRegistration();
      if(reg){reg.showNotification(title,{body,icon:"icon-192.png"});return;}
    }
    new Notification(title,{body,icon:"icon-192.png"});
  }catch(e){}
}

// Silently re-scans every saved search, so bids show up without the user
// having to remember to tap Scan — the closest thing to a real alert we can do
// without a paid always-on backend + push service.
//
// Rate-limited, because this used to run on every single app open: one /scan
// per saved search, back to back, each with a 150s timeout. Three saved
// searches meant up to seven minutes of mobile-data requests per launch and a
// matching pile of work for the backend. Bid boards post daily at best, so a
// few hours between automatic checks costs nothing — and tapping Scan on a
// saved search still runs it immediately, on demand.
const SAVED_SEARCH_CHECK_MS=6*60*60*1000; // 6 hours
async function checkSavedSearches(){
  if(!savedSearches.length)return;
  if(isOffline())return;
  const last=store.get("saved_search_checked_at",0);
  if(last&&Date.now()-last<SAVED_SEARCH_CHECK_MS)return;
  store.set("saved_search_checked_at",Date.now());
  let newTotal=0;
  for(const s of savedSearches){
    try{
      const token=await getSupabaseToken();
      const r=await fetchWithTimeout(SERVER+"/scan",{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({key:licenseKey(),device_id:deviceId(),supabase_token:token,location:s.location,radius:s.radius})
      },150000);
      const d=await r.json();
      if(d.ok){
        const added=mergeOpenBids(d.bids||{},Object.keys(d.city_coords||{}));
        newTotal+=added;
      }
    }catch(e){}
  }
  if(newTotal>0){
    toast(`${plural(newTotal,"new bid")} from your saved searches!`);
    showFeedBadge(true);
    fireNotification("CurbCall Pro — New Bids",`${plural(newTotal,"new bid")} from your saved searches.`);
    if(document.getElementById("screen-feed").classList.contains("active"))renderFeed();
    // Unconditional, unlike the renderFeed() above: this is a background scan
    // that can land while Find is the visible screen, and the summary there
    // would otherwise sit stale until something else happens to touch Bids.
    renderScanSummary();
  }
}

// ── "You are here" preview map on the Find tab ──
// Also doubles as a location picker: click anywhere (or a nearby city
// marker) to set the search center, with a live circle showing the radius
// that'll actually be searched.
// Leaflet doesn't retry a tile that fails to load (dropped connection, a
// slow response) — it just leaves it blank. Cache-bust and retry a few
// times with backoff so a transient network hiccup doesn't leave a
// permanent gap in the map.
// Light basemap. The dark one matched the app's chrome but washed out the pins
// on top of it — which are the entire point of the map — and street names went
// grey-on-grey at the zoom levels people actually use. Defined once here
// rather than repeated at each of the three maps.
// OpenStreetMap's standard style. Their tile usage policy allows this --
// there is no bar on production apps -- but it does set conditions, and all
// of them are met here:
//
//   * exactly this URL, no {s} subdomains and no {r}: they do not serve @2x;
//   * visible licence attribution, enabled on every map below;
//   * HTTPS, never the http:// form;
//   * a real Referer, which a browser sends and nothing in this app strips
//     (no Referrer-Policy meta, no _headers file);
//   * tiles cached per their headers, which Leaflet and the browser do;
//   * no prefetching, no bulk download, no offline "save this area".
//
// Availability is best-effort with no SLA, so keep this a single constant:
// switching provider should never need more than editing this line.
const MAP_TILE_URL="https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const MAP_ATTRIB='&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors &middot; <a href="https://www.openstreetmap.org/fixthemap" target="_blank" rel="noopener">report a map issue</a>';

// The map is a location picker, not somewhere to explore, and it had none of
// the limits that implies.
//
// scrollWheelZoom off is the important one on a PHONE: the map sits mid-page
// in a single scrolling column there, so a wheel/trackpad scroll with the
// cursor over it zoomed the map instead of scrolling the page -- you could
// not get past the map without the page fighting you. Zoom is the +/-
// buttons, double-click, or pinch, all of which are deliberate. Pinch stays
// on because on a phone the wheel does not exist and two fingers are never
// accidental.
//
// Desktop (--bp-desktop and up) doesn't have that collision the same way --
// the map sits in a fixed-height card on a page with real width to spare, a
// mouse user can move off it in any direction to keep scrolling, and
// scroll-to-zoom is the expected way to zoom a map with a mouse. mapOpts()
// below is evaluated at each map's creation time rather than baked into one
// shared constant, so the same map code serves both.
//
// The rest stops the map wandering: no infinite horizontal world repeat, no
// zooming out to the whole planet, and a hard boundary around the area this
// product actually covers, so a stray drag cannot leave you looking at the
// Atlantic with no idea how to get home.
const MAP_BOUNDS=[[15.0,-172.0],[72.0,-52.0]];   // US incl. Alaska and Hawaii
const MAP_OPTS={attributionControl:true,tap:false,scrollWheelZoom:false,
  minZoom:3,maxBounds:MAP_BOUNDS,maxBoundsViscosity:1.0,worldCopyJump:false};
function mapOpts(){return{...MAP_OPTS,scrollWheelZoom:window.innerWidth>=960};}
const TILE_OPTS={maxZoom:19,minZoom:3,noWrap:true,bounds:MAP_BOUNDS,
  attribution:MAP_ATTRIB};

// Back to a dark ring. The ring's job is to separate a coloured pin from the
// basemap, so it inverts whenever the basemap does: light ring on dark
// tiles, dark ring on OSM's pale streets.
const PIN_RING="#1b2233";
const CENTER_PIN={radius:9,color:PIN_RING,fillColor:"#f5b400",fillOpacity:1,weight:3};

function addTileRetry(layer){
  const retries=new WeakMap();
  layer.on("tileerror",(e)=>{
    const tile=e.tile,n=(retries.get(tile)||0)+1;
    if(n>3||!tile)return;
    retries.set(tile,n);
    setTimeout(()=>{
      const base=tile.src.split("?")[0];
      tile.src=base+"?retry="+n+"-"+Date.now();
    },800*n);
  });
}

let findMap=null,findMarker=null,findRadiusCircle=null,findCenter=null,findTownMarkers=[];
// Set the first time the user touches the map themselves (drag, pinch,
// wheel, a tap, the zoom buttons). Background work that only ever wanted
// to give a sensible starting view -- the startup GPS fix -- checks this
// and stays out of the way once someone is driving.
let findUserMoved=false;
// The place name comes back seconds after the tap that asked for it (up to
// three geocoders in turn). Relabel the pin when it arrives, but never move
// the map again: by then the user is often already panning around, and a
// second setView/fitBounds was what snapped them back. Returns false when a
// newer pick has superseded this one, so the caller drops the stale result.
function labelFindPin(lat,lon,label){
  if(!findMarker||!findCenter||findCenter.lat!==lat||findCenter.lon!==lon)return false;
  findMarker.setPopupContent(esc(label));
  return true;
}
function updateFindMap(lat,lon,label){
  const mv=document.getElementById("find-map-view");
  if(!mv||!HAS_MAPS)return; // Leaflet never loaded — the rest of the app still works
  findCenter={lat,lon};
  if(!findMap){
    // tap:false -- Leaflet's own legacy touch-tap handler (Tap.js) is known to
    // misfire or swallow the synthetic click on current iOS Safari in some
    // page layouts; modern mobile browsers reliably fire a native click after
    // a real touch tap on their own, so disabling Leaflet's handler and
    // relying on that is the standard fix for "tapping the map does nothing"
    // reports. (Not independently reproduced on a physical device here --
    // please confirm this actually fixes it on your phone.)
    findMap=L.map(mv,mapOpts()).setView([lat,lon],11);
    const findTiles=L.tileLayer(MAP_TILE_URL,TILE_OPTS).addTo(findMap);
    addTileRetry(findTiles);
    findMap.on("click",e=>pickFindLocation(e.latlng.lat,e.latlng.lng,null));
    // DOM input events rather than Leaflet's movestart/zoomstart: those also
    // fire for our own setView/fitBounds, some of them a frame later, so they
    // can't tell a user's pan from the app's.
    const touched=()=>{findUserMoved=true;};
    ["pointerdown","touchstart","wheel","keydown"].forEach(t=>
      mv.addEventListener(t,touched,{passive:true}));
  } else {
    findMap.setView([lat,lon],11);
  }
  if(findMarker)findMap.removeLayer(findMarker);
  findMarker=L.circleMarker([lat,lon],CENTER_PIN)
    .addTo(findMap).bindPopup(esc(label||"You are here"));
  drawFindRadiusCircle();
  mapSettle(findMap);
}
function drawFindRadiusCircle(){
  if(!findMap||!findCenter)return;
  if(findRadiusCircle)findMap.removeLayer(findRadiusCircle);
  findRadiusCircle=L.circle([findCenter.lat,findCenter.lon],{
    radius:radius*1609.34,color:"#b45309",weight:2,fillOpacity:0.08,fillColor:"#f59e0b",
  }).addTo(findMap);
  // The map is created at a fixed zoom 11 and nothing ever changed it, so the
  // circle was the only thing that grew when you picked a bigger radius. At
  // zoom 11 a phone-sized map shows roughly ten miles across -- so a 50 or
  // 125mi selection drew a circle several times wider than the viewport and
  // all you saw was a fragment of its arc, with your own search center
  // scrolled off-screen entirely. Fit the view to the circle so the area you
  // actually chose is the thing on screen.
  // invalidateSize() first for the same reason mapSettle exists: fitBounds
  // computes zoom from the container size Leaflet currently believes in, and
  // a stale one gives the wrong zoom. Double rAF so the browser has really
  // finished laying the frame out before either call.
  requestAnimationFrame(()=>requestAnimationFrame(()=>{
    if(!findMap||!findRadiusCircle)return;
    findMap.invalidateSize();
    findMap.fitBounds(findRadiusCircle.getBounds(),{padding:[16,16]});
  }));
}
function clearFindTownMarkers(){
  findTownMarkers.forEach(m=>findMap.removeLayer(m));
  findTownMarkers=[];
}
// Nearby-town dots come from Overpass, which is a public service and can take
// several seconds. Three things made that feel worse than it is:
//
//   * the old markers were cleared only when the RESPONSE arrived, so the
//     previous location's dots sat on screen for the whole wait;
//   * the request was awaited after the reverse geocode rather than beside
//     it, so the dots also paid for the geocode;
//   * clicking around fired one query per click with no sequencing, and a
//     slow earlier response could land last and overwrite the right dots.
//
// So: clear immediately, run in parallel, ignore superseded responses, and
// remember what we already fetched.
let findTownSeq=0;
const findTownCache=new Map();      // "lat,lon,radius" -> elements
const FIND_TOWN_CACHE_MAX=40;

function findTownKey(lat,lon){
  // Round to ~1km. Clicking two streets apart is the same set of towns, and
  // without this every pixel of movement is a fresh network round trip.
  return `${lat.toFixed(2)},${lon.toFixed(2)},${radius}`;
}

function drawFindTownMarkers(elements){
  clearFindTownMarkers();
  const seen=new Set();
  for(const el of(elements||[])){
    const name=el.tags?.name;
    if(!name||seen.has(name.toLowerCase())||el.lat==null||el.lon==null)continue;
    seen.add(name.toLowerCase());
    const state=el.tags?.["addr:state"]||"";
    const label=state?`${name}, ${state}`:name;
    const m=L.circleMarker([el.lat,el.lon],{radius:6,color:PIN_RING,fillColor:"#3b82f6",fillOpacity:1,weight:2})
      .addTo(findMap).bindPopup(esc(label));
    m.on("click",()=>pickFindLocation(el.lat,el.lon,label));
    findTownMarkers.push(m);
    if(findTownMarkers.length>=15)break;
  }
}

async function refreshFindTownMarkers(lat,lon){
  if(!findMap)return;
  const mine=++findTownSeq;
  const key=findTownKey(lat,lon);
  const hit=findTownCache.get(key);
  if(hit){ drawFindTownMarkers(hit); return; }
  // Nothing cached, so there will be a wait. Take the stale dots down now
  // rather than leaving the last location's towns sitting on the map.
  clearFindTownMarkers();
  const radiusM=Math.round(radius*1609.34);
  const q=`[out:json][timeout:20];(node["place"~"city|town"](around:${radiusM},${lat},${lon}););out body 30;`;
  try{
    const r=await fetchWithTimeout("https://overpass-api.de/api/interpreter",{
      method:"POST",headers:{"Content-Type":"text/plain"},body:q,
    },15000);
    const d=await r.json();
    // A newer click has happened since this request went out; its answer is
    // the right one and this is not.
    if(mine!==findTownSeq)return;
    findTownCache.set(key,d.elements||[]);
    if(findTownCache.size>FIND_TOWN_CACHE_MAX)
      findTownCache.delete(findTownCache.keys().next().value);
    drawFindTownMarkers(d.elements||[]);
  }catch(e){/* best-effort — clicking blank map still works without town markers */}
}
// Civil divisions that are not places anyone lets a bid from. A rural point
// reverse-geocodes to one of these constantly, and the provider writes them
// with an "of" prefix ("Township of Rock Prairie").
const NON_PLACE_RE=/\b(township|twp|unincorporated|unorganized|census[\s-]designated|CDP|precinct|ward|reservation)\b/i;

// Name the TOWN in a reverse-geocode response, or "" if it names none.
//
// A county is deliberately not accepted here. Standing just outside Bolivar,
// BigDataCloud has no municipality to give and would happily answer "Polk
// County" — so the chain stopped at the first provider and never reached the
// two that can actually say "Bolivar". A county is a real fallback, but only
// after every provider has failed to name a town, which is why it is collected
// separately by resolvePlaceLabel rather than returned from here.
//
// Also excluded: the civil township `locality` reports in rural areas
// ("Township of Rock Prairie"), which is not a place anyone lets a bid from.
function placeLabel(d){
  d=d||{};
  const state=bdcState(d);
  const admin=((d.localityInfo||{}).administrative)||[];
  const named=lvl=>admin
    .filter(e=>e&&typeof e==="object"&&e.adminLevel===lvl&&e.name)
    .map(e=>String(e.name).trim());
  // adminLevel 8 is the municipality; 7 is the civil township, 6 the county.
  const candidates=[d.city,...named(8),d.locality]
    .map(s=>String(s||"").trim()).filter(Boolean);
  for(const name of candidates){
    if(!NON_PLACE_RE.test(name)&&!COUNTY_RE.test(name))
      return state?`${name}, ${state}`:name;
  }
  return "";
}

const COUNTY_RE=/\b(count(?:y|ies)|parish|borough)\b/i;

// The county, or failing that the ZIP — held in reserve by resolvePlaceLabel
// and used only when no provider could name a town.
function coarseLabel(d){
  d=d||{};
  const state=bdcState(d);
  const admin=((d.localityInfo||{}).administrative)||[];
  const county=admin
    .filter(e=>e&&typeof e==="object"&&e.adminLevel===6&&e.name)
    .map(e=>String(e.name).trim())[0]
    ||(COUNTY_RE.test(String(d.locality||""))?String(d.locality).trim():"");
  if(county)return state?`${county}, ${state}`:county;
  return d.postcode?String(d.postcode):"";
}
function bdcState(d){
  return String((d&&d.principalSubdivisionCode)||"").split("-").pop();
}

// Nominatim (OpenStreetMap) — free, keyless, and a completely separate dataset
// from BigDataCloud. The free BigDataCloud endpoint is rate-limited per IP and
// answers with an error object rather than an HTTP error, which reads as "this
// place has no name" and is exactly how a coordinate pair ended up in the box.
const US_STATE_ABBR={"alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR",
 "california":"CA","colorado":"CO","connecticut":"CT","delaware":"DE","florida":"FL",
 "georgia":"GA","hawaii":"HI","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA",
 "kansas":"KS","kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD",
 "massachusetts":"MA","michigan":"MI","minnesota":"MN","mississippi":"MS","missouri":"MO",
 "montana":"MT","nebraska":"NE","nevada":"NV","new hampshire":"NH","new jersey":"NJ",
 "new mexico":"NM","new york":"NY","north carolina":"NC","north dakota":"ND","ohio":"OH",
 "oklahoma":"OK","oregon":"OR","pennsylvania":"PA","rhode island":"RI","south carolina":"SC",
 "south dakota":"SD","tennessee":"TN","texas":"TX","utah":"UT","vermont":"VT","virginia":"VA",
 "washington":"WA","west virginia":"WV","wisconsin":"WI","wyoming":"WY",
 "district of columbia":"DC"};
function nominatimLabel(d){
  const a=(d&&d.address)||{};
  const state=US_STATE_ABBR[String(a.state||"").toLowerCase()]||"";
  // Towns only, same reasoning as placeLabel — county is held in reserve.
  const name=[a.city,a.town,a.village,a.hamlet,a.municipality]
    .map(s=>String(s||"").trim())
    .find(s=>s&&!NON_PLACE_RE.test(s)&&!COUNTY_RE.test(s));
  return name?(state?`${name}, ${state}`:name):"";
}
function nominatimCoarse(d){
  const a=(d&&d.address)||{};
  const state=US_STATE_ABBR[String(a.state||"").toLowerCase()]||"";
  const county=String(a.county||"").trim();
  if(county)return state?`${county}, ${state}`:county;
  return a.postcode?String(a.postcode):"";
}

function milesBetween(lat1,lon1,lat2,lon2){
  const R=3958.8,r=Math.PI/180;
  const dLat=(lat2-lat1)*r,dLon=(lon2-lon1)*r;
  const a=Math.sin(dLat/2)**2+Math.cos(lat1*r)*Math.cos(lat2*r)*Math.sin(dLon/2)**2;
  return 2*R*Math.asin(Math.sqrt(a));
}

// Last resort before coordinates: the nearest real town from OpenStreetMap.
// This is the same Overpass source the map already uses to draw nearby-city
// markers, so it costs nothing new and is known to work from the field.
async function nearestTownLabel(lat,lon,state){
  const q=`[out:json][timeout:15];(node["place"~"city|town|village"](around:40000,${lat},${lon}););out body 40;`;
  const r=await fetchWithTimeout("https://overpass-api.de/api/interpreter",
    {method:"POST",headers:{"Content-Type":"text/plain"},body:q},12000);
  const d=await r.json();
  let best=null,bestD=Infinity;
  for(const el of(d.elements||[])){
    const name=el.tags&&el.tags.name;
    if(!name||el.lat==null||el.lon==null||NON_PLACE_RE.test(name))continue;
    const dist=milesBetween(lat,lon,el.lat,el.lon);
    if(dist<bestD){bestD=dist;best={name,state:(el.tags["addr:state"]||"").trim()};}
  }
  if(!best)return "";
  const st=best.state||state||"";
  return st?`${best.name}, ${st}`:best.name;
}

// One reverse-geocode for the whole app: three independent sources, tried in
// order, so a single provider being rate-limited no longer drops the user back
// to raw coordinates. Returns "" only if all three come up empty.
async function resolvePlaceLabel(lat,lon){
  let state="",coarse="";
  try{
    const r=await fetchWithTimeout(
      `https://api.bigdatacloud.net/data/reverse-geocode-client?latitude=${lat}&longitude=${lon}&localityLanguage=en`,{},9000);
    const d=await r.json();
    state=bdcState(d)||state;
    const v=placeLabel(d);
    if(v)return v;
    coarse=coarse||coarseLabel(d);
  }catch(e){}
  try{
    const r=await fetchWithTimeout(
      `https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=10&addressdetails=1&lat=${lat}&lon=${lon}`,
      {headers:{"Accept":"application/json"}},9000);
    const d=await r.json();
    const v=nominatimLabel(d);
    if(v)return v;
    coarse=coarse||nominatimCoarse(d);
  }catch(e){}
  try{
    const v=await nearestTownLabel(lat,lon,state);
    if(v)return v;
  }catch(e){}
  // Only now: the county, or a ZIP. Both are scannable; neither is a town.
  return coarse;
}

async function pickFindLocation(lat,lon,label){
  const input=document.getElementById("loc-input");
  if(label){
    if(input)input.value=label;
    updateFindMap(lat,lon,label);
    refreshFindTownMarkers(lat,lon);
    return;
  }
  updateFindMap(lat,lon,"Locating...");
  // Start the town dots NOW rather than after the geocode. They are two
  // independent lookups and awaiting them in series meant the dots paid for
  // the geocode as well as their own request.
  const towns=refreshFindTownMarkers(lat,lon);
  // Coordinates only if all three providers came up empty. The server accepts
  // them, but it resolves them with the same reverse geocode that just failed,
  // so a scan from a coordinate pair usually ends up with no city and no
  // portals to read — it looks like it worked and quietly finds nothing.
  const val=(await resolvePlaceLabel(lat,lon))||`${lat.toFixed(4)}, ${lon.toFixed(4)}`;
  // Tapped somewhere else while this name was resolving: that pick owns the
  // box and the pin now, and this answer is about a place they left.
  if(!labelFindPin(lat,lon,val))return;
  if(input)input.value=val;
  await towns;
}

// ── Reusable "click the map to set a location" picker for tabs that don't
// need Find's extra nearby-town-marker/auto-locate machinery (Upcoming,
// Leads) -- just click, reverse-geocode, and fill that tab's own location
// input. Keeps the map standard across every search tab instead of only
// the Find tab having one. ──
function createLocationPicker(containerId,inputId){
  const st={map:null,marker:null,center:null};
  function show(lat,lon,label){
    const mv=document.getElementById(containerId);
    if(!mv||!HAS_MAPS)return;
    st.center={lat,lon};
    if(!st.map){
      st.map=L.map(mv,mapOpts()).setView([lat,lon],11);
      const tiles=L.tileLayer(MAP_TILE_URL,TILE_OPTS).addTo(st.map);
      addTileRetry(tiles);
      st.map.on("click",e=>pick(e.latlng.lat,e.latlng.lng));
    } else {
      st.map.setView([lat,lon],11);
    }
    if(st.marker)st.map.removeLayer(st.marker);
    st.marker=L.circleMarker([lat,lon],CENTER_PIN)
      .addTo(st.map).bindPopup(esc(label||"Selected location"));
    mapSettle(st.map);
  }
  async function pick(lat,lon){
    const input=document.getElementById(inputId);
    show(lat,lon,"Locating...");
    const val=(await resolvePlaceLabel(lat,lon))||`${lat.toFixed(4)}, ${lon.toFixed(4)}`;
    // Same as labelFindPin: relabel, don't re-center -- and drop the answer
    // if a later tap has moved the pin since.
    if(!st.marker||!st.center||st.center.lat!==lat||st.center.lon!==lon)return;
    st.marker.setPopupContent(esc(val));
    if(input)input.value=val;
  }
  return {show, invalidateSize:()=>{if(st.map)st.map.invalidateSize();}, hasMap:()=>!!st.map};
}
// Upcoming has no picker: it duplicated the text field on a screen that is
// the same broad municipal search as Bids. Leads keeps one, where choosing
// a neighbourhood visually is the point.
const leadsPicker=LEADS_ENABLED?createLocationPicker("leads-map-view","leads-loc-input"):null;

// ── Auto ZIP + live map pin ──
// fillInput=false (auto-run on load): only fills the box if it's empty, but
// still drops a pin on the preview map either way.
// fillInput=true (Use My Location button): always overwrites the box + toasts.
async function detectLocation(fillInput){
  if(!navigator.geolocation){if(fillInput)toast("Location isn't available in this browser.");return;}
  navigator.geolocation.getCurrentPosition(async(pos)=>{
    try{
      const{latitude,longitude}=pos.coords;
      // The startup fix can land seconds into the session. If they're already
      // moving the map, jumping it to their GPS position is the snap they
      // reported -- leave the map and the box to them. "Use My Location"
      // (fillInput) is an explicit ask and always moves it.
      if(!fillInput&&findUserMoved)return;
      updateFindMap(latitude,longitude,"You are here");
      const val=await resolvePlaceLabel(latitude,longitude);
      if(val){
        if(!labelFindPin(latitude,longitude,val))return;
        refreshFindTownMarkers(latitude,longitude);
        const input=document.getElementById("loc-input");
        if(input&&(fillInput||!input.value.trim()))input.value=val;
        if(fillInput)toast(val);
        // A brand-new account lands on "No bids yet -- Tap Find to scan your
        // area" and a 90-second wait. Nothing sells this product like
        // opening it to a full feed, so run that first scan for them.
        if(!fillInput)maybeFirstScan();
      }else if(fillInput){
        // Never silently drop a coordinate pair in the box here. The user asked
        // for their town; a number they can't check is worse than being told.
        toast("Couldn't name your location — type a town or ZIP.");
      }
    }catch(e){if(fillInput)toast("Couldn't detect your location — enter a ZIP instead.");}
  },()=>{if(fillInput)toast("Couldn't detect your location — enter a ZIP instead.");},
  {timeout:10000,maximumAge:600000});
}
function autoFillZip(){detectLocation(false);}

// ── The first scan, run for them ──────────────────────────────────────
//
// Exactly once, on a device that has never scanned and has no bids. Every
// condition here is a guard against doing this to somebody who did not want
// it: an existing customer opening the app must never trigger a surprise
// 90-second scan, and neither must a second device, a reinstall with a
// synced feed, or somebody who scanned yesterday and found nothing.
//
// The flag is set BEFORE the scan runs, not after. If it were set on
// success, a scan that failed or was cancelled would re-trigger on the next
// app open, and the one thing worse than no first scan is one that starts
// itself over and over.
// ── First-run onboarding ──

function onboardingNeeded(){
  if(store.get(ONBOARD_KEY,false))return false;
  // Someone who already has a location or bids has been here before this
  // screen existed. Asking them where they work would be absurd.
  if(store.get(HOME_LOC_KEY,""))return false;
  if(store.get("last_scan_debug",null))return false;
  return true;
}

function renderOnbRadius(){
  const box=document.getElementById("onb-radius");
  if(!box)return;
  box.innerHTML=[10,25,50,75,125].map(r=>
    `<div class="r ${r===onbRadius?"on":""}" data-r="${r}">${r} mi</div>`).join("");
  box.querySelectorAll(".r").forEach(el=>{
    el.onclick=()=>{onbRadius=+el.dataset.r;renderOnbRadius();onbCheckCoverage();};
  });
}

// Answering with their real number is the whole point: it proves the product
// knows their town before they have run anything, and it quietly catches a
// location we cannot resolve while there is still someone here to retype it.
let onbCovTimer=null;
function onbCheckCoverage(){
  const el=document.getElementById("onb-cov");
  const loc=(document.getElementById("onb-loc")||{}).value||"";
  if(!el)return;
  clearTimeout(onbCovTimer);
  if(loc.trim().length<3){el.textContent="";return;}
  el.textContent="Checking...";
  onbCovTimer=setTimeout(async()=>{
    try{
      const r=await fetchWithTimeout(SERVER+"/coverage",{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({location:loc.trim(),radius:onbRadius})},20000);
      const d=await r.json();
      if(d.ok)el.textContent=`${d.agencies} agencies posting bids within ${onbRadius} miles`;
      else el.textContent="We couldn't find that place — try a ZIP code";
    }catch(e){el.textContent="";}
  },600);
}

// Where to land after a session appears: the app, or the two setup questions.
//
// onboardingNeeded() can only see THIS browser. On iOS the installed Home
// Screen app and Safari keep separate storage, so the same person answered
// the same questions in each and it read as "it asks every time". A new
// phone, a cleared cache or a second computer did the same thing.
//
// So a local "not onboarded" is now treated as a question rather than an
// answer, and the account is asked before anyone is interrupted.
async function routeAfterAuth(){
  if(!onboardingNeeded()){showApp();return;}
  let done=false;
  try{done=await accountHasOnboarded();}catch(e){done=false;}
  if(done){store.set(ONBOARD_KEY,true);showApp();return;}
  showOnboarding();
}

// Has this ACCOUNT been through setup, on any device?
//
// Never blocks the app on a slow network: if the answer does not arrive
// quickly the caller falls back to what this browser knows, which is the
// behaviour that shipped before this existed.
async function accountHasOnboarded(){
  if(!sb||!currentUser)return false;
  const ask=(async()=>{
    const{data,error}=await sb.from("company_profiles").select("*").maybeSingle();
    if(error||!data)return false;
    // The column arrives with a migration in supabase_sync_schema.sql. Until
    // a project runs it the value is simply undefined, and the fallback below
    // still recognises anyone who filled in any part of their company.
    if(data.onboarded)return true;
    return COMPANY_FIELDS.some(k=>data[k]);
  })();
  const timeout=new Promise(r=>setTimeout(()=>r(false),4000));
  return Promise.race([ask,timeout]);
}

// Record it against the account, so the next device does not ask again.
// Best effort on purpose: a project that has not run the migration rejects
// the unknown column, and being unable to remember this must never stop
// someone finishing setup.
async function pushOnboarded(){
  if(!sb||!currentUser||!currentUser.id)return;
  try{
    await sb.from("company_profiles")
      .upsert({user_id:currentUser.id,onboarded:true},{onConflict:"user_id"});
  }catch(e){/* stays local; the field fallback above still covers most cases */}
}

function showOnboarding(){
  document.getElementById("app").style.display="none";
  document.getElementById("auth-screen").style.display="";
  document.getElementById("auth-card").style.display="none";
  const sent=document.getElementById("auth-sent-card");
  if(sent)sent.style.display="none";
  document.getElementById("onboard-card").style.display="";
  renderOnbRadius();
  const co=document.getElementById("onb-co");
  if(co&&!co.value)co.value=companyProfile.name||"";
}

function onbStep(n){
  document.getElementById("onb-step1").style.display=n===1?"":"none";
  document.getElementById("onb-step2").style.display=n===2?"":"none";
  document.getElementById("onb-dot1").className="onb-dot"+(n>=1?" on":"");
  document.getElementById("onb-dot2").className="onb-dot"+(n>=2?" on":"");
}

function finishOnboarding(){
  store.set(ONBOARD_KEY,true);
  pushOnboarded();
  document.getElementById("onboard-card").style.display="none";
  document.getElementById("auth-card").style.display="";
  showApp();
}

(function(){
  const loc=document.getElementById("onb-loc");
  if(loc)loc.addEventListener("input",onbCheckCoverage);
  const next=document.getElementById("onb-next");
  if(next)next.onclick=()=>{
    const v=(document.getElementById("onb-loc").value||"").trim();
    if(!v){toast("Enter a city or ZIP, or skip for now");return;}
    store.set(HOME_LOC_KEY,v);
    store.set(HOME_RADIUS_KEY,onbRadius);
    onbStep(2);
  };
  const done=document.getElementById("onb-done");
  if(done)done.onclick=()=>{
    const name=(document.getElementById("onb-co").value||"").trim();
    const trade=(document.getElementById("onb-trade").value||"").trim();
    if(name)companyProfile.name=name;
    if(trade)companyProfile.specialty=trade;
    if(name||trade){store.set("company_profile",companyProfile);pushCompanyProfile();}
    finishOnboarding();
  };
  const s1=document.getElementById("onb-skip1");
  if(s1)s1.onclick=()=>onbStep(2);
  const s2=document.getElementById("onb-skip2");
  if(s2)s2.onclick=finishOnboarding;
})();

const FIRST_SCAN_KEY="first_scan_done";
function maybeFirstScan(){
  if(store.get(FIRST_SCAN_KEY,false))return;
  // Wait for the server's answer instead of guessing from an empty cache.
  //
  // Every other guard here is DEVICE-local, and on a new device they are all
  // absent for the same reason: it is a new device. So an existing customer
  // signing in on a second phone passed every one of them and was given an
  // unrequested 90-second scan -- the exact thing the comment below promises
  // cannot happen. "pending" means the answer is still in flight; the next
  // call, after the sync lands, decides properly.
  if(feedSyncState==="pending"||feedSyncState==="has-bids")return;
  if(Object.keys(bidData||{}).length)return;       // already has bids
  if(store.get("last_scan_debug",null))return;     // has scanned before
  const input=document.getElementById("loc-input");
  if(!input||!input.value.trim())return;           // nothing to scan
  // Looked up rather than closed over: the scanBtn const is declared later
  // in the file than this function, and relying on the geolocation callback
  // always firing after that is a temporal-dead-zone bug waiting to happen.
  const btn=document.getElementById("scan-btn");
  if(btn&&btn.disabled)return;                     // a scan is already running
  if(!navigator.onLine)return;
  store.set(FIRST_SCAN_KEY,true);
  const status=document.getElementById("scan-status");
  if(status)status.textContent="Finding work near you\u2026";
  runScan(false);
}

// ── Diagnostics ──
// A scan can come back "successful" with two bids because the area is quiet,
// or because the pipeline found forty and discarded thirty-eight. Those look
// identical from the outside, and the difference is the whole question when
// judging whether search is working. The server reports its funnel; this keeps
// the last one so it can be read in the app instead of in the Render logs.
let lastScanDebug=store.get("last_scan_debug",null);
function recordScanDiagnostics(entry){
  lastScanDebug={...entry,at:new Date().toISOString()};
  store.set("last_scan_debug",lastScanDebug);
}
function diagnosticsText(){
  const lines=[];
  const d=lastScanDebug;
  if(d){
    lines.push(`Last ${d.kind}: ${d.location} · ${d.radius} mi · ${(d.ms/1000).toFixed(1)}s`
      +(d.cached?" (cached)":""));
    lines.push(`  shown: ${d.total}   newly added: ${d.added}`);
    const f=(d.debug&&d.debug.funnel)||null;
    if(d.debug&&d.debug.raw_local!=null)lines.push(`  extracted before filtering: ${d.debug.raw_local}`);
    if(f&&Object.keys(f).length){
      lines.push("  outcome of each extracted bid:");
      Object.keys(f).sort().forEach(k=>lines.push(`    ${k}: ${f[k]}`));
    }
    lines.push(`  at: ${d.at}`);
  }else{
    lines.push("No scan recorded yet on this device.");
  }
  if(lastHealth){
    lines.push("");
    lines.push(`Server: ${lastHealth.status}`);
    const b=lastHealth.backends||{};
    lines.push("  backends: "+Object.keys(b).sort()
      .map(k=>`${k}=${b[k]?"on":"OFF"}`).join(", "));
    (lastHealth.problems||[]).forEach(p=>lines.push(`  ! ${p}`));
    const ls_=lastHealth.local_search||{};
    lines.push(`  search: empty_streak=${ls_.consecutive_empty_searches}`
      +`, degraded=${ls_.degraded}`);
  }
  lines.push("");
  lines.push(`app: ${location.host}  device: ${deviceId()}`);
  return lines.join("\n");
}
let lastHealth=null;
async function loadHealth(){
  const box=document.getElementById("diag-health");
  if(!box)return;
  if(isOffline()){box.textContent="Offline — can't reach the server.";return;}
  box.textContent="Checking server...";
  try{
    // An admin token upgrades /health to the detailed view (scan history,
    // provider error bodies). Without one the server returns the public
    // summary, which is all a non-admin should ever see.
    const tok=adminToken();
    const r=await fetchWithTimeout(
      SERVER+"/health", tok?{headers:{"X-Admin-Token":tok}}:{}, 45000);
    lastHealth=await r.json();
    // r.json() only throws on unparseable JSON -- a response that parses to
    // null or a bare scalar (a proxy/CDN error page that happens to be valid
    // JSON, or the server itself misbehaving) sails through as "success" and
    // left this reading lastHealth.backends on null, throwing right past the
    // rest of this function and leaving the card stuck on "Checking server...".
    if(!lastHealth||typeof lastHealth!=="object"){
      box.textContent="Couldn't reach the server (it may be waking up — try again).";
      lastHealth=null;
      return;
    }
  }catch(e){
    box.textContent="Couldn't reach the server (it may be waking up — try again).";
    return;
  }
  const b=lastHealth.backends||{};
  const problems=lastHealth.problems||[];
  const dot=(on)=>on?'<span style="color:var(--green);">●</span>'
                    :'<span style="color:var(--red);">●</span>';
  box.innerHTML=
    `<div style="margin-bottom:0.4rem;font-weight:700;color:${problems.length?"var(--accent)":"var(--green)"};">`
    +`${problems.length?"Degraded":"All systems configured"}</div>`
    +Object.keys(b).sort().map(k=>
      `<div>${dot(b[k])} ${esc(k.replace(/_/g," "))}</div>`).join("")
    +(problems.length?`<div style="margin-top:0.5rem;">`
      +problems.map(p=>`<div style="color:var(--accent);">• ${esc(p)}</div>`).join("")
      +`</div>`:"");
}
// Admin-only. It exposes how the pipeline behaved and, with an admin token,
// the server's scan history -- useful to whoever runs the service, noise and
// needless detail to a contractor looking for work.
function renderDiagnostics(){
  const d=lastScanDebug;
  const f=(d&&d.debug&&d.debug.funnel)||{};
  const keys=Object.keys(f).sort();
  // "kept" is the good outcome; everything else is a bid that was found and
  // then discarded, which is the number worth watching.
  const discarded=keys.filter(k=>k!=="kept"&&k!=="placed_by_search_town")
                      .reduce((n,k)=>n+f[k],0);
  return `
    <div class="account-card">
      <div class="account-email hdr-ic" style="font-size:var(--fs-base);"><svg class="icon-svg"><use href="#i-activity"/></svg>Diagnostics</div>
      <div class="account-status" style="margin-bottom:0.7rem;">
        What the last search actually did, and whether the server has everything it needs.
        Handy when a search returns less than you expected.
      </div>
      <div class="account-status" id="diag-health" style="line-height:1.7;">Checking server...</div>
      <div style="border-top:1px solid var(--border);margin:0.8rem 0;"></div>
      ${d?`
        <div class="detail-row"><div class="detail-label">Last search</div>
          <div class="detail-val">${esc(d.location)} &middot; ${esc(String(d.radius))} mi
            &middot; ${(d.ms/1000).toFixed(1)}s${d.cached?" (cached)":""}</div></div>
        <div class="detail-row"><div class="detail-label">Bids shown</div>
          <div class="detail-val">${d.total} (${d.added} new)</div></div>
        ${d.debug&&d.debug.raw_local!=null?`<div class="detail-row">
          <div class="detail-label">Found before filtering</div>
          <div class="detail-val">${d.debug.raw_local}</div></div>`:""}
        ${keys.length?`<div class="detail-row">
          <div class="detail-label">What happened to each one${discarded?` — ${discarded} discarded`:""}</div>
          <div class="detail-val" style="line-height:1.7;">
            ${keys.map(k=>`${esc(k.replace(/_/g," "))}: <b>${f[k]}</b>`).join("<br>")}
          </div></div>`:""}
      `:`<div class="account-status">No search recorded on this device yet.</div>`}
      <div style="display:flex;gap:0.6rem;margin-top:0.8rem;">
        <button class="btn-ghost hdr-ic" id="diag-copy" style="margin-top:0;justify-content:center;"><svg class="icon-svg"><use href="#i-copy"/></svg>Copy</button>
        <button class="btn-ghost hdr-ic" id="diag-refresh" style="margin-top:0;justify-content:center;"><svg class="icon-svg"><use href="#i-refresh"/></svg>Recheck</button>
      </div>
    </div>`;
}

// ── Supabase token helper ──
async function getSupabaseToken(){
  if(!sb)return"";
  const{data:{session}}=await sb.auth.getSession();
  return session?.access_token||"";
}

// ── Auto-unlock after Stripe payment ──
let lastUnlockCheck=0;
async function autoUnlock(){
  if(licenseKey())return;
  if(isOffline())return;
  // This runs on every visibilitychange, which on a phone means every single
  // app switch \u2014 previously one un-timed request to a sleeping free-tier
  // backend each time. A purchase doesn't need to be noticed faster than this.
  if(Date.now()-lastUnlockCheck<5*60*1000)return;
  lastUnlockCheck=Date.now();
  try{
    // The token lets the server match a purchase made from the marketing site
    // (whose Stripe links carry no device id) or on a different device.
    const token=await getSupabaseToken();
    const r=await fetchWithTimeout(SERVER+"/mykey",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({device_id:deviceId(),supabase_token:token})
    },30000);
    const d=await r.json();
    if(d.ok&&d.key){
      store.set("license_key",d.key);
      toast("Unlocked \u2014 thanks for subscribing!");
      // The Account tab may be showing a stale "trial expired" card.
      if(document.getElementById("screen-account").classList.contains("active"))renderAccount();
    }
  }catch{}
}

// ── Scan ──
const scanBtn=document.getElementById("scan-btn");
scanBtn.onclick=()=>runScan(false);
// Results are cached per area per day, so the first scan of an area decides
// what you see until midnight. If that one ran while a search backend was
// down, this is how you get a real answer without waiting a day.
byId("force-scan-btn").onclick=()=>runScan(true);
byId("detect-btn").onclick=()=>detectLocation(true);
byId("loc-input").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();runScan();}});

// Deadlines on real bid pages are rarely a bare date — "Due by 12/01/2026 at
// 2:00 PM", "Bids due December 1, 2026 at 2:00 p.m.". Matching only an ISO
// date and otherwise handing the whole string to new Date() failed on all of
// those, which cost the card its "3d left" urgency chip, broke deadline
// sorting, and made Add to Calendar refuse. Pull the date out of the prose
// first. Mirrors _parse_deadline() in license_server.py.
const _MONTH_WORDS="January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec";
const DEADLINE_PATTERNS=[
  /\d{4}-\d{2}-\d{2}/,
  /\d{1,2}\/\d{1,2}\/\d{4}/,
  /\d{1,2}-\d{1,2}-\d{4}/,
  new RegExp("(?:"+_MONTH_WORDS+")\\.?\\s+\\d{1,2},?\\s+\\d{4}","i"),
  /\d{1,2}\/\d{1,2}\/\d{2}(?!\d)/,
];
function deadlineDate(b){
  const raw=b&&b.deadline;
  if(!raw)return null;
  const t=String(raw).replace(/\s+/g," ").trim();
  // No lookbehind anywhere in here on purpose: a regex literal is compiled
  // when the script is parsed, so one unsupported construct would take the
  // whole app down on older iOS Safari rather than just failing this parse.
  const tryParse=(s,stripDots)=>{
    // A bare ISO date is parsed as UTC by JS; pin it to local midnight so a
    // deadline never reads as a day early west of Greenwich.
    const d=/^\d{4}-\d{2}-\d{2}$/.test(s)?new Date(s+"T00:00:00")
                                         :new Date(stripDots?s.replace(/\./g,""):s);
    if(isNaN(d.getTime()))return null;
    d.setHours(0,0,0,0);
    return d;
  };
  for(const pat of DEADLINE_PATTERNS){
    const m=t.match(pat);
    if(!m)continue;
    // Dots are only stripped from the matched date itself ("Sept. 1, 2026"),
    // never from the surrounding sentence.
    const hit=tryParse(m[0].replace(/\bSept\b/i,"Sep"),true);
    if(hit)return hit;
  }
  return /\d/.test(t)?tryParse(t,false):null;
}
// A bid is only "open" if the server says so AND its deadline (if any) hasn't
// already passed — some source pages leave old bids up without ever marking
// them closed, so we double-check the date ourselves.
function isPastDeadline(b){
  const t=deadlineDate(b);
  if(!t)return false;
  const today=new Date();today.setHours(0,0,0,0);
  return t<today;
}
// Words that mean a solicitation is no longer biddable. Anything else counts
// as open. This used to demand the status be exactly "open" and hide anything
// else, which threw away every bid written up as "Accepting Bids", "Active",
// "Advertised" or "Open - Bids Due 12/1". Must stay in step with
// _is_open_bid() in license_server.py.
const CLOSED_STATUS_WORDS=["closed","close date","awarded","award to","cancel",
  "expired","withdrawn","archived","no longer","not accepting","complete",
  "planned","upcoming","anticipated"];
function isOpen(b){
  const s=String((b&&b.status)||"").trim().toLowerCase();
  if(s&&CLOSED_STATUS_WORDS.some(w=>s.includes(w)))return false;
  return !isPastDeadline(b);
}
// Days remaining until deadline (null if unknown/unparseable) — used to
// color-code bid cards so the ones closing soonest visually pop out.
function daysUntil(b){
  const t=deadlineDate(b);
  if(!t)return null;
  const today=new Date();today.setHours(0,0,0,0);
  return Math.round((t-today)/86400000);
}

// Distance from the scan centre, in miles. The server attaches it to every
// placed bid; anything without one sorts as far away rather than as nearest,
// so a bid cached before this shipped cannot jump the queue on a missing field.
function milesOf(b){
  const m=b&&b.miles;
  return Number.isFinite(m)?m:Number.MAX_SAFE_INTEGER;
}
// Mirrors _score_bid on the server. Higher is better.
function fitScore(b){
  let s=0;
  const d=daysUntil(b);
  if(d!=null)s+=d<0?-50:Math.max(0,30-Math.min(d,30));
  const m=b&&b.miles;
  if(Number.isFinite(m))s-=Math.min(m,125)/10;
  if(b&&(b.email||b.phone))s+=3;
  if(b&&b.value)s+=1;
  return s;
}

// Merges freshly-scanned bids into the feed AND prunes stale ones.
// `scannedCities` (from the scan response's city_coords) tells us which
// towns we just got a current answer for — for those, whatever the server
// says is open right now REPLACES what we had, so a bid that got awarded/
// closed/pulled actually disappears instead of sitting in the feed forever.
// Cities we didn't just rescan are left untouched.
function mergeOpenBids(incoming,scannedCities){
  let added=0;
  const now=Date.now();
  const cities=scannedCities&&scannedCities.length?scannedCities:Object.keys(incoming||{});
  for(const city of cities){
    const openOnes=(((incoming||{})[city])||[]).filter(isOpen);
    const have=bidData[city]||[];
    // When was each bid first seen? The city's list is replaced wholesale
    // below, so without carrying this across a rescan every bid would look
    // brand new again — and "Newest" has nothing else to sort on, since bids
    // carry no date from the source.
    const firstSeen=new Map(have.map(b=>[bidId(city,b),b._first_seen]));
    // The same bid often turns up on two different source pages (an
    // aggregator and the agency's own site), which used to produce two
    // identical cards sharing one id — starring one appeared to star both.
    const seen=new Set();
    const merged=[];
    for(const b of openOnes){
      const id=bidId(city,b);
      if(seen.has(id))continue;
      seen.add(id);
      if(firstSeen.has(id))b._first_seen=firstSeen.get(id);
      else{b._first_seen=now;added++;}
      merged.push(b);
    }
    if(merged.length)bidData[city]=merged;
    else delete bidData[city];
  }
  store.set("last_feed",bidData);queueFeedPush();
  return added;
}

// The bid map is gone. Every card now carries the distance from the search
// centre and the feed sorts by it, so a pin cluster restated what the list
// already said -- while costing 240px above the fold on the screen whose
// whole job is showing bids, plus a tile fetch per pan.
//
// The three LOCATION PICKERS (Find, Upcoming, Leads) are a different thing
// and stay: tapping a map to choose where to search has no list equivalent.
// Leaflet is still loaded for them.

// ── Scan progress ────────────────────────────────────────────────────────
//
// The old bar was a lie with a timer attached: +3% every 1.5s, hard-stopped
// at 90%, over a request that can legitimately run two minutes. It told you
// nothing, and a bar frozen at 90% reads as a hung app.
//
// This one only claims things it actually knows.
//
//   * Elapsed seconds are real, and tick.
//   * The bar is elapsed against THIS DEVICE'S OWN median scan time, so the
//     estimate is calibrated to the user's radius, location and connection
//     rather than to a number somebody guessed once. With no history it uses
//     a conservative default and says less.
//   * When elapsed passes the estimate it stops guessing: the bar switches to
//     a moving sweep and the copy says it is taking longer than usual. That
//     is the honest state, and it looks alive instead of stuck.
//   * It can be cancelled. Previously a scan held the button hostage for up
//     to 150 seconds with no way out.
//
// Deliberately NOT invented: per-stage narration ("reading portals...",
// "checking state lettings..."). The server does not report where it is, so
// any such text would be theatre keyed to a timer -- which is precisely the
// thing being removed here. Real per-step progress needs the backend to
// publish it, and a single sync gunicorn worker cannot serve a poll while a
// scan is blocking it, so that is a backend change, not a frontend one.
// Kept per kind, because the three runs are not the same shape of work: a
// bid scan reads dozens of portals, a permit lookup hits one city's open-data
// API. Sharing one history would make both estimates wrong.
const RUN_TIMES_KEY="run_durations";
const RUN_TIME_DEFAULT={scan:45000,upcoming:40000,leads:12000};
const RUN_TIMES_KEEP=10;

function recordRunDuration(kind,ms){
  if(!(ms>1000)||ms>600000)return;       // ignore instant cache hits and junk
  const all=store.get(RUN_TIMES_KEY,{})||{};
  const a=(all[kind]||[]).filter(n=>typeof n==="number");
  a.push(ms);
  all[kind]=a.slice(-RUN_TIMES_KEEP);
  store.set(RUN_TIMES_KEY,all);
}

function expectedRunMs(kind){
  const all=store.get(RUN_TIMES_KEY,{})||{};
  const a=(all[kind]||[]).filter(n=>typeof n==="number"&&n>0);
  if(!a.length)return RUN_TIME_DEFAULT[kind]||45000;
  // Median, not mean: one 150-second timeout should not drag the estimate for
  // every run after it.
  const s=[...a].sort((x,y)=>x-y);
  const m=s.length%2?s[(s.length-1)/2]:(s[s.length/2-1]+s[s.length/2])/2;
  return Math.max(6000,m);
}

function fmtElapsed(ms){
  const t=Math.floor(ms/1000);
  return t<60?`${t}s`:`${Math.floor(t/60)}:${String(t%60).padStart(2,"0")}`;
}

// Owns the bar, the elapsed line and the cancel affordance for one run.
function startScanProgress(wrapId,barId,opts){
  const o=opts||{};
  const pw=document.getElementById(wrapId);
  const pb=document.getElementById(barId);
  if(!pw||!pb)return{finish(){},fail(){}};
  const started=Date.now();
  const expected=o.expected||expectedRunMs("scan");
  pw.style.display="block";
  pb.classList.remove("indeterminate");
  pb.style.width="4%";

  // The meta line lives next to the bar and is created on demand, so no
  // markup changes are needed for the three places this is used.
  let meta=pw.nextElementSibling;
  if(!meta||!meta.classList.contains("scan-meta")){
    meta=document.createElement("div");
    meta.className="scan-meta";
    meta.innerHTML='<span class="elapsed"></span>';
    if(o.onCancel){
      const b=document.createElement("button");
      b.type="button";b.className="scan-cancel";b.textContent="Cancel";
      meta.appendChild(b);
    }
    pw.parentNode.insertBefore(meta,pw.nextSibling);
  }
  const elapsedEl=meta.querySelector(".elapsed");
  const cancelBtn=meta.querySelector(".scan-cancel");
  if(cancelBtn&&o.onCancel){
    cancelBtn.style.display="";
    cancelBtn.onclick=()=>{cancelBtn.disabled=true;cancelBtn.textContent="Cancelling...";o.onCancel();};
  }else if(cancelBtn){cancelBtn.style.display="none";}

  let over=false;
  const tick=setInterval(()=>{
    const el=Date.now()-started;
    const frac=el/expected;
    if(frac<1){
      // Ease off near the end so it never sits on 99% looking broken.
      pb.style.width=(4+Math.min(0.88,frac*0.88)*100).toFixed(1)+"%";
      elapsedEl.textContent=fmtElapsed(el);
    }else{
      if(!over){
        over=true;
        pb.classList.add("indeterminate");
        if(o.onOverrun)o.onOverrun();
      }
      elapsedEl.textContent=fmtElapsed(el)+" — longer than usual";
    }
  },200);

  function stop(){
    clearInterval(tick);
    if(cancelBtn){cancelBtn.disabled=false;cancelBtn.textContent="Cancel";}
  }
  return{
    elapsed(){return Date.now()-started;},
    finish(){
      stop();
      pb.classList.remove("indeterminate");
      pb.style.width="100%";
      if(elapsedEl)elapsedEl.textContent=fmtElapsed(Date.now()-started);
      setTimeout(()=>{
        pw.style.display="none";pb.style.width="0";
        if(meta&&meta.parentNode)meta.parentNode.removeChild(meta);
      },800);
    },
    fail(){
      stop();
      pb.classList.remove("indeterminate");
      pw.style.display="none";pb.style.width="0";
      if(meta&&meta.parentNode)meta.parentNode.removeChild(meta);
    },
  };
}

// What the scan actually did, in the customer's terms.
//
// The work is real and completely invisible: a scan opens dozens of agency
// bid boards across dozens of towns and reads the individual postings off
// them. A contractor who did that by hand would spend an evening on it, and
// all they see is a list of bids appearing.
//
// Deliberately states counts and the clock, and expresses the saving as the
// thing that is literally true -- sites they did not have to open. It does
// NOT multiply by a made-up minutes-per-site figure and announce "saved you
// four hours": that number would be invented, and a contractor who checks
// two of these sites and finds it took them twenty seconds would stop
// believing the rest of the app.
// What to say when a request did not come back at all.
//
// Every one of these used to read "Check your internet", which is a guess,
// and usually the wrong one: the page itself had already loaded, so the
// connection was fine. The common causes are the backend redeploying or
// briefly restarting, and telling a contractor on a job site that their
// signal is bad -- when it is not -- sends them to reboot a router instead of
// tapping the button again ten seconds later. The browser already knows which
// case it is, so ask it.
// "3 bid(s)" is how a form letter counts. The app knows the number, so it
// can say the word: plural(1,"bid") -> "1 bid", plural(3,"bid") -> "3 bids".
// A file the customer will find in Downloads next week. Date.now() gives
// them "curbcall_bids_1788409740206.csv", which sorts oddly and tells them
// nothing; a plain date says which day the list is from.
function stampedName(base,ext){
  const d=new Date(), p=n=>String(n).padStart(2,"0");
  return `${base}-${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}.${ext}`;
}

function plural(n,word,plural_){
  return n+" "+(Math.abs(n)===1?word:(plural_||word+"s"));
}

function offlineOrServer(){
  return navigator.onLine
    ? "Couldn\u2019t reach the bid service \u2014 it may be restarting. "
      + "Try again in a moment."
    : "You\u2019re offline. Reconnect and try again.";
}

function scanEffortLine(d,seconds){
  const e=d&&d.debug||{};
  const sites=e.sites_read|0, towns=e.towns_read|0,
        posts=e.postings_read|0, examined=e.postings_examined|0;
  if(!sites&&!towns)return "";
  const bits=[];
  if(sites) bits.push(`<b>${sites}</b> agency bid page${sites===1?"":"s"}`);
  if(towns) bits.push(`<b>${towns}</b> town${towns===1?"":"s"}`);
  let line=`Checked ${bits.join(" across ")}`;
  if(posts) line+=`, opened <b>${posts}</b> posting${posts===1?"":"s"}`;
  if(seconds) line+=` in <b>${seconds}s</b>`;
  line+=".";
  if(examined) line+=` <span class="dim">${examined} listing`
    +`${examined===1?"":"s"} read and filtered down to the concrete `
    +`work.</span>`;
  return line;
}
function renderScanEffort(d,seconds){
  const box=document.getElementById("scan-effort");
  if(!box)return;
  const html=scanEffortLine(d,seconds);
  box.innerHTML=html;
  box.style.display=html?"block":"none";
}

const RADIUS_STEPS=[10,25,50,75,125];

// What a scan that found nothing should say.
//
// The old answer was "No open bids posted here right now. Try a wider
// radius." -- one grey line, on the screen the user was already on, telling
// them to go and do something themselves. For a contractor whose first scan
// lands in a quiet county that is the whole product experience, and it reads
// as broken rather than as an honest empty week.
//
// Two changes. Say what was actually searched, because the effort is real
// and completely invisible: a 125-mile scan reads dozens of bid pages across
// dozens of towns. And put the next step under their thumb -- the wider
// radius, or the alert that means they never have to think about it again --
// rather than describing it.
function showEmptyScanResult(d,location,cachedNote){
  const status=document.getElementById("scan-status");
  const where=d.location||location;
  const next=RADIUS_STEPS.find(r=>r>radius);
  // Expectation-setting, not filler. A contractor has no way to know whether
  // an empty result means the app is broken or the month is quiet, and
  // municipal concrete work genuinely is lumpy -- across ten sample scans the
  // median was six open bids and the worst was one. Saying so converts a
  // disappointing result into an honest one, which is the difference between
  // cancelling and checking back.
  renderScanEffort(d,null);
  status.innerHTML=
    `<div>Nothing open near ${esc(where)} right now.${esc(cachedNote)}</div>`
    +`<div class="empty-note">Normal for a quiet week \u2014 most areas post a `
    +`handful of concrete jobs a month, not a steady stream. New work appears `
    +`as agencies publish it.</div>`
    +`<div class="empty-actions">`
    +(next?`<button type="button" class="btn-ghost" id="empty-wider">`
           +`Search ${next} miles instead</button>`:"")
    +`<button type="button" class="btn-ghost" id="empty-alert">`
    +`Alert me when work appears here</button></div>`;
  const wider=document.getElementById("empty-wider");
  if(wider)wider.onclick=()=>{
    // Move the visible selection too, or the radius silently disagrees with
    // the buttons the user is looking at.
    radius=next;
    document.querySelectorAll("#radius-row .radius-btn").forEach(b=>
      b.classList.toggle("active",Number(b.dataset.r)===next));
    runScan(false);
  };
  const alert=document.getElementById("empty-alert");
  if(alert)alert.onclick=()=>{
    const save=document.getElementById("save-search-btn");
    if(save)save.click(); else goTo("account");
  };
}

// What the server is actually doing, in words a contractor would use.
const SCAN_PHASES={
  searching:"Searching for bid pages\u2026",
  reading_towns:"Reading town bid pages\u2026",
  checking_details:"Checking deadlines and contacts\u2026",
  finishing:"Almost done\u2026",
};

// Ask the server where the scan has got to.
//
// The bar beside this is driven by a guess at how long a scan usually takes,
// so it counted up whether anything was happening or not -- a stalled scan
// and a working one looked identical for over a minute, which is what makes
// a tool feel broken when it is fine.
//
// Entirely optional. If this endpoint is missing, slow, or the storage behind
// it is down, the scan is unaffected and the estimate carries on alone.
function watchScanProgress(token,statusEl){
  let stop=false,last="";
  (async()=>{
    while(!stop){
      await new Promise(r=>setTimeout(r,2500));
      if(stop)break;
      try{
        const r=await fetchWithTimeout(SERVER+"/scan/progress",{
          method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({token})},8000);
        if(!r.ok)continue;
        const d=await r.json();
        // The scan can finish while this request is in flight. Writing after
        // that replaced the result -- "Nothing open near you" and its buttons,
        // or an error -- with a stale "Reading town bid pages... 10 so far",
        // leaving a finished scan looking stuck with nothing to show.
        if(stop)break;
        if(!d||!d.known||d.done)continue;
        const words=SCAN_PHASES[d.phase];
        if(!words)continue;
        // The count is the part people care about: it says the scan is
        // finding things, not merely still running.
        const txt=(typeof d.found==="number"&&d.found>0)
          ?`${words} ${d.found} so far`:words;
        if(txt!==last&&statusEl){statusEl.textContent=txt;last=txt;}
      }catch(e){/* the estimate is still running; say nothing */}
    }
  })();
  return ()=>{stop=true;};
}

async function runScan(force){
  if(scanBtn.disabled)return; // guard against double-tap / double-submit
  if(!requireOnline("Scanning for bids"))return;
  const location=document.getElementById("loc-input").value.trim();
  if(!location){toast("Enter a ZIP or city first");return;}
  const startedAt=Date.now();
  scanBtn.disabled=true;
  scanBtn.innerHTML='<span class="spin"></span> Scanning...';
  const status=document.getElementById("scan-status");
  status.textContent=`Scanning ${location} \u00b7 ${radius} mi...`;
  // One controller for both the cancel button and the safety timeout, so a
  // cancelled scan and a timed-out one land in the same place.
  const ctrl=new AbortController();
  let cancelled=false;
  const prog=startScanProgress("progress-wrap","progress-bar",{
    onCancel(){cancelled=true;ctrl.abort();},
    onOverrun(){
      status.textContent="Still scanning \u2014 wide radiuses take longer, "
        +"and the server may have been asleep.";
    },
  });
  // Generous timeout \u2014 real scans take a while and the backend can be
  // asleep (Render free tier), but this still guarantees the UI never hangs
  // forever if the server is truly unreachable.
  const killer=setTimeout(()=>ctrl.abort(),150000);
  // Minted here, sent with the scan, and polled alongside it.
  const progressToken="s"+Math.random().toString(36).slice(2)
    +Date.now().toString(36);
  const stopWatching=watchScanProgress(progressToken,status);
  async function post(signal,forceThis){
    const token=await getSupabaseToken();
    return fetch(SERVER+"/scan",{
      method:"POST",headers:{"Content-Type":"application/json"},signal,
      body:JSON.stringify({key:licenseKey(),device_id:deviceId(),
        supabase_token:token,location,radius,force:!!forceThis,
        progress_token:progressToken})
    });
  }
  try{
    let r;
    try{
      r=await post(ctrl.signal,force);
    }catch(err){
      // A timeout here does NOT mean the scan failed. The server keeps
      // working after the client hangs up, finishes, and caches the result
      // for the day -- a 125-mile scan from Republic, MO ran past 150s and
      // banked 32 bids that the phone never saw, because it had already
      // shown "that took too long".
      //
      // So collect it instead of reporting a failure. The retry is NOT
      // forced, which is the whole point: an unforced scan for the same
      // place on the same day is answered from that cache, so this returns
      // in a moment rather than starting the work again.
      if(cancelled||err.name!=="AbortError")throw err;
      clearTimeout(killer);stopWatching();
      status.textContent="Still running on the server \u2014 collecting your "
        +"result...";
      await new Promise(res=>setTimeout(res,4000));
      const c2=new AbortController();
      const k2=setTimeout(()=>c2.abort(),45000);
      try{ r=await post(c2.signal,false); }
      finally{ clearTimeout(k2); }
    }
    clearTimeout(killer);stopWatching();
    prog.finish();
    if(r.status===403){status.textContent="Your trial or subscription isn't active.";toast("Check Account tab");goTo("account");return;}
    const d=await r.json();
    if(!d.ok){
      const msg=d.reason==="location_not_found"?"Couldn't find that location. Try a ZIP code.":"Scan hit a snag. Try again.";
      status.textContent=msg;toast(msg);return;
    }
    const total=d.total_bids||0;
    const added=mergeOpenBids(d.bids||{},Object.keys(d.city_coords||{}));
    // Unconditional: the empty-result branch below stays on this screen
    // (no goTo("feed")), so without this the Find-screen summary would
    // keep showing last scan's count after one that found nothing new.
    renderScanSummary();
    // Only a real scan calibrates the estimate. A cached result returns in
    // milliseconds and would drag the median down to nothing, so the next
    // real scan would show a bar that finishes instantly and then sits.
    if(!d.cached)recordRunDuration("scan",Date.now()-startedAt);
    recordScanDiagnostics({
      kind:"scan",location:d.location||location,radius,
      ms:Date.now()-startedAt,total,added,cached:!!d.cached,debug:d.debug||null,
    });
    const cachedNote=d.cached?" (today\u2019s saved result \u2014 use Force a fresh scan to re-search)":"";
    // Two sources of truth for one question: total_bids is counted on the
    // server, `added` is what mergeOpenBids actually put in the list. They
    // agree today. If they ever stop agreeing, the failure is a screen saying
    // "nothing open near you" while the Bids tab fills up behind it, which
    // reads as a broken app. Whatever reached the list wins.
    if(total>0||added>0){
      status.textContent=`Done \u2014 ${plural(added,"new open bid")} added near ${d.location||location}.`+cachedNote;
      // The effort belongs on a SUCCESSFUL scan too, not only an empty one.
      // "12 bids" says nothing about what it cost to find them.
      renderScanEffort(d,Math.round((Date.now()-startedAt)/1000));
      toast(added>0?`Added ${plural(added,"new bid")}!`:"No new bids \u2014 already in your list.");
      cityFilter="All";goTo("feed");
    }else{
      // An empty result used to be one grey sentence telling the user to go
      // and change something themselves, on the screen they were already
      // sitting on. That reads as a broken app rather than a quiet week --
      // and it is the FIRST thing a new customer sees if their area is
      // thin, which is exactly when they decide whether this is worth
      // paying for.
      //
      // So: say what was actually searched, and put the next step under
      // their thumb instead of describing it.
      showEmptyScanResult(d,location,cachedNote);
    }
  }catch(e){
    clearTimeout(killer);stopWatching();
    prog.fail();
    if(cancelled){
      status.textContent="Scan cancelled.";
    }else if(e.name==="AbortError"){
      // Both the scan AND the follow-up collection timed out. The result may
      // still land in the server's cache, so say what actually helps rather
      // than "connection problem".
      status.textContent="Still working. The server finishes scans even after "
        +"the app stops waiting \u2014 tap Scan again in a minute and it will "
        +"return today\u2019s saved result straight away.";
      toast("Taking longer than usual");
    }else{
      status.textContent=offlineOrServer();
      toast("Connection problem");
    }
  }finally{
    scanBtn.disabled=false;
    scanBtn.innerHTML="Scan for Bids";
  }
}

// ── Upcoming (planned work, not yet a formal bid) ──
let upcomingData=store.get("upcoming_feed",{});
const upcomingBtn=document.getElementById("upcoming-btn");
upcomingBtn.onclick=runUpcoming;
byId("up-loc-input").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();runUpcoming();}});

// Same hashing as bidId, and the same reason: matches app.py's upcoming_id
// and stops two projects with long, similar titles deduping into one.
function upcomingId(city,b){return md5(String(city)+(b.title||"")+(b.scope||"")).slice(0,12);}

async function runUpcoming(){
  if(upcomingBtn.disabled)return; // guard against double-tap / double-submit
  if(!requireOnline("Finding upcoming projects"))return;
  const location=document.getElementById("up-loc-input").value.trim();
  if(!location){toast("Enter a ZIP or city first");return;}
  upcomingBtn.disabled=true;
  upcomingBtn.innerHTML='<span class="spin"></span> Searching...';
  const status=document.getElementById("upcoming-status");
  status.textContent=`Scanning agendas, budgets & plans near ${location}...`;
  const ctrl=new AbortController();
  let cancelled=false;
  const prog=startScanProgress("up-progress-wrap","up-progress-bar",{
    expected:expectedRunMs("upcoming"),
    onCancel(){cancelled=true;ctrl.abort();},
    onOverrun(){status.textContent="Still searching \u2014 agendas and budget documents are slow to read.";},
  });
  const killer=setTimeout(()=>ctrl.abort(),150000);
  try{
    const token=await getSupabaseToken();
    const r=await fetch(SERVER+"/upcoming",{
      method:"POST",headers:{"Content-Type":"application/json"},
      signal:ctrl.signal,
      body:JSON.stringify({key:licenseKey(),device_id:deviceId(),supabase_token:token,location,radius:upRadius})
    });
    clearTimeout(killer);
    prog.finish();
    if(r.status===403){status.textContent="Your trial or subscription isn't active.";toast("Check Account tab");goTo("account");return;}
    const d=await r.json();
    if(!d.ok){
      const msg=d.reason==="location_not_found"?"Couldn't find that location. Try a ZIP code."
        :d.reason==="ai_unavailable"?"Upcoming search isn't configured yet.":"Search hit a snag. Try again.";
      status.textContent=msg;toast(msg);return;
    }
    let added=0;
    for(const city in(d.items||{})){
      const have=upcomingData[city]||[];
      const ids=new Set(have.map(b=>upcomingId(city,b)));
      (d.items[city]||[]).forEach(b=>{const id=upcomingId(city,b);if(!ids.has(id)){have.push(b);ids.add(id);added++;}});
      upcomingData[city]=have;
    }
    recordRunDuration("upcoming",prog.elapsed());
    store.set("upcoming_feed",upcomingData);queueFeedPush();
    // Count what is actually on the tab, not what the server said it found.
    // The same split as the scan path: d.total is the server's number and the
    // list is built from d.items, so a shape mismatch between them shows
    // "found 2 planned projects" above a panel reading "Nothing yet". Whatever
    // the customer can see is what the sentence describes.
    const onTab=Object.values(upcomingData||{}).reduce((n,v)=>n+(v||[]).length,0);
    status.textContent=onTab>0
      ?`Done — found ${plural(onTab,"planned project")} near ${d.location||location}.`
      :"Nothing planned turned up yet. Try a wider radius.";
    if(added>0)toast(`Added ${plural(added,"planned project")}!`);
    renderUpcoming();
  }catch(e){
    clearTimeout(killer);
    prog.fail();
    if(cancelled){
      status.textContent="Search cancelled.";
    }else{
      status.textContent=e.name==="AbortError"
        ?"That took too long — the server might be waking up. Try again in a moment."
        :offlineOrServer();
      toast("Connection problem");
    }
  }finally{
    upcomingBtn.disabled=false;
    upcomingBtn.innerHTML="Find Upcoming Projects";
  }
}

function upcomingCard(city,b){
  return `<div class="bid planned">
    <div class="bid-bar"></div>
    <div class="bid-body">
      <div class="bid-top"><span class="bid-title">${esc(b.title||"Untitled Project")}</span></div>
      ${b.scope?`<div class="bid-scope">${esc(b.scope)}</div>`:""}
      <div class="bid-meta">
        <span class="chip">${esc(city)}</span>
        ${b.timeframe?`<span class="chip">${esc(b.timeframe)}</span>`:""}
        <span class="chip">PLANNED</span>
      </div>
      ${b.contact||b.email||b.phone?`<div class="bid-meta" style="margin-top:0.4rem;">
        ${b.contact?`<span class="chip">${esc(b.contact)}</span>`:""}
        ${safeUrl("mailto:"+b.email)?`<a class="chip" href="${esc(safeUrl("mailto:"+b.email))}">Email</a>`:""}
        ${safeUrl("tel:"+b.phone)?`<a class="chip" href="${esc(safeUrl("tel:"+b.phone))}">${esc(b.phone)}</a>`:""}
      </div>`:""}
      ${safeUrl(b.url)?`<div class="bid-meta" style="margin-top:0.4rem;"><a class="chip" href="${esc(safeUrl(b.url))}" target="_blank" rel="noopener noreferrer">Source</a></div>`:""}
    </div>
  </div>`;
}

function renderUpcoming(){
  const list=document.getElementById("upcoming-list");
  const filterRow=document.getElementById("up-filter-row");
  const cities=Object.keys(upcomingData).filter(c=>(upcomingData[c]||[]).length);
  if(!cities.length){
    filterRow.style.display="none";
    document.getElementById("up-toolbar").style.display="none";
    list.innerHTML=emptyHTML("i-radar","Nothing yet","Search your area to spot planned work before it's a bid.");
    return;
  }
  filterRow.style.display="flex";
  document.getElementById("up-toolbar").style.display="flex";
  let rows=[];
  cities.sort().forEach(c=>upcomingData[c].forEach(b=>rows.push([c,b])));
  const term=(document.getElementById("up-search").value||"").trim().toLowerCase();
  if(term)rows=rows.filter(([c,b])=>(b.title||"").toLowerCase().includes(term)||(b.scope||"").toLowerCase().includes(term)||c.toLowerCase().includes(term));
  // Sort and export, matching Bids and Leads. Upcoming had neither, so a
  // long list could only be read in whatever order the scan returned it and
  // could not leave the app at all.
  const mode=document.getElementById("up-sort").value;
  if(mode==="city")rows.sort((a,b)=>a[0].localeCompare(b[0]));
  else if(mode==="title")rows.sort((a,b)=>String(a[1].title||"").localeCompare(String(b[1].title||"")));
  lastUpcomingRows=rows;
  list.innerHTML=`<div class="feed-label">PLANNED PROJECTS</div>`+rows.map(([c,b])=>upcomingCard(c,b)).join("");
  if(!rows.length)list.innerHTML=emptyHTML("i-search","No matches","Try a different search term.");
}
let lastUpcomingRows=[];
byId("up-sort").onchange=()=>renderUpcoming();
byId("export-upcoming-btn").onclick=()=>
  exportCSV(lastUpcomingRows,"curbcall_upcoming");
byId("up-search").oninput=()=>renderUpcoming();

// ── Residential Leads (driveway/sidewalk permits -- a live lead to pitch,
// not a formal bid to respond to: no deadline, no RFP process, often
// already has a builder/GC of record who may need a concrete sub) ──
let leadsData=store.get("leads_feed",{});
// Lead outreach status -- local-only for now (no Supabase table for leads
// yet, unlike saved_bids/saved_searches), keyed by permit_id so it survives
// leadsData being re-merged/re-fetched.
let leadStatus=store.get("lead_status",{});
function setLeadStatus(permitId,status){
  if(status)leadStatus[permitId]=status;else delete leadStatus[permitId];
  store.set("lead_status",leadStatus);queueFeedPush();
  renderLeads();
}
const leadsBtn=document.getElementById("leads-btn");
leadsBtn.onclick=runLeads;
byId("leads-loc-input").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();runLeads();}});

async function runLeads(){
  if(leadsBtn.disabled)return; // guard against double-tap / double-submit
  if(!requireOnline("Finding residential leads"))return;
  const location=document.getElementById("leads-loc-input").value.trim();
  if(!location){toast("Enter a ZIP or city first");return;}
  leadsBtn.disabled=true;
  leadsBtn.innerHTML='<span class="spin"></span> Searching...';
  const status=document.getElementById("leads-status");
  status.textContent=`Checking city permit records near ${location}...`;
  const ctrl=new AbortController();
  let cancelled=false;
  const prog=startScanProgress("leads-progress-wrap","leads-progress-bar",{
    expected:expectedRunMs("leads"),
    onCancel(){cancelled=true;ctrl.abort();},
    onOverrun(){status.textContent="Still checking permit records...";},
  });
  const killer=setTimeout(()=>ctrl.abort(),60000);
  try{
    const token=await getSupabaseToken();
    const r=await fetch(SERVER+"/residential-leads",{
      method:"POST",headers:{"Content-Type":"application/json"},
      signal:ctrl.signal,
      body:JSON.stringify({key:licenseKey(),device_id:deviceId(),supabase_token:token,location,radius:leadsRadius})
    });
    clearTimeout(killer);
    prog.finish();
    if(r.status===403){status.textContent="Your trial or subscription isn't active.";toast("Check Account tab");goTo("account");return;}
    const d=await r.json();
    if(!d.ok){
      const msg=d.reason==="location_not_found"?"Couldn't find that location. Try a ZIP code.":"Search hit a snag. Try again.";
      status.textContent=msg;toast(msg);return;
    }
    if(!d.covered){
      status.textContent=`Residential leads aren't set up for ${d.location||location} yet — coverage is growing city by city. (Currently: Austin, TX; Cambridge, MA & Baton Rouge, LA.)`;
      return;
    }
    const city=d.location||location;
    const have=leadsData[city]||[];
    const ids=new Set(have.map(l=>l.permit_id));
    let added=0;
    (d.leads||[]).forEach(l=>{if(!ids.has(l.permit_id)){have.push(l);ids.add(l.permit_id);added++;}});
    leadsData[city]=have;
    recordRunDuration("leads",prog.elapsed());
    store.set("leads_feed",leadsData);queueFeedPush();
    // Same rule as Find and Upcoming: count what is on the tab, not what the
    // server reported. This flow is behind LEADS_ENABLED today; leaving the
    // one inconsistent copy behind is how it comes back when that flips.
    const onTab=Object.values(leadsData||{}).reduce((n,v)=>n+(v||[]).length,0);
    status.textContent=onTab>0
      ?`Done — found ${plural(onTab,"residential lead")} near ${d.location||location}.`
      :"No recent driveway/sidewalk permits in this area. Try a wider radius.";
    if(added>0)toast(`Added ${plural(added,"new lead")}!`);
    renderLeads();
  }catch(e){
    clearTimeout(killer);
    prog.fail();
    if(cancelled){
      status.textContent="Search cancelled.";
    }else{
      status.textContent=e.name==="AbortError"
        ?"That took too long — the server might be waking up. Try again in a moment."
        :offlineOrServer();
      toast("Connection problem");
    }
  }finally{
    leadsBtn.disabled=false;
    leadsBtn.innerHTML="Find Residential Leads";
  }
}

const LEAD_TYPE_STYLE={
  open:{bg:"#0f3d24",fg:"var(--green)",label:"Open Lead"},
  builder:{bg:"#0c2a4a",fg:"var(--blue)",label:"Builder's Job"},
  taken:{bg:"#2a2a2a",fg:"var(--text3)",label:"Sub Already Listed"},
  unknown:{bg:"#2a2a2a",fg:"var(--text2)",label:"Contact Listed"},
};
const LEAD_STATUSES=[["called","Called"],["quoted","Quoted"],["won","Won"],["lost","Lost"]];
function leadCard(city,l){
  const t=LEAD_TYPE_STYLE[l.lead_type]||LEAD_TYPE_STYLE.unknown;
  const pid=l.permit_id||"";
  const curStatus=leadStatus[pid]||"";
  return `<div class="bid planned">
    <div class="bid-bar"></div>
    <div class="bid-body">
      <div class="bid-top"><span class="bid-title">${esc(l.address||"Address unknown")}</span></div>
      ${l.description?`<div class="bid-scope">${esc(l.description)}</div>`:""}
      <div class="bid-meta">
        <span class="chip" style="background:${t.bg};color:${t.fg};" title="${esc(l.lead_type_label||'')}">${t.label}</span>
        <span class="chip">${esc(city)}</span>
        ${l.issued_date?`<span class="chip">Permitted ${esc(l.issued_date)}</span>`:""}
        <span class="chip">${esc(l.permit_type||"Residential Permit")}</span>
      </div>
      ${l.contractor_name||l.contractor_phone?`<div class="bid-meta" style="margin-top:0.4rem;">
        ${l.contractor_name?`<span class="chip">${esc(l.contractor_name)}${l.contractor_trade?` (${esc(l.contractor_trade)})`:""}</span>`:""}
        ${safeUrl("tel:"+l.contractor_phone)?`<a class="chip" href="${esc(safeUrl("tel:"+l.contractor_phone))}">${esc(l.contractor_phone)}</a>`:""}
      </div>`:""}
      ${safeUrl(l.url)?`<div class="bid-meta" style="margin-top:0.4rem;"><a class="chip" href="${esc(safeUrl(l.url))}" target="_blank" rel="noopener noreferrer">Permit Record</a></div>`:""}
      ${l.lead_type==="builder"?`<div style="background:var(--surface);color:var(--text2);font-size:var(--fs-sm);line-height:1.4;padding:0.5rem 0.6rem;border-radius:8px;margin-top:0.5rem;">ℹ️ A general contractor already holds this project. Worth a call only if you already have a relationship with them as a subcontractor.</div>`:""}
      ${pid?`<div class="lead-pipeline-row">
        ${LEAD_STATUSES.map(([k,label])=>`<button class="pchip ${curStatus===k?"active-"+k:""}" data-lead-id="${esc(pid)}" data-lead-status="${esc(curStatus===k?"":k)}">${label}</button>`).join("")}
      </div>`:""}
    </div>
  </div>`;
}

let leadsStatusFilter="All";
function renderLeads(){
  const list=document.getElementById("leads-list");
  const filterRow=document.getElementById("leads-filter-row");
  const toolbar=document.getElementById("leads-toolbar");
  const tilesWrap=document.getElementById("leads-tiles");
  const cities=Object.keys(leadsData).filter(c=>(leadsData[c]||[]).length);
  if(!cities.length){
    filterRow.style.display="none";
    toolbar.style.display="none";
    tilesWrap.innerHTML="";
    list.innerHTML=emptyHTML("i-home","Nothing yet","Search your area for fresh driveway &amp; sidewalk permits. Coverage is growing city by city — currently: Austin, TX; Cambridge, MA &amp; Baton Rouge, LA.");
    return;
  }
  filterRow.style.display="flex";
  toolbar.style.display="flex";
  let all=[];
  cities.sort().forEach(c=>leadsData[c].forEach(l=>all.push([c,l])));

  // Status filter tiles -- same pattern as Active Bids' pipeline tiles.
  const counts={All:all.length,none:0,called:0,quoted:0,won:0,lost:0};
  all.forEach(([c,l])=>{const s=leadStatus[l.permit_id]||"";if(s)counts[s]++;else counts.none++;});
  const tileDefs=[["All","All"],["none","Not Contacted"],...LEAD_STATUSES];
  tilesWrap.innerHTML=`<div class="tiles">`+tileDefs.map(([k,label])=>
    `<div class="tile ${leadsStatusFilter===k?"active":""}" data-k="${k}"><div class="n">${counts[k]||0}</div><div class="l">${label}</div></div>`
  ).join("")+`</div>`;
  tilesWrap.querySelectorAll(".tile").forEach(t=>t.onclick=()=>{leadsStatusFilter=t.dataset.k;renderLeads();});

  let rows=all.filter(([c,l])=>{
    const s=leadStatus[l.permit_id]||"";
    return leadsStatusFilter==="All"||(leadsStatusFilter==="none"?!s:s===leadsStatusFilter);
  });
  const term=(document.getElementById("leads-search").value||"").trim().toLowerCase();
  if(term)rows=rows.filter(([c,l])=>(l.address||"").toLowerCase().includes(term)||(l.description||"").toLowerCase().includes(term)||(l.contractor_name||"").toLowerCase().includes(term)||c.toLowerCase().includes(term));
  const sortMode=document.getElementById("leads-sort").value;
  if(sortMode==="new"){
    rows.sort((a,b)=>(b[1].issued_date||"").localeCompare(a[1].issued_date||""));
  }
  // sortMode==="match" (default): leave rows in the order /residential-leads
  // returned them -- already sorted open-lead-first, freshest within type.
  lastLeadsRows=rows;
  list.innerHTML=`<div class="feed-label">RESIDENTIAL LEADS</div>`+rows.map(([c,l])=>leadCard(c,l)).join("");
  if(!rows.length)list.innerHTML=emptyHTML("i-search","No matches","Try a different search term.");
  // Real listeners, not interpolated onclick — a permit id containing a quote
  // would otherwise break the status buttons on that lead.
  list.querySelectorAll("[data-lead-id]").forEach(btn=>{
    btn.onclick=()=>setLeadStatus(btn.dataset.leadId,btn.dataset.leadStatus);
  });
}
byId("leads-search").oninput=()=>renderLeads();
byId("leads-sort").onchange=()=>renderLeads();

let lastLeadsRows=[];
function exportLeadsCSV(rows){
  if(!rows||!rows.length){toast("Nothing to export yet");return;}
  const header=["City","Address","Permit Type","Description","Issued Date","Lead Type","Builder/Contact","Trade","Phone","Permit Record","Status"];
  const lines=[header.map(csvEscape).join(",")];
  rows.forEach(([city,l])=>{
    lines.push([city,l.address,l.permit_type,l.description,l.issued_date,l.lead_type_label||l.lead_type,
                l.contractor_name,l.contractor_trade,l.contractor_phone,l.url,leadStatus[l.permit_id]||""].map(csvEscape).join(","));
  });
  const blob=new Blob([lines.join("\n")],{type:"text/csv"});
  const url=URL.createObjectURL(blob);
  const a=document.createElement("a");
  a.href=url;a.download=stampedName("curbcall-leads","csv");document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
byId("export-leads-btn").onclick=()=>exportLeadsCSV(lastLeadsRows);

// ── Bid helpers ──
// MD5, byte-for-byte compatible with Python's hashlib.md5, because the bid id
// below has to match the desktop app's exactly (app.py: bid_id / upcoming_id).
// SubtleCrypto can't help here — it has no MD5, and it's async, while ids are
// needed synchronously all through rendering.
function md5(input){
  const bytes=Array.from(new TextEncoder().encode(input));
  const bits=bytes.length*8;
  bytes.push(0x80);
  while(bytes.length%64!==56)bytes.push(0);
  const lo=bits>>>0,hi=Math.floor(bits/4294967296)>>>0;
  bytes.push(lo&255,(lo>>>8)&255,(lo>>>16)&255,(lo>>>24)&255,
             hi&255,(hi>>>8)&255,(hi>>>16)&255,(hi>>>24)&255);
  const S=[7,12,17,22,7,12,17,22,7,12,17,22,7,12,17,22,
           5,9,14,20,5,9,14,20,5,9,14,20,5,9,14,20,
           4,11,16,23,4,11,16,23,4,11,16,23,4,11,16,23,
           6,10,15,21,6,10,15,21,6,10,15,21,6,10,15,21];
  const K=[];
  for(let i=0;i<64;i++)K[i]=Math.floor(Math.abs(Math.sin(i+1))*4294967296)>>>0;
  let a0=0x67452301,b0=0xefcdab89,c0=0x98badcfe,d0=0x10325476;
  const rotl=(x,c)=>((x<<c)|(x>>>(32-c)))>>>0;
  for(let off=0;off<bytes.length;off+=64){
    const M=[];
    for(let j=0;j<16;j++){
      M[j]=((bytes[off+j*4])|(bytes[off+j*4+1]<<8)|
            (bytes[off+j*4+2]<<16)|(bytes[off+j*4+3]<<24))>>>0;
    }
    let A=a0,B=b0,C=c0,D=d0;
    for(let i=0;i<64;i++){
      let F,g;
      if(i<16){F=(B&C)|(~B&D);g=i;}
      else if(i<32){F=(D&B)|(~D&C);g=(5*i+1)%16;}
      else if(i<48){F=B^C^D;g=(3*i+5)%16;}
      else{F=C^(B|~D);g=(7*i)%16;}
      F=(F+A+K[i]+M[g])>>>0;
      A=D;D=C;C=B;
      B=(B+rotl(F,S[i]))>>>0;
    }
    a0=(a0+A)>>>0;b0=(b0+B)>>>0;c0=(c0+C)>>>0;d0=(d0+D)>>>0;
  }
  const hex=(n)=>{let s="";for(let i=0;i<4;i++)s+=((n>>>(i*8))&255).toString(16).padStart(2,"0");return s;};
  return hex(a0)+hex(b0)+hex(c0)+hex(d0);
}

// An id used to be the city + title + scope text with spaces stripped, cut to
// 40 characters. That was wrong twice over:
//   * 40 characters isn't enough to tell bids apart. "…Sidewalk Replacement
//     Project Phase 1" and "Phase 2" produced the SAME id, so starring one
//     starred the other and their notes and pipeline status ran together.
//   * it didn't match the desktop app's id for the same bid, so a bid saved on
//     the desktop synced into saved_bids under one key and the web app looked
//     it up under another — the star never showed as saved across devices and
//     saving again wrote a duplicate row.
// Hashing the same three fields the way app.py does fixes both at once.
function bidId(city,b){return md5(String(city)+(b.title||"")+(b.scope||"")).slice(0,12);}
function legacyBidId(city,b){return(city+(b.title||"")+(b.scope||"")).replace(/\s/g,"").slice(0,40);}
// Pulls a rough dollar figure out of a value string like "$310k" or "$1.2M"
// so it can be sorted/summed. -1 means "couldn't parse / not listed".
function parseValue(v){
  const s=String(v||"").toLowerCase();
  const m=s.replace(/,/g,"").match(/[\d.]+/);
  if(!m)return-1;
  let n=parseFloat(m[0]);
  if(s.includes("m"))n*=1000000;else if(s.includes("k"))n*=1000;
  return n;
}
function formatMoney(n){
  if(n>=1000000)return"$"+(n/1000000).toFixed(n%1000000?1:0)+"M";
  if(n>=1000)return"$"+(n/1000).toFixed(n%1000?1:0)+"k";
  return"$"+Math.round(n);
}

let lastFeedRows=[];
// Open bids in a city, minus anything the user dismissed from the card
// itself -- a rescan replaces the whole city's list (see mergeOpenBids), so
// without this a dismissed bid the server still reports as open would just
// silently come back on the next scan instead of staying gone.
function visibleBidsIn(city){
  return (bidData[city]||[]).filter(b=>isOpen(b)&&!dismissed[bidId(city,b)]);
}
// A running total of what scans have already found, shown on the Find
// screen itself -- real state already computed for the Bids tab's own
// header, not invented content. On desktop this also fills the space below
// the map instead of leaving it empty. Called from renderFeed() so it
// never drifts out of sync with the one place bidData is already the
// source of truth for a count.
function renderScanSummary(){
  const el=document.getElementById("scan-summary");
  if(!el)return;
  const cities=Object.keys(bidData).filter(c=>visibleBidsIn(c).length);
  const total=cities.reduce((n,c)=>n+visibleBidsIn(c).length,0);
  if(!total){el.style.display="none";el.innerHTML="";return;}
  el.style.display="";
  const syncText=lastSyncAt
    ?`Last synced ${lastSyncAt.toLocaleTimeString([],{hour:"numeric",minute:"2-digit"})}`
    :"";
  el.innerHTML=`
    <div class="account-card">
      <div class="account-email hdr-ic" style="font-size:var(--fs-base);margin-bottom:0.6rem;"><svg class="icon-svg"><use href="#i-list"/></svg>Your Bids So Far</div>
      <div class="stats-grid">
        <div class="stat-box"><div class="n">${total}</div><div class="l">Open Bids</div></div>
        <div class="stat-box"><div class="n">${cities.length}</div><div class="l">${cities.length===1?"Town":"Towns"}</div></div>
      </div>
      ${syncText?`<div class="account-status" style="margin-top:0.6rem;">${esc(syncText)}</div>`:""}
      <button class="btn-ghost" id="scan-summary-view-btn" style="margin-top:0.8rem;">View All Bids</button>
    </div>`;
  document.getElementById("scan-summary-view-btn").onclick=()=>goTo("feed");
}

// ── Going rates (state DOT bid prices) ──
// Every number here is one a state transportation department published,
// built into rates/<st>.json by tools/build_state_prices.py. States publish
// differently, and the app says which kind it is showing:
//   all_bids  every bid received (MO, OR): average with its low-high range,
//             since one 2025 Missouri sidewalk line ran $7 to $1,269;
//   awarded   winning prices only (FL, MN, OK): no range was published, so
//             none is shown.
// Where a state's records name the winning bid (OR, and MO's bid tabs) the
// winning range is shown too -- the number a contractor pricing to win needs.
const PRICE_DISTRICT_KEY="price_district";
const RATE_THIN_BIDS=5;
const AGENCY={MO:"MoDOT",FL:"FDOT",OR:"ODOT",MN:"MnDOT",OK:"ODOT"};
let rateIndex=null,rateIndexLoading=null;
const rateData={},rateLoading={},rateRerenderQueued={};
function agencyOf(d){return (d&&AGENCY[d.state])||`${(d&&d.state_name)||"State"} DOT`;}
function loadRateIndex(){
  if(rateIndex)return Promise.resolve(rateIndex);
  if(!rateIndexLoading){
    rateIndexLoading=fetch("/rates/index.json")
      .then(r=>r.ok?r.json():null)
      .then(d=>{rateIndex=d&&d.states?d.states:null;if(!rateIndex)rateIndexLoading=null;return rateIndex;})
      .catch(()=>{rateIndexLoading=null;return null;});
  }
  return rateIndexLoading;
}
function loadRates(st){
  st=String(st||"").toUpperCase();
  if(!/^[A-Z]{2}$/.test(st))return Promise.resolve(null);
  if(rateData[st])return Promise.resolve(rateData[st]);
  if(!rateLoading[st]){
    rateLoading[st]=loadRateIndex()
      .then(ix=>ix&&ix[st]?fetch(`/rates/${st.toLowerCase()}.json`).then(r=>r.ok?r.json():null):null)
      .then(d=>{if(d&&d.prices)rateData[st]=d;return rateData[st]||null;})
      // A network failure may be retried later; a state with no rates is not.
      .catch(()=>{delete rateLoading[st];return null;});
  }
  return rateLoading[st];
}
// The rates for a state if they're already here. If they aren't, they're
// fetched and the feed redrawn once, so a bid card never waits on them.
function ratesNow(st){
  st=String(st||"").toUpperCase();
  if(rateData[st])return rateData[st];
  if(/^[A-Z]{2}$/.test(st)&&!rateRerenderQueued[st]){
    rateRerenderQueued[st]=true;
    loadRates(st).then(d=>{if(d)renderFeed();});
  }
  return null;
}
// Two-letter state of a bid: its own field, else the town's "City, ST".
function bidState(city,b){
  const own=String((b&&b.state)||"").trim().toUpperCase();
  if(/^[A-Z]{2}$/.test(own))return own;
  const m=/,\s*([A-Za-z]{2})\b/.exec(String(city||""));
  return m?m[1].toUpperCase():"";
}
// The user's own state: their home location, else where most of their bids are.
function homeState(){
  const m=/,\s*([A-Za-z]{2})\b/.exec(String(store.get(HOME_LOC_KEY,"")||""));
  if(m)return m[1].toUpperCase();
  const n={};
  Object.keys(bidData).forEach(c=>{const s=bidState(c,null);if(s)n[s]=(n[s]||0)+(bidData[c]||[]).length;});
  return Object.keys(n).sort((a,b)=>n[b]-n[a])[0]||"";
}
function districtKey(d){return d.state==="MO"?PRICE_DISTRICT_KEY:`${PRICE_DISTRICT_KEY}_${d.state}`;}
function priceDistrict(d){
  const k=store.get(districtKey(d),"STATEWIDE");
  return d&&d.districts&&d.districts[k]?k:"STATEWIDE";
}
// The newest year this item has a price for in this district.
function latestRate(d,item,district){
  const byYear=d.prices[item]&&d.prices[item][district];
  if(!byYear)return null;
  const year=Object.keys(byYear).sort().pop();
  const [avg,low,high,bids,qty]=byYear[year];
  return{year,avg,low,high,bids,qty};
}
// The newest year of winning bids for this item, from the rates file or the
// state's bid results. null when the state's records don't say who won.
function latestWin(d,item,district){
  const res=bidResults[d.state];
  const byYear=(d.wins&&d.wins[item]&&d.wins[item][district])
    ||(res&&res.wins&&res.wins[item]&&res.wins[item][district]);
  if(!byYear)return null;
  const year=Object.keys(byYear).sort().pop();
  const [avg,low,high,n,p25,p75]=byYear[year];
  return{year,avg,low,high,n,p25:p25??low,p75:p75??high};
}
function fmtRate(n){
  return "$"+(n>=1000?Math.round(n).toLocaleString():n.toFixed(2));
}
function rateRow(d,item,district,withTrend){
  const r=latestRate(d,item,district);
  if(!r)return"";
  const meta=d.items[item];
  // Non-breaking, so "sq yd" never splits across two lines.
  const unit=meta.unit.replace(/ /g," ");
  const thin=r.bids<RATE_THIN_BIDS;
  const win=d.basis==="awarded"?null:latestWin(d,item,district);
  const years=withTrend?Object.keys(d.prices[item][district]).sort():[];
  const spread=r.low!=null&&r.high!=null
    ?`${fmtRate(r.low)}–${fmtRate(r.high)} · ${plural(r.bids,"bid")}`
    :`winning bids · ${plural(r.bids,"contract")}`;
  return`<div class="rate-row">
    <div class="rate-main">
      <div class="rate-name">${esc(meta.name)}</div>
      <div class="rate-sub">${spread}
        · typical job ~${Math.round(r.qty).toLocaleString()} ${esc(unit)}${thin?` <span class="rate-thin">few bids — rough guide</span>`:""}</div>
      ${win?`<div class="rate-sub rate-win">Winning bids ${win.n>1?`${fmtRate(win.low)}–${fmtRate(win.high)}, avg ${fmtRate(win.avg)}`:fmtRate(win.avg)} (${plural(win.n,"job")}, ${esc(win.year)})</div>`:""}
      ${years.length>1?`<div class="rate-trend">${years.map(y=>`<span>${esc(y)} <b>${fmtRate(d.prices[item][district][y][0])}</b></span>`).join("")}</div>`:""}
    </div>
    <div class="rate-val">${fmtRate(r.avg)}<span>/${esc(unit)}</span></div>
  </div>`;
}
function districtSelect(d,id){
  const cur=priceDistrict(d);
  const keys=Object.keys(d.districts);
  if(keys.length<2)return"";
  return`<select class="rate-district" id="${id}" aria-label="${esc(agencyOf(d))} district">${
    keys.map(k=>`<option value="${esc(k)}"${k===cur?" selected":""}>${esc(d.districts[k])}</option>`).join("")}</select>`;
}
function wireDistrictSelect(d,id,rerender){
  const sel=document.getElementById(id);
  if(sel)sel.onchange=()=>{store.set(districtKey(d),sel.value);rerender();};
}
function ratePeriod(d,year){
  return (d.periods&&(d.periods[String(year)]||d.periods.note))||String(year);
}
function rateFootnote(d,district){
  const y=Math.max(...d.years);
  const where=district==="STATEWIDE"?` across ${d.state_name}`:" in this district";
  const what=d.basis==="awarded"
    ?`Average winning prices on ${esc(agencyOf(d))} contracts${where}, ${esc(ratePeriod(d,y))}.`
    :`Averages of every bid ${esc(agencyOf(d))} received in ${y} on state highway jobs${where}.`;
  return`<div class="rate-note">${what} City jobs can run different. Use it as a benchmark, not a quote.
    <a href="${esc(safeUrl(d.source_url)||"")}" target="_blank" rel="noopener noreferrer">Source: ${esc(d.source)}</a></div>`;
}
async function renderHomeRates(){
  const card=document.getElementById("home-rates");
  if(!card)return;
  const d=await loadRates(homeState());
  if(!d){card.style.display="none";return;}
  const district=priceDistrict(d);
  card.innerHTML=`<div class="account-email hdr-ic" style="font-size:var(--fs-base);margin-bottom:0.5rem;"><svg class="icon-svg"><use href="#i-activity"/></svg>Going rates in ${esc(d.state_name)}</div>
    ${districtSelect(d,"home-rate-district")}
    ${d.headline.map(i=>rateRow(d,i,district,false)).join("")}
    <button class="btn-ghost" id="home-rates-all" style="margin-top:0.6rem;">All ${Object.keys(d.items).length} items${d.years.length>1?` + ${d.years.length}-year trend`:""}</button>
    ${rateFootnote(d,district)}`;
  card.style.display="";
  wireDistrictSelect(d,"home-rate-district",renderHomeRates);
  document.getElementById("home-rates-all").onclick=()=>openRates(d.state);
}
let ratesSheetState="";
async function openRates(st){
  const mc=document.getElementById("modal-content");
  st=st||ratesSheetState||homeState();
  const ix=await loadRateIndex();
  if(!ix||!ix[st])st=ix?(Object.keys(ix).includes("MO")?"MO":Object.keys(ix)[0]):st;
  const d=await loadRates(st);
  if(!d){toast("Couldn't load prices right now");return;}
  ratesSheetState=d.state;
  await loadBidResults(d.state);
  const district=priceDistrict(d);
  // Most-bid items first: the ones with the most behind their number.
  const items=Object.keys(d.items)
    .map(i=>[i,latestRate(d,i,district)]).filter(([,r])=>r)
    .sort((a,b)=>b[1].bids-a[1].bids).map(([i])=>i);
  const states=Object.keys(ix||{}).sort();
  mc.innerHTML=`<div class="sheet-head"><h2>Going rates</h2>
      <div class="sheet-sub"><span class="chip">${esc(agencyOf(d))} ${d.basis==="awarded"?"winning":"bid"} prices, ${
        d.years.length>1?`${Math.min(...d.years)}–${Math.max(...d.years)}`:esc(ratePeriod(d,d.years[0]))}</span></div></div>
    ${states.length>1?`<select class="rate-district" id="rates-state" aria-label="State">${states.map(s=>
      `<option value="${esc(s)}"${s===d.state?" selected":""}>${esc(ix[s].name)}</option>`).join("")}</select>`:""}
    ${districtSelect(d,"rates-district")}
    ${yourBidsVsState(d,district)}
    ${lossGapHTML()}
    ${items.length?items.map(i=>rateRow(d,i,district,true)).join("")
      :`<div class="account-status">No prices for this district.</div>`}
    ${rateFootnote(d,district)}
    <div class="modal-actions"><button class="ma-ghost" onclick="closeModal();">Close</button></div>`;
  wireDistrictSelect(d,"rates-district",()=>{openRates(d.state);renderHomeRates();});
  const ss=document.getElementById("rates-state");
  if(ss)ss.onchange=()=>openRates(ss.value);
  document.getElementById("modal-back").classList.add("open");
  document.getElementById("modal").classList.add("open");
}
// Which kinds of work a bid is about, from its own words. Each rule's words
// are used up once matched, so "curb and gutter" doesn't also count as a
// bare curb job and a gutter job, and "curb ramp" isn't a curb job. (Done by
// removing the matched text rather than with lookbehind, which older iOS
// Safari can't parse -- one such regex would take the whole app down there.)
// Categories, not item numbers: each state's file says which of its items
// is the plain sidewalk, curb and gutter, and so on.
const RATE_MATCH=[
  [/curb\s*ramps?|ramps?|\bada\b|curb\s*cuts?|truncated\s*domes?|detectable\s*warnings?/gi,["ramp","domes"]],
  [/sidewalks?|walkways?|pathways?/gi,["sidewalk"]],
  [/curb\s*(?:and|&|\/)\s*gutters?/gi,["curb_gutter"]],
  [/\bcurbs?\b|\bcurbing\b/gi,["curb"]],
  // Not "entrance": "4 ADA ramps near the city hall entrance" is a ramp job.
  [/driveways?|drive\s*approach(?:es)?|paved\s*approach(?:es)?/gi,["driveway"]],
  [/gutters?/gi,["gutter"]],
  [/medians?/gi,["median"]],
];
function ratesForBid(b,d){
  let text=` ${(b&&b.title)||""} ${(b&&b.scope)||""} `;
  const out=[];
  for(const [re,cats] of RATE_MATCH){
    re.lastIndex=0;
    if(!re.test(text))continue;
    cats.forEach(c=>{const i=d.cats[c];if(i&&d.items[i]&&!out.includes(i))out.push(i);});
    re.lastIndex=0;
    text=text.replace(re," ");
  }
  return out.slice(0,4);
}
// ── Ballpark estimate ──
// Quantity the bid states x the state's going rate for that item. Only what
// can be priced honestly is priced:
//   - quantities are read from the bid's own words, never guessed;
//   - sidewalk given only in feet needs a width, which is assumed (5 ft by
//     default), labelled, and changeable in the detail view;
//   - ramps given only as a count are listed as not counted unless the
//     state prices ramps each: an area-priced ramp varies too much to assume;
//   - a quantity repeated in title and scope is counted once.
const SIDEWALK_WIDTH_KEY="sidewalk_width_ft";
const DEFAULT_SIDEWALK_WIDTH=5;
const QTY_ITEMS=[
  [/truncated\s*domes?|detectable\s*warnings?/i,"domes"],
  [/curb\s*ramps?|ramps?|curb\s*cuts?/i,"ramp"],
  [/side\s*walks?|walkways?|pathways?/i,"sidewalk"],
  [/curb\s*(?:and|&|\/)\s*gutters?/i,"curb_gutter"],
  [/\bcurbs?\b|\bcurbing\b/i,"curb"],
  [/driveways?|drive\s*approach(?:es)?|paved\s*approach(?:es)?/i,"driveway"],
  [/gutters?/i,"gutter"],
  [/medians?/i,"median"],
];
// Most specific unit first: "sq ft" contains "ft".
const QTY_RE=new RegExp(
  String.raw`(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?:`+
  String.raw`(s\.?\s?y\.?(?![a-z])|sq(?:uare|\.)?\s*(?:yds?|yards?)\.?(?![a-z]))|`+
  String.raw`(s\.?\s?f\.?(?![a-z])|sq(?:uare|\.)?\s*(?:ft|feet|foot)\.?(?![a-z]))|`+
  String.raw`(l\.?\s?f\.?(?![a-z])|lin(?:ear|\.)?\s*(?:ft|feet|foot)\.?(?![a-z])|feet(?![a-z])|foot(?![a-z])|ft\.?(?![a-z])))`,"gi");
const RAMP_COUNT_RE=/(\d{1,3}(?:,\d{3})*)\s+(?:new\s+|concrete\s+|ada\s+|curb\s+)*(?:ramps?|curb\s*cuts?)\b/gi;
// The kind of work nearest the quantity: the first one after it, or failing
// that the last one before it. "1,200 LF of sidewalk plus 8 ADA ramps" is
// sidewalk, though ramps rank first in QTY_ITEMS. At the same position the
// longer phrase wins, so "curb and gutter" beats "curb".
function nearestItem(s,fromEnd){
  let best=null;
  for(const [re,cat] of QTY_ITEMS){
    const g=new RegExp(re.source,"gi");
    let m;
    while((m=g.exec(s))){
      const pos=fromEnd?s.length-(m.index+m[0].length):m.index;
      if(!best||pos<best.pos||(pos===best.pos&&m[0].length>best.len))
        best={pos,len:m[0].length,cat};
    }
  }
  return best&&best.cat;
}
// {quantities:[{cat, qty, unit:"SY"|"SF"|"LF", raw}], rampCounts:[n]}
function extractQuantities(text){
  const out=[],counts=[],seen=new Set();
  // Clauses: a comma followed by three digits is a thousands separator. Not
  // split at periods: "10,400 S.F. of sidewalk" and "approx. 2500 sq ft"
  // put one inside the very phrase being read.
  const clauses=String(text||"").split(/[;\n]|,(?!\d{3})/);
  for(const clause of clauses){
    QTY_RE.lastIndex=0;
    let m;
    while((m=QTY_RE.exec(clause))){
      // Groups 2-4 are square yards, square feet, linear feet, in that order.
      const unit=m[2]?"SY":m[3]?"SF":"LF";
      const qty=parseFloat(m[1].replace(/,/g,""));
      const after=clause.slice(m.index+m[0].length,m.index+m[0].length+60);
      const before=clause.slice(Math.max(0,m.index-60),m.index);
      const cat=nearestItem(after,false)||nearestItem(before,true);
      if(!cat||!(qty>0))continue;
      const key=`${cat}|${qty}|${unit}`;
      if(seen.has(key))continue;
      seen.add(key);
      out.push({cat,qty,unit,raw:m[0].trim()});
    }
    RAMP_COUNT_RE.lastIndex=0;
    while((m=RAMP_COUNT_RE.exec(clause))){
      const n=parseInt(m[1].replace(/,/g,""),10);
      if(n>0&&!counts.includes(n))counts.push(n);
    }
  }
  // A ramp count only matters if no ramp area was given.
  const rampArea=out.some(q=>q.cat==="ramp");
  return{quantities:out,rampCounts:rampArea?[]:counts};
}
function sidewalkWidth(){
  const w=Number(store.get(SIDEWALK_WIDTH_KEY,DEFAULT_SIDEWALK_WIDTH));
  return w>0&&w<=30?w:DEFAULT_SIDEWALK_WIDTH;
}
// A stated quantity in the unit the state prices the item in, or null.
function toItemUnit(q,itemUnit,width){
  const walkFeet=q.unit==="LF"&&q.cat==="sidewalk";
  if(itemUnit==="sq yd"){
    if(q.unit==="SY")return{n:q.qty};
    if(q.unit==="SF")return{n:q.qty/9};
    if(walkFeet)return{n:q.qty*width/9,assumedWidth:width};
  }else if(itemUnit==="sq ft"){
    if(q.unit==="SF")return{n:q.qty};
    if(q.unit==="SY")return{n:q.qty*9};
    if(walkFeet)return{n:q.qty*width,assumedWidth:width};
  }else if(itemUnit==="ft"){
    if(q.unit==="LF")return{n:q.qty};
  }
  return null;
}
function ballpark(b,d,district,width){
  const {quantities,rampCounts}=extractQuantities(`${(b&&b.title)||""}\n${(b&&b.scope)||""}`);
  const lines=[],skipped=[];
  const label=c=>({ramp:"ADA ramps",domes:"truncated domes",sidewalk:"sidewalk",curb_gutter:"curb and gutter",
    curb:"curb",driveway:"driveway",gutter:"gutter",median:"median"})[c]||c;
  for(const q of quantities){
    const item=d.cats[q.cat],meta=item&&d.items[item];
    const r=meta&&latestRate(d,item,district);
    if(!r){skipped.push(`${q.raw} ${meta?meta.name.toLowerCase():label(q.cat)}: no ${agencyOf(d)} price for ${meta?d.districts[district]:"that item"}`);continue;}
    const conv=toItemUnit(q,meta.unit,width);
    if(!conv){skipped.push(`${q.raw} ${meta.name.toLowerCase()}: can't convert to ${meta.unit}`);continue;}
    lines.push({item,name:meta.name,unit:meta.unit,stated:q,qty:conv.n,
      assumedWidth:conv.assumedWidth,rate:r,subtotal:conv.n*r.avg,small:conv.n<r.qty*0.25});
  }
  const ramp=d.cats.ramp&&d.items[d.cats.ramp];
  const rampRate=ramp&&ramp.unit==="each"&&latestRate(d,d.cats.ramp,district);
  rampCounts.forEach(n=>{
    if(rampRate)lines.push({item:d.cats.ramp,name:ramp.name,unit:"each",stated:{cat:"ramp",qty:n,unit:"EA",raw:plural(n,"ramp")},
      qty:n,rate:rampRate,subtotal:n*rampRate.avg,small:false});
    else skipped.push(`${plural(n,"ramp")} (count only; ${agencyOf(d)} ${ramp?"prices ramps by area":"has no ramp price"})`);
  });
  const total=lines.reduce((t,l)=>t+l.subtotal,0);
  return{lines,skipped,total,year:lines.length?lines[0].rate.year:null};
}
function ballparkChip(city,b){
  const d=ratesNow(bidState(city,b));
  if(!d)return"";
  const est=ballpark(b,d,priceDistrict(d),sidewalkWidth());
  return est.total>0
    ?`<span class="chip est" title="Quantities in this posting at ${esc(agencyOf(d))}'s going rates">≈ ${esc(formatMoney(est.total))} ballpark</span>`
    :"";
}
function ballparkHTML(b,d,district){
  const width=sidewalkWidth();
  const est=ballpark(b,d,district,width);
  if(!est.lines.length&&!est.skipped.length)return"";
  const n=(x)=>Math.round(x).toLocaleString();
  const usesWidth=est.lines.some(l=>l.assumedWidth);
  const avgWord=d.basis==="awarded"?"winning-bid averages":"averages";
  return`<div class="workspace-title" style="margin-top:1rem;">Ballpark — ${esc(d.districts[district])}, ${esc(ratePeriod(d,est.year||Math.max(...d.years)))} ${avgWord}</div>
    ${est.lines.map(l=>`<div class="rate-row">
      <div class="rate-main"><div class="rate-name">${esc(l.name)}</div>
        <div class="rate-sub">${esc(l.stated.raw)}${l.assumedWidth?` × ${l.assumedWidth}\u00a0ft wide (assumed)`:""}${
          (l.stated.unit==="LF"&&l.unit==="ft")||l.unit==="each"?"":` ≈ ${n(l.qty)}\u00a0${esc(l.unit)}`} × ${fmtRate(l.rate.avg)}/${esc(l.unit)}${
          l.small?` <span class="rate-thin">smaller than ${esc(agencyOf(d))}'s typical job, so expect a higher unit price</span>`:""}</div></div>
      <div class="rate-val">$${n(l.subtotal)}</div></div>`).join("")}
    ${est.total>0?`<div class="rate-row est-total"><div class="rate-name">Ballpark for these items</div><div class="rate-val">≈ $${n(est.total)}</div></div>`:""}
    ${b.value?`<div class="rate-note">Posted value: <b>${esc(b.value)}</b></div>`:""}
    ${est.skipped.length?`<div class="rate-note">Not counted: ${est.skipped.map(esc).join("; ")}.</div>`:""}
    ${usesWidth?`<div class="rate-note est-width">Sidewalk width <input id="est-width" type="number" min="1" max="30" step="0.5" value="${esc(String(width))}" aria-label="Sidewalk width in feet"> ft</div>`:""}
    <div class="rate-note">Only the quantities shown, at ${esc(agencyOf(d))}'s average ${d.basis==="awarded"?"winning ":""}bid. Removal, mobilization, traffic control and anything else the job needs aren't included.</div>`;
}

// ── Bid results: who bids this work, and what wins ──
// From the state's own bid tabulations (tools/build_bid_results.py): every
// bidder on every state job with concrete flatwork, in rank order, with
// their unit prices. Rank 1 is the low bid, which is the one awarded.
const bidResults={},bidResultsLoading={};
function loadBidResults(st){
  st=String(st||"").toUpperCase();
  if(bidResults[st])return Promise.resolve(bidResults[st]);
  if(!bidResultsLoading[st]){
    bidResultsLoading[st]=loadRateIndex()
      .then(ix=>ix&&ix[st]&&ix[st].results?fetch(`/results/${st.toLowerCase()}.json`).then(r=>r.ok?r.json():null):null)
      .then(d=>{if(d&&Array.isArray(d.contracts))bidResults[st]=d;return bidResults[st]||null;})
      .catch(()=>{delete bidResultsLoading[st];return null;});
  }
  return bidResultsLoading[st];
}
function monthYear(iso){
  const d=new Date(String(iso)+"T12:00:00");
  return isNaN(d)?String(iso||""):d.toLocaleDateString([],{month:"short",year:"numeric"});
}
// Contractors who bid these items in this district, most active first, with
// how often they won and the unit price they usually put on the main item.
function competitorsHTML(d,items,district){
  const res=bidResults[d.state];
  if(!res||!items.length||res.named===false)return"";
  const main=items[0],meta=d.items[main];
  const jobs=res.contracts.filter(c=>(district==="STATEWIDE"||c.district===district)&&items.some(i=>c.items[i]));
  if(!jobs.length)return"";
  const who={};
  jobs.forEach(c=>c.bidders.forEach(([name],rank)=>{
    const w=who[name]=who[name]||{name,bids:0,wins:0,prices:[]};
    w.bids++;if(rank===0)w.wins++;
    if(c.items[main])w.prices.push(c.items[main][1][rank]);
  }));
  const median=a=>{if(!a.length)return null;const s=[...a].sort((x,y)=>x-y),m=s.length>>1;return s.length%2?s[m]:(s[m-1]+s[m])/2;};
  const top=Object.values(who).sort((a,b)=>b.bids-a.bids||b.wins-a.wins).slice(0,6);
  const recent=[...jobs].sort((a,b)=>b.date<a.date?-1:1).slice(0,4);
  const unit=meta?meta.unit:"";
  const where=district==="STATEWIDE"?d.state_name:d.districts[district];
  return`<div class="workspace-title" style="margin-top:1rem;">Who bids this work — ${esc(where)}</div>
    <table class="ps-table comp-table"><thead><tr><th>Contractor</th><th>Bids</th><th>Won</th><th>${esc(meta?meta.name:"")}<small> typical</small></th></tr></thead><tbody>
    ${top.map(w=>{const m=median(w.prices);return`<tr><td>${esc(w.name)}</td><td>${w.bids}</td><td>${w.wins}</td><td>${m!=null?`${fmtRate(m)}/${esc(unit)}`:"—"}</td></tr>`;}).join("")}
    </tbody></table>
    <div class="comp-recent">${recent.map(c=>{
      const p=c.items[main];
      return`<div class="comp-job"><b>${esc(monthYear(c.date))}</b> · ${esc(c.counties||"")} · ${plural(c.bidders.length,"bidder")}
        <small>${esc(c.desc||"")}. Won by ${esc(c.bidders[0][0])}${p?` at ${fmtRate(p[1][0])}/${esc(unit)}${c.bidders.length>1?`, next ${fmtRate(p[1][1])}`:""}`:""}.</small></div>`;}).join("")}</div>
    <div class="rate-note">${plural(jobs.length,"state job")} with this kind of work, from ${esc(res.source)}, ${esc(monthYear(res.lettings[0]))} – ${esc(monthYear(res.lettings[res.lettings.length-1]))}. "Typical" is the median of their bids on this item.</div>`;
}
// Where a price sits against what wins. Ranges come from the state's
// records of winning bids; where only an average winning price exists the
// comparison is against that, and says so.
function priceCheckHTML(l){
  const d=l&&l.item&&rateData[l.st||"MO"];
  if(!d||!d.items[l.item])return"";
  const district=priceDistrict(d),r=latestRate(d,l.item,district);
  if(!r)return"";
  const w=d.basis==="awarded"?null:latestWin(d,l.item,district);
  const unit=esc(d.items[l.item].unit);
  const price=Number(l.price);
  let verdict="";
  if(price>0){
    if(w&&w.n>=2){
      verdict=price>w.high?`<b class="pc pc-hi">Above every winning bid</b>`
        :price<w.low?`<b class="pc pc-lo">Below every winning bid</b>`:`<b class="pc pc-ok">Inside the winning range</b>`;
    }else if(d.basis==="awarded"||w){
      const avg=w?w.avg:r.avg,ratio=price/avg;
      verdict=ratio>1.25?`<b class="pc pc-hi">Over 25% above the average winning price</b>`
        :ratio<0.75?`<b class="pc pc-lo">Over 25% below the average winning price</b>`:`<b class="pc pc-ok">Near the average winning price</b>`;
    }
  }
  const ref=d.basis==="awarded"
    ?`${esc(agencyOf(d))} ${esc(d.districts[district])}: average winning bid ${money2(r.avg)}/${unit}`
    :`${esc(agencyOf(d))} ${esc(d.districts[district])}: average bid ${money2(r.avg)}/${unit}${
      w?` · winning bids ${w.n>1?`${money2(w.low)}–${money2(w.high)}`:money2(w.avg)} (${plural(w.n,"job")})`:""}`;
  return`${ref}${verdict?` ${verdict}`:""}`;
}

// ── Should you bid this? ──
// From the state's bid results: how many contractors usually bid this kind
// of work in the district, how close second place usually comes, who keeps
// winning it -- plus what the bid itself says (plan holders) and how it
// compares with what this contractor has won before. Facts, each with what
// it rests on; no made-up win probability.
function medianOf(a){if(!a.length)return null;const s=[...a].sort((x,y)=>x-y),m=s.length>>1;return s.length%2?s[m]:(s[m-1]+s[m])/2;}
function resultsNow(st){
  st=String(st||"").toUpperCase();
  if(bidResults[st])return bidResults[st];
  if(/^[A-Z]{2}$/.test(st)&&!rateRerenderQueued["r"+st]){
    rateRerenderQueued["r"+st]=true;
    loadBidResults(st).then(d=>{if(d)renderFeed();});
  }
  return null;
}
function competitionFor(st,items,district){
  const res=bidResults[st];
  if(!res||!items.length)return null;
  const has=c=>items.some(i=>c.items[i]);
  let where=district,jobs=res.contracts.filter(c=>(district==="STATEWIDE"||c.district===district)&&has(c));
  // Too few in one district to say anything: use the whole state, and say so.
  if(jobs.length<4&&district!=="STATEWIDE"){where="STATEWIDE";jobs=res.contracts.filter(has);}
  if(!jobs.length)return null;
  const counts=jobs.map(c=>c.bidders.length);
  const gaps=jobs.filter(c=>c.bidders.length>1).map(c=>{
    const t=c.bidders.map(x=>x[1]).sort((a,b)=>a-b);return t[0]>0?(t[1]-t[0])/t[0]:null;}).filter(x=>x!=null);
  const won={};
  jobs.forEach(c=>{const w=c.bidders[0][0];if(w)won[w]=(won[w]||0)+1;});
  const top=Object.entries(won).sort((a,b)=>b[1]-a[1])[0];
  return{jobs:jobs.length,where,bidders:medianOf(counts),solo:counts.filter(n=>n===1).length,
    gap:gaps.length?medianOf(gaps):null,top:top&&top[1]>=2?{name:top[0],wins:top[1]}:null};
}
// The kinds of work in a bid, as this state's item codes (empty without rates).
function bidItems(city,b){
  const d=rateData[bidState(city,b)];
  return d?ratesForBid(b,d):[];
}
// Plan holders other than this contractor.
function otherHolders(b){
  const me=String((companyProfile&&companyProfile.name)||"").trim().toLowerCase();
  return ((b&&b.plan_holders)||[]).filter(h=>!me||String(h.company||"").trim().toLowerCase()!==me);
}
// Typical number of bidders: the posting's own plan-holder list if it has
// one, else what state jobs of this kind drew. null if neither is known.
function expectedBidders(city,b){
  const holders=(b&&b.plan_holders)||[];
  if(holders.length)return{n:otherHolders(b).length+1,from:"holders"};
  const st=bidState(city,b),d=rateData[st];
  if(!d||!resultsNow(st))return null;
  const comp=competitionFor(st,bidItems(city,b),priceDistrict(d));
  return comp?{n:comp.bidders,from:"history",comp}:null;
}
function competitionChip(city,b){
  const e=expectedBidders(city,b);
  if(!e)return"";
  if(e.n<=2)return`<span class="chip odds-few" title="${e.from==="holders"?"From the plan-holder list":"Typical for state jobs like this"}">Few bidders</span>`;
  if(e.n>=6)return`<span class="chip odds-many" title="${e.from==="holders"?"From the plan-holder list":"Typical for state jobs like this"}">Crowded</span>`;
  return"";
}
// The biggest job this contractor has won, by its prepared total.
function biggestWin(){
  let best=0;
  for(const id in bidPrep)if(pipeline[id]==="won")best=Math.max(best,prepTotals(bidPrep[id]).total||0);
  return best;
}
function oddsHTML(city,b,d,items,district){
  const out=[];
  const holders=otherHolders(b);
  const comp=d&&bidResults[d.state]?competitionFor(d.state,items,district):null;
  let level=null;
  if((b.plan_holders||[]).length){
    out.push(`<b>${plural(holders.length,"other company","other companies")}</b> ${holders.length===1?"has":"have"} taken out plans for this bid.`);
    level=holders.length+1;
  }
  if(comp){
    const where=comp.where==="STATEWIDE"?d.state_name:d.districts[comp.where];
    out.push(`State jobs with this kind of work in ${esc(where)} drew <b>${comp.bidders} bidder${comp.bidders===1?"":"s"}</b> (median of ${comp.jobs})${comp.solo?`; ${comp.solo} had just one`:""}.`);
    if(comp.gap!=null)out.push(`The low bid beat second place by a median <b>${(comp.gap*100).toFixed(1)}%</b>. Price within that of the field or you're likely second.`);
    if(comp.top)out.push(`${esc(comp.top.name)} won ${comp.top.wins} of those ${comp.jobs}.`);
    if(level==null)level=comp.bidders;
  }
  const size=(bidPrep[bidId(city,b)]&&prepTotals(bidPrep[bidId(city,b)]).total)
    ||(d?ballpark(b,d,district,sidewalkWidth()).total:0);
  const big=biggestWin();
  if(size&&big&&size>big*1.5)out.push(`At about ${money0(size)} this is bigger than any job you've marked won (largest ${money0(big)}). Check bonding capacity and crew time.`);
  const dl=daysUntil(b);
  if(dl!=null&&dl>=0&&dl<=2)out.push(`Due ${dl===0?"today":`in ${plural(dl,"day")}`}: little time to price it well.`);
  if(!out.length)return"";
  const verdict=level==null?"":level<=2?`<span class="odds-v odds-few">Little competition</span>`
    :level>=6?`<span class="odds-v odds-many">Crowded: expect a tight price</span>`:`<span class="odds-v">Normal competition</span>`;
  return`<div class="workspace-title" style="margin-top:1rem;">Should you bid this? ${verdict}</div>
    <ul class="odds-list">${out.map(x=>`<li>${x}</li>`).join("")}</ul>
    ${comp?`<div class="rate-note">From ${esc(bidResults[d.state].source)}: state highway jobs, the closest public record of who bids this work. City jobs can draw a different crowd.</div>`:""}`;
}

// ── Target price: what won, at this job's quantities ──
// The middle half of winning unit prices (25th-75th percentile) times this
// job's quantities, for the lines priced against a state item. Where the
// state's records don't say who won, there's no target -- an average of all
// bids isn't a price that wins.
function targetRange(p,st){
  const d=rateData[st];
  if(!d||d.basis==="awarded")return null;
  const district=priceDistrict(d);
  let low=0,high=0,mine=0,n=0;
  (p.lines||[]).forEach(l=>{
    const qty=Number(l.qty)||0;
    if(!l.item||(l.st||"MO")!==d.state||!qty)return;
    const w=latestWin(d,l.item,district);
    if(!w||w.n<3)return;
    low+=qty*w.p25;high+=qty*w.p75;mine+=qty*(Number(l.price)||0);n++;
  });
  if(!n)return null;
  return{low,high,mine:mine*(1+(Number(p.markup)||0)/100),n,total:(p.lines||[]).filter(l=>Number(l.qty)).length,d,district};
}
function targetHTML(p,st){
  const t=targetRange(p,st);
  if(!t)return"";
  const verdict=!t.mine?"":t.mine>t.high?`<b class="pc pc-hi">above that range</b>`
    :t.mine<t.low?`<b class="pc pc-lo">below that range</b>`:`<b class="pc pc-ok">inside that range</b>`;
  return`<div class="target-box">
    <div><span>Winning price for ${t.n===t.total?"these lines":`${t.n} of ${t.total} lines`}</span><b>${money0(t.low)}–${money0(t.high)}</b></div>
    ${t.mine?`<div><span>Your price on them, with markup</span><b>${money0(t.mine)}</b></div><div class="target-v">You're ${verdict}.</div>`:""}
    <div class="rate-note" style="margin-top:0.3rem;">The middle half of winning bids on ${esc(agencyOf(t.d))} jobs (${esc(t.d.districts[t.district])}), at your quantities. Lines without a state item, and items with fewer than 3 winning bids, aren't counted.</div>
  </div>`;
}

// ── Addendum alerts ──
// Saved bids that are still open are re-checked (server: /bid-watch/check)
// a few times a day while the app is open. A new document on the posting,
// or a new addendum number in its text, is flagged on the card and sent as
// a notification. The first check of a bid only records what's there.
const BID_WATCH_KEY="bid_watch";
const BID_WATCH_EVERY_MS=6*3600*1000;
let bidWatch=store.get(BID_WATCH_KEY,{});
let bidWatchRunning=false;
function watchAlert(id){const w=bidWatch[id];return w&&w.alert&&!w.seen?w.alert:"";}
function watchCandidates(){
  return Object.keys(saved).filter(id=>{
    const b=saved[id];
    const url=safeUrl(b&&b.url);
    if(!url||!/^https?:/i.test(url))return false;
    const dl=daysUntil(b);
    if(dl!=null&&dl<0)return false;
    const w=bidWatch[id];
    return !w||!w.at||Date.now()-w.at>BID_WATCH_EVERY_MS;
  });
}
function diffWatch(prev,cur){
  const before=new Set((prev.docs||[]).map(x=>String(x).toLowerCase()));
  const newDocs=(cur.docs||[]).filter(x=>!before.has(String(x).toLowerCase()));
  const had=new Set(prev.addenda||[]);
  const newAdd=(cur.addenda||[]).filter(n=>!had.has(n));
  if(newAdd.length)return`Addendum ${newAdd.join(", ")} posted`;
  if(newDocs.length)return`New document: ${newDocs.slice(0,2).join(", ")}${newDocs.length>2?` and ${newDocs.length-2} more`:""}`;
  return"";
}
async function checkSavedBids(){
  if(bidWatchRunning||isOffline()||!licenseKey())return;
  const ids=watchCandidates().slice(0,25);
  if(!ids.length)return;
  bidWatchRunning=true;
  try{
    const urls=ids.map(id=>safeUrl(saved[id].url));
    const token=await getSupabaseToken();
    const r=await fetchWithTimeout(SERVER+"/bid-watch/check",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({key:licenseKey(),device_id:deviceId(),supabase_token:token,urls})},90000);
    const d=await r.json();
    if(!d||!d.ok)return;
    const alerts=[];
    ids.forEach((id,i)=>{
      const cur=d.results&&d.results[urls[i]];
      if(!cur||!cur.ok)return;
      // The first check only records what's there: comparing against the
      // scan's own document list would flag differences in how the two
      // were read, not changes the agency made.
      const prev=bidWatch[id]&&bidWatch[id].at?bidWatch[id]:null;
      const change=prev?diffWatch(prev,cur):"";
      bidWatch[id]={at:Date.now(),docs:cur.docs||[],addenda:cur.addenda||[],hash:cur.hash||"",
        alert:change||(bidWatch[id]&&bidWatch[id].alert)||"",seen:change?false:!!(bidWatch[id]&&bidWatch[id].seen),
        alertAt:change?Date.now():(bidWatch[id]&&bidWatch[id].alertAt)||0,
        edited:!!(bidWatch[id]&&bidWatch[id].hash&&cur.hash&&bidWatch[id].hash!==cur.hash)};
      if(change)alerts.push([id,change]);
    });
    store.set(BID_WATCH_KEY,bidWatch);
    if(alerts.length){
      alerts.forEach(([id,msg])=>fireNotification(`${saved[id].title||"A saved bid"} changed`,`${msg}. Read it before you bid.`));
      toast(alerts.length===1?`${saved[alerts[0][0]].title||"A saved bid"}: ${alerts[0][1]}`:`${alerts.length} saved bids have new addenda or documents`);
      renderFeed();
      if(document.getElementById("screen-saved").classList.contains("active"))renderSaved();
    }
  }catch(e){/* tried again on the next round */}
  finally{bidWatchRunning=false;}
}
function markWatchSeen(id){
  if(bidWatch[id]&&bidWatch[id].alert&&!bidWatch[id].seen){bidWatch[id].seen=true;store.set(BID_WATCH_KEY,bidWatch);}
}
function watchDetailHTML(id){
  const w=bidWatch[id];
  if(!w||!w.at)return"";
  const when=new Date(w.at).toLocaleString([],{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"});
  return`<div class="rate-note watch-note">${w.alert?`<b class="pc pc-hi">${esc(w.alert)}</b> · `:""}${w.edited&&!w.alert?"The posting's text changed since the last check. · ":""}Watching this bid for addenda. Last checked ${esc(when)}.</div>`;
}

// ── Did you win? ──
// After a prepared or saved bid's due date passes with no outcome, Home asks.
// The answer feeds the win rate and the price history; a loss can record the
// winning bid, so next time's price starts from what actually won. For a
// MoDOT job the state's own results are shown when they're in.
const RESULT_ASKED_KEY="result_asked";
let resultAsked=store.get(RESULT_ASKED_KEY,{});
function outcomeCandidates(){
  const ids=new Set([...Object.keys(bidPrep),...Object.keys(saved)]);
  return [...ids].filter(id=>{
    const b=saved[id]||findBid(id);
    if(!b||resultAsked[id])return false;
    const st=pipeline[id];
    if(st==="won"||st==="lost"||st==="passed")return false;
    const dl=daysUntil(b);
    return dl!=null&&dl<0&&dl>=-120;
  }).sort((a,b)=>daysUntil(saved[b]||findBid(b))-daysUntil(saved[a]||findBid(a))).slice(0,3);
}
// A MoDOT job's own result, matched on its job number (J4P3567) or contract id.
function stateResultFor(b){
  const res=bidResults.MO;
  if(!res||!b)return null;
  const text=`${b.title||""} ${b.scope||""} ${b.bid_number||""}`;
  const jobs=(text.match(/\bJ[0-9][A-Z0-9]{4,6}\b/g)||[]).map(x=>x.toUpperCase());
  return res.contracts.find(c=>c.id===String(b.bid_number||"").trim()
    ||jobs.some(j=>String(c.desc||"").toUpperCase().includes(j)))||null;
}
function outcomeCardHTML(){
  const ids=outcomeCandidates();
  if(!ids.length)return"";
  return`<div class="account-card" id="home-outcomes">
    <div class="account-email hdr-ic" style="font-size:var(--fs-base);margin-bottom:0.4rem;"><svg class="icon-svg"><use href="#i-activity"/></svg>How did these go?</div>
    <div class="account-status" style="margin-bottom:0.4rem;">Your answers build your win rate and price history.</div>
    ${ids.map(id=>{
      const b=saved[id]||findBid(id),r=stateResultFor(b);
      return`<div class="outcome" data-oid="${esc(id)}">
        <div class="outcome-t"><b>${esc(b.title||"Untitled bid")}</b><small>Due ${esc(b.deadline||"")}${r?` · MoDOT result: won by ${esc(r.bidders[0][0]||"the low bidder")} at ${money0(r.bidders[0][1])}, ${plural(r.bidders.length,"bidder")}`:""}</small></div>
        <div class="outcome-b"><button class="btn-ghost" data-out="won">Won</button><button class="btn-ghost" data-out="lost">Lost</button><button class="btn-ghost" data-out="passed">Didn't bid</button></div>
      </div>`;}).join("")}
  </div>`;
}
function wireOutcomeCard(root){
  root.querySelectorAll(".outcome").forEach(row=>{
    const id=row.dataset.oid;
    row.querySelectorAll("[data-out]").forEach(btn=>btn.onclick=()=>{
      const out=btn.dataset.out;
      setPipelineStatus(id,out);
      resultAsked[id]=true;store.set(RESULT_ASKED_KEY,resultAsked);
      if(out==="won"){
        row.innerHTML=`<div class="outcome-t"><b>Nice work.</b><small>Would you tell other contractors how CurbCall helped? It takes a minute.</small></div>
          <div class="outcome-b"><button class="btn-primary" data-review="1" style="margin-top:0;">Leave a review</button></div>`;
        row.querySelector("[data-review]").onclick=()=>{switchScreen("account");setTimeout(()=>{const c=document.getElementById("review-card");if(c)c.scrollIntoView({behavior:"smooth"});},300);};
      }else if(out==="lost"){
        const b=saved[id]||findBid(id),r=stateResultFor(b);
        row.innerHTML=`<div class="outcome-t"><b>What did the winning bid come in at?</b><small>Optional. It shows next to your price on similar jobs.</small></div>
          <div class="outcome-b"><input class="input" data-win inputmode="decimal" placeholder="$ total" value="${r?esc(String(Math.round(r.bidders[0][1]))):""}"><button class="btn-ghost" data-save>Save</button></div>`;
        row.querySelector("[data-save]").onclick=()=>{
          const v=Number(String(row.querySelector("[data-win]").value).replace(/[^0-9.]/g,""));
          if(v>0){const p=prepFor(id);p.result={winning_total:v,at:Date.now()};savePrep(id,(saved[id]&&saved[id]._city)||"");}
          row.remove();toast("Saved");
        };
      }else row.remove();
      const card=document.getElementById("home-outcomes");
      if(card&&!card.querySelector(".outcome"))card.remove();
    });
  });
}
// On jobs lost with a recorded winning bid: how far above the winner you were.
function lossGapHTML(){
  const gaps=[];
  for(const id in bidPrep){
    const p=bidPrep[id],w=p&&p.result&&Number(p.result.winning_total);
    if(pipeline[id]!=="lost"||!w)continue;
    const mine=prepTotals(p).total;
    if(mine>0)gaps.push((mine-w)/w);
  }
  if(!gaps.length)return"";
  const g=medianOf(gaps);
  return`<div class="rate-note">On ${plural(gaps.length,"job")} you lost with the winning bid recorded, you were a median <b>${(g*100).toFixed(1)}% ${g>=0?"above":"below"}</b> the winner.</div>`;
}

// ── Bid workspace ("Prepare bid") ──
// Keeps the contractor in the app from "worth bidding" to "ready to sign":
// a checklist built from what the posting says, a pricing sheet that starts
// from the ballpark, and a one-page summary to copy onto the agency's own
// form. The app never signs, bonds, notarizes or submits -- those steps are
// listed so nothing is missed, and left to the contractor.
//
// One record per bid id, saved locally and synced with the saved bid (the
// saved_bids.prep column). Preparing a bid saves it, so the work follows the
// account to other devices.
function prepFor(id){
  if(!bidPrep[id])bidPrep[id]={checks:{},custom:[],lines:null,markup:0,addenda:"",updated:0};
  return bidPrep[id];
}
const _prepPushTimers={};
function savePrep(id,city){
  prepFor(id).updated=Date.now();
  store.set("bid_prep",bidPrep);
  // Preparing a bid is a decision to pursue it: keep it saved so it syncs
  // and stays on the Active tab after a rescan drops it from the feed.
  if(!saved[id]){
    const b=findBid(id);
    if(b){saved[id]={...b,_city:city};store.set("saved",saved);}
  }
  clearTimeout(_prepPushTimers[id]);
  _prepPushTimers[id]=setTimeout(()=>pushSavedBid(id,city),1200);
}
// What a public concrete bid usually takes. Generic items say "if required":
// the posting decides, and inventing a requirement is as bad as missing one.
function prepChecklist(b,info){
  info=info||{};
  const fromDocs=(t)=>t?` From the bid documents: ${t}`:"";
  const docs=((b&&b.documents)||[]).map(d=>({
    name:(d&&d.name)?String(d.name):"Document",url:safeUrl(typeof d==="string"?d:(d&&d.url))
  })).filter(d=>d.url);
  const items=[
    {key:"docs",label:"Get the bid documents",
      detail:docs.length?"Plans, specs and the bid form. Linked below."
        :"Plans, specs and the bid form. Some agencies require registering on their plan room first.",docs},
    !(b&&b.prebid)&&info.prebid?{key:"prebid",label:"Attend the pre-bid meeting",
      detail:fromDocs(info.prebid).trim(),urgent:/mandatory/i.test(info.prebid)}:null,
    b&&b.prebid?{key:"prebid",label:b.prebid==="mandatory"?"Attend the MANDATORY pre-bid meeting":"Attend the pre-bid meeting",
      detail:(b.prebid==="mandatory"?"Missing it disqualifies the bid. Date and place are in the posting.":"Date and place are in the posting.")+fromDocs(info.prebid),
      urgent:b.prebid==="mandatory"}:null,
    {key:"addenda",label:"Read every addendum",
      detail:(b&&b.addenda?"This bid has addenda. ":"")+"Check for new ones up to the deadline. Most bid forms ask you to list each addendum by number.",
      urgent:!!(b&&b.addenda)},
    info.questions_due?{key:"questions",label:"Send any questions before the deadline",detail:fromDocs(info.questions_due).trim()}:null,
    {key:"site",label:"Visit the site",detail:"Optional, but quantities on paper and in the field don't always agree.",optional:true},
    {key:"price",label:"Price every line item",detail:"Use the Pricing tab."},
    {key:"bond",label:info.bid_security?"Bid bond or bid security":"Bid bond or bid security, if required",
      detail:info.bid_security?fromDocs(info.bid_security).trim()+" Ask your bonding company early."
        :"Often a percentage of the bid amount. The posting says if one is needed. Ask your bonding company early.",urgent:!!info.bid_security},
    {key:"forms",label:"Fill out the bid form and required affidavits",
      detail:"Use the agency's own form. Copy prices from the Summary tab."+(info.required_forms&&info.required_forms.length
        ?` Required by the bid documents: ${info.required_forms.join("; ")}.`:"")},
    {key:"insurance",label:"Insurance certificate, if requested",detail:"Check the posting for limits and who to name as additional insured."},
    {key:"sign",label:"Sign, and notarize if required",detail:"Done by you, outside the app."},
    {key:"submit",label:"Submit before the deadline",
      detail:`${b&&b.deadline?`Due ${b.deadline}. `:""}${info.submission?fromDocs(info.submission).trim():"Sealed envelope or online, as the posting says."} Late bids are usually returned unopened.`,urgent:true},
  ];
  return items.filter(Boolean);
}
function prepProgress(id,b){
  const p=bidPrep[id];
  if(!p)return null;
  const list=prepChecklist(b,p.docInfo).filter(i=>!i.optional).concat((p.custom||[]).map((c,i)=>({key:"c"+i})));
  const done=list.filter(i=>p.checks[i.key]).length;
  return{done,total:list.length};
}
// Starting lines: the ballpark's priced quantities, then anything it could
// read but not price (ramp counts), then one blank line to fill in.
function defaultPrepLines(b,city){
  const lines=[];
  const d=rateData[bidState(city,b)];
  if(d){
    const district=priceDistrict(d);
    const est=ballpark(b,d,district,sidewalkWidth());
    // Start at what wins, where the state's records say: pricing down from
    // an all-bids average starts above most winning bids.
    const start=l=>{const w=latestWin(d,l.item,district);return w?w.avg:l.rate.avg;};
    est.lines.forEach(l=>lines.push({name:l.name,qty:Math.round(l.qty*10)/10,unit:l.unit,
      price:Math.round(start(l)*100)/100,ref:l.rate.avg,item:l.item,st:d.state,
      // The assumption travels with the number it produced.
      note:l.assumedWidth?`From ${l.stated.raw} at ${l.assumedWidth} ft wide (assumed). Check the plans.`:""}));
    if(!est.lines.some(l=>l.unit==="each"&&l.item===d.cats.ramp))
      extractQuantities(`${b.title||""}\n${b.scope||""}`).rampCounts.forEach(n=>
        lines.push({name:"ADA curb ramp",qty:n,unit:"each",price:"",ref:null}));
  }
  if(!lines.length)lines.push({name:"",qty:"",unit:"",price:"",ref:null});
  return lines;
}
function prepTotals(p){
  const sub=(p.lines||[]).reduce((t,l)=>t+(Number(l.qty)||0)*(Number(l.price)||0),0);
  const mk=Number(p.markup)||0;
  return{sub,markup:sub*mk/100,total:sub*(1+mk/100)};
}
function money0(n){return"$"+Math.round(n).toLocaleString();}
function money2(n){return"$"+(Number(n)||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});}

async function openPrep(city,id,step){
  const b=findBid(id)||saved[id];
  if(!b){toast("That bid isn't available any more");return;}
  const st=bidState(city,b);
  await Promise.all([loadRates(st),loadBidResults(st)]);
  const p=prepFor(id);
  if(!p.lines)p.lines=defaultPrepLines(b,city);
  savePrep(id,city);
  step=step||p.step||"checklist";
  p.step=step;
  const mc=document.getElementById("modal-content");
  const prog=prepProgress(id,b);
  const t=prepTotals(p);
  const dleft=daysUntil(b);
  mc.innerHTML=`<div class="sheet-head"><h2>Prepare bid</h2>
      <div class="sheet-sub"><span class="chip">${esc(b.title||"Untitled")}</span>
        ${b.deadline?`<span class="chip"${dleft!=null&&dleft<=2?' style="color:var(--red);"':""}>Due ${esc(b.deadline)}${dleft!=null&&dleft>=0?` · ${dleft===0?"today":dleft+"d left"}`:""}</span>`:""}</div></div>
    <a href="#" class="prep-back" id="prep-back">← Bid details</a>
    <div class="prep-tabs" role="tablist">
      <button role="tab" data-step="checklist" class="${step==="checklist"?"on":""}">Checklist <span>${prog.done}/${prog.total}</span></button>
      <button role="tab" data-step="pricing" class="${step==="pricing"?"on":""}">Pricing <span id="prep-tab-total">${t.total?money0(t.total):""}</span></button>
      <button role="tab" data-step="summary" class="${step==="summary"?"on":""}">Summary</button>
    </div>
    <div id="prep-body"></div>`;
  mc.querySelectorAll(".prep-tabs button").forEach(btn=>btn.onclick=()=>openPrep(city,id,btn.dataset.step));
  document.getElementById("prep-back").onclick=(e)=>{e.preventDefault();openDetail(city,b);};
  const body=document.getElementById("prep-body");
  if(step==="checklist")renderPrepChecklist(body,city,id,b);
  else if(step==="pricing")renderPrepPricing(body,city,id,b);
  else renderPrepSummary(body,city,id,b);
  document.getElementById("modal-back").classList.add("open");
  document.getElementById("modal").classList.add("open");
}

function renderPrepChecklist(body,city,id,b){
  const p=prepFor(id);
  const items=prepChecklist(b,p.docInfo);
  const row=(key,label,detail,opts={})=>`<label class="prep-check${p.checks[key]?" done":""}${opts.urgent?" urgent":""}">
      <input type="checkbox" data-check="${esc(key)}"${p.checks[key]?" checked":""}>
      <span><b>${esc(label)}</b>${opts.optional?' <em>optional</em>':""}
        ${detail?`<small>${esc(detail)}</small>`:""}
        ${opts.docs&&opts.docs.length?`<small class="prep-docs">${opts.docs.slice(0,6).map(d=>`<a href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">${esc(d.name)}</a>`).join(" · ")}</small>`:""}
        ${opts.remove!=null?`<button type="button" class="prep-x" data-remove="${opts.remove}" aria-label="Remove">×</button>`:""}</span>
    </label>`;
  body.innerHTML=`${items.map(i=>row(i.key,i.label,i.detail,i)).join("")}
    ${(p.custom||[]).map((c,i)=>row("c"+i,c,"",{remove:i})).join("")}
    <div class="prep-add"><input class="input" id="prep-new" placeholder="Add your own step"><button class="btn-ghost" id="prep-add-btn">Add</button></div>
    ${b.deadline?`<button class="btn-ghost" id="prep-cal" style="margin-top:0.6rem;">Add deadline to calendar</button>`:""}`;
  body.querySelectorAll("[data-check]").forEach(cb=>cb.onchange=()=>{
    p.checks[cb.dataset.check]=cb.checked;
    if(!cb.checked)delete p.checks[cb.dataset.check];
    savePrep(id,city);openPrep(city,id,"checklist");
  });
  body.querySelectorAll("[data-remove]").forEach(x=>x.onclick=(e)=>{
    e.preventDefault();
    const i=Number(x.dataset.remove);
    p.custom.splice(i,1);
    // Re-key the checks of the custom items after the removed one.
    const next={};
    Object.keys(p.checks).forEach(k=>{
      const m=/^c(\d+)$/.exec(k);
      if(!m)next[k]=p.checks[k];
      else if(+m[1]<i)next[k]=p.checks[k];
      else if(+m[1]>i)next["c"+(+m[1]-1)]=p.checks[k];
    });
    p.checks=next;
    savePrep(id,city);openPrep(city,id,"checklist");
  });
  const add=()=>{
    const v=document.getElementById("prep-new").value.trim();
    if(!v)return;
    (p.custom=p.custom||[]).push(v.slice(0,200));
    savePrep(id,city);openPrep(city,id,"checklist");
  };
  document.getElementById("prep-add-btn").onclick=add;
  document.getElementById("prep-new").onkeydown=(e)=>{if(e.key==="Enter")add();};
  const cal=document.getElementById("prep-cal");
  if(cal)cal.onclick=()=>addToCalendar(city,id);
}

// ── Reading the bid documents (server: /bid-documents/read) ──
// The bid form's own schedule of items is the exact list to price. The
// server fetches the document, reads it, and returns the items and what it
// says about submitting, bid security, the pre-bid meeting and forms.
// Nothing replaces the contractor's lines until they've seen what was read.
function prepDocCandidates(b){
  const out=[];
  ((b&&b.documents)||[]).forEach((d,i)=>{
    const url=safeUrl(typeof d==="string"?d:(d&&d.url));
    if(url&&/^https?:/i.test(url))out.push({name:(d&&d.name)?String(d.name):"Document "+(i+1),url});
  });
  const post=safeUrl(b&&b.url);
  if(post&&/^https?:/i.test(post)&&!out.some(d=>d.url===post))out.push({name:"Original posting",url:post});
  return out;
}
const DOC_UNITS={SY:"sq yd",SQYD:"sq yd",SQYDS:"sq yd",SF:"sq ft",SQFT:"sq ft",LF:"ft",FT:"ft",LINFT:"ft",
  EA:"each",EACH:"each",LS:"lump sum",LUMPSUM:"lump sum",CY:"cu yd",TON:"ton",TONS:"ton",GAL:"gal",HR:"hour",HOUR:"hour"};
function docUnit(u){
  const k=String(u||"").toUpperCase().replace(/[^A-Z]/g,"");
  return DOC_UNITS[k]||String(u||"").toLowerCase();
}
// A schedule row as a pricing line. The state's average is filled in only
// when the description names a flatwork item AND the units agree -- "4 in.
// sidewalk, SY" gets the sidewalk rate; "sidewalk, LS" gets nothing.
function lineFromDocItem(it,d){
  const unit=docUnit(it.unit);
  const line={name:`${it.item_no?it.item_no+". ":""}${it.description}`,qty:it.quantity??"",unit,price:"",ref:null};
  const cat=nearestItem(it.description||"",false);
  const code=cat&&d&&d.cats[cat];
  if(code&&d.items[code]&&d.items[code].unit===unit){
    const district=priceDistrict(d),r=latestRate(d,code,district),w=r&&latestWin(d,code,district);
    if(r){line.ref=r.avg;line.price=Math.round((w?w.avg:r.avg)*100)/100;line.item=code;line.st=d.state;}
  }
  return line;
}
const DOC_ERRORS={
  not_licensed:"Your trial or subscription isn't active.",
  rate_limited:"Daily limit for reading documents reached. Try again tomorrow.",
  no_text:"This document has no readable text. It may be a scanned image.",
  bad_url:"That link can't be read.",
  ai_unavailable:"Document reading isn't available right now.",
  ai_error:"Couldn't read that document. Try again in a minute.",
};
const FETCH_ERRORS={robots_disallow:"That site doesn't allow automated reading. Open it and enter the items by hand.",
  too_large:"That file is too large to read.",http_404:"That document isn't there any more.",http_403:"That site refused the request."};
async function readBidDocument(url){
  try{
    const token=await getSupabaseToken();
    const r=await fetchWithTimeout(SERVER+"/bid-documents/read",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({key:licenseKey(),device_id:deviceId(),supabase_token:token,url})},100000);
    const d=await r.json();
    if(d&&d.ok)return d;
    const msg=d&&d.reason==="fetch_failed"?(FETCH_ERRORS[d.detail]||"Couldn't download that document.")
      :(d&&(DOC_ERRORS[d.reason]||d.detail))||"Couldn't read that document.";
    return{ok:false,msg};
  }catch(e){return{ok:false,msg:offlineOrServer()};}
}
function renderDocPicker(body,city,id,b){
  const docs=prepDocCandidates(b);
  body.innerHTML=`<div class="rate-note" style="margin-top:0;">Pick the document with the bid form or schedule of items.
      The quantities it lists replace the estimate, after you've checked them.</div>
    ${docs.map((d,i)=>`<button class="btn-ghost doc-pick" data-doc="${i}">${esc(d.name)}</button>`).join("")}
    <button class="btn-ghost" id="doc-cancel">Cancel</button>`;
  body.querySelectorAll("[data-doc]").forEach(btn=>btn.onclick=async()=>{
    const d=docs[Number(btn.dataset.doc)];
    body.innerHTML=`<div class="account-status"><span class="spin"></span> Reading ${esc(d.name)}… this can take up to a minute.</div>`;
    const res=await readBidDocument(d.url);
    if(!document.getElementById("prep-body"))return;
    if(!res.ok){
      body.innerHTML=`<div class="alert alert-amber"><span>${esc(res.msg)}</span></div><button class="btn-ghost" id="doc-back">Back to pricing</button>`;
      document.getElementById("doc-back").onclick=()=>openPrep(city,id,"pricing");
      return;
    }
    renderDocReview(body,city,id,b,d,res);
  });
  document.getElementById("doc-cancel").onclick=()=>openPrep(city,id,"pricing");
}
function renderDocReview(body,city,id,b,doc,res){
  const p=prepFor(id);
  // What the documents say about the bid is kept whatever happens to the
  // lines -- it feeds the checklist.
  p.docInfo={source:doc.name,submission:res.submission||"",bid_security:res.bid_security||"",
    prebid:res.prebid||"",questions_due:res.questions_due||"",required_forms:res.required_forms||[],read_at:Date.now()};
  savePrep(id,city);
  const items=res.line_items||[];
  const found=[res.submission&&"how to submit",res.bid_security&&"bid security",res.prebid&&"the pre-bid meeting",
    (res.required_forms||[]).length&&"required forms"].filter(Boolean);
  body.innerHTML=`<div class="workspace-title">Read from ${esc(doc.name)}${res.pages_read?` · ${plural(res.pages_read,"page")}`:""}</div>
    ${res.truncated?`<div class="rate-note">Long document: only the first part was read. Check the rest by hand.</div>`:""}
    ${items.length?`<div class="rate-note">${plural(items.length,"line item")} found. Check them against the document before relying on them.</div>
      <table class="ps-table"><thead><tr><th>Item</th><th>Qty</th></tr></thead><tbody>
      ${items.slice(0,40).map(it=>`<tr><td>${esc(`${it.item_no?it.item_no+". ":""}${it.description}`)}</td><td>${it.quantity!=null?esc(String(it.quantity)):"—"} ${esc(it.unit||"")}</td></tr>`).join("")}
      </tbody></table>
      <div class="act-primary"><button class="ma-gold" id="doc-replace">Use these lines</button><button class="ma-ghost" id="doc-add">Add to my lines</button></div>`
    :`<div class="alert alert-amber"><span>No schedule of items found in this document. Try the bid form if there's another document.</span></div>`}
    ${found.length?`<div class="rate-note">Also found ${found.join(", ")}. It's now on your Checklist.</div>`:""}
    <button class="btn-ghost" id="doc-back" style="margin-top:0.5rem;">Back to pricing</button>`;
  const take=(replace)=>{
    const d=rateData[bidState(city,b)];
    const lines=items.map(it=>lineFromDocItem(it,d));
    p.lines=replace?lines:(p.lines||[]).filter(l=>(l.name||"").trim()||l.qty||l.price).concat(lines);
    if(!p.lines.length)p.lines.push({name:"",qty:"",unit:"",price:"",ref:null});
    savePrep(id,city);openPrep(city,id,"pricing");
  };
  const rep=document.getElementById("doc-replace"),add=document.getElementById("doc-add");
  if(rep)rep.onclick=()=>{if(confirm("Replace your current lines with the items read from the document?"))take(true);};
  if(add)add.onclick=()=>take(false);
  document.getElementById("doc-back").onclick=()=>openPrep(city,id,"pricing");
}

// ── Your own pricing history ──
// Every prepared bid is a record of what this contractor charged for each
// item, and its Bid Status says whether that price won. Lines match on the
// state DOT item they were priced against, or else on their name and unit,
// so "4 in. concrete sidewalk / sq yd" on two bids is the same thing. Lines
// saved before rates covered other states have no state: they were Missouri.
function lineKey(l){
  if(l&&l.item)return`m:${l.st||"MO"}:${l.item}`;
  const n=String((l&&l.name)||"").toLowerCase()
    .replace(/^\s*[a-z0-9-]{1,6}\.\s+/,"")      // "2. " item numbers
    .replace(/[^a-z0-9 ]/g," ").replace(/\s+/g," ").trim();
  return n?`n:${n}|${(l&&l.unit)||""}`:null;
}
function bidTitleFor(id){
  const b=saved[id]||findBid(id);
  return (b&&b.title)||"Untitled bid";
}
// key -> [{id, title, price, status, updated}], newest first.
function priceHistory(excludeId){
  const h={};
  for(const id in bidPrep){
    if(id===excludeId)continue;
    const p=bidPrep[id];
    (p&&p.lines||[]).forEach(l=>{
      const price=Number(l.price),k=lineKey(l);
      if(!k||!(price>0))return;
      (h[k]=h[k]||[]).push({id,title:bidTitleFor(id),price,status:pipeline[id]||"",updated:p.updated||0});
    });
  }
  for(const k in h)h[k].sort((a,b)=>b.updated-a.updated);
  return h;
}
function historyHint(hist){
  if(!hist||!hist.length)return"";
  const label={won:"won",lost:"lost",submitted:"submitted",passed:"passed"};
  return`<small class="pl-ref pl-hist">Your past prices: ${hist.slice(0,3).map(x=>
    `${money2(x.price)}${label[x.status]?` <b class="h-${esc(x.status)}">${label[x.status]}</b>`:""}`).join(" · ")}</small>`;
}
// Other bids with at least one priced line, newest first.
function pastPreparedBids(excludeId){
  return Object.keys(bidPrep)
    .filter(id=>id!==excludeId&&(bidPrep[id].lines||[]).some(l=>Number(l.price)>0))
    .sort((a,b)=>(bidPrep[b].updated||0)-(bidPrep[a].updated||0));
}
function renderPastBidPicker(body,city,id,b){
  const p=prepFor(id);
  const past=pastPreparedBids(id);
  body.innerHTML=`<div class="rate-note" style="margin-top:0;">Copy your prices onto matching lines here, or copy a past bid's lines (quantities left blank, since it's a different job).</div>
    ${past.map(pid=>{
      const pp=bidPrep[pid],t=prepTotals(pp),st=pipeline[pid];
      return`<div class="past-bid"><div><b>${esc(bidTitleFor(pid))}</b>
          <small>${money0(t.total)}${st?` · ${esc(st)}`:""}${pp.result&&pp.result.winning_total?` to ${money0(pp.result.winning_total)}`:""} · ${plural((pp.lines||[]).length,"line")}</small></div>
        <div class="past-actions"><button class="btn-ghost" data-prices="${esc(pid)}">Copy my prices</button>
          <button class="btn-ghost" data-lines="${esc(pid)}">Copy lines</button></div></div>`;}).join("")}
    <button class="btn-ghost" id="past-cancel" style="margin-top:0.5rem;">Cancel</button>`;
  body.querySelectorAll("[data-prices]").forEach(btn=>btn.onclick=()=>{
    const src={};
    (bidPrep[btn.dataset.prices].lines||[]).forEach(l=>{const k=lineKey(l);if(k&&Number(l.price)>0)src[k]=Number(l.price);});
    let n=0;
    (p.lines||[]).forEach(l=>{const k=lineKey(l);if(k&&src[k]!=null){l.price=src[k];n++;}});
    savePrep(id,city);
    toast(n?`Copied ${plural(n,"price")}`:"No lines here match that bid");
    openPrep(city,id,"pricing");
  });
  body.querySelectorAll("[data-lines]").forEach(btn=>btn.onclick=()=>{
    const copied=(bidPrep[btn.dataset.lines].lines||[])
      .filter(l=>(l.name||"").trim())
      .map(l=>({name:l.name,qty:"",unit:l.unit||"",price:l.price,ref:l.ref||null,item:l.item,st:l.st}));
    p.lines=(p.lines||[]).filter(l=>(l.name||"").trim()||l.qty||l.price).concat(copied);
    savePrep(id,city);
    toast(`Added ${plural(copied.length,"line")}. Fill in this job's quantities.`);
    openPrep(city,id,"pricing");
  });
  document.getElementById("past-cancel").onclick=()=>openPrep(city,id,"pricing");
}
// Won/lost averages per state DOT item, beside the state's own average.
function yourBidsVsState(d,district){
  const rows={};
  for(const id in bidPrep){
    const st=pipeline[id];
    if(st!=="won"&&st!=="lost")continue;
    (bidPrep[id].lines||[]).forEach(l=>{
      const price=Number(l.price);
      if(!l.item||(l.st||"MO")!==d.state||!(price>0)||!d.items[l.item])return;
      const r=rows[l.item]=rows[l.item]||{won:[],lost:[]};
      r[st].push(price);
    });
  }
  const avg=a=>a.length?a.reduce((x,y)=>x+y,0)/a.length:null;
  const items=Object.keys(rows);
  if(!items.length)return"";
  const ag=esc(agencyOf(d));
  return`<div class="workspace-title" style="margin-top:0.6rem;">Your bids vs ${ag}</div>
    <table class="ps-table"><thead><tr><th>Item</th><th>You won at</th><th>You lost at</th><th>${ag} avg</th></tr></thead><tbody>
    ${items.map(i=>{
      const r=rows[i],w=avg(r.won),lo=avg(r.lost),m=latestRate(d,i,district);
      return`<tr><td>${esc(d.items[i].name)}</td><td>${w!=null?`${money2(w)}<small> (${r.won.length})</small>`:"—"}</td>
        <td>${lo!=null?`${money2(lo)}<small> (${r.lost.length})</small>`:"—"}</td><td>${m?money2(m.avg):"—"}</td></tr>`;}).join("")}
    </tbody></table>
    <div class="rate-note">From bids you priced in Prepare bid and marked Won or Lost, per unit as ${ag} prices each item.</div>`;
}

// What a pricing line can be: the state DOT's own items (picking one fills in
// its unit and going price), the other work most concrete bids carry, or
// anything typed in. Lines read from a bid form keep the form's wording and
// show as typed.
const EXTRA_LINES=[
  ["Mobilization","lump sum"],["Traffic control","lump sum"],["Remove existing concrete","sq yd"],
  ["Saw cutting","ft"],["Excavation and grading","cu yd"],["Aggregate base","sq yd"],
  ["ADA curb ramp","each"],["Detectable warning panel","each"],["Erosion control","lump sum"],
  ["Restoration, sod or seeding","sq yd"],["Testing","lump sum"],["Bonds and insurance","lump sum"],
];
function lineChoice(l,d){
  if(l.item&&d&&(l.st||"MO")===d.state&&d.items[l.item])return"s:"+l.item;
  const x=EXTRA_LINES.find(([n])=>n===l.name);
  if(x&&!l.item)return"x:"+x[0];
  return String(l.name||"").trim()?"custom":"";
}
// The state's items, the plain ones a posting is matched to first, then
// by how often they're bid.
function pickableItems(d){
  if(!d)return[];
  const cats=new Set(Object.values(d.cats||{}));
  const bids=c=>{const by=d.prices[c]&&d.prices[c].STATEWIDE;return by?by[Object.keys(by).sort().pop()][3]:0;};
  return Object.keys(d.items).filter(c=>d.prices[c])
    .sort((a,b)=>(cats.has(b)-cats.has(a))||(bids(b)-bids(a)));
}
function lineItemSelect(l,i,d){
  const cur=lineChoice(l,d);
  const opt=(v,label)=>`<option value="${esc(v)}"${cur===v?" selected":""}>${esc(label)}</option>`;
  const items=pickableItems(d);
  return`<select class="input pl-pick" data-pick="${i}" aria-label="Item">
      <option value=""${cur===""?" selected":""}>Choose an item…</option>
      ${items.length?`<optgroup label="${esc(agencyOf(d))} items">${items.map(c=>opt("s:"+c,`${d.items[c].name} (${d.items[c].unit})`)).join("")}</optgroup>`:""}
      <optgroup label="Other work">${EXTRA_LINES.map(([n,u])=>opt("x:"+n,`${n} (${u})`)).join("")}</optgroup>
      <option value="custom"${cur==="custom"?" selected":""}>Other (type it)</option>
    </select>
    ${cur==="custom"?`<input class="input pl-name" data-f="name" value="${esc(l.name||"")}" placeholder="Describe the item" aria-label="Item description">`:""}`;
}
function applyLineChoice(l,v,d){
  delete l.note;
  if(v.startsWith("s:")&&d){
    const code=v.slice(2),meta=d.items[code];
    if(!meta)return;
    const district=priceDistrict(d),r=latestRate(d,code,district),w=r&&latestWin(d,code,district);
    // A price already typed is the contractor's; only an empty one is filled.
    const fill=!Number(l.price)||(l.ref&&Number(l.price)===Math.round(l.ref*100)/100);
    Object.assign(l,{name:meta.name,unit:meta.unit,item:code,st:d.state,ref:r?r.avg:null});
    if(fill&&r)l.price=Math.round((w?w.avg:r.avg)*100)/100;
  }else if(v.startsWith("x:")){
    const x=EXTRA_LINES.find(([n])=>n===v.slice(2));
    delete l.item;delete l.st;l.ref=null;
    if(x){l.name=x[0];l.unit=x[1];}
  }else if(v==="custom"){
    delete l.item;delete l.st;l.ref=null;
    if(EXTRA_LINES.some(([n])=>n===l.name)||!l.name)l.name="";
  }else{
    delete l.item;delete l.st;l.ref=null;l.name="";
  }
}

function renderPrepPricing(body,city,id,b){
  const p=prepFor(id);
  const hist=priceHistory(id);
  const stRates=rateData[bidState(city,b)];
  const lineRow=(l,i)=>`<div class="prep-line" data-i="${i}">
      ${lineItemSelect(l,i,stRates)}
      <div class="pl-nums">
        <input class="input" data-f="qty" inputmode="decimal" value="${esc(String(l.qty??""))}" placeholder="Qty" aria-label="Quantity">
        <input class="input" data-f="unit" value="${esc(l.unit||"")}" placeholder="Unit" aria-label="Unit">
        <input class="input" data-f="price" inputmode="decimal" value="${esc(String(l.price??""))}" placeholder="$/unit" aria-label="Unit price">
        <span class="pl-total" data-total="${i}">${money0((Number(l.qty)||0)*(Number(l.price)||0))}</span>
        <button type="button" class="prep-x" data-del="${i}" aria-label="Delete line">×</button>
      </div>
      <small class="pl-ref" data-pc="${i}">${priceCheckHTML(l)}</small>
      ${l.note?`<small class="pl-ref rate-thin">${esc(l.note)}</small>`:""}
      ${historyHint(hist[lineKey(l)])}
    </div>`;
  const t=prepTotals(p);
  const rates=rateData[bidState(city,b)];
  const startNote=!rates?"Prices for this state aren't in the app yet."
    :`Starts from the quantities in the posting at ${esc(agencyOf(rates))}'s ${
      rates.basis==="awarded"?"average winning bid":(rates.wins||bidResults[rates.state])?"average winning bid where it's known, else its average bid":"average bid"}.`;
  const canRead=prepDocCandidates(b).length>0;
  const hasPast=pastPreparedBids(id).length>0;
  body.innerHTML=`${canRead?`<button class="btn-primary" id="pl-read" style="margin-bottom:0.6rem;">Read quantities from the bid form</button>`:""}
    ${hasPast?`<button class="btn-ghost" id="pl-past" style="margin:0 0 0.6rem;">Use a past bid</button>`:""}
    <div class="rate-note" style="margin-top:0;">${p.docInfo&&p.docInfo.source?`Bid documents read: ${esc(p.docInfo.source)}. `:""}${startNote} Change anything to your own numbers.</div>
    <div id="prep-lines">${p.lines.map(lineRow).join("")}</div>
    <div class="prep-add"><button class="btn-ghost" id="pl-add">+ Add line</button><button class="btn-ghost" id="pl-reset">Start over from ballpark</button></div>
    <div id="pt-target">${targetHTML(p,bidState(city,b))}</div>
    <div class="prep-totals">
      <div><span>Subtotal</span><b id="pt-sub">${money0(t.sub)}</b></div>
      <div><span>Markup <input class="input pt-mk" id="pt-mk" inputmode="decimal" value="${esc(String(p.markup||0))}" aria-label="Markup percent">%</span><b id="pt-mkv">${money0(t.markup)}</b></div>
      <div class="pt-grand"><span>Your bid</span><b id="pt-total">${money0(t.total)}</b></div>
    </div>
    ${b.value?`<div class="rate-note">Posted value: <b>${esc(b.value)}</b></div>`:""}
    <div class="rate-note">Add lines the posting doesn't list: removal, mobilization, traffic control, testing.</div>`;
  const refresh=()=>{
    const tt=prepTotals(p);
    p.lines.forEach((l,i)=>{
      const el=body.querySelector(`[data-total="${i}"]`);if(el)el.textContent=money0((Number(l.qty)||0)*(Number(l.price)||0));
      const pc=body.querySelector(`[data-pc="${i}"]`);if(pc)pc.innerHTML=priceCheckHTML(l);
    });
    document.getElementById("pt-sub").textContent=money0(tt.sub);
    document.getElementById("pt-mkv").textContent=money0(tt.markup);
    document.getElementById("pt-total").textContent=money0(tt.total);
    const tab=document.getElementById("prep-tab-total");
    if(tab)tab.textContent=tt.total?money0(tt.total):"";
    const tg=document.getElementById("pt-target");
    if(tg)tg.innerHTML=targetHTML(p,bidState(city,b));
  };
  body.querySelectorAll(".prep-line input").forEach(inp=>inp.oninput=()=>{
    const i=Number(inp.closest(".prep-line").dataset.i),f=inp.dataset.f;
    let v=inp.value;
    if(f==="qty"||f==="price")v=v.replace(/[^0-9.]/g,"");
    p.lines[i][f]=v;
    savePrep(id,city);refresh();
  });
  body.querySelectorAll("[data-pick]").forEach(sel=>sel.onchange=()=>{
    const i=Number(sel.dataset.pick);
    applyLineChoice(p.lines[i],sel.value,stRates);
    savePrep(id,city);openPrep(city,id,"pricing");
    if(sel.value==="custom")setTimeout(()=>{const inp=document.querySelector(`.prep-line[data-i="${i}"] .pl-name`);if(inp)inp.focus();},0);
  });
  body.querySelectorAll("[data-del]").forEach(x=>x.onclick=()=>{
    p.lines.splice(Number(x.dataset.del),1);
    if(!p.lines.length)p.lines.push({name:"",qty:"",unit:"",price:"",ref:null});
    savePrep(id,city);openPrep(city,id,"pricing");
  });
  const past=document.getElementById("pl-past");
  if(past)past.onclick=()=>renderPastBidPicker(body,city,id,b);
  const rd=document.getElementById("pl-read");
  if(rd)rd.onclick=()=>renderDocPicker(body,city,id,b);
  document.getElementById("pl-add").onclick=()=>{
    p.lines.push({name:"",qty:"",unit:"",price:"",ref:null});
    savePrep(id,city);openPrep(city,id,"pricing");
  };
  document.getElementById("pl-reset").onclick=()=>{
    if(!confirm("Replace your lines with the ballpark from the posting?"))return;
    p.lines=defaultPrepLines(b,city);
    savePrep(id,city);openPrep(city,id,"pricing");
  };
  document.getElementById("pt-mk").oninput=(e)=>{
    p.markup=e.target.value.replace(/[^0-9.]/g,"");
    savePrep(id,city);refresh();
  };
}

// Plain text of the summary -- what "Copy" puts on the clipboard and the
// printable page is built from, so the two can't disagree.
function prepSummaryData(id,b,city){
  const p=prepFor(id);
  const lines=(p.lines||[]).filter(l=>(l.name||"").trim()||Number(l.qty)||Number(l.price));
  return{company:companyProfile||{},b,city,lines,t:prepTotals(p),markup:Number(p.markup)||0,addenda:p.addenda||""};
}
function prepSummaryText(s){
  const c=s.company,out=[];
  out.push(`BID SUMMARY: ${s.b.title||""}`);
  if(s.city)out.push(`Location: ${s.city}`);
  if(s.b.bid_number)out.push(`Bid number: ${s.b.bid_number}`);
  if(s.b.deadline)out.push(`Due: ${s.b.deadline}`);
  out.push("");
  out.push(`Bidder: ${c.name||"(company name)"}`);
  if(c.contact)out.push(`Contact: ${c.contact}`);
  if(c.phone)out.push(`Phone: ${c.phone}`);
  if(c.email)out.push(`Email: ${c.email}`);
  out.push("");
  s.lines.forEach((l,i)=>out.push(`${i+1}. ${l.name||"Item"}: ${l.qty||0} ${l.unit||""} x ${money2(l.price)} = ${money2((Number(l.qty)||0)*(Number(l.price)||0))}`));
  out.push("");
  out.push(`Subtotal: ${money2(s.t.sub)}`);
  if(s.markup)out.push(`Markup (${s.markup}%): ${money2(s.t.markup)}`);
  out.push(`TOTAL BID: ${money2(s.t.total)}`);
  if(s.addenda)out.push(`Addenda acknowledged: ${s.addenda}`);
  return out.join("\n");
}
function renderPrepSummary(body,city,id,b){
  const p=prepFor(id);
  const s=prepSummaryData(id,b,city);
  const c=s.company;
  const canFill=prepDocCandidates(b).some(d=>d.name!=="Original posting");
  const dates=bidDates(b,p),info=bidInfoCount();
  body.innerHTML=`<div class="prep-summary">
      <div class="ps-co">${c.name?`<b>${esc(c.name)}</b>`:`<span class="rate-thin">Add your company name in Account so it prints here.</span>`}
        ${[c.contact,c.phone,c.email].filter(Boolean).map(esc).join(" · ")}</div>
      <table class="ps-table"><thead><tr><th>Item</th><th>Qty</th><th>Unit price</th><th>Amount</th></tr></thead><tbody>
        ${s.lines.map(l=>`<tr><td>${esc(l.name||"Item")}</td><td>${esc(String(l.qty||0))} ${esc(l.unit||"")}</td><td>${money2(l.price)}</td><td>${money2((Number(l.qty)||0)*(Number(l.price)||0))}</td></tr>`).join("")
          ||`<tr><td colspan="4">No priced lines yet. Use the Pricing tab.</td></tr>`}
      </tbody></table>
      <div class="prep-totals">
        <div><span>Subtotal</span><b>${money2(s.t.sub)}</b></div>
        ${s.markup?`<div><span>Markup (${esc(String(s.markup))}%)</span><b>${money2(s.t.markup)}</b></div>`:""}
        <div class="pt-grand"><span>Total bid</span><b>${money2(s.t.total)}</b></div>
      </div>
      <div class="detail-label" style="margin-top:0.8rem;">Addenda acknowledged</div>
      <input class="input" id="ps-addenda" value="${esc(p.addenda||"")}" placeholder="e.g. #1, #2 (or none)">
    </div>
    <div class="act-primary">
      ${canFill?`<button class="ma-gold" id="ps-fill">Fill in the agency's bid form</button>`:""}
      <button class="${canFill?"ma-ghost":"ma-gold"}" id="ps-print">Print / save PDF</button>
      <button class="ma-ghost" id="ps-copy">Copy as text</button>
    </div>
    <div class="rate-note">Most agencies only accept their own form, signed by you.${canFill?" Filling it in puts your details and prices into its boxes; you check it and sign.":" Copy these onto it."}</div>
    <div class="workspace-title" style="margin-top:1.2rem;">Next steps</div>
    <div class="ps-steps">
      <a class="btn-ghost" id="ps-bond" href="${esc(bondRequestMail(b,p,city))}">Ask for a bid bond</a>
      <button class="btn-ghost" id="ps-concrete">Get a concrete quote</button>
      ${b.email?`<a class="btn-ghost" id="ps-question" href="${esc(agencyQuestionMail(b))}">Ask the agency a question</a>`:""}
      ${dates.length?`<button class="btn-ghost" id="ps-dates">Add ${dates.length>1?`${dates.length} dates`:"the due date"} to my calendar</button>`:""}
    </div>
    <div class="rate-note">${info>=BID_INFO_FIELDS.length?"":`${info?`${info} of ${BID_INFO_FIELDS.length}`:"No"} bid details saved. `}<a href="#" id="ps-profile">${info?"Edit":"Add"} bid details</a>: your address, license, bonding agent and supplier, so these fill themselves in.</div>`;
  document.getElementById("ps-addenda").oninput=(e)=>{p.addenda=e.target.value.slice(0,200);savePrep(id,city);};
  document.getElementById("ps-copy").onclick=async()=>{
    try{await navigator.clipboard.writeText(prepSummaryText(prepSummaryData(id,b,city)));toast("Summary copied");}
    catch(e){toast("Couldn't copy. Use Print instead");}
  };
  document.getElementById("ps-print").onclick=()=>printPrepSummary(prepSummaryData(id,b,city));
  const fill=document.getElementById("ps-fill");
  if(fill)fill.onclick=()=>renderFormFill(body,city,id,b);
  document.getElementById("ps-concrete").onclick=()=>renderConcretePanel(body,city,id,b);
  const dl=document.getElementById("ps-dates");
  if(dl)dl.onclick=()=>downloadBidDates(b,p,city,id);
  document.getElementById("ps-profile").onclick=(e)=>{e.preventDefault();openBidInfo(()=>openPrep(city,id,"summary"));};
}
function printPrepSummary(s){
  const c=s.company;
  const html=`<!doctype html><html><head><meta charset="utf-8"><title>Bid summary - ${esc(s.b.title||"")}</title>
<style>body{font-family:Arial,Helvetica,sans-serif;color:#111;margin:32px;font-size:13px;}h1{font-size:18px;margin:0 0 4px;}
.m{color:#444;margin:2px 0;}table{width:100%;border-collapse:collapse;margin-top:16px;}th,td{border:1px solid #bbb;padding:6px 8px;text-align:left;}
th{background:#f0f0f0;}td:nth-child(n+2),th:nth-child(n+2){text-align:right;}.t{margin-top:12px;text-align:right;}.t div{margin:3px 0;}
.g{font-size:16px;font-weight:bold;}.n{margin-top:24px;color:#666;font-size:11px;}</style></head><body>
<h1>Bid summary: ${esc(s.b.title||"")}</h1>
${s.city?`<div class="m">Location: ${esc(s.city)}</div>`:""}${s.b.bid_number?`<div class="m">Bid number: ${esc(s.b.bid_number)}</div>`:""}
${s.b.deadline?`<div class="m">Due: ${esc(s.b.deadline)}</div>`:""}
<p><b>${esc(c.name||"")}</b><br>${[c.contact,c.phone,c.email].filter(Boolean).map(esc).join(" &middot; ")}</p>
<table><thead><tr><th>#</th><th>Item</th><th>Quantity</th><th>Unit price</th><th>Amount</th></tr></thead><tbody>
${s.lines.map((l,i)=>`<tr><td>${i+1}</td><td style="text-align:left">${esc(l.name||"Item")}</td><td>${esc(String(l.qty||0))} ${esc(l.unit||"")}</td><td>${money2(l.price)}</td><td>${money2((Number(l.qty)||0)*(Number(l.price)||0))}</td></tr>`).join("")}
</tbody></table>
<div class="t"><div>Subtotal: ${money2(s.t.sub)}</div>${s.markup?`<div>Markup (${esc(String(s.markup))}%): ${money2(s.t.markup)}</div>`:""}
<div class="g">Total bid: ${money2(s.t.total)}</div>${s.addenda?`<div>Addenda acknowledged: ${esc(s.addenda)}</div>`:""}</div>
<div class="n">Prepared with CurbCall Pro. Working copy for transfer onto the agency's official bid form.</div>
<script>window.onload=function(){window.print();}<\/script></body></html>`;
  const w=window.open("","_blank");
  if(!w){toast("Allow pop-ups to print, or use Copy");return;}
  w.document.open();w.document.write(html);w.document.close();
}

// ── Bid paperwork: details every bid form asks for ──
// Entered once, kept with the company profile (company_profiles.bid_info),
// and used to fill the agency's form and write the bond and quote requests.
// No tax ID, signature or anything sworn: those stay with the contractor.
const BID_INFO_FIELDS=[
  ["address","Street address","123 Main St"],
  ["city_state_zip","City, state, ZIP","Aurora, MO 65605"],
  ["title","Your title, for signing","Owner"],
  ["license","License / registration numbers","State or city contractor numbers"],
  ["years","Years in business","12"],
  ["insurance","Insurance carrier and agent","Carrier, agent name and phone"],
  ["bond_company","Bonding company (surety)","Surety name"],
  ["bond_agent","Bonding agent","Agent's name"],
  ["bond_email","Bonding agent's email","agent@example.com"],
  ["supplier","Ready-mix supplier","Supplier name"],
  ["supplier_email","Ready-mix supplier's email","orders@example.com"],
];
function bidInfo(){return (companyProfile&&companyProfile.bid_info&&typeof companyProfile.bid_info==="object")?companyProfile.bid_info:{};}
function bidInfoCount(){const i=bidInfo();return BID_INFO_FIELDS.filter(([k])=>String(i[k]||"").trim()).length;}
function openBidInfo(after){
  const mc=document.getElementById("modal-content");
  const info=bidInfo();
  mc.innerHTML=`<div class="sheet-head"><h2>Bid paperwork details</h2>
      <div class="sheet-sub"><span class="chip">Entered once, used on every bid</span></div></div>
    <div class="rate-note" style="margin-top:0;">Fills the agency's bid form and your bond and concrete requests. Leave out anything you'd rather type yourself. Tax IDs, signatures and notary blocks are never filled in.</div>
    ${BID_INFO_FIELDS.map(([k,label,ph])=>`<div class="field-label">${esc(label)}</div>
      <input class="input bi-field" data-k="${esc(k)}" placeholder="${esc(ph)}" value="${esc(info[k]||"")}" style="margin-bottom:0.6rem;">`).join("")}
    <div class="modal-actions"><button class="ma-gold" id="bi-save">Save</button><button class="ma-ghost" id="bi-cancel">Cancel</button></div>`;
  document.getElementById("bi-save").onclick=()=>{
    const next={};
    mc.querySelectorAll(".bi-field").forEach(f=>{const v=f.value.trim().slice(0,300);if(v)next[f.dataset.k]=v;});
    companyProfile.bid_info=next;
    store.set("company_profile",companyProfile);
    pushCompanyProfile();
    toast("Bid details saved");
    if(after)after();else closeModal();
  };
  document.getElementById("bi-cancel").onclick=()=>{if(after)after();else closeModal();};
  document.getElementById("modal-back").classList.add("open");
  document.getElementById("modal").classList.add("open");
}

// ── Filling the agency's own bid form (server: /bid-documents/fill) ──
// "2. 4 in. concrete sidewalk" -> item 2, "4 in. concrete sidewalk".
function splitItemNo(name){
  const m=/^\s*([A-Za-z0-9-]{1,6})\.\s+(.+)$/.exec(String(name||""));
  return m?{item_no:m[1],description:m[2]}:{item_no:"",description:String(name||"").trim()};
}
function fillPayload(b,p){
  const c=companyProfile||{},i=bidInfo(),t=prepTotals(p);
  const now=new Date(),pad=n=>String(n).padStart(2,"0");
  // Lines carry the markup the way the total does, so the form's unit
  // prices add up to the bid it states.
  const k=1+(Number(p.markup)||0)/100;
  return{
    company:{name:c.name||"",contact:c.contact||"",phone:c.phone||"",email:c.email||"",
      title:i.title||"",address:i.address||"",city_state_zip:i.city_state_zip||"",license:i.license||"",years:i.years||""},
    bid:{number:b.bid_number||"",title:b.title||"",addenda:p.addenda||"",total:Math.round(t.total*100)/100,
      date:`${pad(now.getMonth()+1)}/${pad(now.getDate())}/${now.getFullYear()}`},
    lines:(p.lines||[]).filter(l=>(l.name||"").trim()).map(l=>{
      const s=splitItemNo(l.name),price=Math.round((Number(l.price)||0)*k*100)/100,qty=Number(l.qty)||0;
      return{...s,quantity:qty||"",unit:l.unit||"",unit_price:Number(l.price)?price:"",amount:qty&&Number(l.price)?Math.round(qty*price*100)/100:""};
    }),
  };
}
const FILL_ERRORS={not_fillable:"This form isn't a fillable PDF, so it can't be filled in automatically. Print the summary: its item numbers match the form.",
  nothing_matched:"None of the form's boxes matched your details. Print the summary and copy it over.",
  nothing_to_fill:"Add your company details and prices first.",rate_limited:"Daily limit for filling forms reached. Try again tomorrow."};
async function fillBidForm(url,payload){
  try{
    const token=await getSupabaseToken();
    const r=await fetchWithTimeout(SERVER+"/bid-documents/fill",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({key:licenseKey(),device_id:deviceId(),supabase_token:token,url,...payload})},120000);
    const d=await r.json();
    if(d&&d.ok)return d;
    const msg=d&&d.reason==="fetch_failed"?(FETCH_ERRORS[d.detail]||"Couldn't download that form.")
      :(d&&(FILL_ERRORS[d.reason]||DOC_ERRORS[d.reason]))||"Couldn't fill that form.";
    return{ok:false,msg};
  }catch(e){return{ok:false,msg:offlineOrServer()};}
}
function downloadBase64Pdf(b64,name){
  const bin=atob(b64),bytes=new Uint8Array(bin.length);
  for(let i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);
  const url=URL.createObjectURL(new Blob([bytes],{type:"application/pdf"}));
  const a=document.createElement("a");
  a.href=url;a.download=name;document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(url),4000);
}
function renderFormFill(body,city,id,b){
  const p=prepFor(id);
  const docs=prepDocCandidates(b).filter(d=>d.name!=="Original posting");
  // The document the schedule was read from is most likely the bid form.
  docs.sort((x,y)=>(y.name===(p.docInfo&&p.docInfo.source))-(x.name===(p.docInfo&&p.docInfo.source)));
  const missing=!(companyProfile&&companyProfile.name)||!bidInfoCount();
  body.innerHTML=`<div class="rate-note" style="margin-top:0;">Pick the bid form. Your company details and prices go into its boxes; signatures, notary blocks and tax IDs are left for you.</div>
    ${missing?`<div class="alert alert-amber"><span>Add your company and bid details first so there's something to fill in.</span></div>
      <button class="btn-ghost" id="ff-profile">Add bid details</button>`:""}
    ${docs.map((d,i)=>`<button class="btn-ghost doc-pick" data-doc="${i}">${esc(d.name)}</button>`).join("")||
      `<div class="account-status">No documents are linked to this bid.</div>`}
    <button class="btn-ghost" id="ff-cancel">Back to summary</button>`;
  const prof=document.getElementById("ff-profile");
  if(prof)prof.onclick=()=>openBidInfo(()=>openPrep(city,id,"summary"));
  document.getElementById("ff-cancel").onclick=()=>openPrep(city,id,"summary");
  body.querySelectorAll("[data-doc]").forEach(btn=>btn.onclick=async()=>{
    const d=docs[Number(btn.dataset.doc)];
    body.innerHTML=`<div class="account-status"><span class="spin"></span> Filling in ${esc(d.name)}…</div>`;
    const res=await fillBidForm(d.url,fillPayload(b,p));
    if(!document.getElementById("prep-body"))return;
    if(!res.ok){
      body.innerHTML=`<div class="alert alert-amber"><span>${esc(res.msg)}</span></div><button class="btn-ghost" id="ff-back">Back to summary</button>`;
      document.getElementById("ff-back").onclick=()=>openPrep(city,id,"summary");
      return;
    }
    const fname=`${String(b.title||"bid").replace(/[^a-z0-9]+/gi,"_").slice(0,40)}_bid_form_filled.pdf`;
    body.innerHTML=`<div class="workspace-title">Filled ${plural(res.filled.length,"box","boxes")} on ${esc(d.name)}</div>
      <table class="ps-table"><thead><tr><th>Box on the form</th><th>Filled with</th></tr></thead><tbody>
      ${res.filled.map(f=>`<tr><td>${esc(f.label)}</td><td>${esc(f.value)}</td></tr>`).join("")}</tbody></table>
      <div class="rate-note">${plural(res.left_blank,"box","boxes")} left blank: signatures, notary, tax ID, and anything you haven't entered. Check every box before you sign.</div>
      <div class="act-primary"><button class="ma-gold" id="ff-download">Download filled form</button></div>
      <button class="btn-ghost" id="ff-back" style="margin-top:0.5rem;">Back to summary</button>`;
    document.getElementById("ff-download").onclick=()=>downloadBase64Pdf(res.pdf_b64,fname);
    document.getElementById("ff-back").onclick=()=>openPrep(city,id,"summary");
  });
}

// ── Next steps: bond, concrete, questions, dates ──
// Each one writes an email in the contractor's own mail app for them to
// read and send; nothing is sent from here.
function mailtoUrl(to,subject,body){
  return`mailto:${encodeURIComponent(to||"")}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
}
function signOff(){
  const c=companyProfile||{};
  return[c.contact,bidInfo().title&&c.contact?bidInfo().title:"",c.name,c.phone,c.email].filter(Boolean).join("\n");
}
function bondRequestMail(b,p,city){
  const i=bidInfo(),t=prepTotals(p);
  const first=String(i.bond_agent||"").trim().split(/\s+/)[0];
  const body=[`${first?`Hi ${first},`:"Hello,"}`,"",
    "We're bidding the project below and need a bid bond.","",
    `Project: ${b.title||""}`,city?`Location: ${city}`:"",b.bid_number?`Bid number: ${b.bid_number}`:"",
    b.deadline?`Bid due: ${b.deadline}`:"",t.total?`Our bid: about ${money0(t.total)}`:"",
    `Bond required: ${(p.docInfo&&p.docInfo.bid_security)||"see the bid documents"}`,
    b.url?`Bid documents: ${b.url}`:"","","Thanks,",signOff()].filter((x,k,a)=>x!==""||a[k-1]!=="").join("\n");
  return mailtoUrl(i.bond_email,`Bid bond request: ${b.title||"upcoming bid"}`,body);
}
// Concrete volume from the priced lines: area items with a thickness in
// their name. Curb, ramps and anything else are listed for the supplier to
// size from the plans, never guessed.
function concreteYards(lines){
  const out=[],other=[];
  (lines||[]).forEach(l=>{
    const qty=Number(l.qty)||0,name=String(l.name||"").trim();
    if(!qty||!name)return;
    const m=/(\d+(?:\.\d+)?)\s*(?:in\b|in\.|inch|")/i.exec(name);
    const tIn=m?Number(m[1]):0;
    const area=l.unit==="sq yd"?qty*9:l.unit==="sq ft"?qty:0;
    if(area&&tIn>0&&tIn<=24)out.push({name,qty,unit:l.unit,cy:area*tIn/12/27});
    else if(/concrete|curb|gutter|ramp|sidewalk|walk|driveway|approach|median|slab|flatwork/i.test(name))other.push({name,qty,unit:l.unit});
  });
  return{out,other,total:out.reduce((s,x)=>s+x.cy,0)};
}
function concreteQuoteMail(b,p,city,waste){
  const i=bidInfo(),y=concreteYards(p.lines);
  const k=1+(Number(waste)||0)/100;
  const body=["Hello,","",
    `We're bidding ${b.title||"a concrete job"}${city?` in ${city}`:""}${b.deadline?` (bids due ${b.deadline})`:""} and would like a price on ready-mix.`,"",
    ...y.out.map(x=>`- ${x.name}: ${Math.round(x.qty).toLocaleString()} ${x.unit}, about ${x.cy.toFixed(1)} CY`),
    ...y.other.map(x=>`- ${x.name}: ${Math.round(x.qty).toLocaleString()} ${x.unit} (volume from the plans)`),
    "",y.total?`Total: about ${Math.ceil(y.total*k)} CY including ${Number(waste)||0}% waste.`:"",
    "Mix per the project specs. Start date to be set once the job is awarded.","",
    "Please include delivery and any short-load or minimum charges.","","Thanks,",signOff()].filter((x,j,a)=>x!==""||a[j-1]!=="").join("\n");
  return mailtoUrl(i.supplier_email,`Ready-mix quote: ${y.total?`~${Math.ceil(y.total*k)} CY, `:""}${b.title||""}`,body);
}
function agencyQuestionMail(b){
  const body=["Hello,","",`Regarding ${b.title||"the bid"}${b.bid_number?` (${b.bid_number})`:""}${b.deadline?`, due ${b.deadline}`:""}:`,"",
    "[Your question]","","Thank you,",signOff()].join("\n");
  return mailtoUrl(b.email,`Question: ${b.title||"bid"}${b.bid_number?` (${b.bid_number})`:""}`,body);
}
// Every date the bid's own words give: due, pre-bid meeting, questions.
function bidDates(b,p){
  const info=(p&&p.docInfo)||{},out=[];
  const add=(label,text)=>{const d=deadlineDate({deadline:text});if(d&&!out.some(x=>+x.date===+d&&x.label===label))out.push({label,date:d,text:String(text)});};
  if(b.deadline)add("Bid due",b.deadline);
  if(info.prebid)add(/mandatory/i.test(info.prebid)?"MANDATORY pre-bid meeting":"Pre-bid meeting",info.prebid);
  if(info.questions_due)add("Questions due",info.questions_due);
  return out;
}
function downloadBidDates(b,p,city,id){
  const events=bidDates(b,p);
  if(!events.length){toast("Couldn't read a calendar date for this bid");return;}
  const pad=n=>String(n).padStart(2,"0"),d8=d=>`${d.getFullYear()}${pad(d.getMonth()+1)}${pad(d.getDate())}`;
  const stamp=new Date().toISOString().replace(/[-:]/g,"").split(".")[0]+"Z";
  const lines=["BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//CurbCall Pro//Bid Dates//EN"];
  events.forEach((e,i)=>lines.push("BEGIN:VEVENT",`UID:${id}-${i}@curbcall.app`,`DTSTAMP:${stamp}`,
    `DTSTART;VALUE=DATE:${d8(e.date)}`,`SUMMARY:${icsEscape(`${e.label}: ${b.title||"Bid"}`)}`,
    `DESCRIPTION:${icsEscape(`${e.text}${city?" — "+city:""}`)}`,`LOCATION:${icsEscape(city||"")}`,
    "BEGIN:VALARM","TRIGGER:-P1D","ACTION:DISPLAY",`DESCRIPTION:${icsEscape(e.label)}`,"END:VALARM","END:VEVENT"));
  lines.push("END:VCALENDAR");
  const url=URL.createObjectURL(new Blob([lines.join("\r\n")],{type:"text/calendar"}));
  const a=document.createElement("a");
  a.href=url;a.download=(b.title||"bid").replace(/[^a-z0-9]+/gi,"_").slice(0,40)+"_dates.ics";
  document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
  toast(`${plural(events.length,"date")} downloaded for your calendar`);
}
function renderConcretePanel(body,city,id,b){
  const p=prepFor(id),y=concreteYards(p.lines);
  const waste=Number(store.get("concrete_waste_pct",5))||0;
  body.innerHTML=`<div class="workspace-title">Concrete for this bid</div>
    ${y.out.length?`<table class="ps-table"><thead><tr><th>Line</th><th>Quantity</th><th>Concrete</th></tr></thead><tbody>
      ${y.out.map(x=>`<tr><td>${esc(x.name)}</td><td>${Math.round(x.qty).toLocaleString()} ${esc(x.unit)}</td><td>${x.cy.toFixed(1)} CY</td></tr>`).join("")}</tbody></table>`
      :`<div class="account-status">No line gives an area and a thickness (like "4 in. sidewalk, sq yd"), so there's no volume to work out. The email still lists your lines.</div>`}
    ${y.other.length?`<div class="rate-note">Not counted, size from the plans: ${y.other.map(x=>esc(x.name)).join("; ")}.</div>`:""}
    <div class="rate-note est-width">Waste <input id="cq-waste" type="number" min="0" max="30" step="1" value="${esc(String(waste))}" aria-label="Waste percent"> %
      ${y.total?` · <b id="cq-total">${Math.ceil(y.total*(1+waste/100))} CY</b> to order`:""}</div>
    ${bidInfo().supplier_email?"":`<div class="rate-note">Add your supplier's email in <a href="#" id="cq-profile">bid details</a> and it's filled in for you.</div>`}
    <div class="act-primary"><a class="ma-gold" id="cq-send" href="${esc(concreteQuoteMail(b,p,city,waste))}">Write the quote request</a></div>
    <button class="btn-ghost" id="cq-back" style="margin-top:0.5rem;">Back to summary</button>`;
  const w=document.getElementById("cq-waste");
  w.oninput=()=>{
    const v=Math.max(0,Math.min(30,Number(w.value)||0));
    store.set("concrete_waste_pct",v);
    const tot=document.getElementById("cq-total");
    if(tot)tot.textContent=`${Math.ceil(y.total*(1+v/100))} CY`;
    document.getElementById("cq-send").href=concreteQuoteMail(b,p,city,v);
  };
  const prof=document.getElementById("cq-profile");
  if(prof)prof.onclick=(e)=>{e.preventDefault();openBidInfo(()=>{openPrep(city,id,"summary");});};
  document.getElementById("cq-back").onclick=()=>openPrep(city,id,"summary");
}

async function fillDetailRates(city,b){
  const st=bidState(city,b);
  if(!document.getElementById("detail-rates")||!st)return;
  const [d]=await Promise.all([loadRates(st),loadBidResults(st)]);
  // The sheet may have been closed or replaced while this loaded.
  const box=document.getElementById("detail-rates");
  if(!d||!box)return;
  const items=ratesForBid(b,d);
  const district=priceDistrict(d);
  const odds=oddsHTML(city,b,d,items,district);
  if(!items.length){box.innerHTML=odds;return;}
  const rows=items.map(i=>rateRow(d,i,district,false)).join("");
  if(!rows){box.innerHTML=odds;return;}
  box.innerHTML=`${odds}${ballparkHTML(b,d,district)}
    <div class="workspace-title" style="margin-top:1rem;">Going rates — ${esc(d.districts[district])}</div>
    ${rows}
    <div class="rate-note">${esc(agencyOf(d))} ${d.basis==="awarded"?"average winning prices":"state highway averages"}. <a href="#" id="detail-rates-all">Change district or see all items</a></div>
    ${competitorsHTML(d,items,district)}`;
  document.getElementById("detail-rates-all").onclick=(e)=>{e.preventDefault();openRates(d.state);};
  const w=document.getElementById("est-width");
  if(w)w.onchange=()=>{
    const v=Number(w.value);
    if(v>0&&v<=30){store.set(SIDEWALK_WIDTH_KEY,v);fillDetailRates(city,b);renderFeed();}
  };
}

// ── Home ──
// Every number here is something the app already has: the feed, the
// pipeline, approved reviews (public by RLS), the referral link. Nothing is
// invented to make the screen look busy -- a section with nothing real to
// show is hidden instead.
function homeGreeting(){
  const h=new Date().getHours();
  const part=h<12?"Morning":h<17?"Afternoon":"Evening";
  const name=String((companyProfile&&companyProfile.contact)||"").trim().split(/\s+/)[0];
  return name?`${part}, ${name}`:`Good ${part.toLowerCase()}`;
}
function homeSinceLabel(t){
  const d=new Date(t),now=new Date();
  const days=Math.floor((new Date(now.toDateString())-new Date(d.toDateString()))/86400000);
  if(days<=0)return "earlier today";
  if(days===1)return "yesterday";
  if(days<7)return d.toLocaleDateString([],{weekday:"long"});
  return d.toLocaleDateString([],{month:"short",day:"numeric"});
}
function renderHome(){
  const main=document.getElementById("home-main");
  if(!main)return;
  const rows=[];
  Object.keys(bidData).forEach(c=>visibleBidsIn(c).forEach(b=>rows.push([c,b])));
  const total=rows.length;
  const fresh=prevVisitAt?rows.filter(([c,b])=>(b._first_seen||0)>prevVisitAt).length:total;
  const closing=rows.filter(([c,b])=>{const d=daysUntil(b);return d!=null&&d>=0&&d<=7;}).length;
  const stats=pipelineStatsSummary();
  const where=String(store.get(HOME_LOC_KEY,"")||"").trim();

  document.getElementById("home-title").textContent=homeGreeting();
  document.getElementById("home-sub").textContent=prevVisitAt
    ?`Here's what changed${where?` near ${where}`:""} since ${homeSinceLabel(prevVisitAt)}.`
    :`Here's what's open${where?` near ${where}`:" near you"}.`;

  const soonest=rows
    .filter(([c,b])=>{const d=daysUntil(b);return d!=null&&d>=0;})
    .sort((a,b)=>daysUntil(a[1])-daysUntil(b[1]))
    .slice(0,3);

  main.innerHTML=`
    <div class="account-card">
      <div class="account-email hdr-ic" style="font-size:var(--fs-base);margin-bottom:0.6rem;"><svg class="icon-svg"><use href="#i-activity"/></svg>Your week</div>
      <div class="stats-grid">
        <div class="stat-box"><div class="n">${fresh}</div><div class="l">${prevVisitAt?"New bids":"Open bids"}</div></div>
        <div class="stat-box"><div class="n"${closing?' style="color:var(--red);"':""}>${closing}</div><div class="l">Closing in 7d</div></div>
        <div class="stat-box"><div class="n">${esc(stats.winRate)}</div><div class="l">Win rate</div></div>
        <div class="stat-box"><div class="n">${formatMoney(stats.trackedValue)}</div><div class="l">Tracked</div></div>
      </div>
      <button class="btn-primary" id="home-scan-btn" style="margin-top:0.2rem;">${total?"Scan for new bids":"Run your first scan"}</button>
    </div>
    ${outcomeCardHTML()}
    ${soonest.length?`<div class="feed-label">CLOSING SOON</div><div id="home-soon">${soonest.map(([c,b])=>bidCard(c,b)).join("")}</div>`:""}
    ${total?`<button class="btn-ghost" id="home-all-btn">See all ${plural(total,"open bid")}</button>`
      :emptyHTML("i-list","No bids yet","Run a scan and the work near you shows up here.")}`;

  document.getElementById("home-scan-btn").onclick=()=>goTo("scan");
  const all=document.getElementById("home-all-btn");
  if(all)all.onclick=()=>goTo("feed");
  const soon=document.getElementById("home-soon");
  if(soon)attachBidEvents(soon);
  const outs=document.getElementById("home-outcomes");
  if(outs)wireOutcomeCard(outs);
  loadHomeReviews();
  renderHomeRates();
  loadReferralCard("home-referral-body");
}
// Approved reviews only -- RLS exposes nothing else to this query, and the
// card stays hidden when there are none rather than showing an empty box.
// Fetched once per session; reviews don't change minute to minute.
let homeReviews=null;
async function loadHomeReviews(){
  const card=document.getElementById("home-reviews");
  if(!card)return;
  if(homeReviews===null&&sb&&!isOffline()){
    homeReviews=[];
    try{
      const{data,error}=await sb.from("reviews").select("rating,quote,display_name,company")
        .eq("approved",true).order("created_at",{ascending:false}).limit(2);
      if(!error&&Array.isArray(data))homeReviews=data.filter(r=>r&&String(r.quote||"").trim());
    }catch(e){/* no reviews card -- the rest of Home is unaffected */}
  }
  if(!homeReviews||!homeReviews.length){card.style.display="none";return;}
  const stars=n=>{const k=Math.max(0,Math.min(5,Math.round(+n||0)));return "★".repeat(k)+"☆".repeat(5-k);};
  card.innerHTML=`<div class="account-email hdr-ic" style="font-size:var(--fs-base);margin-bottom:0.4rem;"><svg class="icon-svg"><use href="#i-users"/></svg>From other contractors</div>
    ${homeReviews.map(r=>`<div class="home-review">
      <div class="home-stars" aria-label="${esc(String(r.rating))} out of 5">${stars(r.rating)}</div>
      <div class="home-quote">“${esc(r.quote)}”</div>
      <div class="home-by">${esc([r.display_name,r.company].filter(Boolean).join(" · "))}</div>
    </div>`).join("")}`;
  card.style.display="";
}

function renderFeed(){
  // Called unconditionally, before either of this function's own exit
  // points, so the Find screen's summary never drifts from what Bids
  // itself is about to show.
  renderScanSummary();
  // Home shows the same bids; keep it current when it's the screen on show.
  if(document.getElementById("screen-home")?.classList.contains("active"))renderHome();
  const tiles=document.getElementById("tiles-wrap");
  const list=document.getElementById("feed-list");
  const clearBtn=document.getElementById("clear-feed");
  const filterRow=document.getElementById("filter-row");
  const exportBtn=document.getElementById("export-feed-btn");
  const cities=Object.keys(bidData).filter(c=>visibleBidsIn(c).length);
  const total=cities.reduce((n,c)=>n+visibleBidsIn(c).length,0);
  document.getElementById("feed-sub").textContent=total
    ?`${plural(total,"open bid")} across ${plural(cities.length,"town")}. New scans add more.`
    :"Bids you've found. New scans add to this list.";
  clearBtn.style.display=total?"block":"none";
  filterRow.style.display=total?"flex":"none";
  exportBtn.style.display=total?"inline-flex":"none";
  if(!total){
    tiles.innerHTML="";
    list.innerHTML=emptyHTML("i-list","No bids yet","Tap Find to scan your area.");
    lastFeedRows=[];
    return;
  }
  if(cities.length>1){
    let html=`<div class="tiles"><div class="tile ${cityFilter==="All"?"active":""}" data-c="All"><div class="n">${total}</div><div class="l">All</div></div>`;
    cities.sort().forEach(c=>{const n=visibleBidsIn(c).length;html+=`<div class="tile ${cityFilter===c?"active":""}" data-c="${esc(c)}"><div class="n">${n}</div><div class="l">${esc(c)}</div></div>`;});
    html+=`</div>`;tiles.innerHTML=html;
    tiles.querySelectorAll(".tile").forEach(t=>t.onclick=()=>{cityFilter=t.dataset.c;renderFeed();});
  } else tiles.innerHTML="";
  let rows=[];
  cities.forEach(c=>{if(cityFilter==="All"||cityFilter===c)visibleBidsIn(c).forEach(b=>rows.push([c,b]));});
  const term=(document.getElementById("feed-search").value||"").trim().toLowerCase();
  if(term)rows=rows.filter(([c,b])=>(b.title||"").toLowerCase().includes(term)||(b.scope||"").toLowerCase().includes(term)||c.toLowerCase().includes(term));
  const sortMode=document.getElementById("feed-sort").value;
  if(sortMode==="deadline"){
    rows.sort((a,b)=>{const da=daysUntil(a[1]),db=daysUntil(b[1]);if(da==null)return 1;if(db==null)return-1;return da-db;});
  }else if(sortMode==="value"){
    rows.sort((a,b)=>parseValue(b[1].value)-parseValue(a[1].value));
  }else if(sortMode==="new"){
    // Sorted on when the bid first appeared in a scan. This used to be a bare
    // rows.reverse(), which just flipped city order and had nothing to do with
    // recency. Bids merged before _first_seen existed sort last.
    rows.sort((a,b)=>(b[1]._first_seen||0)-(a[1]._first_seen||0));
  }else if(sortMode==="near"){
    rows.sort((a,b)=>milesOf(a[1])-milesOf(b[1]));
  }else if(sortMode==="fewest"){
    // Plan-holder lists first (the posting's own count), then what state
    // jobs of the same kind drew; bids with neither go last, by fit.
    const key=([c,b])=>{const e=expectedBidders(c,b);return e?e.n:Infinity;};
    rows.sort((a,b)=>key(a)-key(b)||fitScore(b[1])-fitScore(a[1]));
  }else{
    // "Best Match". The server ranks each city's bids by fit, but it ranks
    // them PER CITY -- so at 25 miles, where a board held one or two towns,
    // leaving that order alone was right. At the 125-mile default a board
    // spans twenty towns and the city order is whatever the scan happened to
    // return, which put a job 120 miles out above one 8 miles away.
    //
    // Rank by fit here instead, mirroring the server's weighting: urgency
    // first, then distance, then whether it can be acted on. A bid closing in
    // three days forty miles out still beats one closing in a month next
    // door, because the near one will still be there tomorrow.
    rows.sort((a,b)=>fitScore(b[1])-fitScore(a[1]));
  }
  lastFeedRows=rows;
  list.innerHTML=`<div class="feed-label">OPEN BIDS</div>`+rows.map(([c,b])=>bidCard(c,b)).join("");
  if(!rows.length)list.innerHTML=emptyHTML("i-search","No matches","Try a different search term.");
  attachBidEvents(list);
}
byId("feed-search").oninput=()=>renderFeed();
byId("feed-sort").onchange=()=>renderFeed();
byId("export-feed-btn").onclick=()=>exportCSV(lastFeedRows,"curbcall-bids");

function csvEscape(v){v=String(v==null?"":v);if(/[",\n]/.test(v))return'"'+v.replace(/"/g,'""')+'"';return v;}
function exportCSV(rows,filename){
  if(!rows||!rows.length){toast("Nothing to export yet");return;}
  // Deliberately NOT exported: b.plan_holders. Those are named individuals'
  // business contacts read off a government plan-holder list, and they belong
  // on the card for the job they are bidding -- shown to someone deciding
  // whether to call about that job, not handed over as a spreadsheet to mail
  // in bulk. Do not add them to this header.
  const header=["City","Title","Scope","Est. Value","Deadline","Contact","Email","Phone","Source URL","Status"];
  const lines=[header.map(csvEscape).join(",")];
  rows.forEach(([city,b])=>{
    const id=bidId(city,b);
    lines.push([city,b.title,b.scope,b.value,b.deadline,b.contact,b.email,b.phone,b.url,pipeline[id]||""].map(csvEscape).join(","));
  });
  const blob=new Blob([lines.join("\n")],{type:"text/csv"});
  const url=URL.createObjectURL(blob);
  const a=document.createElement("a");
  a.href=url;a.download=stampedName(filename,"csv");document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
  toast("Exported "+plural(rows.length,"bid"));
}

function bidCard(city,b){
  const id=bidId(city,b);const isSaved=saved[id];
  const dleft=daysUntil(b);
  const barClass=dleft!=null&&dleft<=2?"urgent":dleft!=null&&dleft<=7?"soon":"";
  // Deadline urgency is the single most decision-relevant thing on this
  // card -- give it real visual weight (bold, slightly larger) instead of
  // just a color tint, so it doesn't have to compete equally with chips
  // that are just labels (city, "OPEN").
  const urgent=dleft!=null&&dleft<=7;
  const deadlineChip=b.deadline
    ?`<span class="chip" style="${dleft!=null&&dleft<=2?'color:var(--red);':dleft!=null&&dleft<=7?'color:var(--accent);':''}${urgent?'font-weight:800;font-size:var(--fs-sm);':''}">${esc(b.deadline)}${dleft!=null&&dleft>=0?` \u2022 ${dleft===0?"today":dleft+"d left"}`:""}</span>`
    :"";
  return `<div class="bid" data-id="${esc(id)}" data-city="${esc(city)}">
    <div class="bid-bar ${barClass}" style="${barClass?"":"background:var(--green)"}"></div>
    <div class="bid-body">
      <div class="bid-top">
        <span class="bid-title">${esc(b.title||"Untitled Project")}</span>
        <div class="bid-actions">
          <span class="star ${isSaved?"on":""}" data-star="${esc(id)}" data-city="${esc(city)}">${isSaved?"\u2605":"\u2606"}</span>
          <button type="button" class="bid-remove" data-remove="${esc(id)}" data-city="${esc(city)}" aria-label="Remove this bid" title="Remove">&times;</button>
        </div>
      </div>
      ${b.scope?`<div class="bid-scope">${esc(b.scope)}</div>`:""}
      <div class="bid-meta">
        <span class="chip">${esc(city)}${Number.isFinite(b.miles)?` \u00b7 ${b.miles} mi`:""}</span>
        ${b.value?`<span class="chip value">${esc(b.value)}</span>`:""}
        ${ballparkChip(city,b)}
        ${competitionChip(city,b)}
        ${deadlineChip}
        ${watchAlert(id)?`<span class="chip watch-alert">${esc(watchAlert(id))}</span>`:""}
        ${pipeline[id]?`<span class="chip status-${esc(pipeline[id])}">${esc(String(pipeline[id]).toUpperCase())}</span>`:""}
        ${notes[id]?`<span class="chip">Note</span>`:""}
        ${(()=>{const pg=bidPrep[id]&&prepProgress(id,b);return pg?`<span class="chip">Prep ${pg.done}/${pg.total}</span>`:"";})()}
      </div>
    </div>
  </div>`;
}

function attachBidEvents(container){
  container.querySelectorAll(".bid").forEach(card=>{
    card.onclick=(e)=>{if(e.target.closest(".star,.bid-remove"))return;const b=findBid(card.dataset.id);if(b)openDetail(card.dataset.city,b);};
  });
  container.querySelectorAll(".star").forEach(s=>{
    s.onclick=(e)=>{e.stopPropagation();toggleSave(s.dataset.city,s.dataset.star);};
  });
  container.querySelectorAll(".bid-remove").forEach(btn=>{
    btn.onclick=(e)=>{e.stopPropagation();removeBid(btn.dataset.city,btn.dataset.remove);};
  });
}
function findBid(id){
  for(const c in bidData){const b=bidData[c].find(x=>bidId(c,x)===id);if(b)return b;}
  return saved[id]||null;
}
function toggleSave(city,id){
  if(saved[id]){delete saved[id];deleteSavedBidCloud(id);}
  else{const b=findBid(id);if(b){saved[id]={...b,_city:city};pushSavedBid(id,city);}}
  store.set("saved",saved);
  renderFeed();
  if(document.getElementById("screen-saved").classList.contains("active"))renderSaved();
}
// Hides one bid from the feed for good -- not just today's render. A
// rescan of that city replaces its whole list from the server (see
// mergeOpenBids), so without persisting this a bid the server still calls
// open would just quietly reappear next time that city gets rescanned.
// queueFeedPush() is what makes "for good" true on every device the account
// is signed into, not just this one -- every other write to bidData/
// upcomingData/leadsData/leadStatus already calls it after store.set(),
// this was the one omitted, so a bid removed on one device kept showing up
// on the others.
function removeBid(city,id){
  const b=findBid(id);
  dismissed[id]=true;
  store.set("dismissed",dismissed);
  queueFeedPush();
  renderFeed();
  toast(b&&b.title?`Removed: ${b.title}`:"Removed");
}
// Who is bidding this job as prime -- i.e. who needs a concrete sub. Only
// state lettings carry this; a city posting names the buyer, not the bidders.
//
// Shown here and only here, on the card for the job they are bidding. These
// are real people's work emails off a government plan-holder list, so they
// are not in the CSV export and must not be (see exportCSV).
// A posting URL is often 120 characters of folders and percent-encoding.
// What a contractor needs to know is whose site it is -- they recognise
// their own city's domain -- and the file at the end of it.
function prettyUrl(u){
  try{
    const p=new URL(u);
    const host=p.hostname.replace(/^www\./,"");
    const last=decodeURIComponent(p.pathname.split("/").filter(Boolean).pop()||"");
    if(!last)return host;
    return host+" / "+(last.length>44?last.slice(0,44)+"\u2026":last);
  }catch(e){ return String(u||""); }
}

// Bid documents come in two shapes. A city posting yields bare URLs, because
// all we can do is spot links that look like a packet. A state letting row
// yields {name, url} -- the table's own column headings name each file, so
// the card can say "Bid Book" and "Plans" instead of "Document 1".
function docRow(b){
  const ds=(b&&b.documents)||[];
  if(!ds.length)return"";
  const links=ds.slice(0,5).map((d,i)=>{
    const url=safeUrl(typeof d==="string"?d:(d&&d.url));
    if(!url)return"";
    const name=(d&&d.name)?String(d.name):("Document "+(i+1));
    return `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(name)}</a>`;
  }).filter(Boolean).join(" &middot; ");
  if(!links)return"";
  return `<div class="detail-row"><div class="detail-label">Bid Documents</div>
    <div class="detail-val">${links}</div></div>`;
}

// ── Sub quotes to the primes ──
// On a job too big (or a state job needing prequalification) for a concrete
// crew to bid as prime, the way in is pricing the concrete for the
// contractors who pulled plans. This writes that quote from the
// contractor's own Prepare-bid prices -- one email per prime, sent from
// their own mail app -- and remembers who has been sent one. For Missouri,
// each prime's record on state flatwork jobs comes from MoDOT's tabulations.
function companyKey(name){
  return String(name||"").toLowerCase().replace(/&/g," and ")
    .replace(/\b(inc|llc|l\.l\.c|co|corp|corporation|company|ltd|the)\b\.?/g," ")
    .replace(/[^a-z0-9]+/g," ").trim();
}
// {bids, wins} on state flatwork jobs for a company, from named bid results.
function primeRecord(name,st){
  const res=bidResults[st];
  if(!res||res.named===false)return null;
  const k=companyKey(name);
  if(!k)return null;
  let bids=0,wins=0;
  res.contracts.forEach(c=>c.bidders.forEach(([n],r)=>{if(companyKey(n)===k){bids++;if(r===0)wins++;}}));
  return bids?{bids,wins}:null;
}
const SUB_QUOTE_TERMS="Includes labor, material and equipment for the items listed. Excludes traffic control, bonds, permits and testing unless listed. Quantities per the plans; unit prices apply to final measured quantities. Good for 30 days.";
function subQuoteLines(p){
  const k=1+(Number(p.markup)||0)/100;
  return (p.lines||[]).filter(l=>(l.name||"").trim()&&Number(l.price)>0)
    .map(l=>({name:l.name,qty:Number(l.qty)||0,unit:l.unit||"",price:Math.round(Number(l.price)*k*100)/100}));
}
function subQuoteText(b,p,city,to){
  const c=companyProfile||{},lines=subQuoteLines(p);
  const total=lines.reduce((s,l)=>s+l.qty*l.price,0);
  const first=String((to&&to.contact)||"").trim().split(/\s+/)[0];
  return[`${first?`Hi ${first},`:"Hello,"}`,"",
    `${c.name||"We"} would like to quote the concrete flatwork on ${b.title||"this job"}${b.bid_number?` (${b.bid_number})`:""}${city?`, ${city}`:""}${b.deadline?`, bids due ${b.deadline}`:""}.`,"",
    ...lines.map(l=>`- ${l.name}: ${l.qty?`${l.qty.toLocaleString()} ${l.unit} at `:""}${money2(l.price)}${l.unit?`/${l.unit}`:""}${l.qty?` = ${money2(l.qty*l.price)}`:""}`),
    "",total?`Total for the items listed: ${money2(total)}`:"","",
    p.subTerms||SUB_QUOTE_TERMS,"","Happy to adjust to your item list. Thanks,",signOff()]
    .filter((x,j,a)=>x!==""||a[j-1]!=="").join("\n");
}
function renderSubQuote(city,id){
  const b=findBid(id)||saved[id];
  if(!b)return;
  // Read without creating: looking at this screen doesn't start a workspace.
  const p=bidPrep[id]||{lines:[]},st=bidState(city,b);
  const lines=subQuoteLines(p);
  const holders=otherHolders(b);
  const mc=document.getElementById("modal-content");
  const sent=p.quotes||{};
  const rows=holders.map((h,i)=>({h,i,rec:primeRecord(h.company,st)}))
    .sort((a,b)=>((b.rec&&b.rec.wins)||0)-((a.rec&&a.rec.wins)||0));
  mc.innerHTML=`<div class="sheet-head"><h2>Quote the primes</h2>
      <div class="sheet-sub"><span class="chip">${esc(b.title||"Untitled")}</span>${b.deadline?`<span class="chip">Due ${esc(b.deadline)}</span>`:""}</div></div>
    <a href="#" class="prep-back" id="sq-back">← Bid details</a>
    ${lines.length?`<div class="workspace-title">Your quote</div>
      <table class="ps-table"><thead><tr><th>Item</th><th>Qty</th><th>Unit price</th></tr></thead><tbody>
      ${lines.map(l=>`<tr><td>${esc(l.name)}</td><td>${l.qty?`${esc(l.qty.toLocaleString())} ${esc(l.unit)}`:"—"}</td><td>${money2(l.price)}</td></tr>`).join("")}</tbody></table>
      <div class="rate-note">From your Prepare-bid prices${Number(p.markup)?`, with your ${esc(String(p.markup))}% markup`:""}. <a href="#" id="sq-edit">Change prices</a></div>
      <div class="detail-label" style="margin-top:0.8rem;">Terms</div>
      <textarea class="input" id="sq-terms" rows="3" style="resize:vertical;">${esc(p.subTerms||SUB_QUOTE_TERMS)}</textarea>`
      :`<div class="alert alert-amber"><span>Price your items first: the quote is built from your Prepare-bid prices.</span></div>
      <button class="btn-primary" id="sq-edit">Price this job</button>`}
    ${lines.length?`<div class="workspace-title" style="margin-top:1rem;">Send it to the primes</div>
      ${rows.map(({h,i,rec})=>{
        const k=companyKey(h.company),mail=h.email?safeUrl(mailtoUrl(h.email,`Concrete flatwork quote: ${b.title||""}`,subQuoteText(b,p,city,h))):"";
        const tel=h.phone?safeUrl("tel:"+String(h.phone).replace(/[^\d+]/g,"")):"";
        return`<div class="sq-prime">
          <div><b>${esc(h.company||"Plan holder")}</b>${h.contact?`<small>${esc(h.contact)}</small>`:""}
            ${rec?`<small class="sq-rec">${rec.wins} win${rec.wins===1?"":"s"} in ${plural(rec.bids,"state flatwork bid")} (MoDOT)</small>`:""}
            ${sent[k]?`<small class="sq-sent">Quote sent ${esc(new Date(sent[k]).toLocaleDateString([],{month:"short",day:"numeric"}))}</small>`:""}</div>
          <div class="sq-ways">${mail?`<a class="btn-ghost" data-sq="${i}" href="${esc(mail)}">${sent[k]?"Email again":"Email quote"}</a>`:""}
            ${tel?`<a class="btn-ghost" href="${esc(tel)}">Call</a>`:""}</div></div>`;}).join("")
        ||`<div class="account-status">No plan holders are listed for this bid yet. Copy the quote and send it to whoever is bidding.</div>`}
      <div class="act-primary"><button class="ma-ghost" id="sq-copy">Copy quote</button></div>
      <div class="rate-note">Each email opens in your own mail app for you to read and send. Plan holders are from the agency's list for this job.</div>`:""}`;
  document.getElementById("sq-back").onclick=(e)=>{e.preventDefault();openDetail(city,b);};
  const ed=document.getElementById("sq-edit");
  if(ed)ed.onclick=(e)=>{e.preventDefault();openPrep(city,id,"pricing");};
  const terms=document.getElementById("sq-terms");
  if(terms)terms.onchange=()=>{prepFor(id).subTerms=terms.value.trim().slice(0,800);savePrep(id,city);renderSubQuote(city,id);};
  mc.querySelectorAll("[data-sq]").forEach(a=>a.addEventListener("click",()=>{
    const h=holders[Number(a.dataset.sq)];
    const w=prepFor(id);
    w.quotes=w.quotes||{};w.quotes[companyKey(h.company)]=Date.now();
    savePrep(id,city);
    setTimeout(()=>renderSubQuote(city,id),300);
  }));
  const cp=document.getElementById("sq-copy");
  if(cp)cp.onclick=async()=>{
    try{await navigator.clipboard.writeText(subQuoteText(b,p,city,null));toast("Quote copied");}
    catch(e){toast("Couldn't copy on this device");}
  };
}

function holderBlock(b){
  const hs=(b&&b.plan_holders)||[];
  if(!hs.length)return"";
  const rows=hs.slice(0,12).map(h=>{
    const mail=h.email?safeUrl("mailto:"+h.email):"";
    const tel=h.phone?safeUrl("tel:"+String(h.phone).replace(/[^\d+]/g,"")):"";
    return `<div class="holder">
      <div class="co">${esc(h.company||"")}</div>
      ${h.contact?`<div class="who">${esc(h.contact)}</div>`:""}
      <div class="ways">
        ${tel?`<a href="${esc(tel)}">Call</a>`:""}
        ${mail?`<a href="${esc(mail)}">Email</a>`:""}
      </div></div>`;
  }).join("");
  return `<div class="holders">
    <div class="holders-h">Bidding this job &mdash; ${hs.length} contractor${hs.length===1?"":"s"}</div>
    ${rows}
    <div class="who" style="margin-top:0.1rem;">Pulled from the agency's plan holder list. These are the primes;
    they need someone to price the concrete.</div>
    ${otherHolders(b).length?`<button class="btn-primary" id="sub-quote-open" style="margin-top:0.6rem;">Send my sub quote to the primes</button>`:""}
  </div>`;
}

function openDetail(city,b){
  const mc=document.getElementById("modal-content");
  const id=bidId(city,b);
  const watchMsg=watchAlert(id);
  if(watchMsg)setTimeout(()=>{markWatchSeen(id);renderFeed();},0);
  const pStatus=pipeline[id]||"";
  function row(label,val,link){
    if(!val)return"";
    // Falls back to plain text when the URL is not one we will open.
    const href=safeUrl(link);
    const v=href?`<a href="${esc(href)}">${esc(val)}</a>`:esc(val);
    return`<div class="detail-row"><div class="detail-label">${label}</div><div class="detail-val">${v}</div></div>`;
  }
  const statuses=[["submitted","Submitted"],["won","Won"],["lost","Lost"],["passed","Passed"]];
  // This chip used to be the literal text OPEN, so every bid detail claimed
  // to be open no matter what the server said -- including ones the feed had
  // already worked out were closed.
  const live=isOpen(b);
  const statusLabel=(b.status||(live?"Open":"Closed")).toUpperCase();
  // A notice we have no link for is still worth chasing, so offer the search
  // a person would run rather than rendering nothing. Never a guessed deep
  // link: a plausible-looking URL that 404s is worse than no URL.
  const findUrl="https://www.google.com/search?q="+encodeURIComponent(
    `"${b.title||""}" ${city||""} bid`.trim());
  const dleft=daysUntil(b);
  // Urgency belongs on the screen where the bid/no-bid call gets made, not
  // only on the feed card. "9/3/26" told a contractor nothing about whether
  // they had a fortnight or two days.
  const urg=dleft==null?"":dleft<0?"urgent":dleft<=2?"urgent":dleft<=7?"soon":"";
  const dnote=dleft==null?"":dleft<0?"closed":dleft===0?"closes today"
    :dleft===1?"1 day left":dleft+" days left";
  const dcol=dleft==null?"var(--text2)":dleft<=2?"var(--red)"
    :dleft<=7?"var(--accent)":"var(--green)";
  // Guard on the field, not just on safeUrl. safeUrl("tel:") returns "tel:"
  // -- the scheme test passes on a bare scheme with nothing after it -- so a
  // bid with no phone rendered a gold "Call" button wired to a dead link.
  // The same was already true of Email: every bid without an address has
  // been showing an Email button that opens a blank compose window.
  const tel=b.phone?safeUrl("tel:"+b.phone):"";
  const mail=b.email?safeUrl("mailto:"+b.email):"";
  const post=safeUrl(b.url);
  mc.innerHTML=`<div class="sheet-head">
      <h2>${esc(b.title||"Untitled Project")}</h2>
      <div class="sheet-sub">
        <span class="chip">${esc(city)}${Number.isFinite(b.miles)?` \u00b7 ${b.miles} mi`:""}</span>
        <span class="chip"${live?"":' style="color:var(--red);"'}>${esc(statusLabel)}</span>
      </div>
    </div>
    <div class="facts">
      <div class="fact ${urg}">
        <div class="k">Closes</div>
        <div class="v">${esc(b.deadline||"\u2014")}</div>
        ${dnote?`<div class="n" style="color:${dcol};">${dnote}</div>`
          // A third of postings never state a closing date. A bare dash reads
          // as a bug; saying so, and saying where to look, does not.
          :(b.deadline?"":`<div class="n" style="color:var(--text2);">not stated \u2014 check the posting</div>`)}
      </div>
      <div class="fact">
        <div class="k">Distance</div>
        <div class="v">${Number.isFinite(b.miles)?b.miles+" mi":"\u2014"}</div>
        <div class="n" style="color:var(--text2);">from you</div>
      </div>
      <div class="fact">
        <div class="k">Est. Value</div>
        <div class="v">${b.value?esc(b.value):"\u2014"}</div>
        <div class="n" style="color:var(--text2);">${b.value?"stated":"not listed"}</div>
      </div>
    </div>
    ${b.prebid?`<div class="alert ${b.prebid==="mandatory"?"alert-red":"alert-amber"}">
      <span>${b.prebid==="mandatory"
        ?"<b>Mandatory pre-bid meeting.</b> You cannot bid without attending \u2014 check the posting for date and place."
        :"<b>Pre-bid meeting.</b> Check the posting for date and place."}</span></div>`:""}
    ${b.addenda?`<div class="alert alert-amber">
      <span><b>This bid has addenda.</b> Read them before pricing \u2014 the scope may have changed.</span></div>`:""}
    ${b.source==="state_dot"?`<div class="alert alert-amber">
      <span><b>State highway job.</b> Most states require prequalification to bid
      one of these as prime, and the whole contract is usually bigger than a
      concrete crew takes on alone. The usual way in is as a sub: the letting
      page lists the contractors who pulled plans, and those are the people
      who need your ramps and sidewalks priced.${b.also_in&&b.also_in.length
        ?` This job also covers ${esc(b.also_in.slice(0,4).map(titleCase).join(", "))}${b.also_in.length>4?" and more":""}.`
        :""}</span></div>`:""}
    ${(()=>{const pg=prepProgress(id,b);return`<button class="btn-primary prep-cta" id="prep-open">${
      pg?`Continue preparing \u00b7 ${pg.done}/${pg.total} done`:"Prepare bid \u2192"}</button>`;})()}
    ${watchDetailHTML(id)}
    ${holderBlock(b)}
    ${row("Scope of Work",b.scope)}
    <div id="detail-rates"></div>
    <div class="detail-grid">
      ${row("Posted",b.published)}
      ${row("Bid Number",b.bid_number)}
      ${row("Contact",b.contact)}
      ${row("Email",b.email,b.email?"mailto:"+b.email:null)}
      ${row("Phone",b.phone,b.phone?"tel:"+b.phone:null)}
    </div>
    ${docRow(b)}
    ${post?row("Original Posting",prettyUrl(b.url),b.url)
      :row("Original Posting","Not linked \u2014 search for it",findUrl)}
    <div class="workspace">
      <div class="workspace-title">Your tracking \u2014 private, only you see this</div>
      <div class="detail-row">
        <div class="detail-label">Your estimate</div>
        <input id="bid-value" class="input" style="margin-top:0.3rem;" placeholder="e.g. $45,000" value="${esc(b.value||"")}" />
      </div>
      <div class="detail-row">
        <div class="detail-label">Bid Status</div>
        <div class="pipeline-row">
          ${statuses.map(([k,label])=>`<button class="pchip ${pStatus===k?"active-"+k:""}" data-pstatus="${esc(pStatus===k?"":k)}">${label}</button>`).join("")}
        </div>
      </div>
      <div class="detail-row">
        <div class="detail-label">Notes</div>
        <textarea id="bid-notes" class="input" style="min-height:70px;resize:vertical;margin-top:0.3rem;font-family:inherit;" placeholder="Who you talked to, next steps...">${esc(notes[id]||"")}</textarea>
      </div>
    </div>
    <div class="act-primary">
      ${tel?`<a class="ma-gold" href="${esc(tel)}">Call ${esc(b.phone)}</a>`:""}
      ${mail?`<a class="${tel?"ma-ghost":"ma-gold"}" href="${esc(mail)}">Email</a>`:""}
      ${post?`<a class="ma-ghost" href="${esc(post)}" target="_blank" rel="noopener noreferrer">Open posting</a>`
            :`<a class="ma-ghost" href="${esc(findUrl)}" target="_blank" rel="noopener noreferrer">Find posting</a>`}
    </div>
    <div class="act-more">
      <button class="ma-ghost" data-act="save">${saved[id]?"\u2605 Saved":"\u2606 Save"}</button>
      <button class="ma-ghost" data-act="share">Share</button>
      ${b.deadline?`<button class="ma-ghost" data-act="calendar">Calendar</button>`:""}
      <button class="ma-ghost" data-act="draft">Proposal</button>
    </div>`;
  // Wired as real listeners instead of onclick="fn('${id}')" attributes. A bid
  // id is built from its city + title text, so a bid anywhere like O'Fallon
  // carried an apostrophe straight into the JS string literal and broke the
  // handler — every button on that bid silently did nothing. Closing over the
  // values sidesteps quoting entirely. (encodeURIComponent was not protection
  // here: it leaves apostrophes untouched.)
  document.getElementById("prep-open").onclick=()=>openPrep(city,id);
  const sq=document.getElementById("sub-quote-open");
  if(sq)sq.onclick=async()=>{await Promise.all([loadRates(bidState(city,b)),loadBidResults(bidState(city,b))]);renderSubQuote(city,id);};
  mc.querySelectorAll("[data-pstatus]").forEach(btn=>{
    btn.onclick=()=>{setPipelineStatus(id,btn.dataset.pstatus);refreshAfterPipeline(city,id);};
  });
  mc.querySelectorAll("[data-act]").forEach(btn=>{
    btn.onclick=()=>{
      const a=btn.dataset.act;
      if(a==="share")shareBid(city,id);
      else if(a==="save"){toggleSave(city,id);closeModal();}
      else if(a==="calendar")addToCalendar(city,id);
      else if(a==="draft")draftProposal(city,id);
    };
  });

  fillDetailRates(city,b);
  document.getElementById("modal-back").classList.add("open");
  document.getElementById("modal").classList.add("open");
  document.getElementById("bid-value").addEventListener("blur",()=>{
    // Most scanned bids never state a dollar value at all (the AI
    // extraction deliberately never guesses one) -- this is the only way
    // it gets filled in for most bids. Mirrors the notes field's
    // auto-save-if-not-tracked-yet behavior: entering a value on a bid
    // that isn't saved yet starts tracking it.
    const val=document.getElementById("bid-value").value.trim();
    b.value=val;
    store.set("last_feed",bidData);queueFeedPush();
    if(val&&!saved[id]){
      const[c,bb]=findBidWithCity(id);
      if(bb){saved[id]={...bb,_city:c||city};store.set("saved",saved);}
    }
    if(saved[id]){
      saved[id].value=val;
      store.set("saved",saved);
      pushSavedBid(id,saved[id]._city);
    }
  });
  document.getElementById("bid-notes").addEventListener("blur",()=>{
    const val=document.getElementById("bid-notes").value;
    if(val.trim()){
      notes[id]=val;
      if(!saved[id]){
        const[c,b]=findBidWithCity(id);
        if(b){saved[id]={...b,_city:c||""};store.set("saved",saved);}
      }
    }else{
      delete notes[id];
    }
    store.set("notes",notes);
    if(saved[id])pushSavedBid(id,saved[id]._city);
  });
}
window.shareBid=async function(city,id){
  const b=findBid(id);if(!b)return;
  const text=`${b.title||"Bid"} \u2014 ${city}${b.value?" \u2022 "+b.value:""}${b.deadline?" \u2022 Due "+b.deadline:""}${b.url?"\n"+b.url:""}`;
  try{
    if(navigator.share){await navigator.share({title:b.title||"Bid",text});}
    else{await navigator.clipboard.writeText(text);toast("Copied to clipboard");}
  }catch(e){}
};
// Builds a minimal .ics file for a bid's deadline so it can be dropped
// straight into Google/Apple/Outlook calendar — no backend needed.
// Uses the shared prose-aware parser, so Add to Calendar works on the many
// bids whose deadline isn't written as a bare ISO date.
function icsDate(dateStr){
  const d=deadlineDate({deadline:dateStr});
  if(!d)return null;
  const pad=(n)=>String(n).padStart(2,"0");
  return `${d.getFullYear()}${pad(d.getMonth()+1)}${pad(d.getDate())}`;
}
function icsEscape(s){return String(s||"").replace(/([,;])/g,"\\$1").replace(/\n/g,"\\n");}
window.addToCalendar=function(city,id){
  const b=findBid(id);if(!b)return;
  const dt=icsDate(b.deadline);
  if(!dt){toast("Couldn't read a calendar date from this deadline");return;}
  const stamp=new Date().toISOString().replace(/[-:]/g,"").split(".")[0]+"Z";
  const lines=[
    "BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//CurbCall Pro//Bid Deadlines//EN",
    "BEGIN:VEVENT",
    `UID:${id}@curbcall.app`,
    `DTSTAMP:${stamp}`,
    `DTSTART;VALUE=DATE:${dt}`,
    `SUMMARY:${icsEscape("Bid due: "+(b.title||"Untitled Project"))}`,
    `DESCRIPTION:${icsEscape(city+(b.scope?" — "+b.scope:"")+(b.value?" • "+b.value:""))}`,
    `LOCATION:${icsEscape(city)}`,
    "END:VEVENT","END:VCALENDAR"
  ];
  const blob=new Blob([lines.join("\r\n")],{type:"text/calendar"});
  const url=URL.createObjectURL(blob);
  const a=document.createElement("a");
  a.href=url;a.download=(b.title||"bid").replace(/[^a-z0-9]+/gi,"_").slice(0,40)+"_deadline.ics";
  document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
  toast("Calendar event downloaded");
};
// A photo is displayed in a circle at 56px with object-fit:cover, so a wide
// or tall shot gets centre-cropped -- previously you only discovered what
// survived that crop after the upload had already replaced your old photo.
// This shows the real thing first, at the same aspect and crop rule the
// account card uses, and only uploads once you accept it.
function confirmAvatarFraming(file){
  const url=URL.createObjectURL(file);
  const mc=document.getElementById("modal-content");
  mc.innerHTML=`<h2>Use this photo?</h2>
    <p class="account-status" style="margin-bottom:1rem;">This is exactly how it will
      appear — photos are cropped to a circle from the centre.</p>
    <div style="display:flex;align-items:center;gap:1.2rem;margin-bottom:1.2rem;">
      <img id="avatar-preview-lg" src="${esc(url)}" alt=""
           style="width:120px;height:120px;border-radius:50%;object-fit:cover;
                  flex-shrink:0;border:1px solid var(--border);" />
      <div>
        <img id="avatar-preview-sm" src="${esc(url)}" alt="" class="avatar-lg" />
        <div class="account-status" style="margin-top:0.4rem;">Actual size</div>
      </div>
    </div>
    <div id="avatar-preview-err" class="account-status" style="color:var(--red);margin-bottom:0.8rem;"></div>
    <button class="btn-primary" id="avatar-confirm" style="margin-top:0;">Use this photo</button>
    <button class="btn-ghost" id="avatar-cancel">Cancel</button>`;
  document.getElementById("modal-back").classList.add("open");
  document.getElementById("modal").classList.add("open");

  const done=()=>{URL.revokeObjectURL(url);closeModal();};
  // A file the browser can't decode (a renamed .heic, a corrupt download)
  // would otherwise upload happily and land as a blank circle.
  document.getElementById("avatar-preview-lg").onerror=()=>{
    document.getElementById("avatar-preview-err").textContent=
      "This file isn't an image the browser can display — try a JPG or PNG.";
    const btn=document.getElementById("avatar-confirm");
    btn.disabled=true;btn.textContent="Can't use this file";
  };
  document.getElementById("avatar-cancel").onclick=done;
  document.getElementById("avatar-confirm").onclick=async()=>{
    const btn=document.getElementById("avatar-confirm");
    btn.disabled=true;btn.textContent="Uploading…";
    const ok=await uploadAvatar(file);
    done();
    if(ok)toast("Photo updated!");
  };
}

async function uploadAvatar(file){
  const ext=(file.name.split(".").pop()||"png").toLowerCase();
  const path=`${currentUser.id}/avatar.${ext}`;
  try{
    const{error}=await sb.storage.from("avatars").upload(path,file,{upsert:true,contentType:file.type});
    if(error){toast("Couldn't upload photo. Try again.");return false;}
    const{data}=sb.storage.from("avatars").getPublicUrl(path);
    const url=data.publicUrl+"?t="+Date.now();
    // Confirm the uploaded file is actually readable back before storing it.
    // The upload can report success while the bucket stays private, and the
    // only symptom used to be a permanently blank circle with no error
    // anywhere -- the app believed it had a photo, the <img> just failed.
    const reachable=await new Promise(res=>{
      const probe=new Image();
      probe.onload=()=>res(true);
      probe.onerror=()=>res(false);
      probe.src=url;
      setTimeout(()=>res(false),8000);
    });
    if(!reachable){
      toast("Photo uploaded but can't be loaded back — the avatars bucket may not be public.");
      return false;
    }
    companyProfile.avatar_url=url;
    store.set("company_profile",companyProfile);
    pushCompanyProfile();
    updateUserChip();
    renderAccount();
    return true;
  }catch(err){toast(offlineOrServer());return false;}
}

function closeModal(){
  document.getElementById("modal-back").classList.remove("open");
  document.getElementById("modal").classList.remove("open");
}
byId("modal-back").onclick=closeModal;

// ── AI-drafted bid proposals ──
// Sends the bid + the contractor's own saved company info (set in Account)
// to the backend, which asks OpenAI for a ready-to-edit proposal cover
// letter. Nothing about the company is stored server-side — it's just
// passed through on this one request the same way the license key is.
window.draftProposal=async function(city,id){
  const b=findBid(id);if(!b)return;
  if(!requireOnline("Drafting a proposal"))return;
  toast("Drafting proposal...");
  try{
    const token=await getSupabaseToken();
    const r=await fetchWithTimeout(SERVER+"/draft-proposal",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        key:licenseKey(),device_id:deviceId(),supabase_token:token,
        bid:{title:b.title||"",scope:b.scope||"",deadline:b.deadline||"",city},
        company:companyProfile
      })
    },75000);
    if(r.status===403){toast("Your trial or subscription isn't active.");goTo("account");return;}
    const d=await r.json();
    if(!d.ok){
      const msg=d.reason==="ai_unavailable"?"Proposal drafting isn't configured yet."
        :d.reason==="no_bid"?"Missing bid details.":"Couldn't draft a proposal. Try again.";
      toast(msg);return;
    }
    showProposalModal(b,d.draft);
  }catch(e){
    toast(e.name==="AbortError"?"That took too long. Try again.":"Couldn't reach the server.");
  }
};
function showProposalModal(b,draft){
  const mc=document.getElementById("modal-content");
  mc.innerHTML=`<h2>Proposal Draft</h2>
    <div class="account-status" style="margin-bottom:0.8rem;">${esc(b.title||"")} — AI-generated starting point. Review before sending.</div>
    <textarea id="proposal-text" class="input" style="min-height:280px;resize:vertical;font-family:inherit;">${esc(draft)}</textarea>
    <div class="modal-actions">
      <button class="ma-gold" onclick="copyProposal();">Copy</button>
      <button class="ma-ghost" onclick="closeModal();">Close</button>
    </div>`;
  document.getElementById("modal-back").classList.add("open");
  document.getElementById("modal").classList.add("open");
}
window.copyProposal=async function(){
  const ta=document.getElementById("proposal-text");
  try{await navigator.clipboard.writeText(ta.value);toast("Copied to clipboard");}
  catch(e){ta.select();try{document.execCommand("copy");toast("Copied to clipboard");}catch(e2){toast("Couldn't copy — select and copy manually.");}}
};

// Turns the pipeline tracking you're already doing (Submitted/Won/Lost/
// Passed) into an actual scoreboard — win rate and dollars, built entirely
// from data already saved locally, no backend needed.
function renderPipelineStats(keys,counts){
  const box=document.getElementById("pipeline-stats");
  const decided=counts.won+counts.lost;
  const winRate=decided?Math.round((counts.won/decided)*100)+"%":"—";
  let wonValue=0,trackedValue=0;
  keys.forEach(id=>{
    const v=parseValue(saved[id]&&saved[id].value);
    if(v<0)return;
    trackedValue+=v;
    if(pipeline[id]==="won")wonValue+=v;
  });
  box.innerHTML=`<div class="stats-grid">
    <div class="stat-box"><div class="n">${winRate}</div><div class="l">Win Rate</div></div>
    <div class="stat-box"><div class="n">${counts.won}</div><div class="l">Won (${formatMoney(wonValue)})</div></div>
    <div class="stat-box"><div class="n">${formatMoney(trackedValue)}</div><div class="l">Tracked Value</div></div>
  </div>`;
}

function renderSaved(){
  const list=document.getElementById("saved-list");
  const ptiles=document.getElementById("pipeline-tiles");
  const exportBtn=document.getElementById("export-saved-btn");
  const keys=Object.keys(saved);
  if(!keys.length){
    ptiles.innerHTML="";exportBtn.style.display="none";
    document.getElementById("pipeline-stats").innerHTML="";
    list.innerHTML=emptyHTML("i-star","No active bids","Tap the &#x2606; on any bid to start tracking it here.");
    return;
  }
  exportBtn.style.display="inline-flex";
  const counts={All:keys.length,none:0,submitted:0,won:0,lost:0,passed:0};
  keys.forEach(id=>{const s=pipeline[id];if(s)counts[s]=(counts[s]||0)+1;else counts.none++;});
  renderPipelineStats(keys,counts);
  const cats=[["All","All"],["none","Untracked"],["submitted","Submitted"],["won","Won"],["lost","Lost"],["passed","Passed"]];
  ptiles.innerHTML=cats.filter(([k])=>k==="All"||counts[k]>0)
    .map(([k,label])=>`<div class="tile ${pipelineFilter===k?"active":""}" data-k="${k}"><div class="n">${counts[k]||0}</div><div class="l">${label}</div></div>`).join("");
  ptiles.querySelectorAll(".tile").forEach(t=>t.onclick=()=>{pipelineFilter=t.dataset.k;renderSaved();});
  const filteredKeys=keys.filter(id=>pipelineFilter==="All"||(pipelineFilter==="none"?!pipeline[id]:pipeline[id]===pipelineFilter));
  if(!filteredKeys.length){
    list.innerHTML=emptyHTML("i-search","Nothing here","No active bids with this status.");
    return;
  }
  list.innerHTML=filteredKeys.map(id=>{const b=saved[id];return bidCard(b._city||"",b);}).join("");
  attachBidEvents(list);
}
byId("export-saved-btn").onclick=()=>{
  const keys=Object.keys(saved).filter(id=>pipelineFilter==="All"||(pipelineFilter==="none"?!pipeline[id]:pipeline[id]===pipelineFilter));
  exportCSV(keys.map(id=>[saved[id]._city||"",saved[id]]),"curbcall_saved");
};

// Win rate + $ won + $ tracked across all saved bids — mirrors desktop's
// _pipeline_stats(), used for the Account tab's stats summary.
function pipelineStatsSummary(){
  const ids=Object.keys(saved);
  let won=0,lost=0,wonValue=0,trackedValue=0;
  ids.forEach(id=>{
    const st=pipeline[id];
    if(st==="won")won++;
    if(st==="lost")lost++;
    const v=parseValue(saved[id]&&saved[id].value);
    if(v>=0){
      trackedValue+=v;
      if(st==="won")wonValue+=v;
    }
  });
  const decided=won+lost;
  const winRate=decided?Math.round(won/decided*100)+"%":"—";
  return{count:ids.length,winRate,wonValue,trackedValue};
}
let lastSyncAt=null;
let companyEditMode=false;
// ── Review / testimonial ──
// Submitted here, displayed on the marketing page only after it's approved
// by hand in Supabase. Editing an approved review sends it back for
// approval (enforced by a trigger, not here) so approved text can't be
// swapped afterwards.
let reviewDraft={rating:0,quote:"",submitted:false,approved:false};

async function loadReviewCard(){
  const el=document.getElementById("review-body");
  if(!el)return;
  if(!sb||!currentUser){el.textContent="Sign in to leave a review.";return;}
  try{
    const{data,error}=await sb.from("reviews").select("*").maybeSingle();
    if(error){el.textContent="Reviews aren't set up yet.";return;}
    if(data)reviewDraft={rating:data.rating||0,quote:data.quote||"",
                          submitted:true,approved:!!data.approved};
  }catch(e){el.textContent="Couldn't load your review.";return;}
  renderReviewCard();
}

function renderReviewCard(){
  const el=document.getElementById("review-body");
  if(!el)return;
  const star=(n)=>`<button type="button" data-r="${n}" class="${n<=reviewDraft.rating?"on":""}"
      aria-label="${n} star${n>1?"s":""}"><svg class="icon-svg"><use href="#i-star"/></svg></button>`;
  const status=reviewDraft.submitted
    ?(reviewDraft.approved
        ?`<div class="account-status" style="color:var(--green);margin-top:0.5rem;">Published on the site — thank you.</div>`
        :`<div class="account-status" style="margin-top:0.5rem;">Submitted — waiting to be reviewed before it goes public.</div>`)
    :"";
  el.innerHTML=`
    <div class="stars" id="review-stars">${[1,2,3,4,5].map(star).join("")}</div>
    <textarea class="input" id="review-quote" rows="3" maxlength="400"
      placeholder="What's it done for you? (optional)"
      style="resize:vertical;margin-bottom:0.6rem;">${esc(reviewDraft.quote)}</textarea>
    <button class="btn-ghost" id="review-send" style="margin-top:0;">${reviewDraft.submitted?"Update my review":"Submit review"}</button>
    ${status}`;
  el.querySelectorAll("#review-stars button").forEach(b=>{
    b.onclick=()=>{reviewDraft.rating=parseInt(b.dataset.r);
      reviewDraft.quote=document.getElementById("review-quote").value;
      renderReviewCard();};
  });
  document.getElementById("review-send").onclick=submitReview;
}

async function submitReview(){
  if(!sb||!currentUser)return;
  const quote=document.getElementById("review-quote").value.trim();
  if(!reviewDraft.rating){toast("Pick a star rating first");return;}
  const btn=document.getElementById("review-send");
  btn.disabled=true;btn.textContent="Sending…";
  // Name and company come from the profile rather than a free-text field:
  // it's what a testimonial needs, it's already filled in, and it means one
  // less thing that can be typed in wrong.
  const row={rating:reviewDraft.rating,quote:quote,
             display_name:companyProfile.contact||"",
             company:companyProfile.name||""};
  try{
    const{error}=await sb.from("reviews").upsert(row,{onConflict:"user_id"});
    if(error){toast("Couldn't save your review. Try again.");
      btn.disabled=false;renderReviewCard();return;}
    reviewDraft.quote=quote;reviewDraft.submitted=true;reviewDraft.approved=false;
    renderReviewCard();
    toast("Thanks — we'll take a look.");
  }catch(e){toast("Couldn't reach the server.");btn.disabled=false;renderReviewCard();}
}

async function loadReferralCard(elId="referral-body"){
  const el=document.getElementById(elId);
  if(!el) return;
  try{
    let code=store.get("referral_code","");
    if(!code){
      const token=await getSupabaseToken();
      const r=await fetchWithTimeout(SERVER+"/referral/code",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({supabase_token:token})},30000);
      const d=await r.json();
      if(d.ok&&d.code){code=d.code;store.set("referral_code",code);}
    }
    if(!code){el.textContent="Couldn't load your referral link right now.";return;}
    const link=`${location.origin}/?ref=${encodeURIComponent(code)}`;
    el.innerHTML=`<div class="input" style="display:flex;align-items:center;gap:0.5rem;overflow:hidden;padding:0.6rem 0.8rem;">
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:var(--fs-sm);color:var(--text2);">${esc(link)}</span>
      <button class="btn-ghost referral-copy-btn" style="flex-shrink:0;width:auto;margin:0;padding:0.35rem 0.7rem;font-size:var(--fs-sm);">Copy</button>
    </div>`;
    // Scoped to this card: Home and Account both carry one.
    el.querySelector(".referral-copy-btn").onclick=async()=>{
      try{await navigator.clipboard.writeText(link);toast("Referral link copied");}
      catch(e){toast("Couldn't copy — long-press the link to copy manually");}
    };
  }catch(e){el.textContent="Couldn't load your referral link right now.";}
}
// First letter of whoever this is -- their contact name if they gave one,
// otherwise their email. The placeholder circle had centring and a font size
// and nothing to centre, so an account with no photo showed a bare grey ring
// that reads as an image that failed to load.
function avatarInitial(){
  const src=String((companyProfile&&companyProfile.contact)||"").trim()
           ||String((currentUser&&currentUser.email)||"").trim();
  const ch=src.charAt(0);
  return ch?ch.toUpperCase():"\u00b7";
}

function renderAccount(){
  const body=document.getElementById("account-body");
  const email=currentUser?.email||"";
  const created=(currentUser?.created_at||"").slice(0,10);
  const stats=pipelineStatsSummary();
  const syncText=lastSyncAt
    ?`Last synced ${lastSyncAt.toLocaleTimeString([],{hour:"numeric",minute:"2-digit"})}`
    :"Not synced yet this session";

  // tag stripe links with device id — these are the live $49/mo and $399/yr
  // Payment Links (match the ones on index.html); update both files together
  // if pricing changes.
  const pendingRef=(()=>{try{return localStorage.getItem("pending_ref")||"";}catch(e){return"";}})();
  const refId=pendingRef?deviceId()+REFERRAL_SEP+pendingRef:deviceId();
  // Pre-fill the checkout with the signed-in address.
  //
  // The webhook files a new key under the email used AT STRIPE, while
  // /mykey looks it up by the signed-in account email. Those are the same
  // string right up until someone pays with a personal card that autofills
  // a different address -- and then the key exists, is paid for, and cannot
  // be found. The device id below covers the machine they bought on; it does
  // nothing for the second device, where an unknown device and a mismatched
  // email both miss and a paying customer sees "Trial expired, subscribe
  // below". Pre-filling makes the two addresses agree by default.
  //
  // Stripe leaves the field editable, so this is a default and not a
  // guarantee -- the receipt email still carries the key, and the note under
  // the Subscribe buttons says so.
  const pre=email?`&prefilled_email=${encodeURIComponent(email)}`:"";
  const mLink=`https://buy.stripe.com/aFa00idzefwuayK6DRejK05?client_reference_id=${encodeURIComponent(refId)}${pre}`;
  const aLink=`https://buy.stripe.com/7sY5kC0Ms6ZYeP00ftejK04?client_reference_id=${encodeURIComponent(refId)}${pre}`;

  body.innerHTML=`
    <div class="account-card" style="display:flex;align-items:center;gap:0.9rem;">
      ${companyProfile.avatar_url?`<img class="avatar-lg" src="${esc(companyProfile.avatar_url)}" alt=""
           onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'avatar-placeholder',textContent:this.dataset.initial||''}))" data-initial="${esc(avatarInitial())}">`
        :`<div class="avatar-placeholder">${esc(avatarInitial())}</div>`}
      <div style="flex:1;min-width:0;">
        <div class="account-email">${esc(email)}</div>
        <div class="account-status">Signed in with ${currentUser?.app_metadata?.provider||"email"}${created?` &middot; Member since ${esc(created)}`:""}</div>
        <label class="btn-ghost" for="avatar-input" style="display:inline-flex;align-items:center;justify-content:center;margin-top:0.5rem;padding:0 0.9rem;min-height:44px;width:auto;font-size:var(--fs-sm);cursor:pointer;">Change Photo</label>
        <input type="file" id="avatar-input" accept="image/png,image/jpeg,image/gif,image/webp" style="display:none;" />
      </div>
    </div>
        <!-- Billing sits directly under the profile, not at the bottom.
         A customer whose trial has run out is sent here by the scan screen
         with "Check Account tab", and this card was fifteenth on the page --
         below Stats, Alerts, Company Info, Support, Referrals, Reviews and
         Admin. They had to scroll past all of it to reach the one thing they
         were sent for. It is also simply what people open Account to look
         at. -->
<div class="account-email hdr-ic" style="font-size:var(--fs-base);margin:0.4rem 0 0.5rem;"><svg class="icon-svg"><use href="#i-credit-card"/></svg>Billing</div>
    <div class="plan" id="status-card"><span class="skel-line sm"></span><span class="skel-line lg"></span><span class="skel-line md"></span></div>
    <!-- Deliberately worded as a question and styled quietly. It sat above the
         pricing looking like the primary action, and for someone who has
         never subscribed the Stripe portal is a dead end. Not hidden
         outright: a customer who has just paid but not yet activated their
         key still needs it, and so does anyone chasing an invoice. -->
    <a class="btn-ghost btn-quiet" style="text-decoration:none;display:flex;align-items:center;justify-content:center;gap:0.45rem;box-sizing:border-box;font-size:var(--fs-sm);" href="${PORTAL_URL}" target="_blank"><svg class="icon-svg"><use href="#i-credit-card"/></svg>Already subscribed? Manage billing &rarr;</a>
    <div id="upgrade-section">
      <div class="plan featured">
        <div class="plan-name">Pro Monthly</div>
        <div class="plan-price">$49<span> / month</span></div>
        <ul>${["Unlimited nationwide bid scans","AI bid extraction","Federal + local bids","Save &amp; track leads","Cancel anytime"].map(planLi).join("")}</ul>
        <a class="btn-primary" style="text-decoration:none;" href="${mLink}" target="_blank">Subscribe Monthly</a>
        <p class="renew-note">Renews automatically at $49/month until you cancel. Cancel anytime under Billing above.</p>
      </div>
      <div class="plan">
        <div class="plan-name">Pro Annual &mdash; best value, save $189</div>
        <div class="plan-price">$399<span> / year</span></div>
        <ul>${["Everything in Monthly","About 4 months free","Priority support"].map(planLi).join("")}</ul>
        <a class="btn-ghost" style="text-decoration:none;display:block;text-align:center;" href="${aLink}" target="_blank">Subscribe Annual</a>
        <p class="renew-note">Renews automatically at $399/year until you cancel. Cancel anytime under Billing above.</p>
      </div>
      <div class="license-key-section">
        <div class="field-label" style="margin-top:1.2rem;">Have a license key?</div>
        <!-- Named for the case that actually strands people. Unlocking is
             automatic when the checkout address matches the account; when it
             does not, this box is the way through, and someone who has just
             paid needs to be told that here rather than left to guess. -->
        <p class="renew-note" style="margin-top:0;">Already paid and still locked? If you used a different email address at
          checkout, paste the key from your receipt email here.</p>
        <input class="input" id="key-input" placeholder="BCP-..." />
        <button class="btn-ghost" id="activate-btn">Activate Key</button>
      </div>
    </div>
    <div class="account-card">
      <div class="account-email hdr-ic" style="font-size:var(--fs-base);margin-bottom:0.6rem;"><svg class="icon-svg"><use href="#i-bar-chart"/></svg>Your Stats</div>
      <div class="stats-grid">
        <div class="stat-box"><div class="n">${stats.count}</div><div class="l">Active Bids</div></div>
        <div class="stat-box"><div class="n">${stats.winRate}</div><div class="l">Win Rate</div></div>
        <div class="stat-box"><div class="n">${formatMoney(stats.wonValue)}</div><div class="l">Won</div></div>
        <div class="stat-box"><div class="n">${formatMoney(stats.trackedValue)}</div><div class="l">Tracked</div></div>
      </div>
      <div class="account-status" style="margin-top:0.7rem;display:flex;align-items:center;justify-content:space-between;">
        <span>${esc(syncText)}</span>
        <a href="#" id="sync-now-link" style="color:var(--amber);display:inline-flex;align-items:center;min-height:44px;padding:0 0.25rem;">Sync now</a>
      </div>
    </div>
    ${isAdmin?renderDiagnostics():""}
    <div class="account-card" id="notif-card" style="cursor:pointer;">
      <div class="account-email hdr-ic" style="font-size:var(--fs-base);"><svg class="icon-svg"><use href="#i-bell"/></svg>Bid Alerts</div>
      <div class="account-status" id="notif-status">${esc(notifPermissionLabel())}</div>
    </div>
    <div class="account-card">
      <div style="font-size:var(--fs-base);font-weight:700;display:flex;flex-wrap:wrap;gap:0.5rem;justify-content:space-between;align-items:center;">
        <span class="hdr-ic" style="white-space:nowrap;"><svg class="icon-svg"><use href="#i-briefcase"/></svg>Company Info</span>
        ${companyEditMode?"":`<button class="btn-ghost hdr-ic" id="co-edit-btn" style="padding:0.35rem 0.9rem;min-height:44px;font-size:var(--fs-sm);flex-shrink:0;justify-content:center;align-items:center;"><svg class="icon-svg"><use href="#i-pencil"/></svg>Edit</button>`}
      </div>
      <div class="account-status" style="margin-bottom:0.7rem;">Used to personalize AI-drafted bid proposals, and Contact Person is shown in the corner up top instead of your email. Nothing here is shared beyond that.</div>
      ${companyEditMode?`
      <div class="field-label">Company Name</div>
      <input class="input co-field" id="co-name" data-k="name" placeholder="Acme Concrete LLC" value="${esc(companyProfile.name||"")}" style="margin-bottom:0.6rem;" />
      <div class="field-label">Contact Person</div>
      <input class="input co-field" id="co-contact" data-k="contact" placeholder="Jane Doe" value="${esc(companyProfile.contact||"")}" style="margin-bottom:0.6rem;" />
      <div class="field-label">Phone</div>
      <input class="input co-field" id="co-phone" data-k="phone" placeholder="(555) 555-5555" value="${esc(companyProfile.phone||"")}" style="margin-bottom:0.6rem;" />
      <div class="field-label">Email</div>
      <input class="input co-field" id="co-email" data-k="email" placeholder="you@company.com" value="${esc(companyProfile.email||email||"")}" style="margin-bottom:0.6rem;" />
      <div class="field-label">Specialty (optional)</div>
      <input class="input co-field" id="co-specialty" data-k="specialty" placeholder="sidewalk, ADA ramp &amp; curb concrete work" value="${esc(companyProfile.specialty||"")}" style="margin-bottom:0.8rem;" />
      <div style="display:flex;gap:0.6rem;">
        <button class="btn-primary hdr-ic" id="co-confirm-btn" style="margin-top:0;justify-content:center;"><svg class="icon-svg"><use href="#i-check"/></svg>Confirm</button>
        <button class="btn-ghost" id="co-cancel-btn">Cancel</button>
      </div>
      `:`
      <div class="detail-row"><div class="detail-label">Company Name</div><div class="detail-val">${esc(companyProfile.name||"Not set")}</div></div>
      <div class="detail-row"><div class="detail-label">Contact Person</div><div class="detail-val">${esc(companyProfile.contact||"Not set")}</div></div>
      <div class="detail-row"><div class="detail-label">Phone</div><div class="detail-val">${esc(companyProfile.phone||"Not set")}</div></div>
      <div class="detail-row"><div class="detail-label">Email</div><div class="detail-val">${esc(companyProfile.email||email||"Not set")}</div></div>
      <div class="detail-row"><div class="detail-label">Specialty</div><div class="detail-val">${esc(companyProfile.specialty||"Not set")}</div></div>
      `}
    </div>
    <div class="account-card">
      <div style="font-size:var(--fs-base);font-weight:700;display:flex;flex-wrap:wrap;gap:0.5rem;justify-content:space-between;align-items:center;">
        <span class="hdr-ic" style="white-space:nowrap;"><svg class="icon-svg"><use href="#i-briefcase"/></svg>Bid Paperwork</span>
        <button class="btn-ghost hdr-ic" id="bi-edit-btn" style="padding:0.35rem 0.9rem;min-height:44px;font-size:var(--fs-sm);flex-shrink:0;justify-content:center;align-items:center;"><svg class="icon-svg"><use href="#i-pencil"/></svg>${bidInfoCount()?"Edit":"Add"}</button>
      </div>
      <div class="account-status">Your address, license numbers, bonding agent and ready-mix supplier, entered once. Used to fill in agencies' bid forms and to write bond and concrete quote requests from Prepare bid. ${bidInfoCount()} of ${BID_INFO_FIELDS.length} saved.</div>
    </div>
    <div class="account-card">
      <div class="account-email hdr-ic" style="font-size:var(--fs-base);"><svg class="icon-svg"><use href="#i-life-buoy"/></svg>Support</div>
      <div class="account-status" style="margin-bottom:0.7rem;">Send us a message and we'll get back to you, or email <a href="mailto:${SUPPORT_EMAIL}" style="color:var(--amber);">${SUPPORT_EMAIL}</a> directly.</div>
      <textarea class="input" id="support-msg" rows="4" placeholder="What's going on?" style="resize:vertical;margin-bottom:0.6rem;"></textarea>
      <button class="btn-ghost" id="support-send-btn">Send Message</button>
      <div class="account-status" id="support-status" style="margin-top:0.5rem;"></div>
    </div>
    <div class="account-card" id="referral-card">
      <div class="account-email hdr-ic" style="font-size:var(--fs-base);"><svg class="icon-svg"><use href="#i-users"/></svg>Refer a Contractor</div>
      <div class="account-status" style="margin-bottom:0.7rem;">Give a free month, get a free month — when someone you refer subscribes, you both get bonus time added automatically.</div>
      <div id="referral-body" class="account-status"><span class="skel-line md"></span></div>
    </div>
    <div class="account-card" id="review-card">
      <div class="account-email hdr-ic" style="font-size:var(--fs-base);"><svg class="icon-svg"><use href="#i-star"/></svg>Rate CurbCall Pro</div>
      <div class="account-status" style="margin-bottom:0.7rem;">How's it working for you? With your OK we may quote this on the site — nothing appears publicly until it's reviewed.</div>
      <div id="review-body" class="account-status"><span class="skel-line md"></span></div>
    </div>
    ${isAdmin?`
    <div class="account-card" id="admin-card" style="cursor:pointer;border-color:var(--amber);">
      <div class="account-email hdr-ic" style="font-size:var(--fs-base);"><svg class="icon-svg"><use href="#i-lock"/></svg>Admin</div>
      <div class="account-status">Reviews, agency notices, campaign drafts, backups &rarr;</div>
    </div>
    `:""}
    <div class="account-card">
      <div class="account-email hdr-ic" style="font-size:var(--fs-base);"><svg class="icon-svg"><use href="#i-lock"/></svg>Sign-in &amp; Security</div>
      <div class="account-status" style="margin-bottom:0.8rem;">Change how you get into this account.</div>

      <div class="field-label">New password</div>
      <input class="input" id="sec-password" type="password" autocomplete="new-password"
             placeholder="At least 8 characters" style="margin-bottom:0.5rem;" />
      <button class="btn-ghost" id="sec-password-btn">Change password</button>

      <div class="field-label" style="margin-top:1.1rem;">Email address</div>
      <input class="input" id="sec-email" type="email" autocomplete="email"
             value="${esc(email)}" style="margin-bottom:0.5rem;" />
      <button class="btn-ghost" id="sec-email-btn">Change email</button>
      <div class="account-status" style="margin-top:0.4rem;">We'll send a confirmation link to the new address. Your old one keeps working until you click it.</div>
    </div>

    <div class="account-card" style="border-color:rgba(239,68,68,0.35);">
      <div class="account-email hdr-ic" style="font-size:var(--fs-base);color:var(--red);"><svg class="icon-svg"><use href="#i-trash"/></svg>Delete account</div>
      <div class="account-status" style="margin-bottom:0.8rem;">Permanently removes your account, saved bids and company info, and cancels any subscription. This cannot be undone. A record that you accepted the Terms is kept for ten years, then deleted &mdash; see the <a href="privacy.html" target="_blank" rel="noopener">Privacy Policy</a>.</div>
      <div class="field-label">Type DELETE to confirm</div>
      <input class="input" id="del-confirm" placeholder="DELETE" autocapitalize="characters"
             style="margin-bottom:0.5rem;" />
      <button class="btn-ghost" id="del-account-btn"
              style="color:var(--red);border-color:var(--red);">Delete my account</button>
    </div>

    <button class="btn-ghost" style="margin-top:0.5rem;color:var(--red);border-color:var(--red);" id="signout-btn">Sign out</button>`;

  // ── Sign-in & security ──
  const pwBtn=document.getElementById("sec-password-btn");
  if(pwBtn)pwBtn.onclick=async()=>{
    if(!signInAvailable())return;
    const el=document.getElementById("sec-password");
    const next=el.value;
    if(!next||next.length<8){toast("Password must be at least 8 characters");return;}
    pwBtn.disabled=true;pwBtn.textContent="Changing...";
    try{
      const{error}=await sb.auth.updateUser({password:next});
      if(error){toast("Couldn't change your password. Try a different one, or try again in a bit.");}
      else{el.value="";toast("Password changed");}
    }catch(e){toast("Couldn't reach the server. Try again.");}
    pwBtn.disabled=false;pwBtn.textContent="Change password";
  };

  const emBtn=document.getElementById("sec-email-btn");
  if(emBtn)emBtn.onclick=async()=>{
    if(!signInAvailable())return;
    const next=document.getElementById("sec-email").value.trim();
    if(!next||next.indexOf("@")<1){toast("Enter a valid email address");return;}
    if(next.toLowerCase()===(email||"").toLowerCase()){toast("That's already your email");return;}
    emBtn.disabled=true;emBtn.textContent="Sending...";
    try{
      const{error}=await sb.auth.updateUser({email:next});
      // Supabase does not switch the address until the link in the new
      // inbox is clicked, so say that rather than implying it is done.
      if(error){toast("Couldn't update your email. Double check the address and try again.");}
      else{toast("Check "+next+" for a confirmation link");}
    }catch(e){toast("Couldn't reach the server. Try again.");}
    emBtn.disabled=false;emBtn.textContent="Change email";
  };

  // ── Delete account ──
  const delBtn=document.getElementById("del-account-btn");
  if(delBtn)delBtn.onclick=async()=>{
    const typed=(document.getElementById("del-confirm").value||"").trim().toUpperCase();
    // Typing the word is the confirmation. A dialog people dismiss by reflex
    // is not one, and this is the single irreversible action in the app.
    if(typed!=="DELETE"){toast("Type DELETE in the box to confirm");return;}
    if(!confirm("This permanently deletes your account and cancels any subscription. There is no undo. Continue?"))return;
    delBtn.disabled=true;delBtn.textContent="Deleting...";
    try{
      const token=await getSupabaseToken();
      const r=await fetchWithTimeout(SERVER+"/account/delete",{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({supabase_token:token,device_id:deviceId()})},60000);
      const d=await r.json();
      if(!d.ok){
        // Spell out the one refusal a person can act on. The server will not
        // delete an account with live billing, because it cannot cancel the
        // subscription and will not leave a card being charged with no
        // account left to stop it.
        if(d.reason==="active_subscription"){
          toast("Cancel your subscription in the billing portal first, then come back.");
          if(d.portal)window.open(d.portal,"_blank");
        } else if(d.reason==="not_signed_in"){
          toast("Please sign out and back in, then try again");
        } else if(d.reason==="not_configured"){
          toast("Account deletion isn't available yet — email support@curbcallpro.com");
        } else {
          toast("Couldn't delete your account. Try again or email support.");
        }
        delBtn.disabled=false;delBtn.textContent="Delete my account";return;}
      // Local data goes with it. Leaving cached bids on the device after the
      // account is gone is the opposite of what someone just asked for.
      try{localStorage.clear();}catch(_){}
      await sb.auth.signOut();
      location.reload();
    }catch(e){
      toast("Couldn't reach the server. Try again.");
      delBtn.disabled=false;delBtn.textContent="Delete my account";
    }
  };

  document.getElementById("activate-btn").onclick=async()=>{
    const key=document.getElementById("key-input").value.trim().toUpperCase();
    if(!key){toast("Enter your license key");return;}
    const btn=document.getElementById("activate-btn");
    btn.disabled=true;btn.textContent="Activating...";
    try{
      const r=await fetchWithTimeout(SERVER+"/validate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key,device_id:deviceId()})},60000);
      const d=await r.json();
      if(d.valid){store.set("license_key",key);toast("Activated!");renderAccount();}
      else{toast(d.reason==="rate_limited"?"Too many attempts from this connection today. Try again tomorrow, or contact support.":"That key isn't valid.");btn.disabled=false;btn.textContent="Activate Key";}
    }catch(e){toast("Couldn't reach the server. Try again.");btn.disabled=false;btn.textContent="Activate Key";}
  };
  document.getElementById("signout-btn").onclick=async()=>{
    if(!sb){
      // Offline shim session — there's no server session to end, so just drop
      // the local "this device is signed in" marker and go back to the gate.
      store.set("last_user_email","");currentUser=null;
      showAuth();toast("Signed out");return;
    }
    await sb.auth.signOut();toast("Signed out");
  };
  document.getElementById("notif-card").onclick=toggleNotifPermission;
  const adminCard=document.getElementById("admin-card");
  if(adminCard)adminCard.onclick=openAdminPanel;
  loadReferralCard();
  loadReviewCard();
  // The Diagnostics card is admin-only, so these two are absent for everyone
  // else. Addressing them unguarded threw a TypeError here, which would have
  // aborted the rest of this function and left the Account screen's remaining
  // buttons -- support, sign-out, notifications -- wired to nothing.
  const diagRefresh=document.getElementById("diag-refresh");
  if(diagRefresh)diagRefresh.onclick=()=>loadHealth();
  const diagCopy=document.getElementById("diag-copy");
  if(diagCopy)diagCopy.onclick=async()=>{
    const text=diagnosticsText();
    try{await navigator.clipboard.writeText(text);toast("Diagnostics copied");}
    catch(e){
      // Clipboard access is blocked in some mobile browsers unless the page is
      // focused; showing the text is better than failing silently.
      const mc=document.getElementById("modal-content");
      mc.innerHTML=`<h2>Diagnostics</h2>
        <textarea class="input" style="min-height:260px;font-family:var(--mono);font-size:var(--fs-sm);">${esc(text)}</textarea>
        <div class="modal-actions"><button class="ma-ghost" onclick="closeModal();">Close</button></div>`;
      document.getElementById("modal-back").classList.add("open");
      document.getElementById("modal").classList.add("open");
    }
  };
  if(isAdmin)loadHealth();
  document.getElementById("support-send-btn").onclick=async()=>{
    const msgBox=document.getElementById("support-msg");
    const msg=msgBox.value.trim();
    const statusEl=document.getElementById("support-status");
    if(!msg){toast("Enter a message first");return;}
    statusEl.style.color="";statusEl.textContent="Sending...";
    try{
      const r=await fetchWithTimeout(SERVER+"/support",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({email,message:msg})},60000);
      const d=await r.json();
      if(d.ok){statusEl.style.color="var(--green)";statusEl.textContent="Message sent — we'll get back to you soon.";msgBox.value="";}
      else{statusEl.style.color="var(--red)";statusEl.textContent=d.reason==="email_unavailable"?"Support messages aren't configured yet — email us directly instead.":"Couldn't send your message. Try again.";}
    }catch(e){statusEl.style.color="var(--red)";statusEl.textContent=offlineOrServer();}
  };
  document.getElementById("sync-now-link").onclick=(e)=>{
    e.preventDefault();
    lastSyncAt=new Date();
    syncPullSavedBids();syncPullCompanyProfile();syncPullSavedSearches();syncPullFeeds();
    renderAccount();
  };
  document.getElementById("avatar-input").addEventListener("change",(e)=>{
    const file=e.target.files[0];
    // Reset the input straight away, so picking the SAME file again still
    // fires a change event (it otherwise doesn't, which reads as the app
    // ignoring you when a first attempt didn't take).
    e.target.value="";
    if(!file)return;
    if(!currentUser){toast("Sign in first");return;}
    if(!sb||!requireOnline("Uploading a photo"))return;
    confirmAvatarFraming(file);
  });
  // Company Info now needs an explicit Edit -> Confirm action instead of
  // silently auto-saving on blur -- so a value never changes just because
  // a field lost focus while the user was still deciding what to type.
  const coEditBtn=document.getElementById("co-edit-btn");
  if(coEditBtn)coEditBtn.onclick=()=>{companyEditMode=true;renderAccount();};
  const coConfirmBtn=document.getElementById("co-confirm-btn");
  if(coConfirmBtn)coConfirmBtn.onclick=()=>{
    document.querySelectorAll(".co-field").forEach(f=>{companyProfile[f.dataset.k]=f.value.trim();});
    store.set("company_profile",companyProfile);
    pushCompanyProfile();
    companyEditMode=false;
    renderAccount();
    updateUserChip(); // Contact Person doubles as the chip's display name
    toast("Company info saved");
  };
  const coCancelBtn=document.getElementById("co-cancel-btn");
  if(coCancelBtn)coCancelBtn.onclick=()=>{companyEditMode=false;renderAccount();};
  const biEditBtn=document.getElementById("bi-edit-btn");
  if(biEditBtn)biEditBtn.onclick=()=>openBidInfo(()=>{closeModal();renderAccount();});

  loadAccountStatus();
}

// ── Admin panel ──
// Reachable only from Account, and only when checkAdminStatus() found the
// signed-in email on the server's allowlist -- but that check ONLY decides
// whether this entry point is shown. Every actual admin action below still
// requires the real admin token, exactly as it would from a direct API call;
// this is a nicer way to reach the same server-side gate, not a second one.
function adminToken(){try{return localStorage.getItem("admin_token")||"";}catch(e){return"";}}
function setAdminToken(t){try{localStorage.setItem("admin_token",t);}catch(e){}}
async function adminCall(path,body,tokenOverride){
  // Never throws. Every admin action awaits this, and none of them caught --
  // so one failed request became an unhandled rejection and the button just
  // did nothing: no toast, no error. Worst on the campaign send, where
  // silence is indistinguishable from success and invites a second click.
  try{
    const r=await fetch(SERVER+path,{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({...body,admin_token:tokenOverride||adminToken()})});
    return await r.json();
  }catch(e){
    return {ok:false,reason:"network_error",
            detail:"Couldn't reach the server \u2014 nothing was changed"};
  }
}

function openAdminPanel(){
  const mc=document.getElementById("modal-content");
  if(!adminToken()){
    mc.innerHTML=`<h2>Admin</h2>
      <div class="account-status" style="margin-bottom:0.8rem;">Enter the admin token to continue. It's kept only on this device.</div>
      <input class="input" id="admin-token-input" type="password" placeholder="Admin token" style="margin-bottom:0.8rem;" />
      <div class="modal-actions"><button class="ma-gold" id="admin-unlock-btn">Unlock</button>
        <button class="ma-ghost" onclick="closeModal();">Cancel</button></div>`;
    document.getElementById("admin-unlock-btn").onclick=async()=>{
      const t=document.getElementById("admin-token-input").value.trim();
      if(!t)return;
      // Validate BEFORE storing. This used to store first and clear on a
      // bad answer, which is fine until the fetch itself throws -- offline,
      // or Render cold-starting past the timeout. The clear never ran, so an
      // unverified token stayed in localStorage and every later open skipped
      // this prompt entirely and went to a panel that could only fail.
      const check=await adminCall("/admin/list",{},t);
      if(check.reason==="network_error"){toast(check.detail);return;}
      if(!check.ok){toast("That token didn't work");return;}
      setAdminToken(t);
      renderAdminPanel();
    };
  }else{
    renderAdminPanel();
  }
  document.getElementById("modal-back").classList.add("open");
  document.getElementById("modal").classList.add("open");
}

async function renderAdminPanel(){
  const mc=document.getElementById("modal-content");
  mc.innerHTML=`<h2>Admin</h2><span class="skel-line lg"></span><span class="skel-line md"></span><span class="skel-line md"></span><span class="skel-line sm"></span>`;
  const [list,agency,drafts,reviews]=await Promise.all([
    adminCall("/admin/list",{}),
    adminCall("/agency/review",{}),
    adminCall("/campaign/drafts",{}),
    adminCall("/admin/reviews",{}),
  ]);
  if([list,agency,drafts,reviews].some(r=>r.reason==="network_error")){
    // Without this the modal sat on "Loading..." forever on any network
    // blip, with no way back other than reloading the page.
    mc.innerHTML=`<h2>Admin</h2><div class="account-status">Couldn't reach the server.</div>
      <div class="modal-actions"><button class="ma-gold" onclick="closeModal();openAdminPanel();">Retry</button>
        <button class="ma-ghost" onclick="closeModal();">Close</button></div>`;
    return;
  }
  if(!list.ok||!agency.ok||!drafts.ok||!reviews.ok){
    setAdminToken("");
    mc.innerHTML=`<h2>Admin</h2><div class="account-status">Token stopped working — try again.</div>
      <div class="modal-actions"><button class="ma-gold" onclick="closeModal();openAdminPanel();">Enter token</button>
        <button class="ma-ghost" onclick="closeModal();">Close</button></div>`;
    return;
  }
  const pendingNotices=agency.notices.filter(n=>!n.approved);
  const approvedNotices=agency.notices.filter(n=>n.approved);
  const pendingReviews=reviews.reviews.filter(r=>!r.approved);

  mc.innerHTML=`<h2>Admin</h2>
    <div class="stats-grid" style="margin-bottom:1rem;">
      <div class="stat-box"><div class="n">${list.counts.active_trials}</div><div class="l">Active Trials</div></div>
      <div class="stat-box"><div class="n">${list.counts.active_subs}</div><div class="l">Active Subs</div></div>
      <div class="stat-box"><div class="n">${pendingNotices.length}</div><div class="l">Notices to Review</div></div>
      <div class="stat-box"><div class="n">${drafts.drafts.length}</div><div class="l">Campaign Drafts</div></div>
    </div>

    <div class="account-email hdr-ic" style="font-size:var(--fs-base);margin-bottom:0.5rem;"><svg class="icon-svg"><use href="#i-briefcase"/></svg>Agency Notices${pendingNotices.length?` (${pendingNotices.length} pending)`:""}</div>
    ${pendingNotices.length?pendingNotices.map(n=>`
      <div class="detail-row" style="align-items:center;">
        <div class="detail-label" style="flex:1;">${esc(n.title||"Untitled")} &mdash; ${esc(n.city)}, ${esc(n.state)}</div>
        <button class="btn-ghost" data-notice-approve="${esc(n.id)}" style="color:var(--green);border-color:var(--green);padding:0.3rem 0.7rem;font-size:var(--fs-sm);flex-shrink:0;">Approve</button>
        <button class="btn-ghost" data-notice-delete="${esc(n.id)}" style="color:var(--red);border-color:var(--red);padding:0.3rem 0.7rem;font-size:var(--fs-sm);flex-shrink:0;">Delete</button>
      </div>`).join(""):`<div class="account-status" style="margin-bottom:0.8rem;">Nothing pending.</div>`}
    ${approvedNotices.length?`<div class="account-status" style="margin:0.4rem 0 0.8rem;">${approvedNotices.length} already approved and live.</div>`:""}

    <div class="account-email hdr-ic" style="font-size:var(--fs-base);margin-bottom:0.5rem;"><svg class="icon-svg"><use href="#i-mail"/></svg>Campaign Drafts</div>
    ${drafts.drafts.length?drafts.drafts.map(d=>`
      ${d.preview?`<details style="margin:0.2rem 0 0.4rem;"><summary class="account-status" style="cursor:pointer;">Read it first: to ${esc((d.to||[]).join(", "))}${d.recipients>(d.to||[]).length?` and ${d.recipients-(d.to||[]).length} more`:""}</summary>
        <pre style="white-space:pre-wrap;font-family:inherit;font-size:var(--fs-sm);color:var(--text2);background:var(--bg);border:1px solid var(--border);border-radius:var(--r);padding:0.7rem;margin:0.4rem 0;">Subject: ${esc(d.subject||"")}\n\n${esc(d.preview)}</pre></details>`:""}
      <div class="detail-row" style="align-items:center;">
        <div class="detail-label" style="flex:1;">${esc(d.subject||"Untitled")} &mdash; ${plural(d.recipients,"recipient")}</div>
        <button class="btn-ghost" data-draft-send="${esc(d.draft_id)}" style="color:var(--green);border-color:var(--green);padding:0.3rem 0.7rem;font-size:var(--fs-sm);flex-shrink:0;">Send</button>
        <button class="btn-ghost" data-draft-discard="${esc(d.draft_id)}" style="color:var(--red);border-color:var(--red);padding:0.3rem 0.7rem;font-size:var(--fs-sm);flex-shrink:0;">Discard</button>
      </div>`).join(""):`<div class="account-status" style="margin-bottom:0.8rem;">No drafts waiting.</div>`}

    <div class="account-email hdr-ic" style="font-size:var(--fs-base);margin-bottom:0.5rem;"><svg class="icon-svg"><use href="#i-star"/></svg>Reviews${pendingReviews.length?` (${pendingReviews.length} pending)`:""}</div>
    ${pendingReviews.length?pendingReviews.map(r=>`
      <div class="detail-row" style="align-items:center;">
        <div class="detail-label" style="flex:1;">${"★".repeat(r.rating)} &mdash; ${esc((r.quote||"").slice(0,80))}${(r.quote||"").length>80?"…":""}</div>
        <button class="btn-ghost" data-review-approve="${esc(r.id)}" style="color:var(--green);border-color:var(--green);padding:0.3rem 0.7rem;font-size:var(--fs-sm);flex-shrink:0;">Approve</button>
        <button class="btn-ghost" data-review-reject="${esc(r.id)}" style="color:var(--red);border-color:var(--red);padding:0.3rem 0.7rem;font-size:var(--fs-sm);flex-shrink:0;">Reject</button>
      </div>`).join(""):`<div class="account-status" style="margin-bottom:0.8rem;">Nothing pending.</div>`}

    <div class="account-email hdr-ic" style="font-size:var(--fs-base);margin:0.8rem 0 0.5rem;"><svg class="icon-svg"><use href="#i-download"/></svg>Backup</div>
    <div class="account-status" style="margin-bottom:0.6rem;">Every user table, as JSON. Save it somewhere private — never a public repo.</div>
    <button class="btn-ghost" id="admin-export-btn" style="margin-bottom:1rem;">Download backup</button>

    <div class="modal-actions"><button class="ma-ghost" id="admin-lock-btn">Lock</button>
      <button class="ma-ghost" onclick="closeModal();">Close</button></div>`;

  mc.querySelectorAll("[data-notice-approve]").forEach(b=>b.onclick=async()=>{
    b.disabled=true;b.textContent="Approving...";
    const r=await adminCall("/agency/review",{approve:b.dataset.noticeApprove});
    if(r.ok)renderAdminPanel();else{toast(r.detail||"Couldn't approve that notice");b.disabled=false;b.textContent="Approve";}
  });
  mc.querySelectorAll("[data-notice-delete]").forEach(b=>b.onclick=async()=>{
    if(!confirm("Delete this notice permanently?"))return;
    b.disabled=true;b.textContent="Deleting...";
    await adminCall("/agency/review",{delete:b.dataset.noticeDelete});
    renderAdminPanel();
  });
  mc.querySelectorAll("[data-draft-send]").forEach(b=>b.onclick=async()=>{
    if(!confirm("Send this campaign now? This cannot be undone."))return;
    b.disabled=true;b.textContent="Sending...";
    const r=await adminCall("/campaign/approve",{draft_id:b.dataset.draftSend,confirm:true});
    if(r.ok)toast(`Sent to ${plural(r.sent,"recipient")}${r.failed?`, ${r.failed} failed`:""}`);
    // "Send failed" would be a guess here: the request may well have reached
    // the server. Say what is actually known before anyone clicks again.
    else if(r.reason==="network_error")toast("Lost connection \u2014 check Drafts before resending");
    else toast(r.detail||r.reason||"Send failed");
    renderAdminPanel();
  });
  mc.querySelectorAll("[data-draft-discard]").forEach(b=>b.onclick=async()=>{
    b.disabled=true;b.textContent="Discarding...";
    await adminCall("/campaign/drafts",{discard:b.dataset.draftDiscard});
    renderAdminPanel();
  });
  mc.querySelectorAll("[data-review-approve]").forEach(b=>b.onclick=async()=>{
    b.disabled=true;b.textContent="Approving...";
    const r=await adminCall("/admin/reviews",{approve:b.dataset.reviewApprove});
    if(!r.ok)toast("Couldn't approve that review");
    renderAdminPanel();
  });
  // Reject deletes the review server-side (see /admin/reviews), so it gets
  // the same confirm as deleting an agency notice.
  mc.querySelectorAll("[data-review-reject]").forEach(b=>b.onclick=async()=>{
    if(!confirm("Reject and delete this review permanently?"))return;
    b.disabled=true;b.textContent="Rejecting...";
    const r=await adminCall("/admin/reviews",{reject:b.dataset.reviewReject});
    if(!r.ok)toast("Couldn't reject that review");
    renderAdminPanel();
  });
  document.getElementById("admin-export-btn").onclick=async()=>{
    const r=await adminCall("/admin/export",{});
    if(!r.ok){toast("Export failed");return;}
    const blob=new Blob([JSON.stringify(r,null,2)],{type:"application/json"});
    const a=document.createElement("a");
    a.href=URL.createObjectURL(blob);
    a.download=`curbcall-backup-${new Date().toISOString().slice(0,10)}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  };
  document.getElementById("admin-lock-btn").onclick=()=>{setAdminToken("");closeModal();};
}

// Turns an expires_at ISO timestamp into a live 'Xd Yh left' / 'Yh Zm left'
// countdown instead of a static day count that only ticks over once every
// 24 hours — mirrors subscription.py's time_left_label() on desktop.
function timeLeftLabel(expiresAt){
  if(!expiresAt)return null;
  const end=new Date(expiresAt);
  if(isNaN(end.getTime()))return null;
  const ms=end-new Date();
  if(ms<=0)return"Expired";
  const totalMin=Math.floor(ms/60000);
  const days=Math.floor(totalMin/1440);
  const hours=Math.floor((totalMin%1440)/60);
  const minutes=totalMin%60;
  if(days>0)return`${days}d ${hours}h left`;
  if(hours>0)return`${hours}h ${minutes}m left`;
  return`${minutes}m left`;
}
let trialExpiresAt=null;
let trialCountdownTimer=null;
function tickTrialCountdown(){
  const el=document.getElementById("trial-countdown");
  if(!el||!trialExpiresAt){clearInterval(trialCountdownTimer);trialCountdownTimer=null;return;}
  const label=timeLeftLabel(trialExpiresAt);
  if(label)el.textContent=label;
}
async function loadAccountStatus(){
  // Deliberately not holding a reference to the card across the awaits
  // below. renderAccount() rewrites the whole Account pane, placeholder
  // spinner and all, and it runs again on every company-info save, cancel
  // and chip update. An element captured before /validate and /trial can be
  // detached by the time they answer, so the result lands in a node that is
  // no longer on the page and the spinner spins forever -- which is exactly
  // what "CHECKING YOUR PLAN..." stuck on screen was.
  const key=licenseKey();
  let html="";
  let featured=false;
  let activePaid=false;
  trialExpiresAt=null;
  if(trialCountdownTimer){clearInterval(trialCountdownTimer);trialCountdownTimer=null;}
  if(key){
    try{
      const r=await fetchWithTimeout(SERVER+"/validate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key,device_id:deviceId()})},60000);
      const d=await r.json();
      if(d.valid){html=`<div class="plan-name">Active subscription</div><div class="plan-price" style="font-size:var(--fs-2xl);">${esc(d.plan||"Pro")}</div><p style="color:var(--text2);font-size:var(--fs-base);margin-top:0.4rem;">Expires ${esc(d.expires||"")}</p>`;featured=true;activePaid=true;}
    }catch{}
  }
  // Re-queried for the same reason as the card below: /validate has already
  // been awaited by this point, so a reference taken earlier may be detached.
  const upgradeSection=document.getElementById("upgrade-section");
  if(upgradeSection)upgradeSection.style.display=activePaid?"none":"";
  if(!html){
    try{
      const token=await getSupabaseToken();
      const r=await fetchWithTimeout(SERVER+"/trial",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({device_id:deviceId(),supabase_token:token})},60000);
      const d=await r.json();
      if(d.active){
        trialExpiresAt=d.expires_at||null;
        const countdown=timeLeftLabel(trialExpiresAt)||`${d.days_left||""} days left`;
        html=`<div class="plan-name">Free Trial Active</div><div class="plan-price"><span id="trial-countdown">${esc(countdown)}</span></div><p style="color:var(--text2);font-size:var(--fs-base);">Full access. Subscribe to keep going after trial.</p>`;featured=true;
      }
      else html=`<div class="plan-name" style="color:var(--red);">Trial expired</div><p style="color:var(--text2);font-size:var(--fs-base);margin-top:0.5rem;">Subscribe below to keep scanning.</p>`;
    }catch{
      html=`<div class="plan-name" style="color:var(--text2);">Couldn't reach the server</div><p style="color:var(--text3);font-size:var(--fs-base);margin-top:0.5rem;">Your plan status will show once the connection's back. Try again in a bit.</p>`;
    }
  }
  // Re-query now that the awaits are done: this is the live node, whatever
  // re-renders happened while we waited. Gone entirely means the user left
  // the Account tab, and there is nothing to update.
  const card=document.getElementById("status-card");
  if(!card)return;
  card.innerHTML=html;
  card.className="plan"+(featured?" featured":"");
  if(html.indexOf("Trial expired")>-1)card.style.borderColor="var(--red)";
  if(trialExpiresAt)trialCountdownTimer=setInterval(tickTrialCountdown,60000);
}
// fetch with a hard timeout so a sleepy/slow backend never leaves the UI hanging
function fetchWithTimeout(url,opts,ms=12000){
  const ctrl=new AbortController();
  const t=setTimeout(()=>ctrl.abort(),ms);
  return fetch(url,{...opts,signal:ctrl.signal}).finally(()=>clearTimeout(t));
}

document.addEventListener("visibilitychange",()=>{if(!document.hidden)autoUnlock();});

if("serviceWorker"in navigator){
  window.addEventListener("load",()=>{navigator.serviceWorker.register("sw.js").catch(()=>{});});
}

// Supabase's onAuthStateChange (registered above) fires once immediately with
// whatever session is currently stored, so it alone drives the initial
// auth-screen-vs-app decision — no separate manual check needed here.
// The exception is a missing supabase-js: no client means no listener and no
// event, so nothing would ever decide what to show. Boot that path by hand.
// Deliberately no automatic purge-and-reload here. A missing library is often
// a bad cached copy, but navigator.onLine only reports whether a network
// interface exists — it is true on a phone with no usable signal. Clearing the
// asset cache on that signal would delete the very copies that make the app
// work offline, for exactly the users who need them, and reloading mid-boot
// helps nobody. Bumping the service worker's cache version already clears any
// poisoned entry on deploy; beyond that the recovery is offered to the user
// as a button rather than taken on their behalf. See signInAvailable().
if(!HAS_SB)bootOffline();
updateOfflineBar();
