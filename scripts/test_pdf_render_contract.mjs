import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../pdf_worker/src/index.js',import.meta.url),'utf8');
const start=source.indexOf('export async function renderPdf(');
const end=source.indexOf('export async function cleanDocument(',start);
assert(start>=0&&end>start);
const {renderPdf}=await import('data:text/javascript;base64,'+Buffer.from(source.slice(start,end)).toString('base64'));
const events=[];
let intercept;
const pdf=new Uint8Array([37,80,68,70]);
const page={
  async setJavaScriptEnabled(value){events.push(['javascript',value]);},
  async setRequestInterception(value){events.push(['interception',value]);},
  on(event,callback){assert.equal(event,'request');intercept=callback;},
  async setViewport(value){events.push(['viewport',value]);},
  async emulateMediaType(value){events.push(['media',value]);},
  setDefaultTimeout(value){events.push(['timeout',value]);},
  async setContent(html,options){events.push(['content',html,options]);},
  async pdf(options){events.push(['pdf',options]);return pdf;},
};
assert.equal(await renderPdf(page,'<p>Synthetic report</p>'),pdf);
let blocked=false;intercept({abort(){blocked=true;}});assert(blocked);
assert.deepEqual(events,[
  ['javascript',false],['interception',true],
  ['viewport',{width:1440,height:1000,deviceScaleFactor:1}],['media','print'],['timeout',20000],
  ['content','<p>Synthetic report</p>',{waitUntil:'load',timeout:20000}],
  ['pdf',{format:'A4',landscape:true,preferCSSPageSize:true,printBackground:true,displayHeaderFooter:false,scale:1,timeout:30000}],
]);
console.log('PDF render contract: unchanged A4 layout, scale, colours, timeouts and blocked scripts/network.');
