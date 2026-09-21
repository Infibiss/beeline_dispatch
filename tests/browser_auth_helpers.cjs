const assert = require('node:assert/strict');
const password = 'browser-only-test-password-2026';
async function login(context, url) {
  const response = await context.request.post(url+'/api/auth/login',{data:{username:'admin',password}});
  assert.equal(response.status(),200);
  const identity=await response.json();
  if(!context.authTokens) {
    context.authTokens=new Map();
    const post=context.request.post.bind(context.request);
    context.request.post=(url,options={})=>post(url,{...options,headers:{...options.headers,
      'X-CSRF-Token':context.authTokens.get(new URL(url).origin) || ''}});
  }
  context.authTokens.set(new URL(url).origin,identity.csrf);
}
module.exports={login,password};
