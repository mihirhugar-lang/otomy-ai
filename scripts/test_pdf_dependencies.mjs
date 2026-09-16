// Build the real Worker with Wrangler; never upload or contact Browser Run.
import assert from 'node:assert/strict';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';

const worker=fileURLToPath(new URL('../pdf_worker/',import.meta.url));
const lock=JSON.parse(await readFile(path.join(worker,'package-lock.json'),'utf8'));
const floors={undici:[7,29,0],sharp:[0,35,4]};
for(const [name,floor] of Object.entries(floors)){
  const matches=Object.entries(lock.packages).filter(([key])=>key.endsWith(`node_modules/${name}`));
  assert(matches.length>0,`Expected ${name} in the audited dependency tree`);
  for(const [key,pkg] of matches){
    assert(/^\d+\.\d+\.\d+$/.test(pkg.version),`Review non-stable ${name} version`);
    const parts=pkg.version.split('.').map(Number);
    // Other Undici release lines have different patched versions. A future
    // major migration needs a fresh advisory review, not just a numeric floor.
    if(name==='undici')assert.equal(parts[0],7,'Review the security floor for this Undici major');
    const first=parts.findIndex((part,index)=>part!==floor[index]);
    assert(first<0||parts[first]>floor[first],`Unpatched ${name} dependency`);
    const installed=JSON.parse(await readFile(path.join(worker,key,'package.json'),'utf8'));
    assert.equal(installed.version,pkg.version,`${name} install differs from lockfile`);
  }
}

const forbidden=['extract-zip','@puppeteer/browsers','sharp','undici'];
function checkBundle(metadata){
  assert(Object.values(metadata.outputs||{}).some(output=>output.entryPoint),'Missing Worker entry point');
  for(const output of Object.values(metadata.outputs)){
    for(const [input,details] of Object.entries(output.inputs||{})){
      if(details.bytesInOutput<=0)continue;
      const normalized=input.replaceAll('\\','/');
      for(const name of forbidden)assert(!normalized.includes(`node_modules/${name}/`),`${name} entered Worker runtime`);
    }
    for(const imported of output.imports||[]){
      for(const name of forbidden)assert(imported.path!==name&&!imported.path.startsWith(name+'/'),`${name} external runtime import`);
    }
  }
}
for(const name of forbidden){
  assert.throws(()=>checkBundle({outputs:{worker:{entryPoint:'src/index.js',inputs:{[`node_modules/${name}/index.js`]:{bytesInOutput:1}}}}}),/entered Worker runtime/);
  assert.throws(()=>checkBundle({outputs:{worker:{entryPoint:'src/index.js',imports:[{path:name}]}}}),/external runtime import/);
}
const scratch=await mkdtemp(path.join(tmpdir(),'otomy-pdf-security-'));
try{
  const meta=path.join(scratch,'bundle-meta.json');
  const result=spawnSync(process.execPath,[path.join(worker,'node_modules/wrangler/bin/wrangler.js'),
    'deploy','--dry-run','--outdir',scratch,'--metafile',meta],{
      cwd:worker,env:{...process.env,WRANGLER_SEND_METRICS:'false'},encoding:'utf8',timeout:120000,maxBuffer:8*1024*1024,
    });
  assert.equal(result.status,0,'Private PDF dry-run build failed; no deployment attempted');
  checkBundle(JSON.parse(await readFile(meta,'utf8')));
  console.log('PDF dependencies: patched tooling and no ZIP installer, sharp or undici in the actual Worker build.');
}finally{
  await rm(scratch,{recursive:true,force:true});
}
