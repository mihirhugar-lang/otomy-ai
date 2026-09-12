import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {webcrypto} from 'node:crypto';
globalThis.crypto ||= webcrypto;
const source=await readFile(new URL('../functions/api/report/pdf.js',import.meta.url),'utf8');
const {onRequest,verifyAccess}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const audience='86f6440c52f26a2def8939fe79069246b7025050ce486ecb3441c9278e01a274';
const pair=await crypto.subtle.generateKey({name:'RSASSA-PKCS1-v1_5',modulusLength:2048,publicExponent:new Uint8Array([1,0,1]),hash:'SHA-256'},true,['sign','verify']);
const jwk=await crypto.subtle.exportKey('jwk',pair.publicKey);jwk.kid='fixture-key';
const b64=value=>Buffer.from(JSON.stringify(value)).toString('base64url');
const now=Math.floor(Date.now()/1000);
async function token(overrides={}){
  const unsigned=b64({alg:'RS256',kid:jwk.kid})+'.'+b64({iss:'https://otomy.cloudflareaccess.com',aud:[audience],exp:now+600,...overrides});
  const sig=await crypto.subtle.sign('RSASSA-PKCS1-v1_5',pair.privateKey,new TextEncoder().encode(unsigned));
  return unsigned+'.'+Buffer.from(sig).toString('base64url');
}
const valid=await token();
assert.equal(await verifyAccess(valid,audience,async()=>[jwk]),true);
for(const bad of ['', 'broken', await token({exp:now-1}),await token({aud:['wrong']}),await token({iss:'https://attacker.invalid'}),await token({nbf:now+600}),valid.slice(0,-12)+'AAAAAAAAAAAA']) {
  assert.equal(await verifyAccess(bad,audience,async()=>[jwk]),false);
}
let forwarded=0;
const env={OTOMY_PDF:{fetch:async request=>{
  forwarded++;
  assert.equal(request.url,'https://private-pdf/render');
  assert.equal(request.headers.get('cookie'),null);
  assert.equal(request.headers.get('cf-access-jwt-assertion'),null);
  assert.equal(await request.text(),'<div>synthetic report only</div>');
  return new Response('%PDF-fixture',{headers:{'content-type':'application/pdf','cache-control':'no-store, private'}});
}}};
globalThis.fetch=async url=>{assert.equal(url,'https://otomy.cloudflareaccess.com/cdn-cgi/access/certs');return Response.json({keys:[jwk]});};
function request(host='otomy.ai',extra={}){
  return new Request('https://'+host+'/api/report/pdf',{method:'POST',body:'<div>synthetic report only</div>',headers:{origin:'https://'+host,'content-type':'text/html','cf-access-jwt-assertion':valid,cookie:'never-forward=1',...extra}});
}
assert.equal((await onRequest({request:request(),env})).status,200);
assert.equal(forwarded,1);
for(const [req,status] of [[request('preview.otomy-ai.pages.dev'),403],[request('otomy.ai',{origin:'https://evil.invalid'}),403],[request('otomy.ai',{'cf-access-jwt-assertion':'fake'}),401],[request('otomy.ai',{'content-type':'application/json'}),415],[new Request('https://otomy.ai/api/report/pdf'),405],[request('otomy-ai.pages.dev'),401]]){
  const response=await onRequest({request:req,env});assert.equal(response.status,status);assert.match(response.headers.get('cache-control'),/no-store/);
}
assert.equal(forwarded,1);
assert.equal((await onRequest({request:request(),env:{}})).status,503);
assert.equal((await onRequest({request:request(),env:{OTOMY_PDF:{fetch(){throw Error('private diagnostics')}}}})).status,503);
const frontend=await readFile(new URL('../static/pdf-share.js',import.meta.url),'utf8');
for(const prohibited of ['createObjectURL','window.open(','localStorage','sessionStorage','html2canvas','jspdf'])assert.equal(frontend.includes(prohibited),false);
console.log('PDF route: signed Access tokens, host/origin checks, credential stripping, private errors, and no URL/storage fallback passed.');
