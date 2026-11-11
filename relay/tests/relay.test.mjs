import test from 'node:test';
import assert from 'node:assert/strict';
import {relay} from '../worker.mjs';
const env={STATS_ORIGIN:'https://home.zhouchenxing.com/usage-stats/api',RELAY_TOKEN:'test-only',RATE_LIMITER:{limit:async()=>({success:true})}};
function request(method='POST',body=JSON.stringify({install_id:'a'.repeat(32),product:'this_cup'}),country='SG'){
  const request=new Request('https://old.invalid/v1/check-in',{method,body,headers:{'Content-Type':'application/json','Cookie':'private','Authorization':'private','X-Usage-Country':'US','X-Forwarded-For':'1.2.3.4'}});
  request.cf={country};return request;
}
test('authenticated relay forwards platform country and original body, omits all incoming identity headers',async()=>{
  const r=request();let calls=0;
  const response=await relay(r,env,async(url,init)=>{
    calls++;assert.equal(url,env.STATS_ORIGIN+'/v1/check-in');
    assert.deepEqual(init.headers,{'Content-Type':'application/json','X-Usage-Relay-Key':'test-only','X-Usage-Country':'SG'});
    assert.equal(new TextDecoder().decode(init.body),JSON.stringify({install_id:'a'.repeat(32),product:'this_cup'}));
    assert.equal(init.redirect,'manual');return Response.json({accepted:true});
  });assert.equal(calls,1);assert.equal(response.status,200);assert.equal(response.headers.get('access-control-allow-origin'),'*');
});
test('oversized payload, forbidden method and missing config never reach upstream',async()=>{
  const fetchImpl=()=>{throw Error('must not call')};
  assert.equal((await relay(request('POST','a'.repeat(257)),env,fetchImpl)).status,413);
  assert.equal((await relay(request('PUT'),env,fetchImpl)).status,405);
  assert.equal((await relay(request(),{...env,RELAY_TOKEN:''},fetchImpl)).status,503);
});
test('legacy summary and deletion remain available; unknown country stays unknown',async()=>{
  const get=new Request('https://old.invalid/v1/summary');
  await relay(get,env,async(url,init)=>{assert.equal(init.method,'GET');assert.deepEqual(init.headers,{});return Response.json({schema:1});});
  await relay(request('DELETE',undefined,'EU'),env,async(url,init)=>{assert.equal(init.method,'DELETE');assert.equal(init.headers['X-Usage-Country'],'ZZ');return Response.json({deleted:true});});
});
test('upstream redirects are returned without following or forwarding the relay key',async()=>{
  let calls=0;
  const response=await relay(request(),env,async(url,init)=>{calls++;assert.equal(init.redirect,'manual');return new Response(null,{status:307,headers:{Location:'https://other.invalid/'}})});
  assert.equal(calls,1);assert.equal(response.status,503);
});
test('relay uses only the edge source IP, ignoring client-supplied identity headers',async()=>{
  const r=request();r.headers.set('CF-Connecting-IP','2001:4860:4860::8888');r.headers.set('X-Usage-IP','1.2.3.4');r.headers.set('X-Real-IP','2.3.4.5');
  await relay(r,env,async(url,init)=>{
    assert.deepEqual(init.headers,{'Content-Type':'application/json','X-Usage-Relay-Key':'test-only','X-Usage-Country':'SG','X-Usage-IP':'2001:4860:4860::8888'});
    return Response.json({accepted:true});
  });
});
test('invalid edge IP header is omitted, preserving old country-only relay compatibility',async()=>{
  const r=request();r.headers.set('CF-Connecting-IP','1.2.3.4, 5.6.7.8');r.headers.set('X-Usage-IP','8.8.8.8');
  await relay(r,env,async(url,init)=>{assert.equal(init.headers['X-Usage-IP'],undefined);return Response.json({accepted:true});});
});
